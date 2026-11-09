"""P6 ParamsAndSpeed: fps/timestamp checks and ee speed vs golden."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class FpsCheckResult:
    ok: bool
    estimated_fps: float | None
    message: str


def check_timestamp_fps(
    timestamps: np.ndarray | None,
    *,
    expected_fps: float,
    rel_tol: float = 0.15,
) -> FpsCheckResult:
    if timestamps is None or len(timestamps) < 3:
        return FpsCheckResult(ok=True, estimated_fps=None, message="insufficient timestamps; skip")
    ts = np.asarray(timestamps, dtype=np.float64).reshape(-1)
    dts = np.diff(ts)
    dts = dts[dts > 0]
    if dts.size == 0:
        return FpsCheckResult(ok=False, estimated_fps=None, message="non-positive timestamp deltas")
    med = float(np.median(dts))
    if med <= 0:
        return FpsCheckResult(ok=False, estimated_fps=None, message="invalid median dt")
    est = 1.0 / med
    if abs(est - expected_fps) / max(expected_fps, 1e-6) > rel_tol:
        return FpsCheckResult(
            ok=False,
            estimated_fps=est,
            message=f"fps {est:.2f} outside ±{rel_tol*100:.0f}% of {expected_fps}",
        )
    return FpsCheckResult(ok=True, estimated_fps=est, message="ok")


@dataclass
class SpeedAlignResult:
    scale: float
    source_speed: float
    golden_speed: float
    message: str


def ee_pose_speed(pose_xyz: np.ndarray, fps: float) -> float:
    """Mean per-frame translation speed (m/s) of xyz trajectory (T, 3) or (T, 6+)."""
    xyz = np.asarray(pose_xyz, dtype=np.float64)[:, :3]
    if xyz.shape[0] < 2:
        return 0.0
    deltas = np.linalg.norm(np.diff(xyz, axis=0), axis=1)
    return float(np.mean(deltas) * fps)


def compute_speed_scale(
    source_pose_xyz: np.ndarray,
    source_fps: float,
    *,
    golden_speed_mps: float | None = None,
    golden_pose_xyz: np.ndarray | None = None,
    golden_fps: float = 30.0,
    target_fps: float = 30.0,
) -> SpeedAlignResult:
    """
    Compare ee pose speed (scaled to target_fps) against golden.
    scale > 1 ⇒ source slower than golden (suggest speed-up / drop frames conceptually).
    """
    src_speed = ee_pose_speed(source_pose_xyz, source_fps)
    # Normalize to target_fps equivalent displacement rate
    src_at_target = src_speed * (target_fps / max(source_fps, 1e-6)) * (source_fps / target_fps)
    # Simpler: convert mean delta to 30fps equivalent speed
    src_equiv = ee_pose_speed(source_pose_xyz, target_fps)

    if golden_speed_mps is not None:
        golden = float(golden_speed_mps)
    elif golden_pose_xyz is not None:
        golden = ee_pose_speed(golden_pose_xyz, golden_fps)
        golden = ee_pose_speed(golden_pose_xyz, target_fps)
    else:
        return SpeedAlignResult(
            scale=1.0,
            source_speed=src_equiv,
            golden_speed=0.0,
            message="no golden; scale=1",
        )

    if golden < 1e-8:
        scale = 1.0
    else:
        scale = float(src_equiv / golden)
    return SpeedAlignResult(
        scale=scale,
        source_speed=src_equiv,
        golden_speed=golden,
        message=f"speed_scale={scale:.4f} (source_equiv={src_equiv:.4f}, golden={golden:.4f})",
    )


# Placeholder humanoid golden mean ee translation speed @30fps (m/s), tunable.
DEFAULT_HUMANOID_GOLDEN_EE_SPEED = 0.08


def params_meta_from_speed(result: SpeedAlignResult) -> dict[str, Any]:
    return {
        "speed_align_scale": result.scale,
        "speed_align_source_mps": result.source_speed,
        "speed_align_golden_mps": result.golden_speed,
        "speed_align_message": result.message,
    }
