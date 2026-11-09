from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation


CANONICAL_DIM = 14

# Unified layout: left xyz(3) rpy(3) gripper(1) + right xyz(3) rpy(3) gripper(1)
LEFT_XYZ_SLICE = slice(0, 3)
LEFT_RPY_SLICE = slice(3, 6)
LEFT_GRIPPER_IDX = 6
RIGHT_XYZ_SLICE = slice(7, 10)
RIGHT_RPY_SLICE = slice(10, 13)
RIGHT_GRIPPER_IDX = 13
XYZ_INDICES = (0, 1, 2, 7, 8, 9)
RPY_INDICES = (3, 4, 5, 10, 11, 12)
GRIPPER_INDICES = (6, 13)
JOINT_INDICES = tuple(range(12))


def rot6d_to_matrix(rot6d: np.ndarray, layout: str = "columns") -> np.ndarray:
    """Convert 6D rotation to a rotation matrix.

    ``layout="columns"``: Zhou 6D (first two columns), the legacy source layout.
    ``layout="rows"``: the Dataclean contract (first two rows, EPISODE_CAMERA_GEOMETRY §3.3.3).
    """
    if layout not in ("columns", "rows"):
        raise ValueError(f"layout must be 'columns' or 'rows', got {layout!r}")
    x_raw = rot6d[..., 0:3]
    y_raw = rot6d[..., 3:6]
    x = x_raw / (np.linalg.norm(x_raw, axis=-1, keepdims=True) + 1e-8)
    z = np.cross(x, y_raw)
    z = z / (np.linalg.norm(z, axis=-1, keepdims=True) + 1e-8)
    y = np.cross(z, x)
    return np.stack([x, y, z], axis=-1 if layout == "columns" else -2)


def rot6d_to_euler(rot6d: np.ndarray, layout: str = "columns") -> np.ndarray:
    """(T, 6) rot6d -> (T, 3) extrinsic xyz euler radians."""
    matrix = rot6d_to_matrix(rot6d, layout=layout)
    return Rotation.from_matrix(matrix).as_euler("xyz", degrees=False)


def egodex_to_canonical(raw: np.ndarray, rot6d_layout: str = "columns") -> np.ndarray:
    """EgoDex 20-dim (xyz+rot6d+width x2) -> canonical 14-dim (xyz+rpy+gripper x2).

    Pass ``rot6d_layout="rows"`` for Dataclean standard fields (contract row 6D).
    """
    out = np.zeros((raw.shape[0], CANONICAL_DIM), dtype=np.float64)
    out[:, LEFT_XYZ_SLICE] = raw[:, 0:3]
    out[:, LEFT_RPY_SLICE] = rot6d_to_euler(raw[:, 3:9], layout=rot6d_layout)
    out[:, LEFT_GRIPPER_IDX] = raw[:, 9]
    out[:, RIGHT_XYZ_SLICE] = raw[:, 10:13]
    out[:, RIGHT_RPY_SLICE] = rot6d_to_euler(raw[:, 13:19], layout=rot6d_layout)
    out[:, RIGHT_GRIPPER_IDX] = raw[:, 19]
    return out


def humanoid_state_arrays(state: np.ndarray) -> np.ndarray:
    """Humanoid state: preserve full observation.state (e.g. 28-dim arm+gripper+EE)."""
    return state.astype(np.float64, copy=False)


def humanoid_state_from_raw(raw: dict[str, np.ndarray]) -> np.ndarray:
    """Build 28d humanoid state from merged or split parquet columns."""
    state = raw.get("observation.state")
    if state is not None and state.shape[1] >= 28:
        return state.astype(np.float64, copy=False)

    arm = raw.get("observation.state.arm.position")
    grip = raw.get("observation.state.effector.position")
    end = raw.get("observation.state.end.position")
    if arm is not None and grip is not None and end is not None:
        return np.concatenate([arm, grip, end], axis=1).astype(np.float64)

    if state is not None:
        return state.astype(np.float64, copy=False)
    raise ValueError("missing humanoid state columns")


