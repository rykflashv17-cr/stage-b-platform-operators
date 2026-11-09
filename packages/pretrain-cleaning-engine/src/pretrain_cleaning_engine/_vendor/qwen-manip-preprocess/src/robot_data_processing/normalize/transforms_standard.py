"""Geometry helpers for Dataclean standard fields (quaternion / rotvec)."""
from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation


def quat_xyzw_to_rotvec(quat_xyzw: np.ndarray) -> np.ndarray:
    """(T, 4) xyzw → (T, 3) rotvec."""
    q = np.asarray(quat_xyzw, dtype=np.float64)
    return Rotation.from_quat(q).as_rotvec()


def quat_xyzw_to_wxyz(quat_xyzw: np.ndarray) -> np.ndarray:
    """(T, 4) xyzw → (T, 4) wxyz."""
    q = np.asarray(quat_xyzw, dtype=np.float64)
    return np.concatenate([q[:, 3:4], q[:, 0:3]], axis=1)


def quat_wxyz_to_xyzw(quat_wxyz: np.ndarray) -> np.ndarray:
    """(T, 4) wxyz → (T, 4) xyzw."""
    q = np.asarray(quat_wxyz, dtype=np.float64)
    return np.concatenate([q[:, 1:4], q[:, 0:1]], axis=1)


def xyz_quat_xyzw_to_pose7(block7: np.ndarray) -> np.ndarray:
    """(T, 7) xyz+quat_xyzw → (T, 7) xyz+quat_wxyz."""
    xyz = block7[:, 0:3]
    wxyz = quat_xyzw_to_wxyz(block7[:, 3:7])
    return np.concatenate([xyz, wxyz], axis=1).astype(np.float32)


def rot6d_to_rotvec(rot6d: np.ndarray) -> np.ndarray:
    """(T, 6) → (T, 3) rotvec."""
    x_raw = rot6d[..., 0:3]
    y_raw = rot6d[..., 3:6]
    x = x_raw / (np.linalg.norm(x_raw, axis=-1, keepdims=True) + 1e-8)
    z = np.cross(x, y_raw)
    z = z / (np.linalg.norm(z, axis=-1, keepdims=True) + 1e-8)
    y = np.cross(z, x)
    mat = np.stack([x, y, z], axis=-1)
    return Rotation.from_matrix(mat).as_rotvec().astype(np.float32)


def rot6d_to_quat_wxyz(rot6d: np.ndarray) -> np.ndarray:
    """(T, 6) → (T, 4) quaternion wxyz."""
    x_raw = rot6d[..., 0:3]
    y_raw = rot6d[..., 3:6]
    x = x_raw / (np.linalg.norm(x_raw, axis=-1, keepdims=True) + 1e-8)
    z = np.cross(x, y_raw)
    z = z / (np.linalg.norm(z, axis=-1, keepdims=True) + 1e-8)
    y = np.cross(z, x)
    mat = np.stack([x, y, z], axis=-1)
    quat_xyzw = Rotation.from_matrix(mat).as_quat()
    return quat_xyzw_to_wxyz(quat_xyzw).astype(np.float32)


def xyz_rot6d_to_pose7(xyz: np.ndarray, rot6d: np.ndarray) -> np.ndarray:
    """(T, 3) xyz + (T, 6) rot6d → (T, 7) xyz+quat_wxyz."""
    wxyz = rot6d_to_quat_wxyz(rot6d)
    return np.concatenate([xyz, wxyz], axis=1).astype(np.float32)


def rotation_6d_to_matrices(values: np.ndarray, *, name: str = "rotation_6d") -> np.ndarray:
    """(N, 6) raw Rotation 6D → (N, 3, 3) rotation matrices (first two rows flattened)."""
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 6 or not np.isfinite(values).all():
        raise ValueError(f"{name} must have finite shape [N,6], got {values.shape}.")
    rows = values.reshape(-1, 2, 3)
    first = rows[:, 0]
    first_norm = np.linalg.norm(first, axis=1, keepdims=True)
    if np.any(first_norm <= 1e-8):
        raise ValueError(f"{name} contains a zero first row.")
    first = first / first_norm
    second = rows[:, 1] - np.sum(first * rows[:, 1], axis=1, keepdims=True) * first
    second_norm = np.linalg.norm(second, axis=1, keepdims=True)
    if np.any(second_norm <= 1e-8):
        raise ValueError(f"{name} contains collinear Rotation 6D rows.")
    second = second / second_norm
    return np.stack([first, second, np.cross(first, second)], axis=1)


