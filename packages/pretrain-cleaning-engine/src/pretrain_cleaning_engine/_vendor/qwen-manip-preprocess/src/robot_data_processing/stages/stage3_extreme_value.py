from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from robot_data_processing.schema import DatasetSchema, HUMANOID_SCHEMA
from robot_data_processing.types import GlobalStats


@dataclass
class Stage3Config:
    alpha: float = 0.15
    gripper_state_indices: tuple[int, ...] = (12, 13)
    gripper_action_indices: tuple[int, ...] = (12, 13)
    rpy_state_indices: tuple[int, ...] = ()
    rpy_action_indices: tuple[int, ...] = ()
    joint_limits: tuple[float, float] = (-3.5, 3.5)
    # Per-joint [lo, hi] (joint_indices order) overriding ``joint_limits`` (stage1.joint_limit_bounds).
    joint_limits_per_joint: tuple[tuple[float, float], ...] | None = None
    joint_limit_tolerance: float = 0.0
    ee_xyz_limits: tuple[float, float] = (-2.0, 2.0)
    rpy_limits: tuple[float, float] = (-3.15, 3.15)
    gripper_limits: tuple[float, float] = (-0.01, 1.0)
    min_episode_length: int = 120


@dataclass
class Stage3Result:
    discard: bool
    discard_reasons: list[str]
    remove_frames: np.ndarray
    excluded_count: int


def _check_hard_limits(
    state: np.ndarray,
    action: np.ndarray,
    cfg: Stage3Config,
    schema: DatasetSchema,
) -> np.ndarray:
    num_frames = state.shape[0]
    bad = np.zeros(num_frames, dtype=bool)

    if schema.layout == "joint_gripper":
        from robot_data_processing.stages.stage1_sudden_change import joint_limit_bounds

        joints = list(schema.joint_indices)
        action_joints = list(schema.action_joint_indices)
        for arr, idx in ((state, joints), (action, action_joints)):
            if idx:
                j_lo, j_hi = joint_limit_bounds(
                    cfg.joint_limits_per_joint, len(idx), cfg.joint_limit_tolerance, cfg.joint_limits
                )
                bad |= np.any((arr[:, idx] < j_lo) | (arr[:, idx] > j_hi), axis=1)
        e_lo, e_hi = cfg.ee_xyz_limits
        if state.shape[1] >= 32:
            from robot_data_processing.schema import (
                QUALITY_EEF_LEFT_XYZ_IDX,
                QUALITY_EEF_RIGHT_XYZ_IDX,
                QUALITY_EEF_ROT6D_ABS_MAX,
                QUALITY_EEF_ROT6D_IDX,
            )

            ee_xyz_idx = QUALITY_EEF_LEFT_XYZ_IDX + QUALITY_EEF_RIGHT_XYZ_IDX
            rot_idx = QUALITY_EEF_ROT6D_IDX
            bad |= np.any((state[:, ee_xyz_idx] < e_lo) | (state[:, ee_xyz_idx] > e_hi), axis=1)
            bad |= np.any(
                (state[:, rot_idx] < -QUALITY_EEF_ROT6D_ABS_MAX)
                | (state[:, rot_idx] > QUALITY_EEF_ROT6D_ABS_MAX),
                axis=1,
            )
            if action.shape[1] >= 32:
                bad |= np.any((action[:, ee_xyz_idx] < e_lo) | (action[:, ee_xyz_idx] > e_hi), axis=1)
                bad |= np.any(
                    (action[:, rot_idx] < -QUALITY_EEF_ROT6D_ABS_MAX)
                    | (action[:, rot_idx] > QUALITY_EEF_ROT6D_ABS_MAX),
                    axis=1,
                )
        elif state.shape[1] > 14 and schema.embodiment == "humanoid":
            ee_xyz_idx = (14, 15, 16, 21, 22, 23)
            quat_idx = list(range(17, 21)) + list(range(24, 28))
            bad |= np.any((state[:, ee_xyz_idx] < e_lo) | (state[:, ee_xyz_idx] > e_hi), axis=1)
            bad |= np.any((state[:, quat_idx] < -1.01) | (state[:, quat_idx] > 1.01), axis=1)
            if action.shape[1] > 14:
                bad |= np.any((action[:, ee_xyz_idx] < e_lo) | (action[:, ee_xyz_idx] > e_hi), axis=1)
                if action.shape[1] >= 28:
                    bad |= np.any((action[:, quat_idx] < -1.01) | (action[:, quat_idx] > 1.01), axis=1)
    else:
        xyz = list(schema.xyz_indices)
        if xyz:
            e_lo, e_hi = cfg.ee_xyz_limits
            bad |= np.any((state[:, xyz] < e_lo) | (state[:, xyz] > e_hi), axis=1)
        rpy = list(schema.rpy_indices)
        if rpy:
            r_lo, r_hi = cfg.rpy_limits
            bad |= np.any((state[:, rpy] < r_lo) | (state[:, rpy] > r_hi), axis=1)

    g_lo, g_hi = cfg.gripper_limits
    for gi in schema.gripper_indices:
        bad |= (state[:, gi] < g_lo) | (state[:, gi] > g_hi)
    for gi in schema.action_gripper_indices:
        bad |= (action[:, gi] < g_lo) | (action[:, gi] > g_hi)

    return bad


