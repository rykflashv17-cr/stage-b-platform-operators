"""Frame-length consistency checks for tabular data vs videos."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


def resolve_source_video(
    dataset_root: Path,
    episode_index: int,
    source_video_key: str,
) -> Path:
    from robot_data_processing.adapter_hooks import try_adapter_resolve_video

    hooked = try_adapter_resolve_video(Path(dataset_root), int(episode_index), source_video_key)
    if hooked is not None:
        return hooked

    root = Path(dataset_root)
    from robot_data_processing.v3_layout import is_v3_layout, resolve_v3_video_path

    if is_v3_layout(root):
        return resolve_v3_video_path(root, int(episode_index), source_video_key)
    chunk = int(episode_index) // 1000
    return (
        root
        / "videos"
        / f"chunk-{chunk:03d}"
        / source_video_key
        / f"episode_{int(episode_index):06d}.mp4"
    )


@dataclass
class FrameAlignmentResult:
    ok: bool
    expected_frames: int
    mismatches: list[str] = field(default_factory=list)
    video_frames: dict[str, int] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        return "; ".join(self.mismatches)


def probe_video_frames(path: Path) -> int:
    """Return the number of frames in a video file (ffprobe)."""
    path = Path(path)
    if not path.exists():
        return 0
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
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
    except subprocess.CalledProcessError:
        return 0
    if out.isdigit() and int(out) > 0:
        return int(out)
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
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
    except subprocess.CalledProcessError:
        return 0
    return int(out) if out.isdigit() else 0


def _resolve_camera_video_path(
    dataset_root: Path,
    episode_index: int,
    camera_key: str,
) -> Path | None:
    root = Path(dataset_root)
    candidates = [camera_key]
    if not camera_key.startswith("observation.images."):
        candidates.append(f"observation.images.{camera_key}")
    for key in candidates:
        path = resolve_source_video(root, episode_index, key)
        if path.exists():
            return path
    return None


def export_frame_indices(
    num_frames: int,
    keep_mask: np.ndarray | None,
) -> np.ndarray:
    if keep_mask is None:
        return np.arange(int(num_frames), dtype=np.int64)
    mask = np.asarray(keep_mask).reshape(-1)
    if mask.size != int(num_frames):
        return np.array([], dtype=np.int64)
    return np.flatnonzero(mask != 0).astype(np.int64)


def check_raw_arrays_alignment(raw: dict[str, np.ndarray]) -> FrameAlignmentResult:
    """Check all numeric columns in a raw episode dict share the same length."""
    lengths: dict[str, int] = {}
    for name, arr in raw.items():
        if not isinstance(arr, np.ndarray):
            continue
        if arr.ndim < 1:
            continue
        lengths[name] = int(arr.shape[0])
    if not lengths:
        return FrameAlignmentResult(ok=False, expected_frames=0, mismatches=["no_frame_arrays"])
    expected = max(lengths.values())
    if len(set(lengths.values())) != 1:
        details = ", ".join(f"{k}={v}" for k, v in sorted(lengths.items()) if v != expected)
        return FrameAlignmentResult(
            ok=False,
            expected_frames=expected,
            mismatches=[f"raw_array_length_mismatch:{details}"],
        )
    return FrameAlignmentResult(ok=True, expected_frames=expected)


def check_standard_fields_alignment(
    *,
    num_frames: int,
    fields: dict[str, np.ndarray],
    extrinsic: dict[str, np.ndarray] | None = None,
) -> FrameAlignmentResult:
    """Check standard field / extrinsic arrays match ``num_frames``."""
    expected = int(num_frames)
    mismatches: list[str] = []
    for name, arr in fields.items():
        if not isinstance(arr, np.ndarray) or arr.ndim < 1:
            continue
        length = int(arr.shape[0])
        if length != expected:
            mismatches.append(f"field:{name}={length}!={expected}")
    for name, arr in (extrinsic or {}).items():
        val = np.asarray(arr)
        if val.ndim == 3:
            length = int(val.shape[0])
            if length != expected:
                mismatches.append(f"extrinsic:{name}={length}!={expected}")
    return FrameAlignmentResult(ok=not mismatches, expected_frames=expected, mismatches=mismatches)


def _v3_episode_video_frame_budget(
    dataset_root: Path,
    episode_index: int,
    camera_key: str,
    *,
    fps: float = 30.0,
) -> int | None:
    """For aggregated v3 videos, estimate episode frame count from from/to timestamps."""
    try:
        from robot_data_processing.v3_layout import resolve_v3_video_time_window
    except Exception:
        return None
    window = resolve_v3_video_time_window(Path(dataset_root), int(episode_index), camera_key)
    if window is None or fps <= 0:
        return None
    fr, to = window
    duration = float(to) - float(fr)
    if duration <= 0:
        return None
    # LeRobot v3 to_timestamp is exclusive end ≈ N/fps.
    return max(int(round(duration * float(fps))), 1)


def check_episode_videos_alignment(
    *,
    dataset_root: Path,
    episode_index: int,
    expected_frames: int,
    camera_keys: list[str],
    require_all: bool = True,
    fps: float = 30.0,
) -> FrameAlignmentResult:
    """Check each camera video exists and covers ``expected_frames``.

    For per-episode mp4s, ``nb_frames`` must equal ``expected_frames``.
    For aggregated v3 concat files (``nb_frames`` > episode length), accept when
    meta ``from_timestamp``/``to_timestamp`` imply ~``expected_frames``.
    """
    expected = int(expected_frames)
    mismatches: list[str] = []
    video_frames: dict[str, int] = {}
    checked = 0
    for camera_key in camera_keys:
        path = _resolve_camera_video_path(dataset_root, episode_index, camera_key)
        if path is None:
            if require_all:
                mismatches.append(f"video_missing:{camera_key}")
            continue
        n_frames = probe_video_frames(path)
        video_frames[camera_key] = n_frames
        checked += 1
        if n_frames == expected:
            continue
        if n_frames > expected:
            budget = _v3_episode_video_frame_budget(
                dataset_root, episode_index, camera_key, fps=fps
            )
            if budget is not None and abs(budget - expected) <= 1:
                continue
        mismatches.append(f"video:{camera_key}={n_frames}!={expected}")
    if require_all and checked == 0 and camera_keys:
        mismatches.append("no_videos_found")
    elif require_all and checked != len(camera_keys):
        missing = [
            k
            for k in camera_keys
            if k not in video_frames and f"video_missing:{k}" not in mismatches
        ]
        for key in missing:
            mismatches.append(f"video_missing:{key}")
    return FrameAlignmentResult(
        ok=not mismatches,
        expected_frames=expected,
        mismatches=mismatches,
        video_frames=video_frames,
    )


def check_import_frame_alignment(
    *,
    raw: dict[str, np.ndarray],
    standard_num_frames: int,
    fields: dict[str, np.ndarray],
    extrinsic: dict[str, np.ndarray] | None,
    dataset_root: Path,
    episode_index: int,
    camera_keys: list[str],
    require_videos: bool = True,
) -> FrameAlignmentResult:
    """Import-time check: raw columns, standard fields, and videos share one length."""
    raw_result = check_raw_arrays_alignment(raw)
    if not raw_result.ok:
        return raw_result
    if raw_result.expected_frames != int(standard_num_frames):
        return FrameAlignmentResult(
            ok=False,
            expected_frames=int(standard_num_frames),
            mismatches=[
                f"raw_vs_standard:{raw_result.expected_frames}!={int(standard_num_frames)}",
            ],
        )
    field_result = check_standard_fields_alignment(
        num_frames=standard_num_frames,
        fields=fields,
        extrinsic=extrinsic,
    )
    if not field_result.ok:
        return field_result
    if not require_videos or not camera_keys:
        return FrameAlignmentResult(ok=True, expected_frames=int(standard_num_frames))
    return check_episode_videos_alignment(
        dataset_root=dataset_root,
        episode_index=episode_index,
        expected_frames=standard_num_frames,
        camera_keys=camera_keys,
        require_all=True,
    )


def check_export_frame_alignment(
    *,
    num_frames: int,
    fields: dict[str, np.ndarray],
    extrinsic: dict[str, np.ndarray] | None,
    keep_mask: np.ndarray | None,
    dataset_root: Path,
    source_episode_index: int,
    camera_map: dict[str, str],
    fps: float,
    check_videos: bool = True,
) -> FrameAlignmentResult:
    """Pre-export check: export row count matches source videos at full episode length.

    Set ``check_videos=False`` (e.g. Stage A ``keep_all_frames``) to skip per-episode
    ffprobe / path resolve. Import-time checks and export-time write probes still apply.
    An aggregated v3 source file holds several episodes; its episode length is the
    ``from/to_timestamp`` window times ``fps`` (the export slices that window).
    """
    from robot_data_processing.phases.media import resolve_source_video_time_window
    field_result = check_standard_fields_alignment(
        num_frames=num_frames,
        fields=fields,
        extrinsic=extrinsic,
    )
    if not field_result.ok:
        return field_result

    indices = export_frame_indices(num_frames, keep_mask)
    export_n = int(indices.size)
    if export_n == 0:
        return FrameAlignmentResult(ok=False, expected_frames=0, mismatches=["export_length_zero"])

    if not check_videos or not camera_map:
        return FrameAlignmentResult(ok=True, expected_frames=export_n)

    root = Path(dataset_root)
    mismatches: list[str] = []
    video_frames: dict[str, int] = {}

    for src_key in camera_map:
        path = resolve_source_video(root, source_episode_index, src_key)
        if not path.exists():
            alt = resolve_source_video(root, source_episode_index, f"observation.images.{src_key}")
            path = alt if alt.exists() else path
        if not path.exists():
            mismatches.append(f"video_missing:{src_key}")
            continue
        window = resolve_source_video_time_window(root, source_episode_index, src_key)
        if window is not None:
            n_frames = int(round((float(window[1]) - float(window[0])) * float(fps)))
        else:
            n_frames = probe_video_frames(path)
        video_frames[src_key] = n_frames
        if n_frames != int(num_frames):
            mismatches.append(f"video:{src_key}={n_frames}!={num_frames}")
        if indices.size and int(indices.max()) >= n_frames:
            mismatches.append(f"video:{src_key}_index_oob:max={int(indices.max())}>={n_frames}")

    if len(set(video_frames.values())) > 1:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(video_frames.items()))
        mismatches.append(f"video_cross_camera_mismatch:{detail}")

    return FrameAlignmentResult(
        ok=not mismatches,
        expected_frames=export_n,
        mismatches=mismatches,
        video_frames=video_frames,
    )


def summarize_alignment_failure(result: FrameAlignmentResult, *, stage: str) -> str:
    return f"{stage}_frame_mismatch:{result.reason}"
