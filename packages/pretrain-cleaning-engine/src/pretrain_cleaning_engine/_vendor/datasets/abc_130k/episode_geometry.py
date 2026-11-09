"""ABC-130k P6: keep EEF in left_arm_base; no camera-to-base extrinsics.

Official FK dump (ABC-130k-EEF ``eef_pose.parquet``) is already in ``left_arm_base``.
MCAP has CameraCalibration K but **no** ``T_World_Camera`` / hand-eye, so we do **not**
invent ``T_Episode_CameraReference = I``. ``extrinsic_confidence: untrusted`` omits
``extrinsic.*``; P6 still writes scaled intrinsics + video export map.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from robot_data_processing.normalize.episode_frame_geometry import (
    build_scaled_intrinsics_record,
    target_short_side_size,
)
from robot_data_processing.normalize.standard_types import (
    EpisodeGeometryResult,
    EpisodeRef,
    StandardEpisode,
)

SCHEMA_VERSION = "episode_camera_geometry_v1"
EPISODE_FRAME_DEFINITION = "left_arm_base (no camera extrinsic)"
DEFAULT_SOURCE_CALIBRATION_SIZE = (640, 480)
DEFAULT_VIDEO_SHORT_SIDE = 384

_REQUIRED_EEF = (
    "observation.state.eef.left",
    "observation.state.eef.right",
    "action.eef.left",
    "action.eef.right",
)


def _strip_intrinsic_prefix(intrinsic: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in intrinsic.items():
        short = str(key)
        if short.startswith("intrinsic."):
            short = short[len("intrinsic.") :]
        out[short] = val
    return out


class Abc130kEpisodeGeometry:
    def __init__(self, package_dir):
        self.package_dir = package_dir

    def apply_episode_frame_geometry(
        self,
        standard: StandardEpisode,
        *,
        ref: EpisodeRef,
        geometry_cfg: dict[str, Any],
        keymap: dict[str, Any],
        stored_video_size: tuple[int, int] | None = None,
    ) -> EpisodeGeometryResult:
        del keymap, ref
        cameras = geometry_cfg.get("cameras") or {}
        mapping = dict(
            cameras.get("mapping")
            or {
                "camera_reference": "camera_top",
                "camera_aux_0": "camera_wrist_left",
                "camera_aux_1": "camera_wrist_right",
            }
        )
        wrist_view = list(cameras.get("wrist_view") or ["camera_aux_0", "camera_aux_1"])
        short_side = int(geometry_cfg.get("video_short_side", DEFAULT_VIDEO_SHORT_SIDE))
        src_calib = geometry_cfg.get(
            "source_calibration_size", list(DEFAULT_SOURCE_CALIBRATION_SIZE)
        )
        src_w, src_h = int(src_calib[0]), int(src_calib[1])

        missing = []
        for prefix in _REQUIRED_EEF:
            if f"{prefix}.position" not in standard.fields or f"{prefix}.rotation_6d" not in standard.fields:
                missing.append(prefix)
        if missing:
            return EpisodeGeometryResult(
                fields={},
                extrinsic={},
                camera_keys=[],
                camera_mapping=mapping,
                camera_intrinsics={},
                video_export_map={},
                episode_frame_definition=EPISODE_FRAME_DEFINITION,
                discard=True,
                discard_reasons=[f"missing_eef:{','.join(missing)}"],
            )

        out_fields = dict(standard.fields)
        # EEF already in left_arm_base; do not apply a fake camera-frame transform.
        for prefix in _REQUIRED_EEF:
            out_fields[f"{prefix}.position"] = np.asarray(
                standard.fields[f"{prefix}.position"], dtype=np.float32
            )
            out_fields[f"{prefix}.rotation_6d"] = np.asarray(
                standard.fields[f"{prefix}.rotation_6d"], dtype=np.float32
            )

        src_cam = cameras.get("reference", "observation.images.top")
        aux_src = list(
            cameras.get("aux")
            or ["observation.images.left_wrist", "observation.images.right_wrist"]
        )
        video_export_map = {src_cam: "camera_reference"}
        for i, aux_key in enumerate(aux_src):
            video_export_map[aux_key] = f"camera_aux_{i}"

        camera_keys = [
            "observation.images.camera_reference",
            "observation.images.camera_aux_0",
            "observation.images.camera_aux_1",
        ]

        intrinsic_short = _strip_intrinsic_prefix(standard.intrinsic)
        per_camera_sizes: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}
        output_cameras = {
            "camera_reference": "camera_top",
            "camera_aux_0": "camera_wrist_left",
            "camera_aux_1": "camera_wrist_right",
        }
        for out_cam, src_prefix in output_cameras.items():
            w = intrinsic_short.get(f"{src_prefix}.width")
            h = intrinsic_short.get(f"{src_prefix}.height")
            cam_src = (int(w), int(h)) if w and h else (src_w, src_h)
            if stored_video_size is None:
                cam_dst = target_short_side_size(cam_src[0], cam_src[1], short_side=short_side)
            else:
                cam_dst = stored_video_size
            per_camera_sizes[out_cam] = (cam_src, cam_dst)

        if stored_video_size is None:
            stored_w, stored_h = target_short_side_size(src_w, src_h, short_side=short_side)
        else:
            stored_w, stored_h = stored_video_size

        camera_intrinsics = build_scaled_intrinsics_record(
            intrinsic_short,
            output_cameras=output_cameras,
            source_calibration_size=(src_w, src_h),
            stored_size=(stored_w, stored_h),
            per_camera_sizes=per_camera_sizes,
        )

        episode_frame = str(
            geometry_cfg.get("episode_frame")
            or standard.meta.get("eef_frame")
            or EPISODE_FRAME_DEFINITION
        )
        info_extras = {
            "camera_geometry": {
                "schema_version": SCHEMA_VERSION,
                "episode_frame": episode_frame,
                "camera_mapping": mapping,
                "wrist_view_cameras": wrist_view,
                "source_extrinsic_convention": "none (no T_World_Camera in MCAP)",
                "eef_frame": "left_arm_base",
                "transform_convention": "T_A_B maps coordinates from B to A",
                "translation_unit": "meter",
                "camera_coordinate_convention": "opencv_x_right_y_down_z_forward",
                "intrinsic_storage": "meta/episodes.jsonl::camera_intrinsics",
                "per_frame_extrinsic_keys": [],
                "eef_position_format": "absolute xyz in meters",
                "eef_rotation_format": "absolute Rotation 6D, first two matrix rows flattened",
            }
        }

        return EpisodeGeometryResult(
            fields=out_fields,
            extrinsic={},
            camera_keys=camera_keys,
            camera_mapping=mapping,
            camera_intrinsics=camera_intrinsics,
            video_export_map=video_export_map,
            episode_frame_definition=episode_frame,
            geometry_meta={"schema_version": SCHEMA_VERSION, "eef_frame": "left_arm_base"},
            wrist_view_cameras=wrist_view,
            info_extras=info_extras,
            discard=False,
            discard_reasons=[],
        )


def build_episode_geometry(package_dir):
    return Abc130kEpisodeGeometry(package_dir)


def apply_episode_frame_geometry(
    standard: StandardEpisode,
    *,
    ref: EpisodeRef,
    geometry_cfg: dict[str, Any],
    keymap: dict[str, Any],
    stored_video_size: tuple[int, int] | None = None,
) -> EpisodeGeometryResult:
    return Abc130kEpisodeGeometry(None).apply_episode_frame_geometry(
        standard,
        ref=ref,
        geometry_cfg=geometry_cfg,
        keymap=keymap,
        stored_video_size=stored_video_size,
    )