def matrices_to_rotation_6d(matrices: np.ndarray, *, name: str = "matrices") -> np.ndarray:
    """(N, 4, 4) or (N, 3, 3) → (N, 6) raw Rotation 6D (first two matrix rows flattened)."""
    matrices = np.asarray(matrices, dtype=np.float64)
    if matrices.ndim == 3 and matrices.shape[1:] == (4, 4):
        rot = matrices[:, :3, :3]
    elif matrices.ndim == 3 and matrices.shape[1:] == (3, 3):
        rot = matrices
    else:
        raise ValueError(f"{name} must be Nx4x4 or Nx3x3, got {matrices.shape}.")
    if not np.isfinite(rot).all():
        raise ValueError(f"{name} must be finite.")
    return rot[:, :2, :].reshape(len(rot), 6).astype(np.float32)


def column_rotation_6d_to_matrices(values: np.ndarray, *, name: str = "rotation_6d") -> np.ndarray:
    """(N, 6) Zhou 6D (first two COLUMNS flattened) → (N, 3, 3).

    Some sources (EgoDex / EgoVerse converters) store ``[R00,R10,R20,R01,R11,R21]``;
    decoding them as rows would silently transpose R (REPORT EGD-01 / EGO-01).
    """
    return np.transpose(rotation_6d_to_matrices(values, name=name), (0, 2, 1))


def reencode_column_rotation_6d(
    values: np.ndarray,
    R_source_canonical: np.ndarray,
    *,
    name: str = "rotation_6d",
) -> np.ndarray:
    """Source column 6D → contract row 6D of ``R_E_U = R_E_F @ C`` (positions unchanged)."""
    c = np.asarray(R_source_canonical, dtype=np.float64)
    if c.shape != (3, 3) or not np.allclose(c.T @ c, np.eye(3), atol=1e-6) or not np.isclose(np.linalg.det(c), 1.0):
        raise ValueError(f"{name}: R_source_canonical must be a proper 3x3 rotation.")
    physical = column_rotation_6d_to_matrices(values, name=name)
    return matrices_to_rotation_6d(physical @ c, name=name)


def position_rotation_6d_to_matrices(
    position: np.ndarray,
    rotation_6d: np.ndarray,
    *,
    name: str = "eef",
) -> np.ndarray:
    """(N, 3) position + (N, 6) rotation_6d → (N, 4, 4) SE(3) matrices."""
    position = np.asarray(position, dtype=np.float64)
    rot_mats = rotation_6d_to_matrices(rotation_6d, name=f"{name}.rotation_6d")
    n = len(position)
    if rot_mats.shape[0] != n:
        raise ValueError(f"{name} position/rotation_6d length mismatch: {n} vs {rot_mats.shape[0]}.")
    matrices = np.zeros((n, 4, 4), dtype=np.float64)
    matrices[:, 3, 3] = 1.0
    matrices[:, :3, 3] = position
    matrices[:, :3, :3] = rot_mats
    return matrices


def matrices_to_position_rotation_6d(
    matrices: np.ndarray,
    *,
    name: str = "eef",
) -> tuple[np.ndarray, np.ndarray]:
    """(N, 4, 4) SE(3) → ((N, 3) position, (N, 6) rotation_6d)."""
    matrices = np.asarray(matrices, dtype=np.float64)
    if matrices.ndim != 3 or matrices.shape[1:] != (4, 4):
        raise ValueError(f"{name} must be Nx4x4, got {matrices.shape}.")
    position = matrices[:, :3, 3].astype(np.float32)
    rotation_6d = matrices_to_rotation_6d(matrices, name=name)
    return position, rotation_6d


def xyz_quat_xyzw_to_position_rotation_6d(block7: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(T, 7) xyz+quat_xyzw → ((T, 3) position, (T, 6) rotation_6d)."""
    block7 = np.asarray(block7, dtype=np.float64)
    xyz = block7[:, 0:3]
    rot_mats = Rotation.from_quat(block7[:, 3:7]).as_matrix()
    n = len(block7)
    mats = np.zeros((n, 4, 4), dtype=np.float64)
    mats[:, 3, 3] = 1.0
    mats[:, :3, 3] = xyz
    mats[:, :3, :3] = rot_mats
    return xyz.astype(np.float32), matrices_to_rotation_6d(mats, name="eef")


def as_col(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        return x.reshape(-1, 1)
    return x
