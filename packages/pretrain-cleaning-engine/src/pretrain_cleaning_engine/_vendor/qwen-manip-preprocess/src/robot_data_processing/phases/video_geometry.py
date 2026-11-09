"""Keep camera intrinsics consistent with the pixels actually exported (REPORT EGO-02 / INT-01).

EPISODE_CAMERA_GEOMETRY §5: ``K`` must describe the stored video grid. A single global
``source_calibration_size`` or a first-episode ``camera_sizes`` entry cannot describe
a dataset whose source videos have different resolutions, so:

* P6 probes each episode's source video and rebuilds the pixel transform from the real
  source size to the size the exporter will write (``reconcile_camera_intrinsics``).
  A calibration grid whose aspect ratio differs from the real video cannot be fixed
  by scaling, so that episode is discarded instead of exporting a wrong ``K``.
* ``source_calibration_size: source_video`` declares that source ``K`` is expressed in
  each episode's own source-video pixels (``resolve_calibration_geometry_cfg``).
* After export, the written size must equal the ``stored_image_size`` in ``K``
  (``check_exported_sizes``), and info-level ``camera_sizes`` refuse mixed sizes
  (``CameraSizeRegistry``) instead of keeping whichever episode finished first.
* Stage A resolution unification (``video_resolution``) puts a per-camera plan into
  the video settings; larger sources are scaled to cover the base size and center
  cropped, and ``K`` gets the same scale plus a principal-point shift.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from robot_data_processing.normalize.episode_frame_geometry import (
    pinhole_intrinsics_problem,
    target_short_side_size,
)

SOURCE_VIDEO = "source_video"
VIDEO_OUTPUT_KEY = "_video_output"
DEFAULT_ASPECT_TOLERANCE = 0.01


def video_output_settings(
    media_cfg: dict[str, Any],
    geometry_cfg: dict[str, Any],
    *,
    geometry_enabled: bool,
    is_align: bool,
) -> dict[str, Any]:
    """Resolve the exporter's resize policy once, for P6 and P7 alike."""
    normalize_videos = bool(media_cfg.get("normalize_videos", True)) or bool(is_align)
    short_side = geometry_cfg.get("video_short_side") if geometry_enabled else None
    if short_side is None and (geometry_enabled or normalize_videos):
        short_side = int(media_cfg.get("target_short_side", 384))
    return {
        "normalize_videos": normalize_videos,
        "target_short_side": int(short_side) if short_side is not None else None,
        "target_height": int(media_cfg.get("target_height", media_cfg.get("target_width", 384))),
    }


def expected_stored_size(src_w: int, src_h: int, settings: dict[str, Any]) -> tuple[int, int]:
    """(width, height) the exporter writes for a ``src_w x src_h`` source video."""
    if not settings.get("normalize_videos", True):
        return int(src_w), int(src_h)
    short_side = settings.get("target_short_side")
    if short_side is not None:
        return target_short_side_size(int(src_w), int(src_h), short_side=int(short_side))
    target_h = int(settings.get("target_height", 384))
    if src_h <= target_h:
        return int(src_w) // 2 * 2, int(src_h) // 2 * 2
    return 2 * round(src_w * target_h / src_h / 2), target_h


def stored_video_transform(
    src_w: int,
    src_h: int,
    settings: dict[str, Any],
    camera: str | None = None,
) -> tuple[int, int, np.ndarray, dict[str, int] | None]:
    """(stored_w, stored_h, T, crop) for a ``src_w x src_h`` source of ``camera``.

    ``T`` maps source-video pixels to stored pixels. With a Stage A resolution plan
    (``video_resolution``) the source is scaled to cover the camera's base size and
    center-cropped, so ``T = translate(-crop) @ scale``; otherwise it is a pure scale.
    """
    from robot_data_processing.phases.video_resolution import PLAN_KEY, planned_transform

    cam_plan = (settings.get(PLAN_KEY) or {}).get(str(camera)) if camera is not None else None
    if cam_plan is None:
        dst_w, dst_h = expected_stored_size(src_w, src_h, settings)
        return dst_w, dst_h, np.diag([dst_w / src_w, dst_h / src_h, 1.0]), None
    t = planned_transform(cam_plan, src_w, src_h, camera=str(camera))
    mat = np.array(
        [
            [t["scaled_width"] / src_w, 0.0, -float(t["crop_x"])],
            [0.0, t["scaled_height"] / src_h, -float(t["crop_y"])],
            [0.0, 0.0, 1.0],
        ]
    )
    return t["width"], t["height"], mat, t


