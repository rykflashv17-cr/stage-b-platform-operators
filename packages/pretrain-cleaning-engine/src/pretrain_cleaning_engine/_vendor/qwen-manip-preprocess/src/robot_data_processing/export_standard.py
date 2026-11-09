"""P7 StandardExport: write Dataclean-standard LeRobot dataset."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from robot_data_processing.pose_contract import finalize_pose_contract
from robot_data_processing.ego_action import (
    EGO_POSITION_KEY,
    EGO_POSITION_NAMES,
    EGO_ROTATION_KEY,
    EGO_ROTATION_NAMES,
    REFERENCE_TRANSFORM_KEY,
    build_shifted_ego_action,
    ego_action_metadata,
    is_ego_embodiment,
)
from robot_data_processing.normalize.standard_types import StandardEpisode
from robot_data_processing.frame_alignment import (
    check_export_frame_alignment,
    export_frame_indices,
    probe_video_frames,
)
from robot_data_processing.frame_removal import (
    action_row_shift,
    attach_quality_frame_removal,
    episode_frame_removal_record,
    rebuilt_action_eef,
    state_derived_action_eef_lags,
    write_episode_discard_json,
    write_frame_removal_json,
)
from robot_data_processing.phases.media import export_normalized_videos, export_passthrough_videos
from robot_data_processing.phases.video_geometry import CameraSizeRegistry, check_exported_sizes
from robot_data_processing.schema import SOURCE_TIMESTAMPS_FIELD, is_passthrough_column
from robot_data_processing.types import EpisodeResult

_EXPORT_POOL_STATE: dict[str, Any] = {}

_INTRINSIC_CAMERA_ORDER = ("camera_reference", "camera_aux_0", "camera_aux_1")
# Episode task lineage copied into ``episode_source_map.jsonl`` when an adapter sets it:
# the source directory the task label came from, and the raw label a repaired task replaced.
SOURCE_TASK_LINEAGE_KEYS = ("source_task_dir", "task_repaired_from")


def load_json_with_comments(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    # Strip // line comments (standard_info.json uses them)
    cleaned = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    return json.loads(cleaned)


def resolve_standard_info_path(start: Path | str | None) -> Path | None:
    """Walk ``start`` and its parents until ``standard_info.json`` is found.

    ``package_dir.parents[1]`` only works for packages two levels under the
    Dataclean root (e.g. ``datasets/humanoid_merged``). Nested packages such as
    ``datasets/InternDataA1_sim/lift2`` must walk further.
    """
    if start is None:
        return None
    cur = Path(start).resolve()
    if cur.is_file():
        cur = cur.parent
    for _ in range(12):
        cand = cur / "standard_info.json"
        if cand.is_file():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _feature_spec(shape: list[int], names: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"dtype": "float32", "shape": shape}
    if names:
        out["names"] = names
    return out


def _fit_feature_names(names: list[str], dim: int) -> list[str] | None:
    """Reuse template names when the live column dim matches or can be extended."""
    if dim <= 0:
        return None
    if len(names) == dim:
        return list(names)
    if 0 < dim < len(names):
        return list(names[:dim])
    if len(names) < 1:
        return None
    last = str(names[-1])
    match = re.search(r"(.*?)(\d+)(.*)$", last)
    extra: list[str] = []
    if match is not None:
        prefix, num, suffix = match.group(1), int(match.group(2)), match.group(3)
        extra = [f"{prefix}{num + i}{suffix}" for i in range(1, dim - len(names) + 1)]
    else:
        extra = [f"axis_{i}" for i in range(len(names), dim)]
    return list(names) + extra


def _merge_template_feature(actual: dict[str, Any], tmpl: dict[str, Any]) -> dict[str, Any]:
    """Keep live dtype/shape; copy ``names`` from the template when compatible."""
    out = dict(actual)
    if "names" not in tmpl:
        return out
    names = tmpl.get("names")
    if names is None:
        out["names"] = None
        return out
    if not isinstance(names, list):
        return out
    shape = out.get("shape") or []
    dim = int(shape[0]) if shape else 0
    fitted = _fit_feature_names([str(x) for x in names], dim)
    if fitted is not None:
        out["names"] = fitted
    return out


def _ordered_camera_items(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    keys = list(payload or {})
    preferred = [k for k in _INTRINSIC_CAMERA_ORDER if k in payload]
    rest = [k for k in keys if k not in _INTRINSIC_CAMERA_ORDER]
    return [(k, payload[k]) for k in preferred + rest]


def build_info_features(fields_present: set[str], camera_keys: list[str]) -> dict[str, Any]:
    features: dict[str, Any] = {}
    for name in sorted(fields_present):
        # shape inferred at write time; placeholder updated per column
        features[name] = {"dtype": "float32", "shape": [1]}
    for cam in camera_keys:
        key = cam if cam.startswith("observation.images.") else f"observation.images.{cam}"
        features[key] = _video_feature_spec(384, 384)
    for idx_name in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
        features[idx_name] = {
            "dtype": "float32" if idx_name == "timestamp" else "int64",
            "names": None,
            "shape": [1],
        }
    return features


def _video_feature_spec(
    height: int,
    width: int,
    *,
    fps: float = 30.0,
) -> dict[str, Any]:
    """LeRobot / standard_info video feature: shape [H, W, C]."""
    return {
        "dtype": "video",
        "shape": [int(height), int(width), 3],
        "names": ["height", "width", "channels"],
        "info": {
            "has_audio": False,
            "video.channels": 3,
            "video.codec": "h264",
            "video.fps": float(fps),
            "video.height": int(height),
            "video.is_depth_map": False,
            "video.pix_fmt": "yuv420p",
            "video.width": int(width),
        },
    }


def _camera_short_name(cam_name: str) -> str:
    return cam_name.split(".")[-1] if "." in cam_name else cam_name


def _apply_wrist_view_flags(
    features: dict[str, Any],
    camera_sizes: dict[str, tuple[int, int]],
    wrist_view_cameras: list[str] | None,
) -> None:
    """Annotate every exported video camera with ``camera.is_wrist_view``."""
    wrist_set = {_camera_short_name(name) for name in (wrist_view_cameras or [])}
    for cam_name in camera_sizes:
        key = cam_name if cam_name.startswith("observation.images.") else f"observation.images.{cam_name}"
        spec = features.get(key)
        if not isinstance(spec, dict):
            continue
        info = spec.setdefault("info", {})
        if not isinstance(info, dict):
            info = {}
            spec["info"] = info
        info["camera.is_wrist_view"] = _camera_short_name(cam_name) in wrist_set


def _extrinsic_column_name(key: str) -> str:
    return key if key.startswith("extrinsic.") else f"extrinsic.{key}"


def extra_info_extrinsic_column_name(key: str) -> str:
    """``extra_info.{standard extrinsic name}`` for untrusted-but-complete payloads."""
    return f"extra_info.{_extrinsic_column_name(key)}"


def _is_extra_info_extrinsic_key(name: str) -> bool:
    return str(name).startswith("extra_info.extrinsic.")


def drop_exported_extrinsics(standard: StandardEpisode) -> None:
    """Remove extrinsic payloads from an episode after P6 EEF alignment.

    Does not undo camera-frame EEF; only omits ``extrinsic.*`` from export.
    """
    standard.extrinsic = {}
    for key in [k for k in standard.fields if str(k).startswith("extrinsic.")]:
        standard.fields.pop(key, None)


def add_source_timestamps_declaration(info_extras: dict[str, Any], fields_present: set[str]) -> None:
    """Declare the raw capture-clock column in info.json (keymap / review rule AGI-04).

    Shared by the batch and streaming export paths so both write the same declaration.
    """
    if SOURCE_TIMESTAMPS_FIELD in fields_present:
        info_extras["source_timestamps"] = {
            "column": SOURCE_TIMESTAMPS_FIELD,
            "dtype": "int64",
            "values": "source capture clock, unmodified (not resampled to fps)",
            "timestamp_column": "timestamp = frame_index / fps",
            "episode_summary": "meta/episode_source_map.jsonl::audit.source_timestamps",
        }


def source_timestamps_summary(ts: np.ndarray) -> dict[str, Any]:
    """Episode summary of the source clock (``audit.source_timestamps``)."""
    ts = np.asarray(ts, dtype=np.int64).reshape(-1)
    summary: dict[str, Any] = {"first": int(ts[0]), "last": int(ts[-1])}
    dt = np.diff(ts)
    if dt.size:
        median = int(np.median(dt))
        summary.update(
            median_dt=median,
            max_dt=int(dt.max()),
            min_dt=int(dt.min()),
            # steps longer than 1.5x the median frame interval
            gaps=int(np.count_nonzero(dt > 1.5 * median)) if median > 0 else None,
        )
    return summary


def strip_extrinsic_keys_from_info_extras(info_extras: dict[str, Any] | None) -> dict[str, Any]:
    """Clear exported extrinsic key lists from camera_geometry metadata."""
    if not info_extras:
        return {}
    out = dict(info_extras)
    geom = out.get("camera_geometry")
    if isinstance(geom, dict):
        geom = dict(geom)
        geom["per_frame_extrinsic_keys"] = []
        out["camera_geometry"] = geom
    return out


def _ndarray_series_to_pa(series: np.ndarray) -> pa.Array:
    """Write (n, D) as list[float], or (n, H, W) as list[list[float]] (matrix cells)."""
    series = np.asarray(series, dtype=np.float32)
    if series.ndim == 3:
        return pa.array(series.tolist(), type=pa.list_(pa.list_(pa.float32())))
    if series.ndim == 2:
        return pa.array(series.tolist(), type=pa.list_(pa.float32()))
    if series.ndim == 1:
        return pa.array(series.reshape(-1, 1).tolist(), type=pa.list_(pa.float32()))
    raise ValueError(f"unsupported series ndim={series.ndim} shape={series.shape}")


def _extra_info_series_to_pa(series: np.ndarray) -> pa.Array:
    """Passthrough extra_info columns; preserve integer/float dtype, no clipping."""
    series = np.asarray(series)
    if series.ndim >= 4:
        series = series.reshape(series.shape[0], -1)
    if np.issubdtype(series.dtype, np.integer):
        series = series.astype(np.int64, copy=False)
        value_type: pa.DataType = pa.int64()
    elif np.issubdtype(series.dtype, np.floating):
        if series.dtype == np.float32:
            value_type = pa.float32()
        else:
            series = series.astype(np.float64, copy=False)
            value_type = pa.float64()
    else:
        series = series.astype(np.float64)
        value_type = pa.float64()
    if series.ndim == 1:
        series = series.reshape(-1, 1)
    if series.ndim == 2:
        return pa.array(series.tolist(), type=pa.list_(value_type))
    if series.ndim == 3:
        return pa.array(series.tolist(), type=pa.list_(pa.list_(value_type)))
    raise ValueError(f"unsupported extra_info ndim={series.ndim} shape={series.shape}")


def _broadcast_extrinsics(
    extrinsic: dict[str, np.ndarray],
    indices: np.ndarray,
) -> dict[str, np.ndarray]:
    """Broadcast extrinsics to kept frames as (n, 4, 4)."""
    n = int(indices.size)
    out: dict[str, np.ndarray] = {}
    for key, val in extrinsic.items():
        arr = np.asarray(val, dtype=np.float32)
        if arr.ndim == 2 and arr.shape == (4, 4):
            series = np.repeat(arr[None, :, :], n, axis=0)
        elif arr.ndim == 3 and arr.shape[-2:] == (4, 4):
            series = arr[indices]
        elif arr.ndim == 2 and arr.shape[1] == 16:
            series = arr[indices].reshape(n, 4, 4)
        else:
            flat = arr.reshape(-1)
            if flat.size != 16:
                continue
            series = np.repeat(flat.reshape(1, 4, 4), n, axis=0)
        out[_extrinsic_column_name(key)] = series.astype(np.float32)
    return out


def stash_untrusted_extrinsics_to_extra_info(standard: StandardEpisode) -> list[str]:
    """Copy complete ``T_Episode_*`` matrices to ``extra_info.{standard_name}``.

    Call when ``extrinsic_confidence`` is not trusted but the episode actually
    has parseable extrinsics. Does not write ``extrinsic.*`` features; those
    should still be dropped afterwards. Returns the extra_info keys written.
    """
    payload: dict[str, np.ndarray] = dict(standard.extrinsic or {})
    for key, val in (standard.fields or {}).items():
        if str(key).startswith("extrinsic.") and key not in payload:
            payload[str(key)] = val
    if not payload:
        return []
    extras = _broadcast_extrinsics(
        payload, np.arange(int(standard.num_frames), dtype=np.int64)
    )
    stashed: list[str] = []
    for col, series in extras.items():
        dest = extra_info_extrinsic_column_name(col)
        standard.fields[dest] = np.asarray(series, dtype=np.float32)
        stashed.append(dest)
    return stashed


def apply_untrusted_extrinsic_export(standard: StandardEpisode) -> list[str]:
    """Keep complete untrusted extrinsics under extra_info, then omit ``extrinsic.*``."""
    stashed = stash_untrusted_extrinsics_to_extra_info(standard)
    drop_exported_extrinsics(standard)
    return stashed


def field_feature_dtype(name: str, arr: np.ndarray) -> str:
    """dtype actually written to parquet (see ``_extra_info_series_to_pa``).

    Standard fields are cast to float32; ``extra_info.*`` keeps int64/float64 so the
    declared schema never forces a lossy cast (REPORT AGI-04: ns timestamps).
    """
    if np.asarray(arr).dtype == np.bool_:
        # Per-point masks such as hand_keypoints.<role>.valid (EPISODE_CAMERA_GEOMETRY §3.5.2).
        return "bool"
    if not is_passthrough_column(name):
        return "float32"
    a = np.asarray(arr)
    if np.issubdtype(a.dtype, np.integer):
        return "int64"
    if a.dtype == np.float32:
        return "float32"
    return "float64"


def _feature_spec_for_field(
    name: str,
    arr: np.ndarray | None = None,
    last_dim: int | tuple[int, ...] | None = None,
    dtype: str | None = None,
) -> dict[str, Any]:
    if _is_extra_info_extrinsic_key(name):
        return _feature_spec([4, 4])
    if arr is not None:
        a = np.asarray(arr)
        if a.ndim >= 3 and tuple(a.shape[-2:]) == (4, 4) and str(name).startswith("extra_info."):
            spec = _feature_spec([4, 4])
        else:
            spec = _feature_spec(list(field_frame_shape(a)))
        spec["dtype"] = field_feature_dtype(name, a)
        return spec
    if isinstance(last_dim, (tuple, list)):
        # Per-frame shape with >= 2 dims (e.g. [21, 3] hand keypoints).
        spec = _feature_spec([int(x) for x in last_dim])
    else:
        spec = _feature_spec([int(last_dim if last_dim is not None else 1)])
    if dtype:
        spec["dtype"] = str(dtype)
    return spec


def normalize_intrinsic_payload(intrinsic: dict[str, Any] | None) -> dict[str, Any]:
    """Episode-level intrinsic dict with standard_info short keys (e.g. camera_top.matrix)."""
    out: dict[str, Any] = {}
    for key, val in (intrinsic or {}).items():
        short = str(key)
        if short.startswith("intrinsic."):
            short = short[len("intrinsic.") :]
        arr = np.asarray(val)
        out[short] = arr.tolist()
    return out


def intrinsic_feature_specs(intrinsic: dict[str, Any] | None) -> dict[str, Any]:
    """dtype/shape specs for info.json ``intrinsic`` (same level as fps / embodiment)."""
    specs: dict[str, Any] = {}
    for key, val in normalize_intrinsic_payload(intrinsic).items():
        arr = np.asarray(val, dtype=np.float32)
        if arr.ndim >= 2:
            shape = [int(x) for x in arr.shape]
        else:
            shape = [int(arr.size)]
        specs[key] = _feature_spec(shape)
    return specs


def v2_intrinsic_feature_specs(camera_intrinsics: dict[str, Any] | None) -> dict[str, Any]:
    """Build info.json intrinsic specs for v2 episode geometry (reference/aux cameras)."""
    specs: dict[str, Any] = {}
    for cam, payload in _ordered_camera_items(camera_intrinsics or {}):
        if not isinstance(payload, dict):
            continue
        if "matrix" in payload:
            specs[f"{cam}.matrix"] = _feature_spec([3, 3])
        if "dist_coeffs" in payload:
            dist = payload["dist_coeffs"]
            n = len(dist) if isinstance(dist, list) else 5
            specs[f"{cam}.dist_coeffs"] = _feature_spec([n])
    return specs


def standard_episode_to_table(
    ep: StandardEpisode,
    *,
    keep_mask: np.ndarray | None = None,
    global_index_start: int = 0,
    task_index: int = 0,
    fps: float = 30.0,
) -> pa.Table:
    T = ep.num_frames
    if keep_mask is None:
        indices = np.arange(T, dtype=np.int64)
    else:
        indices = np.flatnonzero(np.asarray(keep_mask).reshape(-1) != 0).astype(np.int64)
    n = int(indices.size)
    # State-derived action.eef is recomputed on the kept rows when frames were dropped.
    rebuilt = rebuilt_action_eef(ep, indices)
    arrays: dict[str, pa.Array] = {}
    for name, arr in ep.fields.items():
        if name in (EGO_POSITION_KEY, EGO_ROTATION_KEY):
            # Recomputed after keep_mask from remaining T_cam, never sliced as-is.
            continue
        if str(name).startswith("extra_info."):
            sub = np.asarray(arr)[indices]
            arrays[name] = _extra_info_series_to_pa(sub)
            continue
        if np.asarray(arr).dtype == np.bool_:
            sub = np.asarray(arr)[indices]
            if sub.ndim == 1:
                sub = sub.reshape(-1, 1)
            arrays[name] = pa.array(sub.tolist(), type=pa.list_(pa.bool_()))
            continue
        sub = rebuilt[name] if name in rebuilt else np.asarray(arr, dtype=np.float32)[indices]
        if sub.ndim == 1:
            sub = sub.reshape(-1, 1)
        arrays[name] = _ndarray_series_to_pa(sub)

    extras = _broadcast_extrinsics(ep.extrinsic, indices)
    for name, series in extras.items():
        arrays[name] = _ndarray_series_to_pa(series)
    if is_ego_embodiment(ep.meta.get("embodiment")):
        camera = extras.get(REFERENCE_TRANSFORM_KEY)
        if camera is not None:
            position, rotation = build_shifted_ego_action(camera)
            arrays[EGO_POSITION_KEY] = _ndarray_series_to_pa(position.astype(np.float32))
            arrays[EGO_ROTATION_KEY] = _ndarray_series_to_pa(rotation.astype(np.float32))

    frame_index = np.arange(n, dtype=np.int64)
    arrays["frame_index"] = pa.array(frame_index)
    arrays["episode_index"] = pa.array(np.full(n, ep.episode_index, dtype=np.int64))
    arrays["index"] = pa.array(np.arange(global_index_start, global_index_start + n, dtype=np.int64))
    arrays["task_index"] = pa.array(np.full(n, task_index, dtype=np.int64))
    arrays["timestamp"] = pa.array((frame_index / max(fps, 1e-6)).astype(np.float32))
    return pa.table(arrays)


def write_standard_episode_parquet(
    output_root: Path,
    ep: StandardEpisode,
    *,
    keep_mask: np.ndarray | None = None,
    global_index_start: int = 0,
    fps: float = 30.0,
    task_index: int = 0,
) -> tuple[Path, int]:
    table = standard_episode_to_table(
        ep,
        keep_mask=keep_mask,
        global_index_start=global_index_start,
        fps=fps,
        task_index=task_index,
    )
    # Episode-level constants (same tier as fps / embodiment): one payload per parquet file.
    meta: dict[bytes, bytes] = dict(table.schema.metadata or {})
    meta[b"fps"] = str(float(fps)).encode("utf-8")
    embodiment = ep.meta.get("embodiment")
    if embodiment is not None:
        meta[b"embodiment"] = str(embodiment).encode("utf-8")
    extra_nf = ep.meta.get("extra_info_non_frame")
    if extra_nf:
        meta[b"extra_info_non_frame"] = json.dumps(
            extra_nf, ensure_ascii=False, default=str
        ).encode("utf-8")
    intr = normalize_intrinsic_payload(ep.intrinsic)
    if intr:
        meta[b"intrinsic"] = json.dumps(intr, ensure_ascii=False).encode("utf-8")
    table = table.replace_schema_metadata(meta)

    chunk = ep.episode_index // 1000
    out_path = output_root / "data" / f"chunk-{chunk:03d}" / f"episode_{ep.episode_index:06d}.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_path)
    return out_path, table.num_rows


def write_parameters(
    output_root: Path,
    ep: StandardEpisode,
) -> None:
    chunk = ep.episode_index // 1000
    out_dir = output_root / "parameters" / f"chunk-{chunk:03d}" / f"episode_{ep.episode_index:06d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "intrinsic": normalize_intrinsic_payload(ep.intrinsic),
        "extrinsic": {},
    }
    for k, v in ep.extrinsic.items():
        arr = np.asarray(v)
        if arr.ndim == 3 and arr.shape[-2:] == (4, 4):
            arr = arr[0]
        payload["extrinsic"][k] = arr.tolist()
    if ep.meta.get("source_episode_index") is not None:
        payload["source_episode_index"] = ep.meta.get("source_episode_index")
    if ep.meta.get("calibration_bundle"):
        payload["source_calibration"] = ep.meta.get("calibration_bundle")
    with (out_dir / "calibration_standard.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _kept_length(ep: StandardEpisode, result: EpisodeResult | None) -> int | None:
    if result is not None and result.discard:
        return None
    if result is not None and result.kept_frames == 0:
        return None
    keep_mask = None if result is None else result.step_validity_mask
    if keep_mask is not None:
        n = int(np.asarray(keep_mask).sum())
        return n if n > 0 else None
    return int(ep.num_frames)


def _default_export_workers(requested: int | None) -> int:
    if requested is not None and requested > 0:
        return int(requested)
    return min(16, os.cpu_count() or 4)


def _default_probe_workers(n_jobs: int) -> int:
    """ThreadPool size for pre-export ffprobe (env EXPORT_PROBE_WORKERS)."""
    n_jobs = max(1, int(n_jobs))
    env = os.environ.get("EXPORT_PROBE_WORKERS", "").strip()
    if env:
        return max(1, min(int(env), n_jobs))
    return max(1, min(64, (os.cpu_count() or 8), n_jobs))


def _prewarm_v3_alignment_caches(roots: list[Path]) -> None:
    """Load info + episode_row_map once per root before threaded probes."""
    from robot_data_processing.v3_layout import episode_row_map, is_v3_layout, load_info

    seen: set[str] = set()
    for root in roots:
        root = Path(root)
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if not is_v3_layout(root):
            continue
        load_info(root)
        episode_row_map(root)


def _export_align_job(
    job: tuple[
        int,
        int,
        dict[str, np.ndarray],
        dict[str, np.ndarray] | None,
        np.ndarray | None,
        str,
        int,
        dict[str, str],
        float,
        bool,
    ],
) -> tuple[int, bool]:
    """Worker: run check_export_frame_alignment; return (order, ok)."""
    (
        order,
        num_frames,
        fields,
        extrinsic,
        keep_mask,
        dataset_root,
        source_episode_index,
        camera_map,
        fps,
        check_videos,
    ) = job
    align = check_export_frame_alignment(
        num_frames=int(num_frames),
        fields=fields,
        extrinsic=extrinsic,
        keep_mask=keep_mask,
        dataset_root=Path(dataset_root),
        source_episode_index=int(source_episode_index),
        camera_map=camera_map,
        fps=float(fps),
        check_videos=bool(check_videos),
    )
    return int(order), bool(align.ok)


def _init_export_pool(state: dict[str, Any]) -> None:
    _EXPORT_POOL_STATE.clear()
    _EXPORT_POOL_STATE.update(state)
    pkg = state.get("adapter_package_dir")
    if pkg:
        from robot_data_processing.adapter_hooks import install_adapter_hooks

        install_adapter_hooks(pkg)


def _export_one_task(
    task: tuple[int, StandardEpisode, np.ndarray | None, int, int],
) -> tuple[dict[str, Any], dict[str, tuple[int, int]], set[str]]:
    """Worker: write one episode parquet + parameters + videos."""
    out_ep_index, ep, keep_mask, global_index_start, task_index = task
    # Ensure episode_index matches output id (may already be remapped by orchestrator)
    ep.episode_index = int(out_ep_index)
    output_root = Path(_EXPORT_POOL_STATE["output_root"])
    camera_map: dict[str, str] = _EXPORT_POOL_STATE["camera_map"]
    fps = float(_EXPORT_POOL_STATE["fps"])
    target_height = int(_EXPORT_POOL_STATE["target_height"])
    target_short_side = _EXPORT_POOL_STATE.get("target_short_side")
    max_keyframe_interval = int(_EXPORT_POOL_STATE["max_keyframe_interval"])
    normalize_videos = bool(_EXPORT_POOL_STATE["normalize_videos"])
    parallel_videos = bool(_EXPORT_POOL_STATE.get("parallel_videos", True))
    skip_parameters = bool(_EXPORT_POOL_STATE.get("skip_parameters", False))
    default_source_root = Path(_EXPORT_POOL_STATE["source_root"])

    try:
        return _export_one_task_files(
            output_root, ep, keep_mask, global_index_start, task_index, fps, camera_map,
            target_height, target_short_side, max_keyframe_interval, normalize_videos,
            parallel_videos, skip_parameters, default_source_root,
        )
    except Exception:
        # A half-written episode (parquet written, a video failed) would be picked up by the
        # shard finalize and fail the whole shard; drop its files so only this episode is lost.
        _remove_episode_files(output_root, int(ep.episode_index))
        raise


def _remove_episode_files(output_root: Path, episode_index: int) -> None:
    name = f"episode_{episode_index:06d}"
    chunk = f"chunk-{episode_index // 1000:03d}"
    for sub in ("data", "videos"):
        for path in (Path(output_root) / sub / chunk).glob(f"**/{name}.*"):
            path.unlink(missing_ok=True)
    shutil.rmtree(Path(output_root) / "parameters" / chunk / name, ignore_errors=True)


def _export_one_task_files(
    output_root: Path,
    ep: StandardEpisode,
    keep_mask: np.ndarray | None,
    global_index_start: int,
    task_index: int,
    fps: float,
    camera_map: dict[str, str],
    target_height: int,
    target_short_side: Any,
    max_keyframe_interval: int,
    normalize_videos: bool,
    parallel_videos: bool,
    skip_parameters: bool,
    default_source_root: Path,
) -> tuple[dict[str, Any], dict[str, tuple[int, int]], set[str]]:
    path, n = write_standard_episode_parquet(
        output_root,
        ep,
        keep_mask=keep_mask,
        global_index_start=global_index_start,
        fps=fps,
        task_index=task_index,
    )
    del path
    if not skip_parameters:
        write_parameters(output_root, ep)
    camera_sizes: dict[str, tuple[int, int]] = {}
    keep_indices = export_frame_indices(ep.num_frames, keep_mask)
    if camera_map:
        src_root = (
            default_source_root
            if ep.meta.get("dataset_root") is None
            else Path(ep.meta["dataset_root"])
        )
        src_ep = int(ep.meta.get("source_episode_index", ep.episode_index))
        if normalize_videos:
            camera_sizes = export_normalized_videos(
                src_root,
                output_root,
                ep.episode_index,
                camera_map,
                target_height=target_height,
                target_short_side=(
                    int(target_short_side) if target_short_side is not None else None
                ),
                max_keyframe_interval=max_keyframe_interval,
                source_episode_index=src_ep,
                parallel_videos=parallel_videos,
                keep_indices=keep_indices,
                fps=fps,
                num_frames=int(ep.num_frames),
                resolution_plan=_EXPORT_POOL_STATE.get("video_resolution_plan"),
            )
        else:
            camera_sizes = export_passthrough_videos(
                src_root,
                output_root,
                ep.episode_index,
                camera_map,
                source_episode_index=src_ep,
                keep_indices=keep_indices,
                fps=fps,
                num_frames=int(ep.num_frames),
            )
        check_exported_sizes(
            ep.meta.get("camera_intrinsics"), camera_sizes, episode_index=int(ep.episode_index)
        )
        out_chunk = ep.episode_index // 1000
        for src_key, std_name in camera_map.items():
            out_video = (
                output_root
                / "videos"
                / f"chunk-{out_chunk:03d}"
                / f"observation.images.{std_name}"
                / f"episode_{ep.episode_index:06d}.mp4"
            )
            if not out_video.exists():
                raise FileNotFoundError(f"Missing exported video: {out_video}")
            n_vid = probe_video_frames(out_video)
            if n_vid != n:
                raise ValueError(
                    f"exported video frame mismatch observation.images.{std_name}: "
                    f"video={n_vid} parquet={n}"
                )
    row = {
        "episode_index": ep.episode_index,
        "tasks": list(ep.meta.get("tasks") or []),
        "length": n,
        "source_episode_index": ep.meta.get("source_episode_index", ep.episode_index),
        "part": ep.meta.get("part"),
        "task": ep.meta.get("task"),
        "dataset_root": ep.meta.get("dataset_root"),
        "task_index": int(task_index),
    }
    for key in SOURCE_TASK_LINEAGE_KEYS:
        if ep.meta.get(key):
            row[key] = str(ep.meta[key])
    if ep.meta.get("camera_intrinsics"):
        row["camera_intrinsics"] = ep.meta["camera_intrinsics"]
    audit = episode_audit_record(ep, keep_indices)
    if audit:
        row["audit"] = audit
    frame_removal = episode_frame_removal_record(ep, keep_indices)
    if frame_removal:
        row["frame_removal"] = frame_removal
    field_names = set(ep.fields.keys())
    field_names.discard(EGO_POSITION_KEY)
    field_names.discard(EGO_ROTATION_KEY)
    field_names.update(_extrinsic_column_name(k) for k in ep.extrinsic.keys())
    if is_ego_embodiment(ep.meta.get("embodiment")):
        ext_names = {_extrinsic_column_name(k) for k in ep.extrinsic.keys()}
        if REFERENCE_TRANSFORM_KEY in ext_names and n >= 2:
            field_names.update((EGO_POSITION_KEY, EGO_ROTATION_KEY))
    return row, camera_sizes, field_names


def _json_default(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return str(value)


def episode_audit_record(ep: StandardEpisode, keep_indices: np.ndarray) -> dict[str, Any]:
    """Per-episode provenance written to ``meta/episode_source_map.jsonl::audit``.

    Records the §6.3 action tail padding (rows and fields, in exported row
    indices), the source frame indices kept, and adapter-provided audit entries
    (``meta["audit"]``: optional-field availability, non-frame sidecars, source
    timestamp provenance, ...).

    Stage B (identity input): the Stage-A row stays under ``stage_a`` as lineage; its
    frame-indexed entries (source timestamps, action tail) are recomputed on the kept
    frames at the top level and dropped from ``stage_a.audit``.
    """
    out: dict[str, Any] = dict(ep.meta.get("audit") or {})
    kept = np.asarray(keep_indices, dtype=np.int64).reshape(-1)
    num_frames = int(ep.num_frames)
    stage_a = out.get("stage_a")
    if isinstance(stage_a, dict) and isinstance(stage_a.get("audit"), dict):
        stale = ("source_timestamps", "action_eef_fill_tail", "extra_info_fields")
        stage_a_audit = {k: v for k, v in stage_a["audit"].items() if k not in stale}
        out["stage_a"] = {**stage_a, "audit": stage_a_audit} if stage_a_audit else {
            k: v for k, v in stage_a.items() if k != "audit"
        }
    ts = ep.fields.get(SOURCE_TIMESTAMPS_FIELD)
    if ts is not None and kept.size and "source_timestamps" not in out:
        out["source_timestamps"] = source_timestamps_summary(np.asarray(ts)[kept])
    shift = int(ep.meta.get("action_row_shift") or 0)
    if shift > 0 and kept.size:
        padded_src = np.arange(num_frames - min(shift, num_frames), num_frames)
        padded_out = np.flatnonzero(np.isin(kept, padded_src)).astype(int).tolist()
        last_kept = int(kept[-1])
        out["action_tail"] = {
            "tail_policy": ep.meta.get("action_tail_policy"),
            "action_row_shift": shift,
            "padded_rows": padded_out,
            "padded_fields": list(ep.meta.get("action_padded_fields") or []),
            # Frames trimmed after the last kept row: the last kept action is the
            # real future target (source frame last_kept+shift), not a padded copy.
            "last_row_target_source_frame": (
                None if padded_out and padded_out[-1] == kept.size - 1 else last_kept + shift
            ),
        }
    fill_lags = state_derived_action_eef_lags(ep.meta)
    if fill_lags and kept.size:
        # action.eef[t] = state.eef[min(t+lag, L-1)] over the exported rows (recomputed
        # there when frames were dropped): the last ``lag`` rows repeat state[L-1].
        lag = max(fill_lags.values())
        n_kept = int(kept.size)
        if shift > 0:
            # A later P4 shift moved the padded region ``shift`` rows up (no rebuild).
            padded_src = np.arange(max(0, num_frames - lag - shift), num_frames)
            padded_rows = np.flatnonzero(np.isin(kept, padded_src)).astype(int).tolist()
        else:
            padded_rows = list(range(max(0, n_kept - lag), n_kept))
        out["action_eef_fill_tail"] = {
            "tail_policy": "repeat_last_state_eef",
            "fill_lags": fill_lags,
            "padded_rows": padded_rows,
            **({"rebuilt_on_kept_frames": True} if n_kept != num_frames and not action_row_shift(ep.meta) else {}),
            "padded_fields": sorted(
                f"action.eef.{role}.{part}" for role in fill_lags for part in ("position", "rotation_6d")
            ),
        }
    if ep.meta.get("extra_info_non_frame"):
        # Episode sidecar; also kept as parquet schema metadata (REPORT AGI-03).
        out["extra_info_non_frame"] = ep.meta["extra_info_non_frame"]
    optional = sorted(name for name in ep.fields if is_passthrough_column(name))
    if optional:
        # Per-episode availability of optional per-frame fields (REPORT AGI-02).
        out["extra_info_fields"] = optional
    if kept.size and (kept.size != num_frames or int(kept[0]) != 0):
        out["kept_source_frames"] = {"first": int(kept[0]), "last": int(kept[-1]), "count": int(kept.size)}
    return out


def field_frame_shape(arr: np.ndarray) -> tuple[int, ...]:
    """Per-frame shape of a (T, ...) field: ``(1,)`` for (T,), ``(D,)`` for (T, D),
    the full trailing shape for (T, A, B) such as ``(21, 3)`` hand keypoints."""
    a = np.asarray(arr)
    if a.ndim <= 1:
        return (1,)
    return tuple(int(x) for x in a.shape[1:])


def episode_feature_meta(
    ep: StandardEpisode,
) -> tuple[dict[str, int | tuple[int, ...]], list[str], dict[str, Any]]:
    """Field dims (int for 1-D frames, shape tuple for >= 2-D), extrinsic keys, and
    intrinsic payload for info.json."""
    field_dims: dict[str, int | tuple[int, ...]] = {}
    for name, arr in ep.fields.items():
        if name in (EGO_POSITION_KEY, EGO_ROTATION_KEY):
            continue
        shape = field_frame_shape(arr)
        field_dims[name] = shape[0] if len(shape) == 1 else shape
    extrinsic_keys = list(ep.extrinsic.keys())
    if is_ego_embodiment(ep.meta.get("embodiment")):
        ext_names = {_extrinsic_column_name(k) for k in extrinsic_keys}
        if REFERENCE_TRANSFORM_KEY in ext_names:
            field_dims[EGO_POSITION_KEY] = 3
            field_dims[EGO_ROTATION_KEY] = 6
    if ep.meta.get("camera_intrinsics"):
        intrinsic = dict(ep.meta["camera_intrinsics"])
    else:
        intrinsic = dict(ep.intrinsic or {})
    return field_dims, extrinsic_keys, intrinsic


def episode_field_dtypes(ep: StandardEpisode) -> dict[str, str]:
    """Written parquet dtype per field (only non-float32 passthrough columns differ)."""
    return {
        name: field_feature_dtype(name, arr)
        for name, arr in ep.fields.items()
        if name not in (EGO_POSITION_KEY, EGO_ROTATION_KEY)
    }


class FieldSchemaRegistry:
    """Per-field shape/dtype over all exported episodes (REPORT AGI-02).

    The info.json schema used to come from the first episode, so optional fields
    missing there vanished and a different dim/dtype elsewhere was declared wrongly.
    A field may be absent from some episodes (recorded as availability, never
    zero-filled), but one field must have one dim and one dtype.
    """

    def __init__(self) -> None:
        self.total = 0
        self.dims: dict[str, Counter] = {}
        self.dtypes: dict[str, Counter] = {}

    def add(self, field_dims: dict[str, int] | None, field_dtypes: dict[str, str] | None = None) -> None:
        self.total += 1
        for name, dim in (field_dims or {}).items():
            key = tuple(int(x) for x in dim) if isinstance(dim, (tuple, list)) else int(dim)
            self.dims.setdefault(str(name), Counter())[key] += 1
            dtype = str((field_dtypes or {}).get(name, "float32"))
            self.dtypes.setdefault(str(name), Counter())[dtype] += 1

    def resolve(self) -> tuple[dict[str, int], dict[str, str]]:
        conflicts = {
            name: {"dims": dict(self.dims[name]), "dtypes": dict(self.dtypes[name])}
            for name in self.dims
            if len(self.dims[name]) > 1 or len(self.dtypes[name]) > 1
        }
        if conflicts:
            raise RuntimeError(
                "per-episode field schema differs (dim or dtype) — a single info.json feature "
                f"would misdescribe some episodes; split the dataset or rename the field: {conflicts}"
            )
        dims = {name: next(iter(c)) for name, c in self.dims.items()}
        dtypes = {name: next(iter(c)) for name, c in self.dtypes.items()}
        return dims, dtypes

    def availability(self) -> dict[str, dict[str, int]]:
        """Fields present in only some episodes: ``{name: {episodes, total_episodes}}``."""
        out: dict[str, dict[str, int]] = {}
        for name, counter in sorted(self.dims.items()):
            n = int(sum(counter.values()))
            if n < self.total:
                out[name] = {"episodes": n, "total_episodes": int(self.total)}
        return out


def select_export_keep_mask(
    ep: StandardEpisode,
    result: EpisodeResult | None,
    *,
    keep_all_frames: bool,
    ignore_quality_discard: bool,
) -> tuple[np.ndarray | None, int] | None:
    """Return ``(keep_mask, n)`` or ``None`` if this episode should not be exported."""
    if (
        result is not None
        and result.discard
        and not ignore_quality_discard
        and not keep_all_frames
    ):
        return None
    if keep_all_frames:
        n = int(ep.num_frames)
        if n <= 0:
            return None
        return None, n
    n = _kept_length(ep, result)
    if n is None:
        if ignore_quality_discard and result is not None and result.discard:
            n = int(ep.num_frames)
            keep_mask = result.step_validity_mask
            if keep_mask is not None:
                n = int(np.asarray(keep_mask).reshape(-1).astype(bool).sum())
            if n <= 0:
                return None
            return keep_mask, n
        return None
    keep_mask = result.step_validity_mask if result is not None else None
    return keep_mask, n


def _build_info_dict(
    *,
    episodes_out: list[dict[str, Any]],
    kept: list[tuple[StandardEpisode, np.ndarray | None, int]] | None,
    fields_present: set[str],
    camera_sizes: dict[str, tuple[int, int]],
    camera_map: dict[str, str],
    fps: float,
    embodiment: str,
    state_action_delay: int,
    tasks_out: list[dict[str, Any]],
    total_frames: int,
    skip_parameters: bool,
    info_extras: dict[str, Any] | None,
    wrist_view_cameras: list[str] | None,
    standard_info_path: Path | None,
    field_dims: dict[str, int] | None = None,
    extrinsic_keys: list[str] | None = None,
    info_intrinsic: dict[str, Any] | None = None,
    max_episode_index: int | None = None,
    field_dtypes: dict[str, str] | None = None,
) -> dict[str, Any]:
    camera_list = sorted(camera_sizes.keys()) or sorted(camera_map.values())
    features = build_info_features(fields_present, camera_list)
    for cam_name, (h, w) in camera_sizes.items():
        key = cam_name if cam_name.startswith("observation.images.") else f"observation.images.{cam_name}"
        features[key] = _video_feature_spec(h, w, fps=fps)
    _apply_wrist_view_flags(features, camera_sizes, wrist_view_cameras)
    resolved_intrinsic: dict[str, Any] = dict(info_intrinsic or {})
    if kept:
        ep0 = kept[0][0]
        # Union over all episodes: optional fields may be absent from the first one.
        described: set[str] = set()
        for ep_k, _, _ in kept:
            for name, arr in ep_k.fields.items():
                if name in (EGO_POSITION_KEY, EGO_ROTATION_KEY) or name in described:
                    continue
                features[name] = _feature_spec_for_field(name, arr=arr)
                described.add(name)
        for key in ep0.extrinsic.keys():
            features[_extrinsic_column_name(key)] = _feature_spec([4, 4])
        if skip_parameters and ep0.meta.get("camera_intrinsics"):
            resolved_intrinsic = v2_intrinsic_feature_specs(ep0.meta["camera_intrinsics"])
        else:
            resolved_intrinsic = intrinsic_feature_specs(ep0.intrinsic)
    else:
        for name, dim in (field_dims or {}).items():
            features[name] = _feature_spec_for_field(
                name,
                last_dim=tuple(dim) if isinstance(dim, (tuple, list)) else int(dim),
                dtype=(field_dtypes or {}).get(name),
            )
        for key in extrinsic_keys or []:
            features[_extrinsic_column_name(str(key))] = _feature_spec([4, 4])

    if max_episode_index is None:
        if kept:
            max_episode_index = max((ep.episode_index for ep, _, _ in kept), default=0)
        elif episodes_out:
            max_episode_index = max((int(row["episode_index"]) for row in episodes_out), default=0)
        else:
            max_episode_index = 0

    info: dict[str, Any] = {
        "codebase_version": "v2.1",
        "robot_type": embodiment,
        "embodiment": embodiment,
        "fps": fps,
        "state_action_delay": state_action_delay,
        "camera_view_direction": "ego" if is_ego_embodiment(embodiment) else "arm_side",
        "total_episodes": len(episodes_out),
        "total_frames": total_frames,
        "total_tasks": max(len(tasks_out), 1),
        "total_videos": len(episodes_out) * max(len(camera_list), 1),
        "total_chunks": max(1, math.ceil((int(max_episode_index) + 1) / 1000)),
        "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": features,
    }
    if info_extras:
        for key, val in info_extras.items():
            if val is not None:
                info[key] = val
    if resolved_intrinsic:
        info["intrinsic"] = resolved_intrinsic
    if standard_info_path and standard_info_path.exists():
        try:
            template = load_json_with_comments(standard_info_path)
            tmpl_feats = template.get("features") or {}
            for k, v in tmpl_feats.items():
                if k.startswith("observation.images."):
                    continue
                if k in features and isinstance(v, dict) and isinstance(features[k], dict):
                    features[k] = _merge_template_feature(features[k], v)
            nested_ext = tmpl_feats.get("extrinsic") if isinstance(tmpl_feats.get("extrinsic"), dict) else None
            if nested_ext:
                for ek, ev in nested_ext.items():
                    col = _extrinsic_column_name(ek)
                    if col in features and isinstance(ev, dict):
                        features[col] = {
                            "dtype": ev.get("dtype", "float32"),
                            "shape": list(ev.get("shape") or [4, 4]),
                            "names": ev.get("names"),
                        }
            for k in list(features):
                if k.startswith("observation.images.") and k not in {
                    (n if n.startswith("observation.images.") else f"observation.images.{n}")
                    for n in camera_sizes
                }:
                    del features[k]
            for cam_name, (h, w) in camera_sizes.items():
                key = (
                    cam_name if cam_name.startswith("observation.images.") else f"observation.images.{cam_name}"
                )
                features[key] = _video_feature_spec(h, w, fps=fps)
            _apply_wrist_view_flags(features, camera_sizes, wrist_view_cameras)
            info["features"] = features
            tmpl_intr = template.get("intrinsic")
            if isinstance(tmpl_intr, dict) and not skip_parameters:
                merged = dict(resolved_intrinsic)
                for ik, iv in tmpl_intr.items():
                    if not isinstance(iv, dict):
                        continue
                    if ik in merged:
                        merged[ik] = {
                            "dtype": iv.get("dtype", merged[ik].get("dtype", "float32")),
                            "shape": list(iv.get("shape") or merged[ik].get("shape")),
                            "names": iv.get("names"),
                        }
                    else:
                        merged[ik] = {
                            "dtype": iv.get("dtype", "float32"),
                            "shape": list(iv.get("shape") or [1]),
                            "names": iv.get("names"),
                        }
                info["intrinsic"] = merged
            for key in ("prompt_template", "horizon"):
                if key in template:
                    info[key] = template[key]
        except Exception:
            pass
    info["total_tasks"] = max(len(tasks_out), 1)
    episode_frame = None
    if kept:
        episode_frame = kept[0][0].meta.get("episode_frame_definition")
    finalize_pose_contract(info, episode_frame=episode_frame)
    _attach_ego_action_info(info, embodiment=embodiment)
    return info


def _attach_ego_action_info(info: dict[str, Any], *, embodiment: str) -> None:
    """Declare action.ego.* only for ego datasets that actually exported the columns."""
    features = info.get("features")
    if not isinstance(features, dict):
        return
    has_fields = EGO_POSITION_KEY in features and EGO_ROTATION_KEY in features
    if not has_fields or not is_ego_embodiment(embodiment):
        features.pop(EGO_POSITION_KEY, None)
        features.pop(EGO_ROTATION_KEY, None)
        info.pop("ego_action", None)
        return
    geometry = info.get("camera_geometry")
    if info.get("camera_calibration") != "available" or not isinstance(geometry, dict):
        # §8.4: ego extension requires valid reference-camera extrinsics.
        features.pop(EGO_POSITION_KEY, None)
        features.pop(EGO_ROTATION_KEY, None)
        info.pop("ego_action", None)
        return
    episode_frame = geometry.get("episode_frame")
    if not episode_frame:
        raise ValueError("Ego action requires camera_geometry.episode_frame.")
    if info.get("camera_view_direction") != "ego":
        # The loader rejects ego action from a non-ego source (REPORT_egodex R4).
        raise ValueError(
            f"Ego action requires camera_view_direction='ego', got {info.get('camera_view_direction')!r}."
        )
    features[EGO_POSITION_KEY] = _feature_spec([3], names=list(EGO_POSITION_NAMES))
    features[EGO_ROTATION_KEY] = _feature_spec([6], names=list(EGO_ROTATION_NAMES))
    info["ego_action"] = ego_action_metadata(str(episode_frame))
    info["features"] = features


def _write_v21_meta_files(
    dataset_root: Path,
    *,
    info: dict[str, Any],
    episodes_out: list[dict[str, Any]],
    tasks_out: list[dict[str, Any]],
    embodiment: str,
    dropped_episodes: list[dict[str, Any]] | None = None,
) -> None:
    meta_dir = dataset_root / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    with (meta_dir / "info.json").open("w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with (meta_dir / "episodes.jsonl").open("w", encoding="utf-8") as f:
        for row in episodes_out:
            ep_row: dict[str, Any] = {
                "episode_index": int(row["episode_index"]),
                "tasks": list(row.get("tasks") or []),
                "length": int(row["length"]),
            }
            if row.get("camera_intrinsics"):
                ep_row["camera_intrinsics"] = row["camera_intrinsics"]
            f.write(json.dumps(ep_row, ensure_ascii=False) + "\n")
    with (meta_dir / "tasks.jsonl").open("w", encoding="utf-8") as f:
        rows = tasks_out or [{"task_index": 0, "task": embodiment}]
        for row in rows:
            f.write(
                json.dumps(
                    {"task_index": int(row["task_index"]), "task": str(row["task"])},
                    ensure_ascii=False,
                )
                + "\n"
            )
    with (meta_dir / "episode_source_map.jsonl").open("w", encoding="utf-8") as f:
        for row in episodes_out:
            f.write(
                json.dumps(
                    {
                        "episode_index": row["episode_index"],
                        "source_episode_index": row.get("source_episode_index"),
                        "part": row.get("part"),
                        "task": row.get("task"),
                        "task_index": row.get("task_index"),
                        "dataset_root": row.get("dataset_root"),
                        **{k: row[k] for k in SOURCE_TASK_LINEAGE_KEYS if row.get(k)},
                        **({"audit": row["audit"]} if row.get("audit") else {}),
                    },
                    ensure_ascii=False,
                    default=_json_default,
                )
                + "\n"
            )
    if info.get("frame_removal"):
        write_frame_removal_json(
            meta_dir,
            (
                (row["episode_index"], row.get("source_episode_index"), row.get("frame_removal"))
                for row in episodes_out
            ),
            total_episodes=len(episodes_out),
        )
    if info.get("episode_discard"):
        write_episode_discard_json(
            meta_dir,
            dropped_episodes or [],
            input_episodes=len(episodes_out) + len(dropped_episodes or []),
        )


def prepare_standard_export_roots(
    output_root: Path,
    *,
    lerobot_version: str,
    v30_aggregate_media: bool | None,
) -> tuple[str, bool, bool, Path, Path | None]:
    """Resolve layout and create the v2.1 write root (cache for pseudo/aggregated v3)."""
    version = str(lerobot_version or "aggregated_v3").lower()
    if version in ("v3.0", "v3"):
        version = "aggregated_v3" if v30_aggregate_media is not False else "pseudo_v3"
    if version == "pseudo_v3":
        aggregate_media = False
    elif version == "aggregated_v3":
        aggregate_media = True if v30_aggregate_media is None else bool(v30_aggregate_media)
    elif version == "both":
        aggregate_media = True if v30_aggregate_media is None else bool(v30_aggregate_media)
    elif version == "v2.1":
        aggregate_media = False
    else:
        raise ValueError(f"Unsupported lerobot_version/layout: {lerobot_version!r}")

    write_v30 = version in ("pseudo_v3", "aggregated_v3", "both")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    cache_root: Path | None = None
    if version in ("pseudo_v3", "aggregated_v3"):
        cache_root = output_root / ".lerobot_export_cache"
        if cache_root.exists():
            shutil.rmtree(cache_root)
        cache_root.mkdir(parents=True, exist_ok=True)
        dataset_root = cache_root
    else:
        dataset_root = output_root
    return version, write_v30, aggregate_media, dataset_root, cache_root


def finalize_standard_export(
    *,
    output_root: Path,
    dataset_root: Path,
    cache_root: Path | None,
    version: str,
    write_v30: bool,
    aggregate_media: bool,
    episodes_out: list[dict[str, Any]],
    fields_present: set[str],
    camera_sizes: dict[str, tuple[int, int]],
    camera_map: dict[str, str],
    fps: float,
    embodiment: str,
    state_action_delay: int,
    tasks_out: list[dict[str, Any]],
    total_frames: int,
    skip_parameters: bool,
    info_extras: dict[str, Any] | None,
    wrist_view_cameras: list[str] | None,
    standard_info_path: Path | None,
    skipped: int,
    skipped_frame_alignment: int,
    export_workers: int,
    parallel_videos: bool,
    renumber_episodes: bool,
    keep_all_frames: bool,
    v30_output_root: Path | None = None,
    v30_data_file_size_in_mb: int = 100,
    v30_video_file_size_in_mb: int = 200,
    field_dims: dict[str, int] | None = None,
    extrinsic_keys: list[str] | None = None,
    info_intrinsic: dict[str, Any] | None = None,
    field_dtypes: dict[str, str] | None = None,
    kept: list[tuple[StandardEpisode, np.ndarray | None, int]] | None = None,
    dropped_episodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write v2.1 meta, convert to LeRobot layout, return the export summary.

    ``dropped_episodes``: whole input episodes left out (``episode_discard_record``); without
    it ``info.episode_discard`` is not declared.
    """
    if info_intrinsic is None and skip_parameters:
        for row in episodes_out:
            if row.get("camera_intrinsics"):
                info_intrinsic = v2_intrinsic_feature_specs(row["camera_intrinsics"])
                break
    camera_list = sorted(camera_sizes.keys()) or sorted(camera_map.values())
    info = _build_info_dict(
        episodes_out=episodes_out,
        kept=kept,
        fields_present=fields_present,
        camera_sizes=camera_sizes,
        camera_map=camera_map,
        fps=fps,
        embodiment=embodiment,
        state_action_delay=state_action_delay,
        tasks_out=tasks_out,
        total_frames=total_frames,
        skip_parameters=skip_parameters,
        info_extras=info_extras,
        wrist_view_cameras=wrist_view_cameras,
        standard_info_path=standard_info_path,
        field_dims=field_dims,
        extrinsic_keys=extrinsic_keys,
        info_intrinsic=info_intrinsic,
        field_dtypes=field_dtypes,
    )
    if dropped_episodes is None:
        info.pop("episode_discard", None)
    _write_v21_meta_files(
        dataset_root,
        info=info,
        episodes_out=episodes_out,
        tasks_out=tasks_out,
        embodiment=embodiment,
        dropped_episodes=dropped_episodes,
    )
    summary: dict[str, Any] = {
        "output_root": str(output_root),
        "dataset_root": str(dataset_root),
        "lerobot_version": version,
        "total_episodes": len(episodes_out),
        "total_frames": total_frames,
        "skipped_episodes": skipped,
        "skipped_frame_alignment": skipped_frame_alignment,
        "cameras": camera_list,
        "state_action_delay": state_action_delay,
        "export_workers": export_workers,
        "parallel_videos": parallel_videos,
        "renumber_episodes": renumber_episodes,
        "keep_all_frames": keep_all_frames,
        "codebase_version": "v2.1" if version == "v2.1" else None,
    }
    if write_v30:
        from robot_data_processing.export_lerobot_v30 import (
            install_pseudo_v30_into_root,
            install_v30_dataset,
            install_v30_dataset_into_root,
        )

        if version == "pseudo_v3":
            assert cache_root is not None
            v30_summary = install_pseudo_v30_into_root(cache_root, output_root)
            if cache_root.exists():
                shutil.rmtree(cache_root)
            summary["codebase_version"] = v30_summary.get("codebase_version")
            summary["layout"] = "pseudo_v3"
        elif version == "aggregated_v3":
            assert cache_root is not None
            v30_summary = install_v30_dataset_into_root(
                cache_root,
                output_root,
                data_file_size_in_mb=int(v30_data_file_size_in_mb),
                video_file_size_in_mb=int(v30_video_file_size_in_mb),
            )
            if cache_root.exists():
                shutil.rmtree(cache_root)
            summary["codebase_version"] = v30_summary.get("codebase_version")
            summary["layout"] = "aggregated_v3"
        else:
            v30_root = (
                Path(v30_output_root)
                if v30_output_root is not None
                else output_root.parent / f"{output_root.name}_v30"
            )
            if aggregate_media:
                v30_summary = install_v30_dataset(
                    dataset_root,
                    v30_root,
                    data_file_size_in_mb=int(v30_data_file_size_in_mb),
                    video_file_size_in_mb=int(v30_video_file_size_in_mb),
                )
            else:
                from robot_data_processing.export_lerobot_v30 import install_pseudo_v30_from_v21

                v30_summary = install_pseudo_v30_from_v21(dataset_root, v30_root)
            (v30_root / "lerobot_v30_export.json").write_text(
                json.dumps(v30_summary, indent=2),
                encoding="utf-8",
            )
        summary["lerobot_v30"] = v30_summary
        stats_root = (
            Path(v30_summary.get("output_root") or output_root)
            if version == "both"
            else output_root
        )
        from robot_data_processing.export_lerobot_v30 import ensure_lerobot_v30_stats

        ensure_lerobot_v30_stats(stats_root, num_workers=export_workers)
        if version in ("pseudo_v3", "aggregated_v3"):
            (output_root / "lerobot_v30_export.json").write_text(
                json.dumps(v30_summary, indent=2),
                encoding="utf-8",
            )
    report_path = output_root / "export_report.json"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary


def export_standard_dataset(
    *,
    output_root: Path,
    episodes: list[tuple[StandardEpisode, EpisodeResult | None]],
    camera_map: dict[str, str],
    source_root: Path,
    fps: float = 30.0,
    embodiment: str = "unknown",
    state_action_delay: int = 1,
    target_height: int = 384,
    target_short_side: int | None = None,
    max_keyframe_interval: int = 10,
    standard_info_path: Path | None = None,
    normalize_videos: bool = True,
    export_workers: int | None = None,
    parallel_videos: bool = True,
    renumber_episodes: bool = False,
    task_name_to_index: dict[str, int] | None = None,
    show_progress: bool = True,
    skip_parameters: bool = False,
    info_extras: dict[str, Any] | None = None,
    wrist_view_cameras: list[str] | None = None,
    lerobot_version: str = "aggregated_v3",
    v30_output_root: Path | None = None,
    v30_data_file_size_in_mb: int = 100,
    v30_video_file_size_in_mb: int = 200,
    v30_aggregate_media: bool | None = None,
    keep_all_frames: bool = False,
    ignore_quality_discard: bool = False,
    adapter_package_dir: Path | str | None = None,
    allow_mixed_camera_sizes: bool = False,
    video_resolution_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export filtered standard episodes (+ optional video normalize).

    Always writes a single LeRobot root under ``output_root`` (no per-task subdirs).
    When ``renumber_episodes`` is True, output episode ids become contiguous 0..N-1
    while ``meta.source_episode_index`` keeps the source id for video lookup.

    ``lerobot_version`` / layout controls the on-disk LeRobot layout:
    - ``pseudo_v3``: v3 path templates, one episode per file (no media concat)
    - ``aggregated_v3`` / ``v3.0``: true aggregated v3 chunk files
    - ``v2.1``: legacy per-episode paths (staging only; not a public final format)
    - ``both``: pseudo_v3 at output + aggregated at ``v30_output_root``
    """
    from tqdm import tqdm

    if adapter_package_dir is not None:
        from robot_data_processing.adapter_hooks import install_adapter_hooks

        install_adapter_hooks(adapter_package_dir)

    version = str(lerobot_version or "aggregated_v3").lower()
    # Normalize aliases
    if version in ("v3.0", "v3"):
        version = "aggregated_v3" if v30_aggregate_media is not False else "pseudo_v3"
    if version == "pseudo_v3":
        aggregate_media = False
    elif version == "aggregated_v3":
        aggregate_media = True if v30_aggregate_media is None else bool(v30_aggregate_media)
    elif version == "both":
        aggregate_media = True if v30_aggregate_media is None else bool(v30_aggregate_media)
    elif version == "v2.1":
        aggregate_media = False
    else:
        raise ValueError(f"Unsupported lerobot_version/layout: {lerobot_version!r}")

    write_v30 = version in ("pseudo_v3", "aggregated_v3", "both")
    if version in ("pseudo_v3", "aggregated_v3"):
        renumber_episodes = True if renumber_episodes or version == "aggregated_v3" else renumber_episodes
    if version == "aggregated_v3":
        renumber_episodes = True

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    cache_root: Path | None = None
    if version in ("pseudo_v3", "aggregated_v3"):
        cache_root = output_root / ".lerobot_export_cache"
        if cache_root.exists():
            shutil.rmtree(cache_root)
        cache_root.mkdir(parents=True, exist_ok=True)
        dataset_root = cache_root
    else:
        dataset_root = output_root

    # Filter + assign lengths / global indices up front so workers can run independently.
    kept: list[tuple[StandardEpisode, np.ndarray | None, int]] = []
    skipped = 0
    skipped_frame_alignment = 0
    # Stage A keep_all_frames: skip per-ep ffprobe / is_v3_layout thrash (P1 already checked).
    check_videos = not bool(keep_all_frames)
    n_candidates = len(episodes)
    # pending: (order, ep, keep_mask, n, src_root, src_ep)
    pending: list[tuple[int, StandardEpisode, np.ndarray | None, int, Path, int]] = []
    if show_progress:
        print(
            f"[P7] pre-export filter candidates={n_candidates} "
            f"check_videos={check_videos} keep_all_frames={keep_all_frames}",
            flush=True,
        )
    for ep_i, (ep, result) in enumerate(episodes):
        if (
            result is not None
            and result.discard
            and not ignore_quality_discard
            and not keep_all_frames
        ):
            skipped += 1
            continue
        if keep_all_frames:
            n = int(ep.num_frames)
            if n <= 0:
                skipped += 1
                continue
            keep_mask = None
        else:
            n = _kept_length(ep, result)
            if n is None:
                if ignore_quality_discard and result is not None and result.discard:
                    # Soft-ignore discard but still honor mask when present
                    n = int(ep.num_frames)
                    keep_mask = result.step_validity_mask
                    if keep_mask is not None:
                        n = int(np.asarray(keep_mask).reshape(-1).astype(bool).sum())
                    if n <= 0:
                        skipped += 1
                        continue
                else:
                    skipped += 1
                    continue
            else:
                keep_mask = result.step_validity_mask if result is not None else None
            if keep_mask is not None:
                attach_quality_frame_removal(ep, result)
        src_root = Path(ep.meta.get("dataset_root", source_root))
        src_ep = int(ep.meta.get("source_episode_index", ep.episode_index))
        pending.append((len(pending), ep, keep_mask, n, src_root, src_ep))

    align_ok: dict[int, bool] = {}
    if not pending:
        pass
    elif not check_videos:
        # Field-only check is cheap; keep serial to avoid thread overhead.
        for order, ep, keep_mask, n, src_root, src_ep in pending:
            align = check_export_frame_alignment(
                num_frames=int(ep.num_frames),
                fields=ep.fields,
                extrinsic=ep.extrinsic,
                keep_mask=keep_mask,
                dataset_root=src_root,
                source_episode_index=src_ep,
                camera_map=camera_map,
                fps=float(fps),
                check_videos=False,
            )
            align_ok[order] = bool(align.ok)
    else:
        probe_workers = _default_probe_workers(len(pending))
        _prewarm_v3_alignment_caches([p[4] for p in pending])
        jobs = [
            (
                order,
                int(ep.num_frames),
                ep.fields,
                ep.extrinsic,
                keep_mask,
                str(src_root),
                int(src_ep),
                camera_map,
                float(fps),
                True,
            )
            for order, ep, keep_mask, n, src_root, src_ep in pending
        ]
        if show_progress:
            print(
                f"[P7] pre-export video probe jobs={len(jobs)} workers={probe_workers}",
                flush=True,
            )
        if probe_workers <= 1 or len(jobs) <= 1:
            for job in jobs:
                order, ok = _export_align_job(job)
                align_ok[order] = ok
                if show_progress and (order + 1) % 2000 == 0:
                    print(
                        f"[P7] pre-export probe {order + 1}/{len(jobs)}",
                        flush=True,
                    )
        else:
            with ThreadPoolExecutor(max_workers=probe_workers) as pool:
                futs = {pool.submit(_export_align_job, job): job[0] for job in jobs}
                done = 0
                for fut in as_completed(futs):
                    order, ok = fut.result()
                    align_ok[order] = ok
                    done += 1
                    if show_progress and (done % 2000 == 0 or done == len(jobs)):
                        print(
                            f"[P7] pre-export probe {done}/{len(jobs)}",
                            flush=True,
                        )

    for order, ep, keep_mask, n, _src_root, _src_ep in pending:
        if not align_ok.get(order, False):
            skipped += 1
            skipped_frame_alignment += 1
            continue
        ep.meta.setdefault("source_episode_index", ep.episode_index)
        kept.append((ep, keep_mask, n))
    if show_progress:
        print(
            f"[P7] pre-export filter done kept={len(kept)} skipped={skipped} "
            f"skipped_align={skipped_frame_alignment}",
            flush=True,
        )

    if renumber_episodes:
        for new_i, (ep, _mask, _n) in enumerate(kept):
            ep.meta.setdefault("source_episode_index", ep.episode_index)
            ep.episode_index = new_i

    # Task index map: prefer source tasks.jsonl indices; else assign densely.
    source_task_map = dict(task_name_to_index or {})
    task_to_index: dict[str, int] = {}
    tasks_out: list[dict[str, Any]] = []
    used_task_index_to_name: dict[int, str] = {}
    next_synth_index = (max(source_task_map.values()) + 1) if source_task_map else 0

    def _task_names_for(ep: StandardEpisode) -> list[str]:
        names = ep.meta.get("tasks") or []
        if isinstance(names, str):
            names = [names]
        names = [str(x) for x in names if x is not None and str(x) != ""]
        if not names and ep.meta.get("task"):
            names = [str(ep.meta["task"])]
        if not names:
            names = [embodiment]
        return names

    def _task_index_for(ep: StandardEpisode) -> int:
        nonlocal next_synth_index
        names = _task_names_for(ep)
        # Keep full list on meta for episodes.jsonl
        ep.meta["tasks"] = names
        name = names[0]
        if name in task_to_index:
            return task_to_index[name]
        if name in source_task_map:
            idx = int(source_task_map[name])
        else:
            idx = int(next_synth_index)
            next_synth_index += 1
        task_to_index[name] = idx
        used_task_index_to_name[idx] = name
        return idx

    export_tasks: list[tuple[int, StandardEpisode, np.ndarray | None, int, int]] = []
    global_index = 0
    for ep, keep_mask, n in kept:
        ti = _task_index_for(ep)
        export_tasks.append((ep.episode_index, ep, keep_mask, global_index, ti))
        global_index += n

    tasks_out = [
        {"task_index": int(ti), "task": used_task_index_to_name[ti]}
        for ti in sorted(used_task_index_to_name)
    ]

    workers = _default_export_workers(export_workers)
    episodes_out: list[dict[str, Any]] = []
    fields_present: set[str] = set()
    # camera_name → (height, width); first-seen wins (episodes share scale policy)
    camera_sizes: dict[str, tuple[int, int]] = {}
    total_frames = 0

    pool_state = {
        "output_root": str(dataset_root),
        "camera_map": camera_map,
        "fps": fps,
        "target_height": target_height,
        "target_short_side": target_short_side,
        "max_keyframe_interval": max_keyframe_interval,
        "normalize_videos": normalize_videos,
        "parallel_videos": parallel_videos,
        "skip_parameters": skip_parameters,
        "source_root": str(source_root),
        "adapter_package_dir": str(adapter_package_dir) if adapter_package_dir else None,
        "video_resolution_plan": video_resolution_plan,
    }

    size_registry = CameraSizeRegistry()

    def _merge_camera_sizes(sizes: dict[str, tuple[int, int]]) -> None:
        # Count every episode; first-wins would hide mixed resolutions (REPORT EGO-02 / INT-01).
        size_registry.add(sizes)

    if workers <= 1 or len(export_tasks) <= 1:
        _init_export_pool(pool_state)
        iterator: Any = export_tasks
        if show_progress:
            iterator = tqdm(export_tasks, desc="export standard")
        for task in iterator:
            row, sizes, field_names = _export_one_task(task)
            episodes_out.append(row)
            _merge_camera_sizes(sizes)
            fields_present.update(field_names)
            total_frames += int(row["length"])
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_export_pool,
            initargs=(pool_state,),
        ) as pool:
            futures = {pool.submit(_export_one_task, task): task[0] for task in export_tasks}
            iterator = as_completed(futures)
            if show_progress:
                iterator = tqdm(iterator, total=len(futures), desc=f"export standard ({workers}w)")
            results_by_ep: dict[int, tuple[dict[str, Any], dict[str, tuple[int, int]], set[str]]] = {}
            for fut in iterator:
                ep_id = futures[fut]
                results_by_ep[ep_id] = fut.result()
        for ep_id, _ep, _mask, _g0, _ti in export_tasks:
            row, sizes, field_names = results_by_ep[ep_id]
            episodes_out.append(row)
            _merge_camera_sizes(sizes)
            fields_present.update(field_names)
            total_frames += int(row["length"])

    camera_sizes.update(size_registry.resolve(allow_mixed=allow_mixed_camera_sizes))
    info_extras = {**(info_extras or {}), "camera_image_size_counts": size_registry.summary()}
    field_registry = FieldSchemaRegistry()
    for ep_k, _mask, _n in kept:
        field_registry.add(episode_feature_meta(ep_k)[0], episode_field_dtypes(ep_k))
    field_registry.resolve()
    if field_registry.availability():
        info_extras["field_availability"] = field_registry.availability()
    add_source_timestamps_declaration(info_extras, fields_present)
    camera_list = sorted(camera_sizes.keys()) or sorted(camera_map.values())
    info = _build_info_dict(
        episodes_out=episodes_out,
        kept=kept,
        fields_present=fields_present,
        camera_sizes=camera_sizes,
        camera_map=camera_map,
        fps=fps,
        embodiment=embodiment,
        state_action_delay=state_action_delay,
        tasks_out=tasks_out,
        total_frames=total_frames,
        skip_parameters=skip_parameters,
        info_extras=info_extras,
        wrist_view_cameras=wrist_view_cameras,
        standard_info_path=standard_info_path,
    )
    # This path keeps no per-episode drop list, so no episode_discard record.
    info.pop("episode_discard", None)
    _write_v21_meta_files(
        dataset_root,
        info=info,
        episodes_out=episodes_out,
        tasks_out=tasks_out,
        embodiment=embodiment,
    )

    summary: dict[str, Any] = {
        "output_root": str(output_root),
        "dataset_root": str(dataset_root),
        "lerobot_version": version,
        "total_episodes": len(episodes_out),
        "total_frames": total_frames,
        "skipped_episodes": skipped,
        "skipped_frame_alignment": skipped_frame_alignment,
        "cameras": camera_list,
        "state_action_delay": state_action_delay,
        "export_workers": workers,
        "parallel_videos": parallel_videos,
        "renumber_episodes": renumber_episodes,
        "keep_all_frames": keep_all_frames,
        "codebase_version": "v2.1" if version == "v2.1" else None,
    }

    if write_v30:
        from robot_data_processing.export_lerobot_v30 import (
            install_pseudo_v30_into_root,
            install_v30_dataset,
            install_v30_dataset_into_root,
        )

        if version == "pseudo_v3":
            assert cache_root is not None
            v30_summary = install_pseudo_v30_into_root(cache_root, output_root)
            if cache_root.exists():
                shutil.rmtree(cache_root)
            summary["codebase_version"] = v30_summary.get("codebase_version")
            summary["layout"] = "pseudo_v3"
        elif version == "aggregated_v3":
            assert cache_root is not None
            v30_summary = install_v30_dataset_into_root(
                cache_root,
                output_root,
                data_file_size_in_mb=int(v30_data_file_size_in_mb),
                video_file_size_in_mb=int(v30_video_file_size_in_mb),
            )
            if cache_root.exists():
                shutil.rmtree(cache_root)
            summary["codebase_version"] = v30_summary.get("codebase_version")
            summary["layout"] = "aggregated_v3"
        else:
            # both: keep per-episode tree at output_root, write v3 aside
            v30_root = (
                Path(v30_output_root)
                if v30_output_root is not None
                else output_root.parent / f"{output_root.name}_v30"
            )
            if aggregate_media:
                v30_summary = install_v30_dataset(
                    dataset_root,
                    v30_root,
                    data_file_size_in_mb=int(v30_data_file_size_in_mb),
                    video_file_size_in_mb=int(v30_video_file_size_in_mb),
                )
            else:
                from robot_data_processing.export_lerobot_v30 import install_pseudo_v30_from_v21

                v30_summary = install_pseudo_v30_from_v21(dataset_root, v30_root)
            (v30_root / "lerobot_v30_export.json").write_text(
                json.dumps(v30_summary, indent=2),
                encoding="utf-8",
            )
        summary["lerobot_v30"] = v30_summary
        stats_root = (
            Path(v30_summary.get("output_root") or output_root)
            if version == "both"
            else output_root
        )
        from robot_data_processing.export_lerobot_v30 import ensure_lerobot_v30_stats

        ensure_lerobot_v30_stats(stats_root, num_workers=workers)
        if version in ("pseudo_v3", "aggregated_v3"):
            (output_root / "lerobot_v30_export.json").write_text(
                json.dumps(v30_summary, indent=2),
                encoding="utf-8",
            )

    report_path = output_root / "export_report.json"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary
