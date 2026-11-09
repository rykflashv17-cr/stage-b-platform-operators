from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from robot_data_processing.loader import (
    episode_parquet_path,
    filter_table_by_indices,
    keep_indices_from_table,
    read_episode_table,
    valid_keep_length,
)
from robot_data_processing.mask import keep_indices_from_mask
from robot_data_processing.preprocess import (
    apply_robomind_temporal_alignment,
    extract_state_action_from_table,
    update_table_action_columns,
)
from robot_data_processing.schema import DatasetSchema, stack_column
from robot_data_processing.stages.state_action_temporal_alignment import (
    apply_state_action_temporal_alignment,
    matched_alignment_dims,
)


@dataclass
class EpisodeExportResult:
    episode_index: int
    original_length: int
    exported_length: int
    truncated: bool
    video_keys: list[str] = field(default_factory=list)


@dataclass
class ExportSummary:
    total_episodes: int
    total_frames: int
    truncated_episodes: int
    skipped_episodes: int = 0
    episodes: list[EpisodeExportResult] = field(default_factory=list)


@dataclass
class LerobotExportContext:
    source_root: Path
    output_root: Path
    info: dict[str, Any]
    fps: float
    chunks_size: int
    video_keys: list[str]
    src_episodes: dict[int, dict[str, Any]]
    src_source_map: dict[int, dict[str, Any]]
    recompute_video_stats: bool


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=err)


# Every exported (re-encoded) video: H.264 / libx264, closed fixed GOP of VIDEO_GOP frames
# (no scene-cut keyframes), constant quality VIDEO_CRF. The review (DEEP-GOP / DEEP-CRF)
# reads these back from the x264 SEI and keyframe positions.
VIDEO_GOP = 10
VIDEO_CRF = 18
VIDEO_CODEC = "libx264"
_ENCODER_ARGS_CACHE: list[str] | None = None


def _ffmpeg_video_encoders() -> set[str]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    encoders: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            encoders.add(parts[1])
    return encoders


def _probe_video_encoder(encoder: str, extra: list[str]) -> bool:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=0.1",
            "-frames:v",
            "1",
            "-c:v",
            encoder,
            *extra,
            "-f",
            "null",
            "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return proc.returncode == 0


# Scratch cut that is re-encoded again (normalize): lossless, fastest preset, so the cut
# costs little and adds no generation loss to the final CRF encode.
INTERMEDIATE_ENCODER_ARGS = ["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p"]


def _video_encoder_args() -> list[str]:
    """libx264 args for every filtergraph export: CRF ``VIDEO_CRF``, fixed GOP ``VIDEO_GOP``.

    No fallback encoder: h264_v4l2m2m / mpeg4 cannot honour CRF, so a missing libx264
    is a hard error instead of silently exporting off-spec video.
    """
    global _ENCODER_ARGS_CACHE
    if _ENCODER_ARGS_CACHE is not None:
        return _ENCODER_ARGS_CACHE

    extra = [
        "-pix_fmt", "yuv420p",
        "-crf", str(VIDEO_CRF),
        "-g", str(VIDEO_GOP),
        "-keyint_min", str(VIDEO_GOP),
        "-sc_threshold", "0",
    ]
    if VIDEO_CODEC not in _ffmpeg_video_encoders() or not _probe_video_encoder(VIDEO_CODEC, extra):
        raise RuntimeError(
            f"ffmpeg {VIDEO_CODEC} is required (export spec: GOP={VIDEO_GOP}, CRF={VIDEO_CRF}); "
            "install an ffmpeg build with libx264"
        )
    _ENCODER_ARGS_CACHE = ["-c:v", VIDEO_CODEC, *extra]
    return _ENCODER_ARGS_CACHE


def _export_video_with_trim_runs(
    src: Path, dst: Path, runs: list[tuple[int, int]], encoder_args: list[str] | None = None
) -> None:
    """Re-encode selected frame runs; required when Stage4 removes interior/static frames."""
    if not runs:
        raise ValueError(f"No frame runs to export for video: {src}")

    parts: list[str] = []
    concat_inputs: list[str] = []
    for i, (start, end) in enumerate(runs):
        parts.append(
            f"[0:v]trim=start_frame={start}:end_frame={end},"
            f"setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
        )
        concat_inputs.append(f"[v{i}]")

    if len(runs) == 1:
        filter_complex = parts[0].replace("[v0]", "[out]")
    else:
        filter_complex = (
            ";".join(parts)
            + f";{''.join(concat_inputs)}concat=n={len(runs)}:v=1:a=0,format=yuv420p[out]"
        )

    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            *(encoder_args or _video_encoder_args()),
            str(dst),
        ]
    )


