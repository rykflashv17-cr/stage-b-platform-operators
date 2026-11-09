from __future__ import annotations

import numpy as np


def build_step_validity_mask(num_frames: int, abnormal_frames: np.ndarray) -> np.ndarray:
    """Build prefix validity mask: 1 before first abnormal frame, 0 from first abnormal onward."""
    mask = np.ones(num_frames, dtype=np.int8)
    if abnormal_frames.size == 0 or not abnormal_frames.any():
        return mask
    first_bad = int(np.flatnonzero(abnormal_frames)[0])
    mask[first_bad:] = 0
    return mask


def build_frame_keep_mask(
    num_frames: int,
    abnormal_frames: np.ndarray,
    remove_frames: np.ndarray | None = None,
) -> np.ndarray:
    """Per-frame keep mask (1=keep, 0=drop): prefix truncate then optional frame removals."""
    keep = build_step_validity_mask(num_frames, abnormal_frames)
    if remove_frames is not None and remove_frames.any():
        keep = keep & (~remove_frames.astype(np.int8))
    return keep


def keep_indices_from_mask(step_validity_mask: np.ndarray) -> np.ndarray:
    """Return sorted frame indices where mask == 1."""
    return np.flatnonzero(step_validity_mask.astype(np.int8) > 0).astype(np.int64)


def compute_per_joint_action_zero_exclude(
    action_arm: np.ndarray,
    epsilon: float = 1e-4,
) -> np.ndarray:
    """Per-joint startup exclude mask (T, D).

    True = frame still before this joint's action first leaves zero.
    Joints that never leave zero are excluded for all frames on that dim only.
    """
    num_frames, num_dims = action_arm.shape
    exclude = np.zeros((num_frames, num_dims), dtype=bool)
    for d in range(num_dims):
        nz = np.flatnonzero(np.abs(action_arm[:, d]) > epsilon)
        if nz.size:
            exclude[: int(nz[0]), d] = True
        else:
            exclude[:, d] = True
    return exclude


def compute_per_joint_stage1_exclude(
    action_arm: np.ndarray,
    epsilon: float = 1e-4,
    grace_frames: int = 0,
) -> np.ndarray:
    """Per-joint Stage1 exclude mask (T, D): zero segment + post-zero grace per joint."""
    exclude = compute_per_joint_action_zero_exclude(action_arm, epsilon)
    if grace_frames <= 0:
        return exclude
    num_frames = action_arm.shape[0]
    for d in range(action_arm.shape[1]):
        nz = np.flatnonzero(np.abs(action_arm[:, d]) > epsilon)
        if not nz.size:
            continue
        end = min(int(nz[0]) + grace_frames, num_frames)
        exclude[int(nz[0]) : end, d] = True
    return exclude


def apply_per_dim_exclude(flags: np.ndarray, exclude: np.ndarray) -> None:
    """Clear detection flags where per-dimension startup exclude is active."""
    if exclude.shape != flags.shape:
        raise ValueError(f"exclude shape {exclude.shape} != flags shape {flags.shape}")
    flags[exclude] = False