def _check_percentile_band(
    values: np.ndarray,
    q01: np.ndarray,
    q99: np.ndarray,
    alpha: float,
    exempt: set[int],
    startup_exclude_per_joint: np.ndarray | None = None,
    align_dims: int | None = None,
) -> np.ndarray:
    lo = q01 - alpha * (q99 - q01)
    hi = q99 + alpha * (q99 - q01)
    bad = np.zeros(values.shape[0], dtype=bool)
    for d in range(values.shape[1]):
        if d in exempt:
            continue
        bad_d = (values[:, d] < lo[d]) | (values[:, d] > hi[d])
        if startup_exclude_per_joint is not None and align_dims is not None and d < align_dims:
            if d < startup_exclude_per_joint.shape[1]:
                bad_d[startup_exclude_per_joint[:, d]] = False
        bad |= bad_d
    return bad


def run_stage3(
    state: np.ndarray,
    action: np.ndarray,
    stats: GlobalStats,
    cfg: Stage3Config,
    schema: DatasetSchema = HUMANOID_SCHEMA,
    startup_exclude_per_joint: np.ndarray | None = None,
) -> Stage3Result:
    num_frames = state.shape[0]
    hard_bad = _check_hard_limits(state, action, cfg, schema)

    state_exempt = set(cfg.gripper_state_indices) | set(cfg.rpy_state_indices)
    action_exempt = set(cfg.gripper_action_indices) | set(cfg.rpy_action_indices)

    align_dims = min(len(schema.action_joint_indices), action.shape[1]) if schema.layout == "joint_gripper" else state.shape[1]
    percentile_bad = _check_percentile_band(
        state,
        stats.state_q01,
        stats.state_q99,
        cfg.alpha,
        state_exempt,
        startup_exclude_per_joint=startup_exclude_per_joint,
        align_dims=align_dims,
    )
    percentile_bad |= _check_percentile_band(
        action,
        stats.action_q01,
        stats.action_q99,
        cfg.alpha,
        action_exempt,
        startup_exclude_per_joint=startup_exclude_per_joint,
        align_dims=align_dims,
    )

    remove = hard_bad | percentile_bad

    kept = num_frames - int(remove.sum())
    discard = kept < cfg.min_episode_length
    reasons: list[str] = []
    if discard:
        reasons.append(f"remaining_frames={kept}<{cfg.min_episode_length}")

    return Stage3Result(
        discard=discard,
        discard_reasons=reasons,
        remove_frames=remove,
        excluded_count=int(remove.sum()),
    )
