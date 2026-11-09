from __future__ import annotations

import numpy as np
from scipy import signal
from scipy.ndimage import median_filter

from robot_data_processing.schema import mad


def smooth_1d(
    x: np.ndarray,
    median_kernel: int = 5,
    savgol_window: int = 11,
    savgol_polyorder: int = 3,
) -> np.ndarray:
    if x.size < savgol_window:
        return x.copy()
    x_med = median_filter(x, size=median_kernel, mode="nearest")
    return signal.savgol_filter(
        x_med,
        window_length=savgol_window,
        polyorder=savgol_polyorder,
        mode="nearest",
    )


def smooth_2d(
    x: np.ndarray,
    median_kernel: int = 5,
    savgol_window: int = 11,
    savgol_polyorder: int = 3,
) -> np.ndarray:
    """Smooth (T, D) along time axis."""
    if x.shape[0] < savgol_window:
        return x.copy()
    x_med = median_filter(x, size=(median_kernel, 1), mode="nearest")
    return signal.savgol_filter(
        x_med,
        window_length=savgol_window,
        polyorder=savgol_polyorder,
        axis=0,
        mode="nearest",
    )


def compute_derivatives(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    accel = np.gradient(np.gradient(x, axis=0), axis=0)
    jerk = np.gradient(accel, axis=0)
    return accel, jerk


def hybrid_threshold(
    values: np.ndarray,
    k: float,
    percentile: float,
    std_floor_ratio: float = 0.01,
) -> float:
    """MAD threshold with percentile and std floor to avoid degenerate MAD≈0."""
    p_thr = float(np.percentile(values, percentile))
    m = mad(values)
    s = float(np.std(values))
    scale = max(m, std_floor_ratio * s)
    return max(k * scale, p_thr, 1e-12)


def detect_sudden_changes_1d(
    x: np.ndarray,
    k_residual: float = 8.0,
    k_accel: float = 8.0,
    k_jerk: float = 8.0,
    percentile_floor: float = 99.9,
    median_kernel: int = 5,
    savgol_window: int = 11,
    savgol_polyorder: int = 3,
) -> np.ndarray:
    if x.shape[0] < savgol_window:
        return np.zeros(x.shape[0], dtype=bool)

    smooth = smooth_1d(x, median_kernel, savgol_window, savgol_polyorder)
    residual = np.abs(x - smooth)
    accel, jerk = compute_derivatives(x)

    thr_r = hybrid_threshold(residual, k_residual, percentile_floor)
    accel_pct = min(percentile_floor, 99.95)
    thr_a = hybrid_threshold(np.abs(accel), k_accel, accel_pct)
    thr_j = hybrid_threshold(np.abs(jerk), k_jerk, accel_pct)

    return (residual > thr_r) & ((np.abs(accel) > thr_a) | (np.abs(jerk) > thr_j))


def detect_sudden_changes_1d_with_thresholds(
    x: np.ndarray,
    thr_r: float,
    thr_a: float,
    thr_j: float,
    median_kernel: int = 5,
    savgol_window: int = 11,
    savgol_polyorder: int = 3,
) -> np.ndarray:
    if x.shape[0] < savgol_window:
        return np.zeros(x.shape[0], dtype=bool)

    smooth = smooth_1d(x, median_kernel, savgol_window, savgol_polyorder)
    residual = np.abs(x - smooth)
    accel, jerk = compute_derivatives(x)
    return (residual > thr_r) & ((np.abs(accel) > thr_a) | (np.abs(jerk) > thr_j))


def detect_sudden_changes_2d_with_thresholds(
    x: np.ndarray,
    thr_r: np.ndarray,
    thr_a: np.ndarray,
    thr_j: np.ndarray,
    channel_offset: int = 0,
    median_kernel: int = 5,
    savgol_window: int = 11,
    savgol_polyorder: int = 3,
) -> np.ndarray:
    """Return (T, D) bool flags using global per-channel thresholds."""
    flags = np.zeros_like(x, dtype=bool)
    for d in range(x.shape[1]):
        ch = channel_offset + d
        flags[:, d] = detect_sudden_changes_1d_with_thresholds(
            x[:, d],
            float(thr_r[ch]),
            float(thr_a[ch]),
            float(thr_j[ch]),
            median_kernel=median_kernel,
            savgol_window=savgol_window,
            savgol_polyorder=savgol_polyorder,
        )
    return flags


def detect_sudden_changes_2d(
    x: np.ndarray,
    **kwargs,
) -> np.ndarray:
    """Return (T, D) bool flags."""
    flags = np.zeros_like(x, dtype=bool)
    for d in range(x.shape[1]):
        flags[:, d] = detect_sudden_changes_1d(x[:, d], **kwargs)
    return flags