def probe_source_video_size(
    dataset_root: Path,
    episode_index: int,
    source_key: str,
) -> tuple[int, int] | None:
    """(width, height) of an episode's source video, or None when it is missing."""
    from robot_data_processing.phases.media import (
        probe_video_size,
        resolve_source_video,
        source_video_is_usable,
    )

    keys = [str(source_key)]
    if not str(source_key).startswith("observation.images."):
        keys.append(f"observation.images.{source_key}")
    for key in keys:
        path = resolve_source_video(Path(dataset_root), int(episode_index), key)
        if source_video_is_usable(path):
            h, w = probe_video_size(path)
            return int(w), int(h)
    return None


def _source_location(standard: Any, ref: Any) -> tuple[Path, int]:
    root = standard.meta.get("dataset_root") or getattr(ref, "dataset_root", None) or "."
    ep = standard.meta.get("source_episode_index", getattr(ref, "episode_index", standard.episode_index))
    return Path(root), int(ep)


def resolve_calibration_geometry_cfg(
    standard: Any,
    ref: Any,
    geometry_cfg: dict[str, Any],
) -> tuple[dict[str, Any], tuple[int, int] | None]:
    """Replace ``source_calibration_size: source_video`` by this episode's probed size.

    Returns the per-episode config and the ``stored_video_size`` to pass to P6
    (None when the calibration size is a fixed, declared grid).
    """
    if geometry_cfg.get("source_calibration_size") != SOURCE_VIDEO:
        return geometry_cfg, None
    cameras = geometry_cfg.get("cameras") or {}
    ref_key = str(cameras.get("reference", "observation.images.camera_top"))
    root, src_ep = _source_location(standard, ref)
    size = probe_source_video_size(root, src_ep, ref_key)
    if size is None:
        raise FileNotFoundError(f"source video {ref_key} missing for calibration size (ep{src_ep})")
    settings = geometry_cfg.get(VIDEO_OUTPUT_KEY) or {}
    stored = expected_stored_size(size[0], size[1], settings)
    return {**geometry_cfg, "source_calibration_size": [size[0], size[1]]}, stored


