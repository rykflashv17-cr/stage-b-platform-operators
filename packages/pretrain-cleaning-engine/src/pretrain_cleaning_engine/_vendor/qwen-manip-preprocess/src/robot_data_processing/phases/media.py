"""P5 MediaNormalize: rename cameras, height≤target (keep AR, no upscale), libx264 GOP=10 CRF=18."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from robot_data_processing.lerobot_export import (
    INTERMEDIATE_ENCODER_ARGS,
    _contiguous_runs,
    _export_video_with_trim_runs,
    VIDEO_GOP,
    _run,
    _video_encoder_args,
    extract_video_by_indices,
    trim_video,
)


def probe_video_size(path: Path) -> tuple[int, int]:
    """Return ``(height, width)`` via ffprobe. Raises on failure."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=height,width",
        "-of",
        "json",
        str(path),
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    h = int(stream["height"])
    w = int(stream["width"])
    if h <= 0 or w <= 0:
        raise RuntimeError(f"invalid video size {w}x{h} for {path}")
    return h, w


def _even(n: int) -> int:
    return n if n % 2 == 0 else max(n - 1, 2)


def _normalize_encode(
    src: Path,
    dst: Path,
    vf: str,
    *,
    max_keyframe_interval: int,
    num_frames: int | None = None,
    fps: float | None = None,
) -> None:
    """Re-encode ``src`` → ``dst``. When ``num_frames`` is set, lock output length.

    ``63.333…s`` windows (e.g. 1900 frames @ 30fps) otherwise lose the last
    frame after a second ffmpeg pass: ``-t`` / timebase rounds to 63.3s = 1899.

    Encoding is fixed by ``_video_encoder_args`` (libx264, GOP=10, CRF=18, no
    scene-cut keyframes); a different ``max_keyframe_interval`` is a config error.
    Failures raise: copying the source would export off-spec GOP/CRF/size.
    """
    if int(max_keyframe_interval) != VIDEO_GOP:
        raise ValueError(
            f"media.max_keyframe_interval={max_keyframe_interval}; exported videos require GOP={VIDEO_GOP}"
        )
    enc_args = _video_encoder_args()
    want = int(num_frames) if num_frames is not None and int(num_frames) > 0 else None
    fps_v = float(fps) if fps is not None and float(fps) > 0 else None
    parts = [vf]
    if want is not None and fps_v is not None:
        parts.insert(0, f"fps={fps_v:g}")
    if want is not None:
        # Clone the last frame if the previous cut/timebase dropped one.
        parts.append("tpad=stop=-1:stop_mode=clone")
    filtergraph = ",".join(parts)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        filtergraph,
        *enc_args,
        "-an",
    ]
    if want is not None:
        cmd.extend(["-frames:v", str(want)])
    cmd.append(str(dst))
    _run(cmd)
    if want is not None:
        got = probe_video_frames_safe(dst)
        if got != want:
            raise RuntimeError(
                f"normalize produced {got} frames, expected {want}: {dst}"
            )