def load_info(source_root: Path) -> dict[str, Any]:
    with (source_root / "meta" / "info.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def video_keys_from_info(info: dict[str, Any]) -> list[str]:
    return [k for k, v in info["features"].items() if v.get("dtype") == "video"]


def video_path(source_root: Path, episode_index: int, video_key: str) -> Path:
    chunk = episode_index // 1000
    return (
        source_root
        / "videos"
        / f"chunk-{chunk:03d}"
        / video_key
        / f"episode_{episode_index:06d}.mp4"
    )


def parameters_path(source_root: Path, episode_index: int) -> Path:
    chunk = episode_index // 1000
    return source_root / "parameters" / f"chunk-{chunk:03d}" / f"episode_{episode_index:06d}"


def valid_prefix_length(table) -> int:
    """Backward-compatible alias for kept frame count."""
    return valid_keep_length(table)


def _contiguous_runs(indices: np.ndarray) -> list[tuple[int, int]]:
    if indices.size == 0:
        return []
    runs: list[tuple[int, int]] = []
    start = prev = int(indices[0])
    for idx in indices[1:]:
        idx = int(idx)
        if idx == prev + 1:
            prev = idx
            continue
        runs.append((start, prev + 1))
        start = prev = idx
    runs.append((start, prev + 1))
    return runs


def extract_video_by_indices(src: Path, dst: Path, indices: np.ndarray, fps: float) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    indices = np.asarray(indices, dtype=np.int64)
    if indices.size == 0:
        raise ValueError(f"Cannot export empty video: {src}")
    # Contiguous prefix [0..N): a single trim run (see trim_video).
    if np.array_equal(indices, np.arange(indices.size)) and int(indices[0]) == 0:
        trim_video(src, dst, int(indices.size), fps)
        return

    runs = _contiguous_runs(indices)
    _export_video_with_trim_runs(src, dst, runs)


def _replace_column(table, name: str, values: np.ndarray):
    idx = table.column_names.index(name)
    return table.set_column(idx, name, pa.array(values))


def filter_parquet_table(
    table,
    keep_indices: np.ndarray,
    episode_index: int,
    global_index_start: int,
    fps: float,
) -> pa.Table:
    table = filter_table_by_indices(table, keep_indices)
    if "step_validity_mask" in table.column_names:
        table = table.drop(["step_validity_mask"])

    keep_len = table.num_rows
    if keep_len == 0:
        return table

    frame_index = np.arange(keep_len, dtype=np.int64)
    timestamp = (frame_index / fps).astype(np.float32)
    episode_col = np.full(keep_len, episode_index, dtype=np.int64)
    index_col = np.arange(global_index_start, global_index_start + keep_len, dtype=np.int64)

    # Source ``timestamps`` (capture clock) stay as filtered: rewriting them to
    # ts0 + k*dt would erase real gaps (EGO-03).
    table = _replace_column(table, "frame_index", frame_index)
    table = _replace_column(table, "timestamp", timestamp)
    table = _replace_column(table, "episode_index", episode_col)
    table = _replace_column(table, "index", index_col)
    return table


def slice_parquet_table(
    table,
    keep_len: int,
    episode_index: int,
    global_index_start: int,
    fps: float,
) -> pa.Table:
    """Legacy prefix-only slice; prefer filter_parquet_table with keep_indices."""
    return filter_parquet_table(
        table,
        np.arange(keep_len, dtype=np.int64),
        episode_index,
        global_index_start,
        fps,
    )


def probe_video_frames(path: Path) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_frames",
        "-of",
        "csv=p=0",
        str(path),
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
    if out.isdigit() and int(out) > 0:
        return int(out)
    # Fallback: decode count (slow).
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "csv=p=0",
        str(path),
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
    return int(out) if out.isdigit() else 0


def trim_video(src: Path, dst: Path, num_frames: int, fps: float) -> None:
    """First ``num_frames`` frames in display order, re-encoded.

    No ``-frames:v N -c copy``: that counts packets in decode order, so with B-frames
    (``has_b_frames>0``) the last 1-2 rows get a later frame and pts jump (HB-R2).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if num_frames <= 0:
        raise ValueError(f"Cannot trim video to {num_frames} frames: {src}")
    _export_video_with_trim_runs(src, dst, [(0, int(num_frames))])


_QUANTILES: tuple[tuple[float, str], ...] = (
    (0.01, "q01"),
    (0.10, "q10"),
    (0.50, "q50"),
    (0.90, "q90"),
    (0.99, "q99"),
)


def _empty_feature_stats() -> dict[str, Any]:
    out: dict[str, Any] = {"min": [], "max": [], "mean": [], "std": [], "count": [0]}
    for _, key in _QUANTILES:
        out[key] = []
    return out


def _stats_1d(values: np.ndarray) -> dict[str, Any]:
    """LeRobot v3 feature stats: min/max/mean/std/count + q01/q10/q50/q90/q99."""
    if values.size == 0:
        return _empty_feature_stats()
    if values.ndim == 1:
        values = values.reshape(-1, 1)
    values = np.asarray(values, dtype=np.float64)
    out: dict[str, Any] = {
        "min": values.min(axis=0).tolist(),
        "max": values.max(axis=0).tolist(),
        "mean": values.mean(axis=0).tolist(),
        "std": values.std(axis=0).tolist(),
        "count": [int(values.shape[0])],
    }
    for q, key in _QUANTILES:
        out[key] = np.quantile(values, q, axis=0).tolist()
    return out


def _stats_scalar(values: np.ndarray) -> dict[str, Any]:
    if values.size == 0:
        return {"min": [], "max": [], "mean": [], "std": [], "count": [0]}
    return {
        "min": [float(values.min())],
        "max": [float(values.max())],
        "mean": [float(values.mean())],
        "std": [float(values.std())],
        "count": [int(values.size)],
    }


def _video_stats_from_samples(samples: np.ndarray) -> dict[str, Any]:
    if samples.size == 0:
        return {
            "min": [[[0.0]], [[0.0]], [[0.0]]],
            "max": [[[1.0]], [[1.0]], [[1.0]]],
            "mean": [[[0.5]], [[0.5]], [[0.5]]],
            "std": [[[0.25]], [[0.25]], [[0.25]]],
            "count": [0],
        }
    # samples: (N, 3)
    mins, maxs, means, stds = [], [], [], []
    for c in range(3):
        col = samples[:, c]
        mins.append([[float(col.min())]])
        maxs.append([[float(col.max())]])
        means.append([[float(col.mean())]])
        stds.append([[float(col.std())]])
    return {
        "min": mins,
        "max": maxs,
        "mean": means,
        "std": stds,
        "count": [int(samples.shape[0])],
    }


def sample_video_pixels(path: Path, max_samples: int = 361) -> np.ndarray:
    total = probe_video_frames(path)
    if total <= 0:
        return np.zeros((0, 3), dtype=np.float64)
    n_samples = min(max_samples, total)
    stride = max(1, total // n_samples)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        f"select='not(mod(n\\,{stride}))',scale=1:1",
        "-vsync",
        "vfr",
        "-frames:v",
        str(n_samples),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    if not raw:
        return np.zeros((0, 3), dtype=np.float64)
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.float64) / 255.0
    return arr


def compute_episode_stats(table: pa.Table, video_paths: dict[str, Path]) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    for name in table.column_names:
        col = table.column(name).combine_chunks()
        if name in video_paths:
            stats[name] = _video_stats_from_samples(sample_video_pixels(video_paths[name]))
            continue
        values = col.to_numpy(zero_copy_only=False)
        if values.dtype == object or (isinstance(values, np.ndarray) and values.dtype == object):
            arr = stack_column(values)
            stats[name] = _stats_1d(arr)
        else:
            stats[name] = _stats_scalar(np.asarray(values, dtype=np.float64))
    return stats


def _aggregate_global_stats(episode_stats: list[dict[str, Any]], info: dict[str, Any]) -> dict[str, Any]:
    """Build meta/stats.json-style aggregates for state/action vectors."""
    state_key = "observation.state"
    action_key = "action"
    if not episode_stats or state_key not in episode_stats[0]:
        return {}

    def _weighted(feature: str, stat_name: str) -> list[float]:
        total = 0
        acc: list[float] = []
        for ep in episode_stats:
            count = ep[state_key]["count"][0] if feature == state_key else ep[action_key]["count"][0]
            vals = ep[feature][stat_name]
            total += count
            if not acc:
                acc = [0.0] * len(vals)
            for i, v in enumerate(vals):
                acc[i] += float(v) * count
        return [v / max(total, 1) for v in acc]

    def _global_min(feature: str) -> list[float]:
        vals = [v for ep in episode_stats for v in ep[feature]["min"]]
        if not vals:
            return []
        dim = len(episode_stats[0][feature]["min"])
        out = []
        for d in range(dim):
            out.append(min(ep[feature]["min"][d] for ep in episode_stats))
        return out

    def _global_max(feature: str) -> list[float]:
        dim = len(episode_stats[0][feature]["min"])
        return [max(ep[feature]["max"][d] for ep in episode_stats) for d in range(dim)]

    return {
        "state": {
            "min": _global_min(state_key),
            "max": _global_max(state_key),
            "mean": _weighted(state_key, "mean"),
            "std": _weighted(state_key, "std"),
        },
        "action": {
            "min": _global_min(action_key),
            "max": _global_max(action_key),
            "mean": _weighted(action_key, "mean"),
            "std": _weighted(action_key, "std"),
        },
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def prepare_episode_table_for_export(
    dataset_root: Path,
    episode_index: int,
    schema: DatasetSchema,
    *,
    alignment_enabled: bool,
    alignment_lag: int,
    alignment_plan: str = "apply_stats",
    manual_delay: int | None = None,
):
    table = read_episode_table(episode_parquet_path(dataset_root, episode_index))
    if not alignment_enabled or schema.embodiment not in ("humanoid", "robomind_ur"):
        return table
    state, action = extract_state_action_from_table(table, schema)
    if alignment_plan == "manual":
        if manual_delay is None:
            raise ValueError("manual alignment_plan requires manual_delay")
        from robot_data_processing.stages.state_action_temporal_alignment import (
            apply_manual_action_delay,
        )

        aligned_action = apply_manual_action_delay(action, int(manual_delay))
    elif schema.embodiment == "robomind_ur":
        aligned_action = apply_robomind_temporal_alignment(state, action, alignment_lag)
    else:
        num_dims = matched_alignment_dims(schema, state, action)
        aligned_action = apply_state_action_temporal_alignment(
            state, action, alignment_lag, num_dims
        )
    return update_table_action_columns(table, aligned_action, schema)


def create_export_context(
    source_root: Path,
    output_root: Path,
    *,
    recompute_video_stats: bool = True,
) -> LerobotExportContext:
    source_root = Path(source_root)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    info = load_info(source_root)
    meta_out = output_root / "meta"
    meta_out.mkdir(parents=True, exist_ok=True)
    for name in (
        "tasks.jsonl",
        "modality.json",
        "dreamzero_metadata.json",
        "dreamzero_ee_metadata.json",
        "dreamzero_ee_stats.json",
    ):
        src = source_root / "meta" / name
        if src.exists():
            shutil.copy2(src, meta_out / name)
    return LerobotExportContext(
        source_root=source_root,
        output_root=output_root,
        info=info,
        fps=float(info.get("fps", 30)),
        chunks_size=int(info.get("chunks_size", 1000)),
        video_keys=video_keys_from_info(info),
        src_episodes={row["episode_index"]: row for row in _load_jsonl(source_root / "meta" / "episodes.jsonl")},
        src_source_map={
            row["merged_episode_index"]: row
            for row in _load_jsonl(source_root / "meta" / "episode_source_map.jsonl")
        },
        recompute_video_stats=recompute_video_stats,
    )


def _export_episode_videos(
    ctx: LerobotExportContext,
    *,
    episode_index: int,
    chunk: int,
    keep_indices: np.ndarray,
    parallel_videos: bool,
) -> dict[str, Path]:
    out_videos: dict[str, Path] = {}

    def _one(vk: str) -> tuple[str, Path]:
        src_vid = video_path(ctx.source_root, episode_index, vk)
        dst_vid = ctx.output_root / "videos" / f"chunk-{chunk:03d}" / vk / f"episode_{episode_index:06d}.mp4"
        if not src_vid.exists():
            raise FileNotFoundError(f"Missing source video: {src_vid}")
        extract_video_by_indices(src_vid, dst_vid, keep_indices, ctx.fps)
        return vk, dst_vid

    if parallel_videos and len(ctx.video_keys) > 1:
        with ThreadPoolExecutor(max_workers=len(ctx.video_keys)) as pool:
            futures = [pool.submit(_one, vk) for vk in ctx.video_keys]
            for fut in futures:
                vk, dst = fut.result()
                out_videos[vk] = dst
    else:
        for vk in ctx.video_keys:
            vk, dst = _one(vk)
            out_videos[vk] = dst
    return out_videos


def export_single_episode(
    ctx: LerobotExportContext,
    *,
    dataset_root: Path,
    schema: DatasetSchema,
    episode_index: int,
    step_validity_mask: np.ndarray,
    global_index: int,
    alignment_enabled: bool,
    alignment_lag: int,
    parallel_videos: bool = True,
    alignment_plan: str = "apply_stats",
    manual_delay: int | None = None,
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, EpisodeExportResult | None]:
    keep_indices = keep_indices_from_mask(step_validity_mask)
    if keep_indices.size == 0:
        return global_index, None, None, None, None

    table = prepare_episode_table_for_export(
        dataset_root,
        episode_index,
        schema,
        alignment_enabled=alignment_enabled,
        alignment_lag=alignment_lag,
        alignment_plan=alignment_plan,
        manual_delay=manual_delay,
    )
    original_length = table.num_rows
    keep_len = int(keep_indices.size)
    truncated = keep_len < original_length

    chunk = episode_index // 1000
    out_parquet = ctx.output_root / "data" / f"chunk-{chunk:03d}" / f"episode_{episode_index:06d}.parquet"
    out_parquet.parent.mkdir(parents=True, exist_ok=True)
    sliced = filter_parquet_table(table, keep_indices, episode_index, global_index, ctx.fps)
    pq.write_table(sliced, out_parquet, compression="snappy")

    out_videos = _export_episode_videos(
        ctx,
        episode_index=episode_index,
        chunk=chunk,
        keep_indices=keep_indices,
        parallel_videos=parallel_videos,
    )

    src_params = parameters_path(ctx.source_root, episode_index)
    if src_params.exists():
        dst_params = ctx.output_root / "parameters" / f"chunk-{chunk:03d}" / f"episode_{episode_index:06d}"
        if dst_params.exists():
            shutil.rmtree(dst_params)
        shutil.copytree(src_params, dst_params)

    src_ep = ctx.src_episodes.get(
        episode_index,
        {"episode_index": episode_index, "tasks": [], "length": original_length},
    )
    episode_row = {
        "episode_index": episode_index,
        "tasks": src_ep.get("tasks", []),
        "length": keep_len,
    }
    ep_stats = compute_episode_stats(sliced, out_videos if ctx.recompute_video_stats else {})
    stats_row = {"episode_index": episode_index, "stats": ep_stats}

    src_map = ctx.src_source_map.get(episode_index, {})
    source_map_row = {
        "merged_episode_index": episode_index,
        "merged_global_index_start": global_index,
        "merged_global_index_end": global_index + keep_len - 1,
        "merged_num_frames": keep_len,
        "source_repo_rel": src_map.get("source_repo_rel"),
        "source_episode_index": src_map.get("source_episode_index"),
        "source_parquet_rel": src_map.get("source_parquet_rel"),
        "merged_parquet_rel": f"data/chunk-{chunk:03d}/episode_{episode_index:06d}.parquet",
        "videos": {
            vk: {
                "src": src_map.get("videos", {}).get(vk, {}).get("src"),
                "dst": f"videos/chunk-{chunk:03d}/{vk}/episode_{episode_index:06d}.mp4",
            }
            for vk in ctx.video_keys
        },
        "parameters_copied": src_params.exists(),
        "parameters_dst_rel": f"parameters/chunk-{chunk:03d}/episode_{episode_index:06d}",
    }

    export_result = EpisodeExportResult(
        episode_index=episode_index,
        original_length=original_length,
        exported_length=keep_len,
        truncated=truncated,
        video_keys=ctx.video_keys,
    )
    return global_index + keep_len, episode_row, stats_row, source_map_row, export_result


def finalize_lerobot_export(
    ctx: LerobotExportContext,
    episodes_out: list[dict[str, Any]],
    episodes_stats_out: list[dict[str, Any]],
    source_map_out: list[dict[str, Any]],
    export_results: list[EpisodeExportResult],
    *,
    skipped_episodes: int = 0,
) -> ExportSummary:
    meta_out = ctx.output_root / "meta"
    _write_jsonl(meta_out / "episodes.jsonl", episodes_out)
    _write_jsonl(meta_out / "episodes_stats.jsonl", episodes_stats_out)
    _write_jsonl(meta_out / "episode_source_map.jsonl", source_map_out)

    total_frames = sum(row["length"] for row in episodes_out)
    truncated_count = sum(1 for ep in export_results if ep.truncated)
    num_eps = len(episodes_out)

    out_info = json.loads(json.dumps(ctx.info))
    out_info.update(
        {
            "total_episodes": num_eps,
            "total_frames": total_frames,
            "total_videos": num_eps * len(ctx.video_keys),
            "total_chunks": max(1, math.ceil(num_eps / ctx.chunks_size)),
            "splits": {"train": f"0:{num_eps}"},
        }
    )
    with (meta_out / "info.json").open("w", encoding="utf-8") as f:
        json.dump(out_info, f, indent=4, ensure_ascii=False)
        f.write("\n")

    global_stats = _aggregate_global_stats([row["stats"] for row in episodes_stats_out], out_info)
    if global_stats:
        with (meta_out / "stats.json").open("w", encoding="utf-8") as f:
            json.dump(global_stats, f, indent=2, ensure_ascii=False)
            f.write("\n")

    summary = ExportSummary(
        total_episodes=num_eps,
        total_frames=total_frames,
        truncated_episodes=truncated_count,
        skipped_episodes=skipped_episodes,
        episodes=export_results,
    )
    with (ctx.output_root / "export_report.json").open("w", encoding="utf-8") as f:
        json.dump(
            {
                "source_root": str(ctx.source_root),
                "output_root": str(ctx.output_root),
                "total_episodes": summary.total_episodes,
                "total_frames": summary.total_frames,
                "truncated_episodes": summary.truncated_episodes,
                "skipped_episodes": summary.skipped_episodes,
                "episodes": [ep.__dict__ for ep in summary.episodes],
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    return summary


_EXPORT_POOL_STATE: dict[str, Any] = {}


def _init_export_pool(state: dict[str, Any]) -> None:
    global _EXPORT_POOL_STATE
    _EXPORT_POOL_STATE = state


def _export_worker(task: tuple[int, np.ndarray]) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    EpisodeExportResult | None,
]:
    episode_index, mask = task
    ctx: LerobotExportContext = _EXPORT_POOL_STATE["ctx"]
    _, episode_row, stats_row, source_map_row, export_result = export_single_episode(
        ctx,
        dataset_root=_EXPORT_POOL_STATE["dataset_root"],
        schema=_EXPORT_POOL_STATE["schema"],
        episode_index=episode_index,
        step_validity_mask=mask,
        global_index=0,
        alignment_enabled=_EXPORT_POOL_STATE["alignment_enabled"],
        alignment_lag=_EXPORT_POOL_STATE["alignment_lag"],
        parallel_videos=_EXPORT_POOL_STATE.get("parallel_videos", True),
        alignment_plan=_EXPORT_POOL_STATE.get("alignment_plan", "apply_stats"),
        manual_delay=_EXPORT_POOL_STATE.get("manual_delay"),
    )
    return episode_row, stats_row, source_map_row, export_result


def _assign_global_indices(
    output_root: Path,
    episodes_out: list[dict[str, Any]],
    source_map_out: list[dict[str, Any]],
) -> None:
    episodes_out.sort(key=lambda row: row["episode_index"])
    source_map_out.sort(key=lambda row: row["merged_episode_index"])
    global_index = 0
    for ep_row, sm_row in zip(episodes_out, source_map_out):
        if ep_row["episode_index"] != sm_row["merged_episode_index"]:
            raise ValueError(
                f"Episode index mismatch: {ep_row['episode_index']} vs {sm_row['merged_episode_index']}"
            )
        length = int(ep_row["length"])
        sm_row["merged_global_index_start"] = global_index
        sm_row["merged_global_index_end"] = global_index + length - 1
        sm_row["merged_num_frames"] = length

        ep_idx = int(ep_row["episode_index"])
        parquet_path = (
            output_root / "data" / f"chunk-{ep_idx // 1000:03d}" / f"episode_{ep_idx:06d}.parquet"
        )
        table = pq.read_table(parquet_path)
        table = _replace_column(
            table,
            "index",
            np.arange(global_index, global_index + length, dtype=np.int64),
        )
        pq.write_table(table, parquet_path, compression="snappy")
        global_index += length


def _default_export_workers(requested: int | None) -> int:
    if requested is not None and requested > 0:
        return requested
    return min(16, os.cpu_count() or 4)


def export_lerobot_from_results(
    *,
    source_root: Path,
    output_root: Path,
    schema: DatasetSchema,
    results: list[Any],
    alignment_lag: int = 0,
    alignment_enabled: bool = False,
    recompute_video_stats: bool = False,
    export_workers: int | None = None,
    parallel_videos: bool = True,
    show_progress: bool = True,
    alignment_plan: str = "apply_stats",
    manual_delay: int | None = None,
) -> ExportSummary:
    from tqdm import tqdm

    workers = _default_export_workers(export_workers)
    ctx = create_export_context(source_root, output_root, recompute_video_stats=recompute_video_stats)
    episodes_out: list[dict[str, Any]] = []
    episodes_stats_out: list[dict[str, Any]] = []
    source_map_out: list[dict[str, Any]] = []
    export_results: list[EpisodeExportResult] = []
    skipped = 0

    ordered = sorted(results, key=lambda r: r.episode_index)
    tasks: list[tuple[int, np.ndarray]] = []
    for result in ordered:
        if result.step_validity_mask is None or result.step_validity_mask.size == 0:
            skipped += 1
            continue
        if int(result.step_validity_mask.sum()) == 0:
            skipped += 1
            continue
        tasks.append((result.episode_index, result.step_validity_mask))

    if workers <= 1:
        global_index = 0
        iterator = tasks
        if show_progress:
            iterator = tqdm(tasks, desc="export lerobot")
        for episode_index, mask in iterator:
            global_index, episode_row, stats_row, source_map_row, export_result = export_single_episode(
                ctx,
                dataset_root=source_root,
                schema=schema,
                episode_index=episode_index,
                step_validity_mask=mask,
                global_index=global_index,
                alignment_enabled=alignment_enabled,
                alignment_lag=alignment_lag,
                parallel_videos=parallel_videos,
                alignment_plan=alignment_plan,
                manual_delay=manual_delay,
            )
            if export_result is None:
                skipped += 1
                continue
            episodes_out.append(episode_row)
            episodes_stats_out.append(stats_row)
            source_map_out.append(source_map_row)
            export_results.append(export_result)
    else:
        pool_state = {
            "ctx": ctx,
            "dataset_root": source_root,
            "schema": schema,
            "alignment_enabled": alignment_enabled,
            "alignment_lag": alignment_lag,
            "parallel_videos": parallel_videos,
            "alignment_plan": alignment_plan,
            "manual_delay": manual_delay,
        }
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_export_pool,
            initargs=(pool_state,),
        ) as pool:
            futures = {pool.submit(_export_worker, task): task[0] for task in tasks}
            iterator = as_completed(futures)
            if show_progress:
                iterator = tqdm(iterator, total=len(futures), desc=f"export lerobot ({workers}w)")
            for fut in iterator:
                episode_row, stats_row, source_map_row, export_result = fut.result()
                if export_result is None:
                    skipped += 1
                    continue
                episodes_out.append(episode_row)
                episodes_stats_out.append(stats_row)
                source_map_out.append(source_map_row)
                export_results.append(export_result)
        _assign_global_indices(output_root, episodes_out, source_map_out)
        export_results.sort(key=lambda ep: ep.episode_index)

    return finalize_lerobot_export(
        ctx,
        episodes_out,
        episodes_stats_out,
        source_map_out,
        export_results,
        skipped_episodes=skipped,
    )


def export_lerobot_dataset(
    source_root: Path,
    output_root: Path,
    episode_indices: list[int],
    *,
    results: list[Any],
    schema: DatasetSchema,
    alignment_lag: int = 0,
    alignment_enabled: bool = False,
    recompute_video_stats: bool = False,
    export_workers: int | None = None,
    parallel_videos: bool = True,
    show_progress: bool = True,
    alignment_plan: str = "apply_stats",
    manual_delay: int | None = None,
) -> ExportSummary:
    """Export LeRobot dataset from pipeline EpisodeResult list (mask applied in-memory, no intermediate parquet)."""
    result_by_ep = {r.episode_index: r for r in results}
    missing = [ep for ep in episode_indices if ep not in result_by_ep]
    if missing:
        raise ValueError(f"Missing EpisodeResult for indices: {missing[:5]}{'...' if len(missing) > 5 else ''}")
    ordered_results = [result_by_ep[ep] for ep in episode_indices if ep in result_by_ep]
    return export_lerobot_from_results(
        source_root=source_root,
        output_root=output_root,
        schema=schema,
        results=ordered_results,
        alignment_lag=alignment_lag,
        alignment_enabled=alignment_enabled,
        recompute_video_stats=recompute_video_stats,
        export_workers=export_workers,
        parallel_videos=parallel_videos,
        show_progress=show_progress,
        alignment_plan=alignment_plan,
        manual_delay=manual_delay,
    )


@dataclass
class AlignmentIssue:
    episode_index: int
    issue: str


def verify_lerobot_alignment(output_root: Path, episode_indices: list[int] | None = None) -> dict[str, Any]:
    output_root = Path(output_root)
    info = load_info(output_root)
    fps = float(info.get("fps", 30))
    video_keys = video_keys_from_info(info)

    episodes = _load_jsonl(output_root / "meta" / "episodes.jsonl")
    if episode_indices is None:
        episode_indices = [row["episode_index"] for row in episodes]
    ep_lengths = {row["episode_index"]: row["length"] for row in episodes}

    issues: list[AlignmentIssue] = []
    checked = 0
    expected_global = 0

    for episode_index in episode_indices:
        checked += 1
        expected_len = ep_lengths.get(episode_index)
        parquet_path = output_root / "data" / f"chunk-{episode_index // 1000:03d}" / f"episode_{episode_index:06d}.parquet"
        if not parquet_path.exists():
            issues.append(AlignmentIssue(episode_index, "missing parquet"))
            continue

        table = pq.read_table(parquet_path)
        n_rows = table.num_rows
        if expected_len is not None and n_rows != expected_len:
            issues.append(AlignmentIssue(episode_index, f"episodes.jsonl length={expected_len} != parquet rows={n_rows}"))

        if n_rows == 0:
            continue

        frame_index = table.column("frame_index").combine_chunks().to_numpy()
        if not np.array_equal(frame_index, np.arange(n_rows, dtype=np.int64)):
            issues.append(AlignmentIssue(episode_index, "frame_index not 0..N-1"))

        index_col = table.column("index").combine_chunks().to_numpy()
        if not np.array_equal(index_col, np.arange(expected_global, expected_global + n_rows, dtype=np.int64)):
            issues.append(AlignmentIssue(episode_index, f"global index mismatch, expected start={expected_global}"))

        episode_col = table.column("episode_index").combine_chunks().to_numpy()
        if not np.all(episode_col == episode_index):
            issues.append(AlignmentIssue(episode_index, "episode_index column mismatch"))

        ts = table.column("timestamp").combine_chunks().to_numpy()
        expected_ts = np.arange(n_rows, dtype=np.float32) / fps
        if not np.allclose(ts, expected_ts, rtol=0, atol=1e-4):
            issues.append(AlignmentIssue(episode_index, "timestamp not aligned with frame_index/fps"))

        for vk in video_keys:
            vid = output_root / "videos" / f"chunk-{episode_index // 1000:03d}" / vk / f"episode_{episode_index:06d}.mp4"
            if not vid.exists():
                issues.append(AlignmentIssue(episode_index, f"missing video {vk}"))
                continue
            n_vid = probe_video_frames(vid)
            if n_vid != n_rows:
                issues.append(AlignmentIssue(episode_index, f"{vk} frames={n_vid} != parquet rows={n_rows}"))

        expected_global += n_rows

    info_frames = int(info.get("total_frames", -1))
    info_eps = int(info.get("total_episodes", -1))
    if info_frames != expected_global:
        issues.append(AlignmentIssue(-1, f"info.total_frames={info_frames} != summed rows={expected_global}"))
    if info_eps != len(episode_indices):
        issues.append(AlignmentIssue(-1, f"info.total_episodes={info_eps} != checked={len(episode_indices)}"))

    return {
        "aligned": len(issues) == 0,
        "episodes_checked": checked,
        "total_frames": expected_global,
        "issues": [{"episode_index": i.episode_index, "issue": i.issue} for i in issues],
    }
