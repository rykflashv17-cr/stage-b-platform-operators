from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from robot_data_processing.schema import DatasetSchema, stack_column
from robot_data_processing.transforms import (
    CANONICAL_DIM,
    LEFT_GRIPPER_IDX,
    LEFT_RPY_SLICE,
    LEFT_XYZ_SLICE,
    RIGHT_GRIPPER_IDX,
    RIGHT_RPY_SLICE,
    RIGHT_XYZ_SLICE,
    egodex_to_canonical,
)


@dataclass
class Stage5Config:
    enabled: bool = False  # default off: eef frame/direction is human-reviewed, not transformed in-pipeline
    reference_frame: str = "camera_top_frame0"
    rotation_correction_euler_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
    egodex_extrinsics_column: str = "observation.camera_extrinsics_world"
    humanoid_calibration_relpath: str = (
        "parameters/chunk-{chunk:03d}/episode_{episode:06d}/calibration_bundle_optimized.json"
    )
    humanoid_camera_extrinsic_key: str = "camera_front_to_arm_left"


@dataclass
class Stage5Result:
    aligned_state: np.ndarray
    aligned_action: np.ndarray
    reference_origin_xyz: np.ndarray
    reference_forward_xyz: np.ndarray
    applied: bool


def _rotation_correction_matrix(euler_xyz: tuple[float, float, float]) -> np.ndarray:
    if euler_xyz == (0.0, 0.0, 0.0):
        return np.eye(3, dtype=np.float64)
    return Rotation.from_euler("xyz", euler_xyz, degrees=False).as_matrix()


def _matrix_from_xyz_quat(xyz: np.ndarray, quat_xyzw: np.ndarray) -> np.ndarray:
    mat = np.eye(4, dtype=np.float64)
    mat[:3, :3] = Rotation.from_quat(quat_xyzw).as_matrix()
    mat[:3, 3] = xyz
    return mat


def _matrix_from_xyz_rpy(xyz: np.ndarray, rpy: np.ndarray) -> np.ndarray:
    mat = np.eye(4, dtype=np.float64)
    mat[:3, :3] = Rotation.from_euler("xyz", rpy, degrees=False).as_matrix()
    mat[:3, 3] = xyz
    return mat