def reconcile_camera_intrinsics(
    standard: Any,
    ref: Any,
    camera_intrinsics: dict[str, Any],
    video_export_map: dict[str, str],
    geometry_cfg: dict[str, Any],
) -> list[str]:
    """Rescale every ``K`` record onto the size this episode's video is exported at.

    Mutates ``camera_intrinsics`` in place; returns discard reasons (empty when OK).
    """
    settings = geometry_cfg.get(VIDEO_OUTPUT_KEY)
    if not settings or not camera_intrinsics:
        return []
    tol = float(geometry_cfg.get("calibration_aspect_tolerance", DEFAULT_ASPECT_TOLERANCE))
    out_to_src = {str(v): str(k) for k, v in (video_export_map or {}).items()}
    root, src_ep = _source_location(standard, ref)
    reasons: list[str] = []
    for out_cam, record in camera_intrinsics.items():
        src_key = out_to_src.get(str(out_cam))
        if src_key is None:
            continue
        size = probe_source_video_size(root, src_ep, src_key)
        if size is None:
            continue  # the export-side missing-video check discards the episode
        src_w, src_h = size
        calib = record.get("source_calibration_image_size") or {}
        cal_w, cal_h = int(calib.get("width", src_w)), int(calib.get("height", src_h))
        if abs((cal_w / cal_h) / (src_w / src_h) - 1.0) > tol:
            reasons.append(
                f"calibration_size_mismatch:{out_cam}:calib={cal_w}x{cal_h}:video={src_w}x{src_h}"
            )
            continue
        try:
            dst_w, dst_h, t_video, crop = stored_video_transform(src_w, src_h, settings, out_cam)
        except ValueError as exc:
            reasons.append(f"resolution_unplanned:{out_cam}:{exc}")
            continue
        # Calibration grid → source video (same aspect) → stored pixels.
        t_new = t_video @ np.diag([src_w / cal_w, src_h / cal_h, 1.0])
        t_old = np.asarray(record["source_to_stored_pixel_transform"], dtype=np.float64)
        old = record.get("stored_image_size") or {}
        same_size = (int(old.get("width", -1)), int(old.get("height", -1))) == (dst_w, dst_h)
        if same_size and (crop is None or np.allclose(t_old, t_new, atol=1e-9)):
            record["source_video_size"] = {"width": src_w, "height": src_h}
            continue
        # K_stored = T_old @ K_calib  →  K_calib = T_old^-1 @ K_stored.
        k_calib = np.linalg.solve(t_old, np.asarray(record["matrix"], dtype=np.float64))
        k_new = t_new @ k_calib
        if pinhole_intrinsics_problem(k_new, dst_w, dst_h):
            reasons.append(f"invalid_intrinsics:{out_cam}:{dst_w}x{dst_h}")
            continue
        record["matrix"] = k_new.tolist()
        record["stored_image_size"] = {"width": dst_w, "height": dst_h}
        record["source_to_stored_pixel_transform"] = t_new.tolist()
        record["source_video_size"] = {"width": src_w, "height": src_h}
        if crop is not None and (crop["crop_x"] or crop["crop_y"] or (
            crop["scaled_width"], crop["scaled_height"]) != (dst_w, dst_h)
        ):
            record["stored_crop"] = {
                "scaled_width": crop["scaled_width"],
                "scaled_height": crop["scaled_height"],
                "x": crop["crop_x"],
                "y": crop["crop_y"],
                "width": dst_w,
                "height": dst_h,
            }
    return reasons


def check_exported_sizes(
    camera_intrinsics: dict[str, Any] | None,
    camera_sizes: dict[str, tuple[int, int]],
    *,
    episode_index: int,
) -> None:
    """Fail when a written video does not have the size its ``K`` was built for."""
    for out_cam, record in (camera_intrinsics or {}).items():
        hw = camera_sizes.get(out_cam) or camera_sizes.get(f"observation.images.{out_cam}")
        stored = record.get("stored_image_size")
        if hw is None or not stored:
            continue
        if (int(hw[1]), int(hw[0])) != (int(stored["width"]), int(stored["height"])):
            raise RuntimeError(
                f"ep{episode_index} {out_cam}: exported {hw[1]}x{hw[0]} but K built for "
                f"{stored['width']}x{stored['height']}"
            )


class CameraSizeRegistry:
    """Collect per-episode camera sizes; one dataset-level size per camera or fail."""

    def __init__(self) -> None:
        self.counts: dict[str, Counter] = {}

    def add(self, sizes: dict[str, Any] | None) -> None:
        for name, hw in (sizes or {}).items():
            self.counts.setdefault(str(name), Counter())[(int(hw[0]), int(hw[1]))] += 1

    def resolve(self, *, allow_mixed: bool = False) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        mixed: dict[str, dict[str, int]] = {}
        for name, counter in self.counts.items():
            if len(counter) > 1:
                mixed[name] = {f"{w}x{h}": n for (h, w), n in counter.most_common()}
            out[name] = counter.most_common(1)[0][0]
        if mixed and not allow_mixed:
            raise RuntimeError(
                "mixed exported camera sizes (per-episode K is sized correctly, but a single "
                f"info.json shape would be wrong): {mixed}. Resize to one size "
                "(media.target_short_side + fixed aspect) or split the dataset by size, "
                "or set export.allow_mixed_camera_sizes to publish per-episode sizes only."
            )
        return out

    def summary(self) -> dict[str, dict[str, int]]:
        return {
            name: {f"{w}x{h}": n for (h, w), n in counter.most_common()}
            for name, counter in self.counts.items()
        }