def normalize_video(
    src: Path,
    dst: Path,
    *,
    target_height: int = 384,
    max_keyframe_interval: int = 10,
    num_frames: int | None = None,
    fps: float | None = None,
) -> tuple[int, int]:
    """Re-encode to H.264; scale height down to ``target_height`` (never upscale).

    Returns final ``(height, width)``.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        raise FileNotFoundError(src)

    try:
        src_h, src_w = probe_video_size(src)
    except Exception:
        src_h, src_w = 0, 0

    # Only downscale when taller than target; otherwise keep native resolution.
    if src_h > 0 and src_h <= int(target_height):
        out_h, out_w = _even(src_h), _even(src_w)
        # Keep size (force even dims for yuv420) but still re-encode for GOP.
        vf = f"scale={out_w}:{out_h}"
    elif src_h > int(target_height):
        out_h = int(target_height)
        # width auto, even
        vf = f"scale=-2:{out_h}"
        out_w = 0  # filled after encode via probe
    else:
        # Unknown source size: attempt target-height scale (ffmpeg fails → raise)
        vf = f"scale=-2:{int(target_height)}"
        out_h, out_w = int(target_height), 0

    _normalize_encode(
        src,
        dst,
        vf,
        max_keyframe_interval=max_keyframe_interval,
        num_frames=num_frames,
        fps=fps,
    )

    try:
        return probe_video_size(dst)
    except Exception:
        if out_w > 0:
            return out_h, out_w
        if src_h > 0 and src_w > 0:
            return src_h, src_w
        return int(target_height), int(target_height)


def normalize_video_short_side(
    src: Path,
    dst: Path,
    *,
    target_short_side: int = 384,
    max_keyframe_interval: int = 10,
    num_frames: int | None = None,
    fps: float | None = None,
) -> tuple[int, int]:
    """Re-encode to H.264; scale so the shorter side equals ``target_short_side``."""
    from robot_data_processing.normalize.episode_frame_geometry import target_short_side_size

    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        raise FileNotFoundError(src)

    try:
        src_h, src_w = probe_video_size(src)
    except Exception:
        src_h, src_w = 0, 0

    if src_h > 0 and src_w > 0:
        out_w, out_h = target_short_side_size(src_w, src_h, short_side=target_short_side)
        vf = f"scale={out_w}:{out_h}"
    else:
        vf = f"scale=-2:{int(target_short_side)}"
        out_h, out_w = int(target_short_side), 0

    _normalize_encode(
        src,
        dst,
        vf,
        max_keyframe_interval=max_keyframe_interval,
        num_frames=num_frames,
        fps=fps,
    )

    try:
        return probe_video_size(dst)
    except Exception:
        if out_w > 0:
            return out_h, out_w
        return int(target_short_side), int(target_short_side)


def normalize_video_scale_crop(
    src: Path,
    dst: Path,
    *,
    cam_plan: dict,
    camera: str = "",
    max_keyframe_interval: int = 10,
    num_frames: int | None = None,
    fps: float | None = None,
) -> tuple[int, int]:
    """Re-encode to H.264 with the Stage A resolution plan: scale to cover the camera's
    base size, then center-crop onto it (``video_resolution.cover_crop_transform``)."""
    from robot_data_processing.phases.video_resolution import planned_transform

    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        raise FileNotFoundError(src)
    src_h, src_w = probe_video_size(src)
    t = planned_transform(cam_plan, src_w, src_h, camera=camera)
    vf = f"scale={t['scaled_width']}:{t['scaled_height']}"
    if (t["scaled_width"], t["scaled_height"]) != (t["width"], t["height"]):
        vf += f",crop={t['width']}:{t['height']}:{t['crop_x']}:{t['crop_y']}"
    _normalize_encode(
        src,
        dst,
        vf,
        max_keyframe_interval=max_keyframe_interval,
        num_frames=num_frames,
        fps=fps,
    )
    try:
        return probe_video_size(dst)
    except Exception:
        return int(t["height"]), int(t["width"])


def resolve_source_video(
    dataset_root: Path,
    episode_index: int,
    source_video_key: str,
) -> Path:
    from robot_data_processing.frame_alignment import resolve_source_video as _resolve

    return _resolve(dataset_root, episode_index, source_video_key)


def source_video_is_usable(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        p = Path(path)
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def resolve_source_video_time_window(
    dataset_root: Path,
    episode_index: int,
    source_video_key: str,
) -> tuple[float, float] | None:
    """Aggregated v3 ``(from_timestamp, to_timestamp)`` or None for per-episode mp4s."""
    from robot_data_processing.v3_layout import is_v3_layout, resolve_v3_video_time_window

    root = Path(dataset_root)
    if not is_v3_layout(root):
        return None
    window = resolve_v3_video_time_window(root, int(episode_index), source_video_key)
    if window is not None:
        return window
    # Retry with observation.images. prefix variants (camera_map may use short names).
    if not source_video_key.startswith("observation.images."):
        return resolve_v3_video_time_window(
            root, int(episode_index), f"observation.images.{source_video_key}"
        )
    return None


def slice_video_time_window(
    src: Path,
    dst: Path,
    *,
    from_timestamp: float,
    to_timestamp: float,
    fps: float,
    num_frames: int | None = None,
    encoder_args: list[str] | None = None,
) -> None:
    """Cut ``[from_timestamp, to_timestamp)`` from an aggregated concat mp4.

    Tries stream-copy first; re-encodes when frame count must match ``num_frames``.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    start = float(from_timestamp)
    end = float(to_timestamp)
    duration = end - start
    if duration <= 0:
        raise ValueError(f"invalid time window [{start}, {end}) for {src}")
    want = int(num_frames) if num_frames is not None and int(num_frames) > 0 else None
    # Re-encode needs a slightly longer -t so tpad can clone the last frame
    # when  n/fps  (e.g. 1900/30=63.333…) rounds down to n-1.
    reencode_t = duration
    if want is not None and float(fps) > 0:
        reencode_t = max(duration, (want + 2) / float(fps))

    def _cmd(*, stream_copy: bool) -> list[str]:
        # -ss after -i for frame-accurate cuts on concat files.
        t_out = duration if stream_copy else reencode_t
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{t_out:.6f}",
        ]
        if stream_copy:
            cmd.extend(["-c", "copy", "-an", str(dst)])
        else:
            enc = encoder_args or _video_encoder_args()
            vf_parts = ["tpad=stop=-1:stop_mode=clone"] if want is not None else []
            if vf_parts:
                cmd.extend(["-vf", ",".join(vf_parts)])
            if want is not None:
                cmd.extend(["-frames:v", str(want)])
            cmd.extend([*enc, "-an", str(dst)])
        return cmd

    # A stream-copy cut is frame accurate only from frame 0: a later start lands on the
    # previous keyframe (keyint can be 250), shifting video against the data rows.
    try:
        if start > 0:
            raise RuntimeError("window does not start at 0: re-encode")
        _run(_cmd(stream_copy=True))
        if want is None:
            return
        n = probe_video_frames_safe(dst)
        if n == want:
            return
    except Exception:
        pass
    if dst.exists():
        dst.unlink(missing_ok=True)
    _run(_cmd(stream_copy=False))
    if want is not None:
        n = probe_video_frames_safe(dst)
        if n != want:
            raise RuntimeError(
                f"time-window slice produced {n} frames, expected {want}: {dst}"
            )