def _decompose_xyz_rpy(mat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    xyz = mat[:3, 3].copy()
    rpy = Rotation.from_matrix(mat[:3, :3]).as_euler("xyz", degrees=False)
    return xyz, rpy


def _transform_hand_pose(
    xyz: np.ndarray,
    orient: np.ndarray,
    T_ref: np.ndarray,
    R_corr: np.ndarray,
    *,
    orient_is_quat: bool,
) -> tuple[np.ndarray, np.ndarray]:
    if orient_is_quat:
        T_world = _matrix_from_xyz_quat(xyz, orient)
    else:
        T_world = _matrix_from_xyz_rpy(xyz, orient)
    T_aligned = T_ref @ T_world
    R_a = R_corr @ T_aligned[:3, :3]
    xyz_a = R_corr @ T_aligned[:3, 3]
    rpy_a = Rotation.from_matrix(R_a).as_euler("xyz", degrees=False)
    return xyz_a, rpy_a


def _apply_pose_transform(
    pose: np.ndarray,
    T_ref: np.ndarray,
    R_corr: np.ndarray,
) -> np.ndarray:
    out = np.zeros_like(pose)
    out[:, LEFT_GRIPPER_IDX] = pose[:, LEFT_GRIPPER_IDX]
    out[:, RIGHT_GRIPPER_IDX] = pose[:, RIGHT_GRIPPER_IDX]
    for xyz_sl, rpy_sl in ((LEFT_XYZ_SLICE, LEFT_RPY_SLICE), (RIGHT_XYZ_SLICE, RIGHT_RPY_SLICE)):
        xyz = pose[:, xyz_sl]
        orient = pose[:, rpy_sl]
        num_frames = pose.shape[0]
        xyz_a = np.zeros((num_frames, 3), dtype=np.float64)
        rpy_a = np.zeros((num_frames, 3), dtype=np.float64)
        for t in range(num_frames):
            xyz_a[t], rpy_a[t] = _transform_hand_pose(
                xyz[t],
                orient[t],
                T_ref,
                R_corr,
                orient_is_quat=False,
            )
        out[:, xyz_sl] = xyz_a
        out[:, rpy_sl] = rpy_a
    return out


def _reference_forward_xyz(T_ref: np.ndarray, R_corr: np.ndarray) -> np.ndarray:
    basis = R_corr @ T_ref[:3, :3] @ np.array([1.0, 0.0, 0.0], dtype=np.float64)
    norm = np.linalg.norm(basis)
    return basis / norm if norm > 1e-8 else basis


def humanoid_end_to_pose_gripper(end_position: np.ndarray, effector: np.ndarray) -> np.ndarray:
    """Convert humanoid EE (xyz+quat x2) + gripper to canonical 14-dim pose."""
    num_frames = end_position.shape[0]
    out = np.zeros((num_frames, CANONICAL_DIM), dtype=np.float64)
    out[:, LEFT_XYZ_SLICE] = end_position[:, 0:3]
    out[:, LEFT_RPY_SLICE] = Rotation.from_quat(end_position[:, 3:7]).as_euler("xyz", degrees=False)
    out[:, LEFT_GRIPPER_IDX] = effector[:, 0]
    out[:, RIGHT_XYZ_SLICE] = end_position[:, 7:10]
    out[:, RIGHT_RPY_SLICE] = Rotation.from_quat(end_position[:, 10:14]).as_euler("xyz", degrees=False)
    out[:, RIGHT_GRIPPER_IDX] = effector[:, 1]
    return out


def _load_humanoid_T_world_cam(dataset_root: Path, episode_index: int, cfg: Stage5Config) -> np.ndarray:
    chunk = episode_index // 1000
    rel = cfg.humanoid_calibration_relpath.format(chunk=chunk, episode=episode_index)
    cal_path = dataset_root / rel
    if not cal_path.exists():
        raise FileNotFoundError(f"Missing humanoid calibration: {cal_path}")
    cal = json.loads(cal_path.read_text(encoding="utf-8"))
    key = cfg.humanoid_camera_extrinsic_key
    if key not in cal["extrinsics"]:
        raise KeyError(f"Calibration missing extrinsic key: {key}")
    # T_cam_arm: maps arm/base points into camera frame; invert for camera pose in arm/base frame.
    T_cam_arm = np.asarray(cal["extrinsics"][key]["matrix"], dtype=np.float64)
    return np.linalg.inv(T_cam_arm)


def _egodex_world_poses(
    raw_state: np.ndarray,
    raw_action: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    return egodex_to_canonical(raw_state), egodex_to_canonical(raw_action)


def _humanoid_world_poses(raw: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    end_pos = raw["observation.state.end.position"]
    effector = raw["observation.state.effector.position"]
    state_pose = humanoid_end_to_pose_gripper(end_pos, effector)
    action_pose = state_pose.copy()
    return state_pose, action_pose


def _T_ref_from_camera_top_frame0(T_world_cam: np.ndarray) -> np.ndarray:
    """Express poses in the coordinate system of camera_top at frame 0 (origin + orientation)."""
    return np.linalg.inv(T_world_cam)


def run_stage5(
    dataset_root: Path,
    episode_index: int,
    schema: DatasetSchema,
    raw: dict[str, np.ndarray],
    cfg: Stage5Config,
) -> Stage5Result:
    if not cfg.enabled:
        raise ValueError("Stage5 disabled; nothing to align")

    R_corr = _rotation_correction_matrix(cfg.rotation_correction_euler_xyz)

    if schema.embodiment == "egodex":
        ext_col = cfg.egodex_extrinsics_column
        if ext_col not in raw:
            raise KeyError(f"EgoDex stage5 requires column: {ext_col}")
        extrinsics = raw[ext_col]
        state_world, action_world = _egodex_world_poses(
            raw[schema.state_column],
            raw[schema.action_column],
        )
        T_world_cam0 = extrinsics[0].reshape(4, 4)
        T_ref = _T_ref_from_camera_top_frame0(T_world_cam0)
    elif schema.embodiment == "humanoid":
        state_world, action_world = _humanoid_world_poses(raw)
        T_world_cam0 = _load_humanoid_T_world_cam(dataset_root, episode_index, cfg)
        T_ref = _T_ref_from_camera_top_frame0(T_world_cam0)
    else:
        raise ValueError(f"Unsupported embodiment for stage5: {schema.embodiment}")

    aligned_state = _apply_pose_transform(state_world, T_ref, R_corr)
    aligned_action = _apply_pose_transform(action_world, T_ref, R_corr)

    return Stage5Result(
        aligned_state=aligned_state,
        aligned_action=aligned_action,
        reference_origin_xyz=np.zeros(3, dtype=np.float64),
        reference_forward_xyz=_reference_forward_xyz(T_ref, R_corr),
        applied=True,
    )


def read_stage5_raw_context(
    path: Path,
    schema: DatasetSchema,
    cfg: Stage5Config,
) -> dict[str, np.ndarray]:
    import pyarrow.parquet as pq

    columns = list(schema.raw_columns)
    if schema.embodiment == "egodex" and cfg.egodex_extrinsics_column not in columns:
        columns = [*columns, cfg.egodex_extrinsics_column]
    table = pq.read_table(path, columns=columns)
    raw: dict[str, np.ndarray] = {}
    for name in columns:
        if name not in table.column_names:
            continue
        col = table.column(name).combine_chunks()
        raw[name] = stack_column(col.to_numpy(zero_copy_only=False))
    return raw
