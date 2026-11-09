"""Absolute next-frame ego action sourced from the reference camera pose.

Only ego embodiments (egodex / egostandard / egoverse) attach these columns.
Teleop and humanoid datasets omit them entirely — never fabricate zeros.
"""
from __future__ import annotations

from typing import Any

import numpy as np

EGO_EMBODIMENTS = frozenset({"egodex", "egostandard", "egoverse"})
EGO_POSITION_KEY = "action.ego.position"
EGO_ROTATION_KEY = "action.ego.rotation_6d"
REFERENCE_TRANSFORM_KEY = "extrinsic.camera_reference.T_Episode_CameraReference"

EGO_POSITION_NAMES = [
    "ego_position_x_m",
    "ego_position_y_m",
    "ego_position_z_m",
]
EGO_ROTATION_NAMES = [
    "ego_rotation_6d_r0c0",
    "ego_rotation_6d_r0c1",
    "ego_rotation_6d_r0c2",
    "ego_rotation_6d_r1c0",
    "ego_rotation_6d_r1c1",
    "ego_rotation_6d_r1c2",
]


def is_ego_embodiment(embodiment: str | None) -> bool:
    return str(embodiment or "").strip().lower() in EGO_EMBODIMENTS


def ego_action_metadata(episode_frame: str) -> dict[str, Any]:
    return {
        "schema": "reference_camera_ego_action",
        "intended_usage": "condition",
        "target_observation_offset_frames": 1,
        "target_pose_convention": "T_E_CameraReference(t+1)",
        "tail_policy": "repeat_previous_absolute_ego_action",
        "padded_tail_frames_per_episode": 1,
        "tail_pose_convention": "action.ego(L-1) = action.ego(L-2) = T_E_CameraReference(L-1)",
        "relative_anchor_frame": "camera_reference_at_chunk_anchor",
        "relative_pose_convention": "inverse(T_E_CameraReference(a)) @ T_E_CameraReference(a+k+1)",
        "source_pose_key": REFERENCE_TRANSFORM_KEY,
        "physical_frame": "camera_reference_optical_frame",
        "pose_storage_frame": episode_frame,
        "translation_unit": "meter",
        "axis_convention": "opencv_x_right_y_down_z_forward",
        "position_key": EGO_POSITION_KEY,
        "rotation_key": EGO_ROTATION_KEY,
        "rotation_6d_convention": "first_two_rotation_matrix_rows_flattened",
    }


def reference_camera_ego_values(camera: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Split T_E_C(t); never invert or re-anchor the camera to its current self."""
    camera = np.asarray(camera)
    if camera.ndim != 3 or camera.shape[1:] != (4, 4) or not np.isfinite(camera).all():
        raise ValueError("Ego source must be finite reference-camera poses [N,4,4].")
    rotation = camera[:, :3, :3].astype(np.float64)
    if (
        not np.allclose(camera[:, 3], [0, 0, 0, 1], atol=1e-6, rtol=0)
        or not np.allclose(rotation.transpose(0, 2, 1) @ rotation, np.eye(3), atol=1e-4, rtol=0)
        or not np.allclose(np.linalg.det(rotation), 1, atol=1e-4, rtol=0)
    ):
        raise ValueError("Ego source must be proper homogeneous SE(3) transforms.")
    return camera[:, :3, 3].copy(), camera[:, :2, :3].reshape(-1, 6).copy()


def build_shifted_ego_action(camera: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """action.ego[t] = T_cam[t+1]; last row repeats the previous absolute target."""
    position, rotation = reference_camera_ego_values(camera)
    if position.shape[0] < 2:
        raise ValueError("Ego action requires at least two camera observations.")
    return (
        np.concatenate((position[1:], position[-1:]), axis=0),
        np.concatenate((rotation[1:], rotation[-1:]), axis=0),
    )
