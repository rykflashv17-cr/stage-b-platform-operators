"""Optional LeRobot v2.1 → v3.0 export conversion for Dataclean P7 output."""
from __future__ import annotations

import json
import os
import logging
import shutil
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import tqdm

from robot_data_processing.lerobot_export import (
    _stats_1d,
    _video_stats_from_samples,
    _write_jsonl,
    sample_video_pixels,
)
from robot_data_processing.schema import stack_column
from robot_data_processing.stats_quantiles import compute_global_quantiles, provenance_payload

logger = logging.getLogger(__name__)

V21 = "v2.1"
V30 = "v3.0"

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_DATA_FILE_SIZE_IN_MB = 100
DEFAULT_VIDEO_FILE_SIZE_IN_MB = 200

INFO_PATH = "meta/info.json"
STATS_PATH = "meta/stats.json"
STATS_QUANTILE_PROVENANCE_PATH = "meta/stats_quantile_provenance.json"
EPISODES_DIR = "meta/episodes"
DATA_DIR = "data"
VIDEO_DIR = "videos"
CHUNK_FILE_PATTERN = "chunk-{chunk_index:03d}/file-{file_index:03d}"
DEFAULT_TASKS_PATH = "meta/tasks.parquet"
DEFAULT_EPISODES_PATH = EPISODES_DIR + "/" + CHUNK_FILE_PATTERN + ".parquet"
DEFAULT_DATA_PATH = DATA_DIR + "/" + CHUNK_FILE_PATTERN + ".parquet"
DEFAULT_VIDEO_PATH = VIDEO_DIR + "/{video_key}/" + CHUNK_FILE_PATTERN + ".mp4"
LEGACY_EPISODES_PATH = "meta/episodes.jsonl"
LEGACY_EPISODES_STATS_PATH = "meta/episodes_stats.jsonl"
LEGACY_TASKS_PATH = "meta/tasks.jsonl"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _flatten_dict(d: dict, parent_key: str = "", sep: str = "/") -> dict:
    items: list[tuple[str, Any]] = []
    for key, value in d.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else key
        if isinstance(value, dict):
            items.extend(_flatten_dict(value, new_key, sep=sep).items())
        else:
            items.append((new_key, value))
    return dict(items)


def _unflatten_dict(d: dict, sep: str = "/") -> dict:
    out: dict[str, Any] = {}
    for key, value in d.items():
        parts = key.split(sep)
        cur = out
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
    return out