def probe_video_frames_safe(path: Path) -> int:
    from robot_data_processing.frame_alignment import probe_video_frames

    return int(probe_video_frames(path))


def _link_or_copy(src: Path, dst: Path) -> None:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def export_passthrough_videos(
    dataset_root: Path,
    output_root: Path,
    episode_index: int,
    camera_map: dict[str, str],
    *,
    source_episode_index: int | None = None,
    keep_indices: np.ndarray | None = None,
    fps: float = 30.0,
    num_frames: int | None = None,
) -> dict[str, tuple[int, int]]:
    """Hardlink/copy source videos; trim/re-encode only when frames are dropped."""
    src_ep = int(episode_index if source_episode_index is None else source_episode_index)
    out_chunk = episode_index // 1000
    frame_count = int(
        num_frames if num_frames is not None else (keep_indices.size if keep_indices is not None else 0)
    )
    sizes: dict[str, tuple[int, int]] = {}
    for src_key, std_name in camera_map.items():
        src = resolve_source_video(dataset_root, src_ep, src_key)
        if not src.exists():
            alt = resolve_source_video(dataset_root, src_ep, f"observation.images.{src_key}")
            src = alt if alt.exists() else src
        if not source_video_is_usable(src):
            continue
        dst = (
            output_root
            / "videos"
            / f"chunk-{out_chunk:03d}"
            / f"observation.images.{std_name}"
            / f"episode_{episode_index:06d}.mp4"
        )
        time_window = resolve_source_video_time_window(dataset_root, src_ep, src_key)
        trim_src, trim_tmp = _prepare_video_source(
            src,
            keep_indices=keep_indices,
            fps=fps,
            num_frames=frame_count,
            time_window=time_window,
        )
        try:
            _link_or_copy(trim_src, dst)
            try:
                sizes[std_name] = probe_video_size(dst)
            except Exception:
                sizes[std_name] = (0, 0)
        finally:
            if trim_tmp is not None and Path(trim_tmp).exists():
                Path(trim_tmp).unlink(missing_ok=True)
    return sizes


