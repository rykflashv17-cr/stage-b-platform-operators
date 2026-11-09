"""Shared SE(3) / pose / intrinsic helpers for episode-frame geometry (P6)."""
from __future__ import annotations

from typing import Any

import numpy as np
from scipy.spatial.transform import Rotation

from robot_data_processing.normalize.transforms_standard import (
    quat_wxyz_to_xyzw,
    quat_xyzw_to_wxyz,
)


def target_short_side_size(
    source_width: int,
    source_height: int,
    *,
    short_side: int = 384,
) -> tuple[int, int]:
    """Return (stored_width, stored_height) with short side = short_side, other dim even."""
    if source_width < 1 or source_height < 1:
        raise ValueError(f"Invalid source video size {source_width}x{source_height}.")
    if source_width >= source_height:
        target_height = int(short_side)
        target_width = 2 * round(source_width * target_height / source_height / 2)
    else:
        target_width = int(short_side)
        target_height = 2 * round(source_height * target_width / source_width / 2)
    return int(target_width), int(target_height)


def scale_intrinsics(
    matrix: np.ndarray,
    *,
    source_width: int,
    source_height: int,
    stored_width: int,
    stored_height: int,
) -> np.ndarray:
    """K_stored = diag(sx, sy, 1) @ K_source."""
    sx = stored_width / max(source_width, 1)
    sy = stored_height / max(source_height, 1)
    pixel_transform = np.diag([sx, sy, 1.0])
    return (pixel_transform @ np.asarray(matrix, dtype=np.float64)).astype(np.float64)


def pinhole_intrinsics_problem(matrix: Any, width: int, height: int) -> str | None:
    """Reason ``matrix`` is not a pinhole K for a ``width``x``height`` image, else None.

    Rejects transposed K (``[[fx,0,0],[0,fy,0],[cx,cy,1]]``), which has cx = cy = 0.
    """
    k = np.asarray(matrix, dtype=np.float64)
    if k.shape != (3, 3) or not np.isfinite(k).all():
        return f"K must be a finite 3x3 matrix, got shape {k.shape}"
    if not np.allclose(k[2], [0.0, 0.0, 1.0], atol=1e-9) or abs(k[1, 0]) > 1e-9:
        return f"K is not upper-triangular with last row [0,0,1]: {k.tolist()}"
    if not (k[0, 0] > 0 and k[1, 1] > 0):
        return f"K focal lengths must be positive: fx={k[0, 0]} fy={k[1, 1]}"
    if not (0 < k[0, 2] < width and 0 < k[1, 2] < height):
        return f"principal point ({k[0, 2]}, {k[1, 2]}) outside {width}x{height}"
    return None