def humanoid_action_arrays(action: np.ndarray) -> np.ndarray:
    """Humanoid action in quality space: 28d = arm12 + grip2 + eef14.

    Legacy packed teleop actions are 14d (arm+gripper only); pad EEF slots with
    zeros so Stage1/3 hard limits can use a stable layout. Pseudo-v3 / standard
    fields pack real ``action.eef.*``: Stage A quality is 28d xyz+quat; Stage B
    ``pack_standard_joint_eef_quality`` uses 32d xyz+rotation_6d.
    """
    action = np.asarray(action, dtype=np.float64)
    if action.ndim != 2:
        raise ValueError(f"humanoid action must be 2D, got {action.shape}")
    if action.shape[1] >= 28:
        return action[:, :28]
    out = np.zeros((action.shape[0], 28), dtype=np.float64)
    n = min(action.shape[1], 28)
    out[:, :n] = action[:, :n]
    return out


def _pad_ee_block(ee: np.ndarray, size: int = 7) -> np.ndarray:
    out = np.zeros((ee.shape[0], size), dtype=np.float64)
    out[:, 0] = ee[:, 0]
    return out


def robomind_ur_build_state(raw: dict[str, np.ndarray]) -> np.ndarray:
    """Puppet state 26D: left EE(7) + right EE(7) + left arm(6) + right arm(6)."""
    pl = raw["puppet/arm_left_position_align"]
    pr = raw["puppet/arm_right_position_align"]
    gle = raw["puppet/end_effector_left_position_align"]
    gre = raw["puppet/end_effector_right_position_align"]
    return np.concatenate([_pad_ee_block(gle), _pad_ee_block(gre), pl, pr], axis=1)


def robomind_ur_build_action(raw: dict[str, np.ndarray]) -> np.ndarray:
    """Master+puppet action 52D per Robomind modality.json."""
    ml = raw["master/arm_left_position_align"]
    mr = raw["master/arm_right_position_align"]
    mgl = raw["master/end_effector_left_position_align"]
    mgr = raw["master/end_effector_right_position_align"]
    pl = raw["puppet/arm_left_position_align"]
    pr = raw["puppet/arm_right_position_align"]
    pgl = raw["puppet/end_effector_left_position_align"]
    pgr = raw["puppet/end_effector_right_position_align"]
    master_ee = np.concatenate([_pad_ee_block(mgl), _pad_ee_block(mgr)], axis=1)
    master_arm = np.concatenate([ml, mr], axis=1)
    puppet_ee = np.concatenate([_pad_ee_block(pgl), _pad_ee_block(pgr)], axis=1)
    puppet_arm = np.concatenate([pl, pr], axis=1)
    return np.concatenate([master_ee, master_arm, puppet_ee, puppet_arm], axis=1)


def robomind_ur_compact_teleop(state: np.ndarray, action: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """14D teleop pair: grippers + dual 6-DoF arms (puppet state vs master command)."""
    s = np.concatenate([state[:, 0:1], state[:, 7:8], state[:, 14:20], state[:, 20:26]], axis=1)
    a = np.concatenate([action[:, 0:1], action[:, 7:8], action[:, 14:20], action[:, 20:26]], axis=1)
    return s, a


def robomind_ur_apply_compact_to_action(action: np.ndarray, compact: np.ndarray) -> np.ndarray:
    out = action.copy()
    out[:, 0:1] = compact[:, 0:1]
    out[:, 7:8] = compact[:, 1:2]
    out[:, 14:20] = compact[:, 2:8]
    out[:, 20:26] = compact[:, 8:14]
    return out


# Backward-compatible aliases
humanoid_state_to_canonical = humanoid_state_arrays
humanoid_action_to_canonical = humanoid_action_arrays