def _serialize_stats(stats: dict[str, Any]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for key, value in _flatten_dict(stats).items():
        if isinstance(value, np.ndarray):
            serialized[key] = value.tolist()
        elif isinstance(value, np.generic):
            serialized[key] = value.item()
        elif isinstance(value, (int, float, list)):
            serialized[key] = value
        else:
            raise NotImplementedError(f"Unsupported stats value type {type(value)} for key {key}")
    return _unflatten_dict(serialized)


def _cast_stats_to_numpy(stats: dict[str, Any]) -> dict[str, dict[str, np.ndarray]]:
    flat = {key: np.atleast_1d(np.asarray(value)) for key, value in _flatten_dict(stats).items()}
    return _unflatten_dict(flat)


def _aggregate_feature_stats(stats_ft_list: list[dict[str, dict]]) -> dict[str, dict[str, np.ndarray]]:
    means = np.stack([s["mean"] for s in stats_ft_list])
    variances = np.stack([s["std"] ** 2 for s in stats_ft_list])
    counts = np.stack([s["count"] for s in stats_ft_list])
    total_count = counts.sum(axis=0)
    while counts.ndim < means.ndim:
        counts = np.expand_dims(counts, axis=-1)
    weighted_means = means * counts
    total_mean = weighted_means.sum(axis=0) / total_count
    delta_means = means - total_mean
    weighted_variances = (variances + delta_means**2) * counts
    total_variance = weighted_variances.sum(axis=0) / total_count
    aggregated = {
        "min": np.min(np.stack([s["min"] for s in stats_ft_list]), axis=0),
        "max": np.max(np.stack([s["max"] for s in stats_ft_list]), axis=0),
        "mean": total_mean,
        "std": np.sqrt(total_variance),
        "count": total_count,
    }
    # File/episode-level quantiles are not mergeable (REPORT STAT-01): never
    # average them or take their min/max. write_lerobot_v30_stats recomputes
    # global quantiles from the full data via stats_quantiles.
    return aggregated


def _aggregate_stats(stats_list: list[dict[str, dict]]) -> dict[str, dict[str, np.ndarray]]:
    data_keys = {key for stats in stats_list for key in stats}
    aggregated_stats: dict[str, dict[str, np.ndarray]] = {key: {} for key in data_keys}
    for key in data_keys:
        stats_with_key = [stats[key] for stats in stats_list if key in stats]
        aggregated_stats[key] = _aggregate_feature_stats(stats_with_key)
    return aggregated_stats


def _load_info(dataset_root: Path) -> dict[str, Any]:
    return _load_json(dataset_root / INFO_PATH)


def _get_parquet_file_size_in_mb(parquet_path: Path) -> float:
    metadata = pq.read_metadata(parquet_path)
    total_uncompressed_size = 0
    for row_group in range(metadata.num_row_groups):
        rg_metadata = metadata.row_group(row_group)
        for column in range(rg_metadata.num_columns):
            total_uncompressed_size += rg_metadata.column(column).total_uncompressed_size
    return total_uncompressed_size / (1024**2)


def _get_parquet_num_frames(parquet_path: Path) -> int:
    return pq.read_metadata(parquet_path).num_rows


def _get_file_size_in_mb(file_path: Path) -> float:
    return file_path.stat().st_size / (1024**2)


def _update_chunk_file_indices(chunk_idx: int, file_idx: int, chunks_size: int) -> tuple[int, int]:
    if file_idx == chunks_size - 1:
        return chunk_idx + 1, 0
    return chunk_idx, file_idx + 1


def _load_jsonlines(fpath: Path) -> list[Any]:
    rows: list[Any] = []
    with fpath.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _list_v21_episode_parquets(dataset_root: Path) -> list[tuple[int, Path]]:
    rows: list[tuple[int, Path]] = []
    data_dir = dataset_root / DATA_DIR
    for path in sorted(data_dir.glob("chunk-*/*.parquet")):
        if not path.name.startswith("episode_"):
            continue
        ep = int(path.stem.split("_")[1])
        rows.append((ep, path))
    return sorted(rows, key=lambda x: x[0])


def _video_paths_for_episode(
    dataset_root: Path,
    episode_index: int,
    video_keys: list[str],
) -> dict[str, Path]:
    chunk = episode_index // 1000
    out: dict[str, Path] = {}
    for key in video_keys:
        p = dataset_root / VIDEO_DIR / f"chunk-{chunk:03d}" / key / f"episode_{episode_index:06d}.mp4"
        if p.exists():
            out[key] = p
    return out


def _numeric_column_values(table, name: str) -> np.ndarray | None:
    """Return one parquet column as float64 [N, D], or None if non-numeric."""
    try:
        col = table.column(name).combine_chunks()
        if name.startswith("extrinsic."):
            values = col.to_pylist()
            return np.stack([np.asarray(x, dtype=np.float64).reshape(-1) for x in values], axis=0)
        values = col.to_numpy(zero_copy_only=False)
        if isinstance(values, np.ndarray) and values.dtype == object:
            try:
                arr = stack_column(values)
            except ValueError:
                arr = np.stack(
                    [np.asarray(x, dtype=np.float64).reshape(-1) for x in values],
                    axis=0,
                )
        else:
            arr = values
        arr = np.asarray(arr, dtype=np.float64)
        return arr.reshape(len(arr), -1)
    except Exception:
        return None


def _stats_for_table_column(table, name: str) -> dict[str, Any] | None:
    """Return LeRobot feature stats for one parquet column, or None if non-numeric."""
    values = _numeric_column_values(table, name)
    return None if values is None else _stats_1d(values)


def _load_numeric_columns(path: str, names: list[str]) -> dict[str, np.ndarray]:
    """Quantile-pass loader: requested numeric columns of one parquet shard."""
    available = set(pq.read_schema(path).names)
    table = pq.read_table(path, columns=[name for name in names if name in available])
    out: dict[str, np.ndarray] = {}
    for name in table.column_names:
        values = _numeric_column_values(table, name)
        if values is not None:
            out[name] = values
    return out


def _default_lerobot_stats_workers() -> int:
    for key in ("LEROBOT_STATS_WORKERS", "RELATIVE_STATS_WORKERS", "MERGE_WORKERS"):
        env = os.environ.get(key, "").strip()
        if env:
            return max(1, int(env))
    return max(1, min(32, (os.cpu_count() or 8)))


def _video_feature_keys(info: dict[str, Any]) -> list[str]:
    return [
        key
        for key, spec in (info.get("features") or {}).items()
        if isinstance(spec, dict) and spec.get("dtype") == "video"
    ]


def _list_dataset_parquet_files(dataset_root: Path) -> list[Path]:
    """v3 ``file-*.parquet`` first, else v2.1 ``episode_*.parquet``."""
    dataset_root = Path(dataset_root)
    v3 = sorted((dataset_root / DATA_DIR).glob("chunk-*/file-*.parquet"))
    if v3:
        return v3
    return [path for _, path in _list_v21_episode_parquets(dataset_root)]


def _parquet_feature_stats_job(job: dict[str, Any]) -> dict[str, Any]:
    table = pq.read_table(job["parquet"])
    skip = set(job.get("skip_columns") or [])
    video_paths = {str(k): Path(p) for k, p in (job.get("video_paths") or {}).items()}
    stats: dict[str, Any] = {}
    for name in table.column_names:
        if name in skip or name.startswith("observation.images."):
            continue
        if name in video_paths:
            stats[name] = _video_stats_from_samples(sample_video_pixels(video_paths[name]))
            continue
        item = _stats_for_table_column(table, name)
        if item is not None:
            stats[name] = item
    out: dict[str, Any] = {"stats": stats}
    if job.get("episode_index") is not None:
        out["episode_index"] = int(job["episode_index"])
    return out


def _run_stats_jobs(
    jobs: list[dict[str, Any]],
    *,
    num_workers: int,
    desc: str,
) -> list[dict[str, Any]]:
    if not jobs:
        return []
    workers = max(1, min(int(num_workers), len(jobs)))
    results: list[dict[str, Any] | None] = [None] * len(jobs)
    if workers <= 1:
        for i, job in enumerate(jobs):
            results[i] = _parquet_feature_stats_job(job)
        return [row for row in results if row is not None]

    print(f"[stats] {desc} workers={workers} files={len(jobs)}", flush=True)
    from robot_data_processing.stats_quantiles import frozen_gc

    with frozen_gc(), ProcessPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_parquet_feature_stats_job, job): i for i, job in enumerate(jobs)}
        done = 0
        for fut in as_completed(futs):
            idx = futs[fut]
            results[idx] = fut.result()
            done += 1
            if done % 200 == 0 or done == len(jobs):
                print(f"[stats] {desc} {done}/{len(jobs)}", flush=True)
    return [row for row in results if row is not None]


def write_lerobot_v30_stats(
    dataset_root: Path,
    *,
    num_workers: int | None = None,
    include_video: bool = False,
) -> dict[str, dict[str, np.ndarray]]:
    """Compute LeRobot v3 ``meta/stats.json`` from parquet files (parallel).

    Numeric / vector columns only. Video pixel stats are skipped unless
    ``include_video=True`` (ffmpeg sampling).
    """
    dataset_root = Path(dataset_root)
    info = _load_info(dataset_root)
    skip = [] if include_video else _video_feature_keys(info)
    paths = _list_dataset_parquet_files(dataset_root)
    if not paths:
        raise FileNotFoundError(f"No parquet files under {dataset_root / DATA_DIR}")
    workers = _default_lerobot_stats_workers() if num_workers is None else max(1, int(num_workers))
    jobs = [{"parquet": str(path), "skip_columns": skip} for path in paths]
    rows = _run_stats_jobs(jobs, num_workers=workers, desc="lerobot v3 stats")
    stats_list = [_cast_stats_to_numpy(row["stats"]) for row in rows if row.get("stats")]
    if not stats_list:
        raise RuntimeError(f"No numeric columns to aggregate in {dataset_root}")
    aggregated = _aggregate_stats(stats_list)
    extrema = {
        key: (
            np.asarray(ft["min"], dtype=np.float64).reshape(-1),
            np.asarray(ft["max"], dtype=np.float64).reshape(-1),
            int(np.asarray(ft["count"]).reshape(-1)[0]),
        )
        for key, ft in aggregated.items()
        if key not in skip and int(np.asarray(ft["count"]).reshape(-1)[0]) > 0
    }
    # Per-file stats are folded into ``aggregated``; release them before the quantile pool forks.
    del rows, stats_list, jobs
    quantiles, errors = compute_global_quantiles(
        [str(path) for path in paths], _load_numeric_columns, extrema, num_workers=workers
    )
    for key, values in quantiles.items():
        aggregated[key].update(values)
    _write_stats(aggregated, dataset_root)
    _write_json(dataset_root / STATS_QUANTILE_PROVENANCE_PATH, provenance_payload(errors))
    print(
        f"[stats] wrote {dataset_root / STATS_PATH} features={len(aggregated)} files={len(paths)}",
        flush=True,
    )
    return aggregated