def _prepare_video_source(
    src: Path,
    *,
    keep_indices: np.ndarray | None,
    fps: float,
    num_frames: int,
    time_window: tuple[float, float] | None = None,
    intermediate: bool = False,
) -> tuple[Path, Path | None]:
    """Return ``(source_path, temp_path_to_delete)``.

    Order: optional aggregated-v3 time-window slice, then keep_indices trim. With both,
    the kept indices are offset to the episode's first frame in the shared file and cut in
    one frame-accurate pass (no intermediate slice).
    """
    src = Path(src)
    if not source_video_is_usable(src):
        raise ValueError(f"empty or missing source video: {src}")
    temps: list[Path] = []
    working = src

    if time_window is not None and keep_indices is not None:
        indices = np.asarray(keep_indices, dtype=np.int64).reshape(-1)
        if indices.size == 0:
            raise ValueError(f"Cannot export video with empty keep_indices: {src}")
        first = int(round(float(time_window[0]) * float(fps)))
        window_frames = int(round((float(time_window[1]) - float(time_window[0])) * float(fps)))
        if int(indices.max()) >= window_frames:
            raise ValueError(
                f"keep index {int(indices.max())} outside the episode window of {window_frames} frames: {src}"
            )
        fd, tmp_name = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        tmp = Path(tmp_name)
        _export_video_with_trim_runs(
            src, tmp, _contiguous_runs(indices + first),
            encoder_args=INTERMEDIATE_ENCODER_ARGS if intermediate else None,
        )
        return tmp, tmp

    if time_window is not None:
        fr, to = time_window
        # Skip slice only when window starts at 0 and keep_indices will take a
        # prefix from frame 0 of a per-episode file — but concat files always
        # need an end cut, so always slice when a window is provided.
        fd, tmp_name = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        tmp = Path(tmp_name)
        slice_video_time_window(
            working,
            tmp,
            from_timestamp=float(fr),
            to_timestamp=float(to),
            fps=fps,
            # The whole source episode (window), not the kept count.
            num_frames=int(round((float(to) - float(fr)) * float(fps))),
            encoder_args=INTERMEDIATE_ENCODER_ARGS if intermediate else None,
        )
        temps.append(tmp)
        working = tmp

    if keep_indices is None:
        return working, temps[-1] if temps else None

    indices = np.asarray(keep_indices, dtype=np.int64).reshape(-1)
    if indices.size == 0:
        for t in temps:
            t.unlink(missing_ok=True)
        raise ValueError(f"Cannot export video with empty keep_indices: {src}")

    # Contiguous prefix [0, N): trim to N frames. Source may be longer after
    # delay-tail truncate (num_frames = T-n while mp4 still has T).
    if np.array_equal(indices, np.arange(indices.size)) and int(indices[0]) == 0:
        want = int(indices.size)
        try:
            got = probe_video_frames_safe(working)
        except Exception:
            got = -1
        if got == want:
            return working, temps[-1] if temps else None
        fd, tmp_name = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        tmp = Path(tmp_name)
        trim_video(working, tmp, want, fps)
        for t in temps:
            t.unlink(missing_ok=True)
        return tmp, tmp

    fd, tmp_name = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    tmp = Path(tmp_name)
    extract_video_by_indices(working, tmp, indices, fps)
    for t in temps:
        t.unlink(missing_ok=True)
    return tmp, tmp