def build_scaled_intrinsics_record(
    intrinsic: dict[str, Any],
    *,
    output_cameras: dict[str, str],
    source_calibration_size: tuple[int, int],
    stored_size: tuple[int, int],
    per_camera_sizes: dict[str, tuple[tuple[int, int], tuple[int, int]]] | None = None,
) -> dict[str, Any]:
    """Build episodes.jsonl camera_intrinsics block.

    ``per_camera_sizes`` maps output camera name → ``((src_w, src_h), (dst_w, dst_h))``
    when cameras do not share one calibration / stored resolution.
    """
    src_w, src_h = source_calibration_size
    dst_w, dst_h = stored_size
    pixel_transform = np.diag([dst_w / max(src_w, 1), dst_h / max(src_h, 1), 1.0])
    result: dict[str, Any] = {}
    for out_cam, src_prefix in output_cameras.items():
        mat_key = f"{src_prefix}.matrix"
        dist_key = f"{src_prefix}.dist_coeffs"
        if mat_key not in intrinsic:
            continue
        cam_src_w, cam_src_h = src_w, src_h
        cam_dst_w, cam_dst_h = dst_w, dst_h
        cam_transform = pixel_transform
        if per_camera_sizes and out_cam in per_camera_sizes:
            (cam_src_w, cam_src_h), (cam_dst_w, cam_dst_h) = per_camera_sizes[out_cam]
            cam_transform = np.diag(
                [cam_dst_w / max(cam_src_w, 1), cam_dst_h / max(cam_src_h, 1), 1.0]
            )
        source_matrix = np.asarray(intrinsic[mat_key], dtype=np.float64)
        stored_matrix = scale_intrinsics(
            source_matrix,
            source_width=cam_src_w,
            source_height=cam_src_h,
            stored_width=cam_dst_w,
            stored_height=cam_dst_h,
        )
        problem = pinhole_intrinsics_problem(stored_matrix, cam_dst_w, cam_dst_h)
        if problem:
            raise ValueError(f"Scaled intrinsics for {out_cam}: {problem}")
        record: dict[str, Any] = {
            "matrix": stored_matrix.tolist(),
            "source_calibration_image_size": {"width": cam_src_w, "height": cam_src_h},
            "stored_image_size": {"width": cam_dst_w, "height": cam_dst_h},
            "source_to_stored_pixel_transform": cam_transform.tolist(),
            "rectified": False,
        }
        # Unknown distortion is omitted, never zero-filled (contract §5.1).
        if intrinsic.get(dist_key) is not None:
            record["dist_coeffs"] = list(intrinsic[dist_key])
        result[out_cam] = record
    return result


def poses_wxyz_to_matrices(poses: np.ndarray, *, name: str = "pose") -> np.ndarray:
    poses = np.asarray(poses, dtype=np.float64)
    if poses.ndim != 2 or poses.shape[1] != 7 or not np.isfinite(poses).all():
        raise ValueError(f"{name} must be a finite Nx7 pose array.")
    quaternion = poses[:, 3:]
    norm = np.linalg.norm(quaternion, axis=1)
    if np.max(np.abs(norm - 1.0)) > 1e-2:
        raise ValueError(f"{name} contains non-unit quaternions.")
    matrices = np.zeros((len(poses), 4, 4), dtype=np.float64)
    matrices[:, 3, 3] = 1.0
    matrices[:, :3, 3] = poses[:, :3]
    matrices[:, :3, :3] = Rotation.from_quat(
        quat_wxyz_to_xyzw(quaternion / norm[:, None])
    ).as_matrix()
    return matrices


def matrices_to_poses_wxyz(matrices: np.ndarray, *, name: str = "pose") -> np.ndarray:
    matrices = np.asarray(matrices, dtype=np.float64)
    if matrices.ndim != 3 or matrices.shape[1:] != (4, 4):
        raise ValueError(f"{name} must be an Nx4x4 matrix array.")
    poses = np.empty((len(matrices), 7), dtype=np.float32)
    poses[:, :3] = matrices[:, :3, 3]
    quaternion = quat_xyzw_to_wxyz(Rotation.from_matrix(matrices[:, :3, :3]).as_quat())
    quaternion[quaternion[:, 0] < 0] *= -1
    poses[:, 3:] = quaternion
    return poses


def invert_se3(matrix: np.ndarray) -> np.ndarray:
    m = np.asarray(matrix, dtype=np.float64).reshape(4, 4)
    inv = np.eye(4, dtype=np.float64)
    r = m[:3, :3]
    inv[:3, :3] = r.T
    inv[:3, 3] = -r.T @ m[:3, 3]
    return inv


def compose_se3(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    return np.asarray(left, dtype=np.float64) @ np.asarray(right, dtype=np.float64)


def transform_poses_to_episode_frame(
    poses_wxyz: np.ndarray,
    T_episode_from_source: np.ndarray,
) -> np.ndarray:
    """Apply constant T_E_S to each pose: T_E_X = T_E_S @ T_S_X."""
    mats = poses_wxyz_to_matrices(poses_wxyz)
    t = np.asarray(T_episode_from_source, dtype=np.float64).reshape(4, 4)
    out = np.einsum("ij,njk->nik", t, mats)
    return matrices_to_poses_wxyz(out)