def ensure_lerobot_v30_stats(
    dataset_root: Path,
    *,
    num_workers: int | None = None,
    include_video: bool = False,
) -> dict[str, Any]:
    """Write ``meta/stats.json`` when missing or still the empty ``{}`` placeholder."""
    dataset_root = Path(dataset_root)
    path = dataset_root / STATS_PATH
    # stats.json without quantile provenance was written by the retired
    # min/max quantile merge (STAT-01) or is an empty placeholder: rebuild it.
    if path.exists() and (dataset_root / STATS_QUANTILE_PROVENANCE_PATH).exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload:
                return payload
        except Exception:
            pass
    return write_lerobot_v30_stats(
        dataset_root, num_workers=num_workers, include_video=include_video
    )


def write_episodes_stats_jsonl(
    dataset_root: Path,
    *,
    num_workers: int | None = None,
    include_video: bool = False,
) -> Path:
    """Create ``meta/episodes_stats.jsonl`` for a v2.1 export (parallel)."""
    dataset_root = Path(dataset_root)
    out_path = dataset_root / LEGACY_EPISODES_STATS_PATH
    info = _load_info(dataset_root)
    video_keys = _video_feature_keys(info)
    skip = [] if include_video else video_keys
    workers = _default_lerobot_stats_workers() if num_workers is None else max(1, int(num_workers))
    jobs: list[dict[str, Any]] = []
    for episode_index, parquet_path in _list_v21_episode_parquets(dataset_root):
        job: dict[str, Any] = {
            "episode_index": int(episode_index),
            "parquet": str(parquet_path),
            "skip_columns": skip,
        }
        if include_video:
            job["video_paths"] = {
                key: str(path)
                for key, path in _video_paths_for_episode(
                    dataset_root, episode_index, video_keys
                ).items()
            }
        jobs.append(job)
    rows = _run_stats_jobs(jobs, num_workers=workers, desc="episodes_stats.jsonl")
    rows.sort(key=lambda row: int(row["episode_index"]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_path, rows)
    return out_path


def _validate_local_dataset_version(local_path: Path) -> None:
    info = _load_info(local_path)
    dataset_version = info.get("codebase_version") or "unknown"
    if dataset_version != V21:
        raise ValueError(
            f"Local dataset has codebase_version '{dataset_version}', expected '{V21}'. "
            "This converter only supports v2.1 → v3.0."
        )


def _legacy_load_episodes(local_dir: Path) -> dict[int, dict[str, Any]]:
    episodes = _load_jsonlines(local_dir / LEGACY_EPISODES_PATH)
    return {item["episode_index"]: item for item in sorted(episodes, key=lambda x: x["episode_index"])}


def _legacy_load_episodes_stats(local_dir: Path) -> dict[int, dict[str, dict[str, np.ndarray]]]:
    episodes_stats = _load_jsonlines(local_dir / LEGACY_EPISODES_STATS_PATH)
    return {
        item["episode_index"]: _cast_stats_to_numpy(item["stats"])
        for item in sorted(episodes_stats, key=lambda x: x["episode_index"])
    }


def _legacy_load_tasks(local_dir: Path) -> dict[int, str]:
    tasks = _load_jsonlines(local_dir / LEGACY_TASKS_PATH)
    return {item["task_index"]: item["task"] for item in sorted(tasks, key=lambda x: x["task_index"])}


def _get_video_keys(root: Path) -> list[str]:
    info = _load_info(root)
    return sorted(
        key for key, ft in (info.get("features") or {}).items() if isinstance(ft, dict) and ft.get("dtype") == "video"
    )


def _get_image_keys(root: Path) -> list[str]:
    info = _load_info(root)
    return [
        key for key, ft in (info.get("features") or {}).items() if isinstance(ft, dict) and ft.get("dtype") == "image"
    ]


def _concatenate_video_files(input_video_paths: list[Path], output_video_path: Path) -> None:
    if not input_video_paths:
        raise FileNotFoundError("No input video paths provided.")
    output_video_path = Path(output_video_path)
    output_video_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ffconcat", delete=False, encoding="utf-8") as tmp:
        tmp.write("ffconcat version 1.0\n")
        for input_path in input_video_paths:
            tmp.write(f"file '{Path(input_path).resolve()}'\n")
        tmp_path = tmp.name
    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            tmp_path,
            "-c",
            "copy",
            "-movflags",
            "faststart",
            str(output_video_path),
        ]
        subprocess.run(cmd, check=True)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _get_video_duration_in_s(video_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return float(result.stdout.strip())


def _get_video_nframes(video_path: Path) -> int:
    """Count packets (frames) via ffprobe; falls back to duration*fps estimate only on failure."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_packets",
        "-show_entries",
        "stream=nb_read_packets",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    text = (result.stdout or "").strip()
    if not text:
        raise RuntimeError(f"ffprobe returned empty frame count for {video_path}")
    return int(text.splitlines()[-1].strip())


VIDEO_CONCAT_TICK_LOG = "meta/video_concat_tick_issues.jsonl"
# Flag when |duration*fps - nframes| or |meta_span*fps - sum_len| exceeds this many frames.
_TICK_ERR_FRAMES = 0.51


def _v21_episode_parquet_path(root: Path, episode_index: int) -> Path:
    chunk = int(episode_index) // 1000
    return root / DATA_DIR / f"chunk-{chunk:03d}" / f"episode_{int(episode_index):06d}.parquet"


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _validate_concatenated_video(
    *,
    root: Path,
    new_root: Path,
    video_key: str,
    chunk_idx: int,
    file_idx: int,
    output_path: Path,
    source_paths: list[Path],
    batch_rows: list[dict[str, Any]],
    fps: float,
) -> dict[str, Any] | None:
    """Check merged video duration/ticks vs meta timestamps and source lengths.

    Returns an issue record (also appended to ``meta/video_concat_tick_issues.jsonl``)
    when incorrect; otherwise ``None``.
    """
    if not source_paths or not batch_rows:
        return None

    fps = float(max(fps, 1e-6))
    from_key = f"videos/{video_key}/from_timestamp"
    to_key = f"videos/{video_key}/to_timestamp"

    episode_indices = [int(r["episode_index"]) for r in batch_rows]
    ep_lengths: list[int] = []
    ep_tick_mismatches: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    prev_to: float | None = None

    for row in sorted(batch_rows, key=lambda r: float(r[from_key])):
        ep = int(row["episode_index"])
        parquet = _v21_episode_parquet_path(root, ep)
        length = int(_get_parquet_num_frames(parquet)) if parquet.exists() else -1
        ep_lengths.append(length)
        frm = float(row[from_key])
        to = float(row[to_key])
        span = to - frm
        ticks = span * fps
        if length >= 0 and abs(ticks - length) > _TICK_ERR_FRAMES:
            ep_tick_mismatches.append(
                {
                    "episode_index": ep,
                    "length": length,
                    "from_timestamp": frm,
                    "to_timestamp": to,
                    "meta_ticks": ticks,
                    "tick_error": ticks - length,
                }
            )
        if prev_to is not None and abs(frm - prev_to) > 1e-6:
            gaps.append(
                {
                    "episode_index": ep,
                    "gap_seconds": frm - prev_to,
                    "gap_ticks": (frm - prev_to) * fps,
                }
            )
        prev_to = to

    sum_len = int(sum(x for x in ep_lengths if x >= 0))
    first_from = min(float(r[from_key]) for r in batch_rows)
    last_to = max(float(r[to_key]) for r in batch_rows)
    meta_span = last_to - first_from

    try:
        out_duration = _get_video_duration_in_s(output_path)
    except Exception as exc:  # noqa: BLE001
        out_duration = float("nan")
        duration_error = f"{type(exc).__name__}: {exc}"
    else:
        duration_error = None

    try:
        out_nframes = _get_video_nframes(output_path)
    except Exception as exc:  # noqa: BLE001
        out_nframes = -1
        nframes_error = f"{type(exc).__name__}: {exc}"
    else:
        nframes_error = None

    reasons: list[str] = []
    if duration_error:
        reasons.append(f"ffprobe_duration_failed:{duration_error}")
    if nframes_error:
        reasons.append(f"ffprobe_nframes_failed:{nframes_error}")
    if out_nframes >= 0 and sum_len > 0 and out_nframes != sum_len:
        reasons.append(f"nframes_vs_sum_length:{out_nframes}!={sum_len}")
    if (
        out_nframes >= 0
        and np.isfinite(out_duration)
        and abs(out_duration * fps - out_nframes) > _TICK_ERR_FRAMES
    ):
        reasons.append(
            f"duration_vs_nframes_ticks:{(out_duration * fps) - out_nframes:.3f}"
        )
    if (
        sum_len > 0
        and np.isfinite(out_duration)
        and abs(out_duration * fps - sum_len) > _TICK_ERR_FRAMES
    ):
        reasons.append(
            f"duration_vs_sum_length_ticks:{(out_duration * fps) - sum_len:.3f}"
        )
    if (
        sum_len > 0
        and abs(meta_span * fps - sum_len) > _TICK_ERR_FRAMES
    ):
        reasons.append(
            f"meta_span_vs_sum_length_ticks:{(meta_span * fps) - sum_len:.3f}"
        )
    if ep_tick_mismatches:
        reasons.append(f"per_episode_meta_tick_mismatch:{len(ep_tick_mismatches)}")
    if gaps:
        reasons.append(f"timestamp_gaps:{len(gaps)}")

    if not reasons:
        return None

    record = {
        "ok": False,
        "reasons": reasons,
        "video_key": video_key,
        "chunk_index": int(chunk_idx),
        "file_index": int(file_idx),
        "output_video": str(output_path),
        "fps": fps,
        "num_source_episodes": len(source_paths),
        "source_episode_indices": episode_indices,
        "source_videos": [str(p) for p in source_paths],
        "source_lengths": ep_lengths,
        "sum_source_length": sum_len,
        "meta_from_timestamp": first_from,
        "meta_to_timestamp": last_to,
        "meta_span_seconds": meta_span,
        "output_duration_seconds": out_duration if np.isfinite(out_duration) else None,
        "output_nframes": out_nframes if out_nframes >= 0 else None,
        "duration_minus_sum_len_over_fps": (
            (out_duration - sum_len / fps) if (sum_len > 0 and np.isfinite(out_duration)) else None
        ),
        "tick_error_duration": (
            (out_duration * fps - sum_len) if (sum_len > 0 and np.isfinite(out_duration)) else None
        ),
        "nframes_minus_sum_len": (out_nframes - sum_len) if out_nframes >= 0 and sum_len > 0 else None,
        "per_episode_meta_tick_mismatches": ep_tick_mismatches,
        "timestamp_gaps": gaps,
    }
    log_path = Path(new_root) / VIDEO_CONCAT_TICK_LOG
    _append_jsonl(log_path, record)
    logger.warning(
        "video concat tick issue video_key=%s chunk=%s file=%s reasons=%s sources=%d → %s",
        video_key,
        chunk_idx,
        file_idx,
        reasons,
        len(source_paths),
        log_path,
    )
    print(
        f"[P7/v30] WARN tick issue {video_key} chunk-{chunk_idx:03d}/file-{file_idx:03d}.mp4 "
        f"reasons={reasons} sources={len(source_paths)} log={log_path}",
        flush=True,
    )
    return record


def _flush_video_concat_batch(
    *,
    root: Path,
    new_root: Path,
    video_key: str,
    chunk_idx: int,
    file_idx: int,
    paths_to_cat: list[Path],
    episodes_metadata: list[dict[str, Any]],
    row_indices_in_batch: list[int],
    fps: float,
    tick_issues: list[dict[str, Any]],
) -> None:
    """Concatenate one MB-budget video file, stamp chunk/file indices, validate ticks."""
    if not paths_to_cat:
        return
    output_path = new_root / DEFAULT_VIDEO_PATH.format(
        video_key=video_key, chunk_index=chunk_idx, file_index=file_idx
    )
    _concatenate_video_files(paths_to_cat, output_path)
    for past_row_idx in row_indices_in_batch:
        episodes_metadata[past_row_idx][f"videos/{video_key}/chunk_index"] = chunk_idx
        episodes_metadata[past_row_idx][f"videos/{video_key}/file_index"] = file_idx
    batch_rows = [episodes_metadata[i] for i in row_indices_in_batch]
    issue = _validate_concatenated_video(
        root=root,
        new_root=new_root,
        video_key=video_key,
        chunk_idx=chunk_idx,
        file_idx=file_idx,
        output_path=output_path,
        source_paths=list(paths_to_cat),
        batch_rows=batch_rows,
        fps=fps,
    )
    if issue is not None:
        tick_issues.append(issue)


V30_INTRINSIC_STORAGE = "meta/episodes/chunk-*/file-*.parquet::camera_intrinsics"



def _convert_info(
    root: Path,
    new_root: Path,
    data_file_size_in_mb: int,
    video_file_size_in_mb: int,
) -> None:
    info = _load_json(root / INFO_PATH)
    info["codebase_version"] = V30
    info.pop("total_chunks", None)
    info.pop("total_videos", None)
    info["data_files_size_in_mb"] = data_file_size_in_mb
    info["video_files_size_in_mb"] = video_file_size_in_mb
    info["data_path"] = DEFAULT_DATA_PATH
    if info.get("video_path") is not None:
        info["video_path"] = DEFAULT_VIDEO_PATH
    info["fps"] = int(info["fps"])
    for key in info.get("features", {}):
        if info["features"][key].get("dtype") == "video":
            continue
        info["features"][key]["fps"] = info["fps"]
    camera_geometry = info.get("camera_geometry")
    if isinstance(camera_geometry, dict):
        camera_geometry["intrinsic_storage"] = V30_INTRINSIC_STORAGE
    logger.info("Converting info from %s to %s", root, new_root)
    _write_json(new_root / INFO_PATH, info)


def _convert_tasks(root: Path, new_root: Path) -> None:
    logger.info("Converting tasks from %s to %s", root, new_root)
    tasks = _legacy_load_tasks(root)
    df_tasks = pd.DataFrame({"task_index": list(tasks.keys())}, index=pd.Index(list(tasks.values()), name="task"))
    out_path = new_root / DEFAULT_TASKS_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_tasks.to_parquet(out_path)


def _concat_data_files(
    paths_to_cat: list[Path],
    new_root: Path,
    chunk_idx: int,
    file_idx: int,
    image_keys: list[str],
    *,
    global_index_start: int,
) -> None:
    dataframes = [pd.read_parquet(file) for file in paths_to_cat]
    concatenated_df = pd.concat(dataframes, ignore_index=True)
    n = len(concatenated_df)
    concatenated_df["index"] = np.arange(
        int(global_index_start), int(global_index_start) + n, dtype=np.int64
    )
    path = new_root / DEFAULT_DATA_PATH.format(chunk_index=chunk_idx, file_index=file_idx)
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = None
    if image_keys:
        schema = pa.Schema.from_pandas(concatenated_df)
    concatenated_df.to_parquet(path, index=False, schema=schema)


def _episode_index_from_stem(path: Path) -> int:
    return int(path.stem.split("_")[1])


def _convert_data(root: Path, new_root: Path, data_file_size_in_mb: int) -> list[dict[str, Any]]:
    data_dir = root / DATA_DIR
    ep_paths = sorted(data_dir.glob("*/*.parquet"))
    image_keys = _get_image_keys(root)

    chunk_idx = 0
    file_idx = 0
    size_in_mb = 0.0
    num_frames = 0
    paths_to_cat: list[Path] = []
    episodes_metadata: list[dict[str, Any]] = []

    logger.info("Converting data files from %d episodes", len(ep_paths))
    for ep_path in tqdm.tqdm(ep_paths, desc="convert data files"):
        episode_index = _episode_index_from_stem(ep_path)
        ep_size_in_mb = _get_parquet_file_size_in_mb(ep_path)
        ep_num_frames = _get_parquet_num_frames(ep_path)

        if size_in_mb + ep_size_in_mb >= data_file_size_in_mb and paths_to_cat:
            batch_start = int(episodes_metadata[-len(paths_to_cat)]["dataset_from_index"])
            _concat_data_files(
                paths_to_cat,
                new_root,
                chunk_idx,
                file_idx,
                image_keys,
                global_index_start=batch_start,
            )
            chunk_idx, file_idx = _update_chunk_file_indices(chunk_idx, file_idx, DEFAULT_CHUNK_SIZE)
            size_in_mb = 0.0
            paths_to_cat = []

        episodes_metadata.append(
            {
                "episode_index": episode_index,
                "data/chunk_index": chunk_idx,
                "data/file_index": file_idx,
                "dataset_from_index": num_frames,
                "dataset_to_index": num_frames + ep_num_frames,
            }
        )
        size_in_mb += ep_size_in_mb
        num_frames += ep_num_frames
        paths_to_cat.append(ep_path)

    if paths_to_cat:
        batch_start = int(episodes_metadata[-len(paths_to_cat)]["dataset_from_index"])
        _concat_data_files(
            paths_to_cat,
            new_root,
            chunk_idx,
            file_idx,
            image_keys,
            global_index_start=batch_start,
        )
    return episodes_metadata


def _convert_videos_of_camera(
    root: Path,
    new_root: Path,
    video_key: str,
    video_file_size_in_mb: int,
) -> list[dict[str, Any]]:
    videos_dir = root / VIDEO_DIR
    ep_paths = sorted(videos_dir.glob(f"*/{video_key}/*.mp4"))
    info = _load_info(root)
    fps = float(info.get("fps") or 30.0)

    chunk_idx = 0
    file_idx = 0
    size_in_mb = 0.0
    duration_in_s = 0.0
    paths_to_cat: list[Path] = []
    episodes_metadata: list[dict[str, Any]] = []
    row_indices_in_batch: list[int] = []
    tick_issues: list[dict[str, Any]] = []

    for ep_path in tqdm.tqdm(ep_paths, desc=f"convert videos of {video_key}"):
        episode_index = _episode_index_from_stem(ep_path)
        ep_size_in_mb = _get_file_size_in_mb(ep_path)
        ep_duration_in_s = _get_video_duration_in_s(ep_path)

        if size_in_mb + ep_size_in_mb >= video_file_size_in_mb and paths_to_cat:
            _flush_video_concat_batch(
                root=root,
                new_root=new_root,
                video_key=video_key,
                chunk_idx=chunk_idx,
                file_idx=file_idx,
                paths_to_cat=paths_to_cat,
                episodes_metadata=episodes_metadata,
                row_indices_in_batch=row_indices_in_batch,
                fps=fps,
                tick_issues=tick_issues,
            )
            chunk_idx, file_idx = _update_chunk_file_indices(chunk_idx, file_idx, DEFAULT_CHUNK_SIZE)
            size_in_mb = 0.0
            duration_in_s = 0.0
            paths_to_cat = []
            row_indices_in_batch = []

        row_idx = len(episodes_metadata)
        episodes_metadata.append(
            {
                "episode_index": episode_index,
                f"videos/{video_key}/chunk_index": chunk_idx,
                f"videos/{video_key}/file_index": file_idx,
                f"videos/{video_key}/from_timestamp": duration_in_s,
                f"videos/{video_key}/to_timestamp": duration_in_s + ep_duration_in_s,
            }
        )
        paths_to_cat.append(ep_path)
        row_indices_in_batch.append(row_idx)
        size_in_mb += ep_size_in_mb
        duration_in_s += ep_duration_in_s

    if paths_to_cat:
        _flush_video_concat_batch(
            root=root,
            new_root=new_root,
            video_key=video_key,
            chunk_idx=chunk_idx,
            file_idx=file_idx,
            paths_to_cat=paths_to_cat,
            episodes_metadata=episodes_metadata,
            row_indices_in_batch=row_indices_in_batch,
            fps=fps,
            tick_issues=tick_issues,
        )
    if tick_issues:
        print(
            f"[P7/v30] {video_key}: {len(tick_issues)} concatenated file(s) failed tick/timestamp checks "
            f"(see {new_root / VIDEO_CONCAT_TICK_LOG})",
            flush=True,
        )
    return episodes_metadata


def _convert_videos(root: Path, new_root: Path, video_file_size_in_mb: int) -> list[dict[str, Any]] | None:
    logger.info("Converting videos from %s to %s", root, new_root)
    video_keys = _get_video_keys(root)
    if not video_keys:
        return None

    eps_metadata_per_cam = [_convert_videos_of_camera(root, new_root, camera, video_file_size_in_mb) for camera in video_keys]
    num_eps_per_cam = [len(eps_cam_map) for eps_cam_map in eps_metadata_per_cam]
    if len(set(num_eps_per_cam)) != 1:
        raise ValueError(f"All cameras must have the same number of episodes ({num_eps_per_cam}).")

    episodes_metadata: list[dict[str, Any]] = []
    num_episodes = num_eps_per_cam[0]
    for ep_idx in tqdm.tqdm(range(num_episodes), desc="convert videos"):
        ep_ids = [eps_metadata_per_cam[cam_idx][ep_idx]["episode_index"] for cam_idx in range(len(video_keys))]
        if len(set(ep_ids)) != 1:
            raise ValueError(f"All episode indices need to match ({ep_ids}).")
        ep_dict: dict[str, Any] = {"episode_index": ep_ids[0]}
        for cam_idx in range(len(video_keys)):
            cam_meta = dict(eps_metadata_per_cam[cam_idx][ep_idx])
            cam_meta.pop("episode_index", None)
            ep_dict.update(cam_meta)
        episodes_metadata.append(ep_dict)
    return episodes_metadata


def _generate_episode_metadata_dict(
    episodes_legacy_metadata: dict[int, dict[str, Any]],
    episodes_metadata: list[dict[str, Any]],
    episodes_stats: dict[int, dict[str, dict[str, np.ndarray]]],
    episodes_videos: list[dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    videos_by_episode: dict[int, dict[str, Any]] = {}
    if episodes_videos is not None:
        videos_by_episode = {row["episode_index"]: row for row in episodes_videos}

    for ep_metadata in episodes_metadata:
        episode_index = ep_metadata["episode_index"]
        ep_legacy_metadata = episodes_legacy_metadata[episode_index]
        ep_stats = episodes_stats[episode_index]
        ep_video = videos_by_episode.get(episode_index, {})
        ep_ids_set = {episode_index, ep_legacy_metadata["episode_index"], ep_metadata["episode_index"]}
        if episodes_videos is not None and ep_video:
            ep_ids_set.add(ep_video["episode_index"])
        if len(ep_ids_set) != 1:
            raise ValueError(f"Number of episodes is not the same ({ep_ids_set}).")
        ep_dict = {
            **ep_metadata,
            **ep_video,
            **ep_legacy_metadata,
            **_flatten_dict({"stats": ep_stats}),
        }
        ep_dict["meta/episodes/chunk_index"] = 0
        ep_dict["meta/episodes/file_index"] = 0
        yield ep_dict


def _write_episodes(episodes_rows: list[dict[str, Any]], new_root: Path) -> None:
    df = pd.DataFrame(episodes_rows)
    fpath = new_root / DEFAULT_EPISODES_PATH.format(chunk_index=0, file_index=0)
    fpath.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(fpath, index=False)


def _write_stats(stats: dict[str, dict[str, np.ndarray]], new_root: Path) -> None:
    _write_json(new_root / STATS_PATH, _serialize_stats(stats))


def _convert_episodes_metadata(
    root: Path,
    new_root: Path,
    episodes_metadata: list[dict[str, Any]],
    episodes_video_metadata: list[dict[str, Any]] | None = None,
) -> None:
    logger.info("Converting episodes metadata from %s to %s", root, new_root)
    episodes_legacy_metadata = _legacy_load_episodes(root)
    episodes_stats = _legacy_load_episodes_stats(root)

    num_eps_set = {len(episodes_legacy_metadata), len(episodes_metadata)}
    if episodes_video_metadata is not None:
        num_eps_set.add(len(episodes_video_metadata))
    if len(num_eps_set) != 1:
        raise ValueError(f"Number of episodes is not the same ({num_eps_set}).")

    episodes_rows = list(
        _generate_episode_metadata_dict(
            episodes_legacy_metadata,
            episodes_metadata,
            episodes_stats,
            episodes_video_metadata,
        )
    )
    _write_episodes(episodes_rows, new_root)
    _write_stats(_aggregate_stats(list(episodes_stats.values())), new_root)


def convert_v21_directory_to_v30(
    v21_root: Path,
    v30_root: Path,
    *,
    data_file_size_in_mb: int = DEFAULT_DATA_FILE_SIZE_IN_MB,
    video_file_size_in_mb: int = DEFAULT_VIDEO_FILE_SIZE_IN_MB,
) -> Path:
    """Convert a local v2.1 dataset directory to v3.0 layout under ``v30_root``."""
    v21_root = Path(v21_root)
    v30_root = Path(v30_root)
    if v30_root.exists():
        shutil.rmtree(v30_root)
    v30_root.mkdir(parents=True, exist_ok=True)

    info = _load_info(v21_root)
    if str(info.get("codebase_version", V21)) != V21:
        raise ValueError(
            f"Expected codebase_version {V21}, got {info.get('codebase_version')!r} in {v21_root}"
        )

    stats_path = v21_root / LEGACY_EPISODES_STATS_PATH
    if not stats_path.exists():
        write_episodes_stats_jsonl(v21_root)

    _validate_local_dataset_version(v21_root)
    _convert_info(v21_root, v30_root, data_file_size_in_mb, video_file_size_in_mb)
    _convert_tasks(v21_root, v30_root)
    episodes_metadata = _convert_data(v21_root, v30_root, data_file_size_in_mb)
    episodes_videos_metadata = _convert_videos(v21_root, v30_root, video_file_size_in_mb)
    _convert_episodes_metadata(v21_root, v30_root, episodes_metadata, episodes_videos_metadata)
    return v30_root


def _v30_summary(output_root: Path, *, v21_source: Path | None = None) -> dict[str, Any]:
    info = _load_info(output_root)
    tick_log = Path(output_root) / VIDEO_CONCAT_TICK_LOG
    tick_issue_count = 0
    if tick_log.exists():
        tick_issue_count = sum(1 for line in tick_log.read_text(encoding="utf-8").splitlines() if line.strip())
    return {
        "lerobot_version": V30,
        "output_root": str(output_root),
        "v21_source": str(v21_source) if v21_source is not None else None,
        "codebase_version": info.get("codebase_version"),
        "total_episodes": info.get("total_episodes"),
        "total_frames": info.get("total_frames"),
        "data_path": info.get("data_path"),
        "video_path": info.get("video_path"),
        "video_concat_tick_issue_count": tick_issue_count,
        "video_concat_tick_log": str(tick_log) if tick_issue_count else None,
    }


def install_v30_dataset(
    v21_root: Path,
    v30_root: Path,
    *,
    data_file_size_in_mb: int = DEFAULT_DATA_FILE_SIZE_IN_MB,
    video_file_size_in_mb: int = DEFAULT_VIDEO_FILE_SIZE_IN_MB,
) -> dict[str, Any]:
    """Convert v2.1 layout at ``v21_root`` into a standalone v3.0 dataset at ``v30_root``."""
    convert_v21_directory_to_v30(
        Path(v21_root),
        Path(v30_root),
        data_file_size_in_mb=int(data_file_size_in_mb),
        video_file_size_in_mb=int(video_file_size_in_mb),
    )
    return _v30_summary(v30_root, v21_source=Path(v21_root))


def install_v30_dataset_into_root(
    v21_root: Path,
    output_root: Path,
    *,
    data_file_size_in_mb: int = DEFAULT_DATA_FILE_SIZE_IN_MB,
    video_file_size_in_mb: int = DEFAULT_VIDEO_FILE_SIZE_IN_MB,
) -> dict[str, Any]:
    """Convert v2.1 layout and install ``meta/``, ``data/``, ``videos/`` under ``output_root``."""
    output_root = Path(output_root)
    staging = output_root.parent / f".{output_root.name}_v30_staging"
    if staging.exists():
        shutil.rmtree(staging)
    install_v30_dataset(
        v21_root,
        staging,
        data_file_size_in_mb=data_file_size_in_mb,
        video_file_size_in_mb=video_file_size_in_mb,
    )
    for sub in ("meta", "data", "videos"):
        src = staging / sub
        dst = output_root / sub
        if not src.exists():
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))
    if staging.exists():
        shutil.rmtree(staging)
    return _v30_summary(output_root, v21_source=Path(v21_root))


def export_lerobot_v30_from_v21(
    v21_root: Path,
    *,
    output_root: Path | None = None,
    lerobot_root: Path | None = None,
    data_file_size_in_mb: int = DEFAULT_DATA_FILE_SIZE_IN_MB,
    video_file_size_in_mb: int = DEFAULT_VIDEO_FILE_SIZE_IN_MB,
    replace_v21: bool = False,
) -> dict[str, Any]:
    """Convert an existing v2.1 export to v3.0.

    ``lerobot_root`` is deprecated and ignored; conversion is implemented locally.
    """
    del lerobot_root
    v21_root = Path(v21_root)
    if output_root is None:
        output_root = v21_root.parent / f"{v21_root.name}_v30"
    output_root = Path(output_root)

    summary = install_v30_dataset(
        v21_root,
        output_root,
        data_file_size_in_mb=int(data_file_size_in_mb),
        video_file_size_in_mb=int(video_file_size_in_mb),
    )

    if replace_v21 and output_root.resolve() != v21_root.resolve():
        backup = v21_root.parent / f"{v21_root.name}_v21_backup"
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(v21_root), str(backup))
        shutil.move(str(output_root), str(v21_root))
        output_root = v21_root
        backup_note = str(backup)
        summary["output_root"] = str(output_root)
        summary["v21_backup"] = backup_note
    else:
        backup_note = None
        summary["v21_backup"] = None
    return summary


def _link_or_copy(src: Path, dst: Path) -> str:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def _rewrite_parquet_indices(
    src: Path,
    dst: Path,
    *,
    new_episode_index: int,
    global_index_start: int,
) -> int:
    """Write a v3 parquet whose ``index`` is the dataset-global frame id."""
    table = pq.read_table(src)
    n = table.num_rows
    cols: dict[str, pa.Array] = {}
    for name in table.column_names:
        if name == "episode_index":
            cols[name] = pa.array(np.full(n, int(new_episode_index), dtype=np.int64))
        elif name == "index":
            cols[name] = pa.array(
                np.arange(global_index_start, global_index_start + n, dtype=np.int64)
            )
        else:
            cols[name] = table[name]
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(cols, metadata=table.schema.metadata), dst)
    return int(n)


def install_pseudo_v30_from_v21(
    v21_root: Path,
    v30_root: Path,
    *,
    chunks_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """Build a dataloader-compatible v3.0 layout without aggregating media.

    Each v2.1 ``episode_XXXXXX`` parquet/mp4 is hardlinked (or copied) to the
    standard v3 path template as its own ``file-{file_index}``. Episode meta uses
    ``from_timestamp=0`` and contiguous global ``dataset_from/to_index``.
    """
    v21_root = Path(v21_root)
    v30_root = Path(v30_root)
    if v30_root.exists():
        for sub in ("meta", "data", "videos"):
            p = v30_root / sub
            if p.exists():
                shutil.rmtree(p)
    v30_root.mkdir(parents=True, exist_ok=True)

    info = _load_info(v21_root)
    if str(info.get("codebase_version", V21)) not in (V21, "v2.1"):
        # Allow already-written per-episode trees that still use episode_* names.
        pass
    stats_path = v21_root / LEGACY_EPISODES_STATS_PATH
    if not stats_path.exists():
        write_episodes_stats_jsonl(v21_root)

    episodes_legacy = _legacy_load_episodes(v21_root)
    episodes_stats = _legacy_load_episodes_stats(v21_root)
    video_keys = _get_video_keys(v21_root)
    fps = float(info.get("fps", 30))

    ep_paths = _list_v21_episode_parquets(v21_root)
    if not ep_paths:
        raise FileNotFoundError(f"No episode_*.parquet under {v21_root / DATA_DIR}")

    link_counts = {"hardlink": 0, "copy": 0}
    episodes_rows: list[dict[str, Any]] = []
    global_index = 0
    for episode_index, src_parquet in ep_paths:
        length = _get_parquet_num_frames(src_parquet)
        chunk_idx = int(episode_index) // int(chunks_size)
        file_idx = int(episode_index) % int(chunks_size)
        dst_parquet = v30_root / DEFAULT_DATA_PATH.format(chunk_index=chunk_idx, file_index=file_idx)
        _rewrite_parquet_indices(
            src_parquet,
            dst_parquet,
            new_episode_index=int(episode_index),
            global_index_start=global_index,
        )
        link_counts["rewrite"] = link_counts.get("rewrite", 0) + 1

        row: dict[str, Any] = {
            "episode_index": int(episode_index),
            "data/chunk_index": chunk_idx,
            "data/file_index": file_idx,
            "dataset_from_index": global_index,
            "dataset_to_index": global_index + length,
            "length": int(length),
            "meta/episodes/chunk_index": 0,
            "meta/episodes/file_index": 0,
        }
        legacy = episodes_legacy.get(int(episode_index), {})
        tasks = legacy.get("tasks") or []
        if isinstance(tasks, str):
            tasks = [tasks]
        row["tasks"] = list(tasks)
        if legacy.get("camera_intrinsics") is not None:
            if not isinstance(legacy["camera_intrinsics"], dict):
                # The loader indexes camera_intrinsics as a mapping (REPORT_egodex R3).
                raise TypeError(
                    f"episode {episode_index}: camera_intrinsics must be a dict, "
                    f"got {type(legacy['camera_intrinsics']).__name__}"
                )
            row["camera_intrinsics"] = legacy["camera_intrinsics"]

        duration = float(length) / max(fps, 1e-6)
        for vk in video_keys:
            src_vid = (
                v21_root
                / VIDEO_DIR
                / f"chunk-{chunk_idx:03d}"
                / vk
                / f"episode_{int(episode_index):06d}.mp4"
            )
            if not src_vid.exists():
                # fall back to path helper (same layout)
                src_vid = _video_paths_for_episode(v21_root, int(episode_index), [vk]).get(vk)
            if src_vid is None or not Path(src_vid).exists():
                raise FileNotFoundError(f"Missing video for ep {episode_index} key {vk}")
            dst_vid = v30_root / DEFAULT_VIDEO_PATH.format(
                video_key=vk, chunk_index=chunk_idx, file_index=file_idx
            )
            mode = _link_or_copy(Path(src_vid), dst_vid)
            link_counts[mode] = link_counts.get(mode, 0) + 1
            prefix = f"videos/{vk}"
            row[f"{prefix}/chunk_index"] = chunk_idx
            row[f"{prefix}/file_index"] = file_idx
            row[f"{prefix}/from_timestamp"] = 0.0
            row[f"{prefix}/to_timestamp"] = duration

        ep_stats = episodes_stats.get(int(episode_index))
        if ep_stats is not None:
            row.update(_flatten_dict({"stats": ep_stats}))
        episodes_rows.append(row)
        global_index += length

    # info.json
    new_info = dict(info)
    new_info["codebase_version"] = V30
    new_info.pop("total_chunks", None)
    new_info.pop("total_videos", None)
    new_info["data_files_size_in_mb"] = 0  # per-episode sentinel
    new_info["video_files_size_in_mb"] = 0
    new_info["data_path"] = DEFAULT_DATA_PATH
    new_info["video_path"] = DEFAULT_VIDEO_PATH
    new_info["chunks_size"] = int(chunks_size)
    new_info["fps"] = int(fps)
    new_info["total_episodes"] = len(episodes_rows)
    new_info["total_frames"] = int(global_index)
    for key, ft in (new_info.get("features") or {}).items():
        if isinstance(ft, dict) and ft.get("dtype") != "video":
            ft["fps"] = int(fps)
    cam_geom = new_info.get("camera_geometry")
    if isinstance(cam_geom, dict):
        cam_geom["intrinsic_storage"] = V30_INTRINSIC_STORAGE
    _write_json(v30_root / INFO_PATH, new_info)

    _convert_tasks(v21_root, v30_root)
    _write_episodes(episodes_rows, v30_root)
    write_lerobot_v30_stats(v30_root)

    # Preserve source map / frame-removal / episode-discard records when present (episode indices unchanged).
    for name in ("episode_source_map.jsonl", "frame_removal.json", "episode_discard.json"):
        src_meta = v21_root / "meta" / name
        if src_meta.exists():
            (v30_root / "meta").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_meta, v30_root / "meta" / name)

    summary = _v30_summary(v30_root, v21_source=v21_root)
    summary["layout"] = "pseudo_v3"
    summary["link_mode_counts"] = link_counts
    return summary


def install_pseudo_v30_into_root(
    v21_root: Path,
    output_root: Path,
    *,
    chunks_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """Install pseudo-v3 under ``output_root`` (replaces meta/data/videos)."""
    output_root = Path(output_root)
    staging = output_root.parent / f".{output_root.name}_pseudo_v30_staging"
    if staging.exists():
        shutil.rmtree(staging)
    summary = install_pseudo_v30_from_v21(v21_root, staging, chunks_size=chunks_size)
    for sub in ("meta", "data", "videos"):
        src = staging / sub
        dst = output_root / sub
        if not src.exists():
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))
    if staging.exists():
        shutil.rmtree(staging)
    summary["output_root"] = str(output_root)
    return summary