def export_normalized_videos(
    dataset_root: Path,
    output_root: Path,
    episode_index: int,
    camera_map: dict[str, str],
    *,
    target_height: int = 384,
    target_short_side: int | None = None,
    max_keyframe_interval: int = 10,
    source_episode_index: int | None = None,
    parallel_videos: bool = True,
    keep_indices: np.ndarray | None = None,
    fps: float = 30.0,
    num_frames: int | None = None,
    resolution_plan: dict | None = None,
) -> dict[str, tuple[int, int]]:
    """camera_map: source_key → standard camera name (e.g. camera_top).

    When ``target_short_side`` is set, resize so the shorter side equals that value
    (v2 episode geometry contract). Otherwise scale by ``target_height``.
    ``resolution_plan`` (Stage A, per camera) takes precedence: scale + center crop
    onto the camera's base size.

    Returns ``{camera_name: (height, width)}`` for written videos.
    """
    src_ep = int(episode_index if source_episode_index is None else source_episode_index)
    out_chunk = episode_index // 1000
    # Output length: the kept frames when a keep list is given (callers pass the source
    # episode length as ``num_frames``; using it would pad the cut video back to full length).
    frame_count = int(keep_indices.size if keep_indices is not None else (num_frames or 0))

    jobs: list[tuple[str, Path, Path, tuple[float, float] | None]] = []
    for src_key, std_name in camera_map.items():
        src = resolve_source_video(dataset_root, src_ep, src_key)
        if not src.exists():
            alt = resolve_source_video(dataset_root, src_ep, f"observation.images.{src_key}")
            src = alt if alt.exists() else src
        if not source_video_is_usable(src):
            continue
        dst = (
            output_root
            / "videos"
            / f"chunk-{out_chunk:03d}"
            / f"observation.images.{std_name}"
            / f"episode_{episode_index:06d}.mp4"
        )
        time_window = resolve_source_video_time_window(dataset_root, src_ep, src_key)
        jobs.append((std_name, src, dst, time_window))

    if not jobs:
        return {}

    def _one(job: tuple[str, Path, Path, tuple[float, float] | None]) -> tuple[str, int, int]:
        std_name, src, dst, time_window = job
        trim_src, trim_tmp = _prepare_video_source(
            src,
            keep_indices=keep_indices,
            fps=fps,
            num_frames=frame_count,
            time_window=time_window,
            intermediate=True,  # normalize re-encodes it at CRF / GOP
        )
        try:
            cam_plan = (resolution_plan or {}).get(std_name)
            if cam_plan is not None:
                h, w = normalize_video_scale_crop(
                    trim_src,
                    dst,
                    cam_plan=cam_plan,
                    camera=std_name,
                    max_keyframe_interval=max_keyframe_interval,
                    num_frames=frame_count if frame_count > 0 else None,
                    fps=fps,
                )
            elif target_short_side is not None:
                h, w = normalize_video_short_side(
                    trim_src,
                    dst,
                    target_short_side=int(target_short_side),
                    max_keyframe_interval=max_keyframe_interval,
                    num_frames=frame_count if frame_count > 0 else None,
                    fps=fps,
                )
            else:
                h, w = normalize_video(
                    trim_src,
                    dst,
                    target_height=target_height,
                    max_keyframe_interval=max_keyframe_interval,
                    num_frames=frame_count if frame_count > 0 else None,
                    fps=fps,
                )
            return std_name, h, w
        finally:
            if trim_tmp is not None and trim_tmp.exists():
                trim_tmp.unlink(missing_ok=True)

    sizes: dict[str, tuple[int, int]] = {}
    if parallel_videos and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futs = [pool.submit(_one, job) for job in jobs]
            for fut in as_completed(futs):
                name, h, w = fut.result()
                sizes[name] = (h, w)
    else:
        for job in jobs:
            name, h, w = _one(job)
            sizes[name] = (h, w)
    return sizes
