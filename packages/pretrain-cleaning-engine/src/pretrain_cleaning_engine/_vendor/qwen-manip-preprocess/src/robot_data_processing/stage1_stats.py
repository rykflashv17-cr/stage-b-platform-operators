from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import numpy as np
from tqdm import tqdm

from robot_data_processing.loader import (
    episode_parquet_path,
    load_quality_arrays,
    nonempty_quality_pair,
)
from robot_data_processing.schema import DatasetSchema
from robot_data_processing.smoothing import compute_derivatives, smooth_1d


@dataclass
class Stage1GlobalStats:
    """Global per-channel thresholds for Stage1 sudden-change detection."""

    thr_residual: np.ndarray
    thr_accel: np.ndarray
    thr_jerk: np.ndarray
    num_frames: int
    num_episodes: int
    num_channels: int

    def save(self, path: str) -> None:
        np.savez_compressed(
            path,
            thr_residual=self.thr_residual,
            thr_accel=self.thr_accel,
            thr_jerk=self.thr_jerk,
            num_frames=np.array([self.num_frames]),
            num_episodes=np.array([self.num_episodes]),
            num_channels=np.array([self.num_channels]),
        )

    @classmethod
    def load(cls, path: str) -> Stage1GlobalStats:
        data = np.load(path)
        n_ch = int(data["num_channels"][0]) if "num_channels" in data else len(data["thr_residual"])
        return cls(
            thr_residual=data["thr_residual"],
            thr_accel=data["thr_accel"],
            thr_jerk=data["thr_jerk"],
            num_frames=int(data["num_frames"][0]),
            num_episodes=int(data["num_episodes"][0]),
            num_channels=n_ch,
        )


def _init_metric_histograms(num_channels: int, num_bins: int) -> dict[str, np.ndarray]:
    n = num_channels
    return {
        "residual_hist": np.zeros((n, num_bins), dtype=np.int64),
        "accel_hist": np.zeros((n, num_bins), dtype=np.int64),
        "jerk_hist": np.zeros((n, num_bins), dtype=np.int64),
        "residual_min": np.full(n, np.inf),
        "residual_max": np.full(n, -np.inf),
        "accel_min": np.full(n, np.inf),
        "accel_max": np.full(n, -np.inf),
        "jerk_min": np.full(n, np.inf),
        "jerk_max": np.full(n, -np.inf),
        "num_frames": np.int64(0),
        "num_episodes": np.int64(0),
    }


def _episode_channel_features(
    x: np.ndarray,
    median_kernel: int,
    savgol_window: int,
    savgol_polyorder: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    if x.shape[0] < savgol_window:
        return None
    smooth = smooth_1d(x, median_kernel, savgol_window, savgol_polyorder)
    residual = np.abs(x - smooth)
    accel, jerk = compute_derivatives(x)
    return residual, np.abs(accel), np.abs(jerk)


def _collect_episode_metrics(
    state: np.ndarray,
    action: np.ndarray,
    schema: DatasetSchema,
    median_kernel: int,
    savgol_window: int,
    savgol_polyorder: int,
) -> list[tuple[int, np.ndarray, np.ndarray, np.ndarray]]:
    out: list[tuple[int, np.ndarray, np.ndarray, np.ndarray]] = []
    for d in range(state.shape[1]):
        feat = _episode_channel_features(state[:, d], median_kernel, savgol_window, savgol_polyorder)
        if feat is not None:
            out.append((schema.stage1_state_channel_offset + d, *feat))
    for d in range(action.shape[1]):
        feat = _episode_channel_features(action[:, d], median_kernel, savgol_window, savgol_polyorder)
        if feat is not None:
            out.append((schema.stage1_action_channel_offset + d, *feat))
    return out


def _merge_pass1(acc: dict, local: dict) -> None:
    for key in ("residual_min", "accel_min", "jerk_min"):
        if key in local:
            acc[key] = np.minimum(acc[key], local[key])
    for key in ("residual_max", "accel_max", "jerk_max"):
        if key in local:
            acc[key] = np.maximum(acc[key], local[key])
    acc["num_frames"] += local.get("num_frames", np.int64(0))
    acc["num_episodes"] = np.int64(
        int(acc.get("num_episodes", 0)) + int(local.get("num_episodes", 0))
    )


def _pass1_worker(args: tuple) -> dict | None:
    root_str, ep_idx, mk, sw, sp, action_from_state, schema = args
    from robot_data_processing.adapter_hooks import has_adapter_quality_loader

    path = episode_parquet_path(Path(root_str), ep_idx)
    if not path.exists() and not has_adapter_quality_loader():
        return None
    try:
        data = load_quality_arrays(
            Path(root_str), ep_idx, schema=schema, action_from_state=action_from_state
        )
        pair = nonempty_quality_pair(data)
        if pair is None:
            print(f"[stage1-stats] skip empty quality arrays ep={ep_idx}", flush=True)
            return None
        state, action = pair
        num_channels = schema.stage1_num_channels
        local = _init_metric_histograms(num_channels, 1)
        local["num_frames"] = np.int64(state.shape[0])
        local["num_episodes"] = np.int64(1)
        for ch, res, acc, jerk in _collect_episode_metrics(
            state, action, schema, mk, sw, sp
        ):
            local["residual_min"][ch] = min(local["residual_min"][ch], float(res.min()))
            local["residual_max"][ch] = max(local["residual_max"][ch], float(res.max()))
            local["accel_min"][ch] = min(local["accel_min"][ch], float(acc.min()))
            local["accel_max"][ch] = max(local["accel_max"][ch], float(acc.max()))
            local["jerk_min"][ch] = min(local["jerk_min"][ch], float(jerk.min()))
            local["jerk_max"][ch] = max(local["jerk_max"][ch], float(jerk.max()))
        return local
    except Exception as exc:
        print(f"[stage1-stats] skip ep={ep_idx}: {type(exc).__name__}: {exc}", flush=True)
        return None


def _pass2_worker(
    args: tuple,
    bounds: dict,
    num_bins: int,
    schema: DatasetSchema,
) -> dict | None:
    root_str, ep_idx, mk, sw, sp, action_from_state, _schema = args
    from robot_data_processing.adapter_hooks import has_adapter_quality_loader

    path = episode_parquet_path(Path(root_str), ep_idx)
    if not path.exists() and not has_adapter_quality_loader():
        return None
    try:
        data = load_quality_arrays(
            Path(root_str), ep_idx, schema=schema, action_from_state=action_from_state
        )
        pair = nonempty_quality_pair(data)
        if pair is None:
            print(f"[stage1-stats] skip empty quality arrays ep={ep_idx}", flush=True)
            return None
        state, action = pair
        num_channels = schema.stage1_num_channels
        local = _init_metric_histograms(num_channels, num_bins)
        rows_res, rows_acc, rows_jerk = [], [], []
        channels = []
        for ch, res, acc, jerk in _collect_episode_metrics(
            state, action, schema, mk, sw, sp
        ):
            channels.append(ch)
            rows_res.append(res)
            rows_acc.append(acc)
            rows_jerk.append(jerk)
        if not channels:
            local["num_frames"] = np.int64(state.shape[0])
            local["num_episodes"] = np.int64(1)
            return local

        res_mat = np.stack(rows_res, axis=1)
        acc_mat = np.stack(rows_acc, axis=1)
        jerk_mat = np.stack(rows_jerk, axis=1)

        for i, ch in enumerate(channels):
            for mat, hkey, mn, mx in [
                (res_mat, "residual_hist", "residual_min", "residual_max"),
                (acc_mat, "accel_hist", "accel_min", "accel_max"),
                (jerk_mat, "jerk_hist", "jerk_min", "jerk_max"),
            ]:
                vals = mat[:, i]
                vmin, vmax = bounds[mn][ch], bounds[mx][ch]
                span = max(vmax - vmin, 1e-12)
                idx = np.floor((vals - vmin) / span * (num_bins - 1)).astype(np.int64)
                np.clip(idx, 0, num_bins - 1, out=idx)
                local[hkey][ch] += np.bincount(idx, minlength=num_bins)

        local["num_frames"] = np.int64(state.shape[0])
        local["num_episodes"] = np.int64(1)
        return local
    except Exception as exc:
        print(f"[stage1-stats] skip ep={ep_idx}: {type(exc).__name__}: {exc}", flush=True)
        return None


def _percentile_from_hist(
    hist: np.ndarray,
    vmin: np.ndarray,
    vmax: np.ndarray,
    q: float,
    num_bins: int,
    num_channels: int,
) -> np.ndarray:
    out = np.zeros(num_channels, dtype=np.float64)
    span = vmax - vmin
    span = np.where(span < 1e-12, 1.0, span)
    target = q / 100.0
    for d in range(num_channels):
        cdf = np.cumsum(hist[d])
        total = cdf[-1]
        if total == 0:
            out[d] = vmin[d]
            continue
        idx = int(np.searchsorted(cdf, target * total, side="left"))
        idx = min(idx, num_bins - 1)
        out[d] = vmin[d] + (idx / (num_bins - 1)) * span[d]
    return out


def _global_scale_from_hist(
    hist: np.ndarray,
    vmin: np.ndarray,
    vmax: np.ndarray,
    num_bins: int,
    num_channels: int,
) -> np.ndarray:
    centers = np.linspace(0.0, 1.0, num_bins)
    scales = np.zeros(num_channels, dtype=np.float64)
    span = vmax - vmin
    span = np.where(span < 1e-12, 1.0, span)
    for d in range(num_channels):
        counts = hist[d].astype(np.float64)
        total = counts.sum()
        if total == 0:
            scales[d] = 1e-12
            continue
        probs = counts / total
        mean = np.sum(probs * centers) * span[d] + vmin[d]
        var = np.sum(probs * (centers * span[d] + vmin[d] - mean) ** 2)
        std = float(np.sqrt(max(var, 0.0)))
        abs_dev = np.abs(centers * span[d] + vmin[d] - mean)
        m = float(np.sum(probs * abs_dev))
        scales[d] = max(m, 0.01 * std, 1e-12)
    return scales


def _hybrid_from_hist(
    hist: np.ndarray,
    vmin: np.ndarray,
    vmax: np.ndarray,
    num_bins: int,
    num_channels: int,
    k: float,
    percentile: float,
) -> np.ndarray:
    p_thr = _percentile_from_hist(hist, vmin, vmax, percentile, num_bins, num_channels)
    scale = _global_scale_from_hist(hist, vmin, vmax, num_bins, num_channels)
    return np.maximum(np.maximum(k * scale, p_thr), 1e-12)


def compute_stage1_stats_pass1(
    dataset_root: Path,
    episode_indices: list[int],
    schema: DatasetSchema,
    median_kernel: int,
    savgol_window: int,
    savgol_polyorder: int,
    num_workers: int = 128,
    show_progress: bool = True,
    action_from_state: bool = False,
) -> dict[str, np.ndarray]:
    num_channels = schema.stage1_num_channels
    args_list = [
        (str(dataset_root), idx, median_kernel, savgol_window, savgol_polyorder, action_from_state, schema)
        for idx in episode_indices
    ]
    acc = _init_metric_histograms(num_channels, 1)
    if not args_list:
        return acc

    ctx = mp.get_context("fork")
    with ctx.Pool(processes=num_workers) as pool:
        it = pool.imap_unordered(_pass1_worker, args_list, chunksize=32)
        if show_progress:
            it = tqdm(it, total=len(args_list), desc="stage1 stats pass1")
        for local in it:
            if local is None:
                continue
            _merge_pass1(acc, local)
    return acc


def merge_stage1_stats_pass1(partials: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    if not partials:
        raise ValueError("merge_stage1_stats_pass1 requires at least one partial")
    num_channels = int(partials[0]["residual_min"].shape[0])
    acc = _init_metric_histograms(num_channels, 1)
    for p in partials:
        _merge_pass1(acc, p)
    return {
        "residual_min": acc["residual_min"],
        "residual_max": acc["residual_max"],
        "accel_min": acc["accel_min"],
        "accel_max": acc["accel_max"],
        "jerk_min": acc["jerk_min"],
        "jerk_max": acc["jerk_max"],
        "num_frames": acc["num_frames"],
        "num_episodes": acc["num_episodes"],
    }


def compute_stage1_stats_pass2(
    dataset_root: Path,
    episode_indices: list[int],
    bounds: dict[str, np.ndarray],
    schema: DatasetSchema,
    median_kernel: int,
    savgol_window: int,
    savgol_polyorder: int,
    num_workers: int = 128,
    num_bins: int = 65536,
    show_progress: bool = True,
    action_from_state: bool = False,
) -> dict[str, np.ndarray]:
    num_channels = schema.stage1_num_channels
    args_list = [
        (str(dataset_root), idx, median_kernel, savgol_window, savgol_polyorder, action_from_state, schema)
        for idx in episode_indices
    ]
    hist_acc = _init_metric_histograms(num_channels, num_bins)
    if not args_list:
        return hist_acc

    pass2_fn = partial(_pass2_worker, bounds=bounds, num_bins=num_bins, schema=schema)
    ctx = mp.get_context("fork")
    with ctx.Pool(processes=num_workers) as pool:
        it = pool.imap_unordered(pass2_fn, args_list, chunksize=16)
        if show_progress:
            it = tqdm(it, total=len(args_list), desc="stage1 stats pass2")
        for local in it:
            if local is None:
                continue
            hist_acc["residual_hist"] += local["residual_hist"]
            hist_acc["accel_hist"] += local["accel_hist"]
            hist_acc["jerk_hist"] += local["jerk_hist"]
            hist_acc["num_frames"] += local["num_frames"]
            hist_acc["num_episodes"] += local.get("num_episodes", np.int64(0))
    return hist_acc


def merge_stage1_stats_pass2(partials: list[dict[str, np.ndarray]]) -> dict[str, np.ndarray]:
    if not partials:
        raise ValueError("merge_stage1_stats_pass2 requires at least one partial")
    acc = {
        "residual_hist": np.zeros_like(partials[0]["residual_hist"]),
        "accel_hist": np.zeros_like(partials[0]["accel_hist"]),
        "jerk_hist": np.zeros_like(partials[0]["jerk_hist"]),
        "num_frames": np.int64(0),
        "num_episodes": np.int64(0),
    }
    for p in partials:
        acc["residual_hist"] += p["residual_hist"]
        acc["accel_hist"] += p["accel_hist"]
        acc["jerk_hist"] += p["jerk_hist"]
        acc["num_frames"] += p.get("num_frames", np.int64(0))
        acc["num_episodes"] += p.get("num_episodes", np.int64(0))
    return acc


def finalize_stage1_stats(
    bounds: dict[str, np.ndarray],
    hist: dict[str, np.ndarray],
    *,
    k_residual: float,
    k_accel: float,
    k_jerk: float,
    percentile_floor: float,
    num_bins: int = 65536,
    num_episodes: int | None = None,
) -> Stage1GlobalStats:
    num_channels = int(bounds["residual_min"].shape[0])
    accel_pct = min(percentile_floor, 99.95)
    n_eps = int(num_episodes if num_episodes is not None else hist.get("num_episodes", 0))
    return Stage1GlobalStats(
        thr_residual=_hybrid_from_hist(
            hist["residual_hist"],
            bounds["residual_min"],
            bounds["residual_max"],
            num_bins,
            num_channels,
            k_residual,
            percentile_floor,
        ),
        thr_accel=_hybrid_from_hist(
            hist["accel_hist"],
            bounds["accel_min"],
            bounds["accel_max"],
            num_bins,
            num_channels,
            k_accel,
            accel_pct,
        ),
        thr_jerk=_hybrid_from_hist(
            hist["jerk_hist"],
            bounds["jerk_min"],
            bounds["jerk_max"],
            num_bins,
            num_channels,
            k_jerk,
            accel_pct,
        ),
        num_frames=int(hist["num_frames"]),
        num_episodes=n_eps,
        num_channels=num_channels,
    )


def save_stage1_stats_partial(path: Path, partial: dict[str, np.ndarray]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **{k: np.asarray(v) for k, v in partial.items()})


def load_stage1_stats_partial(path: Path) -> dict[str, np.ndarray]:
    data = np.load(Path(path))
    return {k: data[k] for k in data.files}


def compute_stage1_global_stats(
    dataset_root: Path,
    episode_indices: list[int],
    schema: DatasetSchema,
    median_kernel: int,
    savgol_window: int,
    savgol_polyorder: int,
    k_residual: float,
    k_accel: float,
    k_jerk: float,
    percentile_floor: float,
    num_workers: int = 128,
    num_bins: int = 65536,
    show_progress: bool = True,
    action_from_state: bool = False,
) -> Stage1GlobalStats:
    pass1 = compute_stage1_stats_pass1(
        dataset_root,
        episode_indices,
        schema,
        median_kernel=median_kernel,
        savgol_window=savgol_window,
        savgol_polyorder=savgol_polyorder,
        num_workers=num_workers,
        show_progress=show_progress,
        action_from_state=action_from_state,
    )
    bounds = merge_stage1_stats_pass1([pass1])
    hist = compute_stage1_stats_pass2(
        dataset_root,
        episode_indices,
        bounds,
        schema,
        median_kernel=median_kernel,
        savgol_window=savgol_window,
        savgol_polyorder=savgol_polyorder,
        num_workers=num_workers,
        num_bins=num_bins,
        show_progress=show_progress,
        action_from_state=action_from_state,
    )
    return finalize_stage1_stats(
        bounds,
        hist,
        k_residual=k_residual,
        k_accel=k_accel,
        k_jerk=k_jerk,
        percentile_floor=percentile_floor,
        num_bins=num_bins,
        num_episodes=len(episode_indices),
    )


def load_or_compute_stage1_stats(
    dataset_root: Path,
    cache_path: Path,
    episode_indices: list[int],
    schema: DatasetSchema,
    s1_cfg,
    recompute: bool,
    num_workers: int,
    num_bins: int,
    show_progress: bool = True,
    action_from_state: bool = False,
) -> Stage1GlobalStats:
    if cache_path.exists() and not recompute:
        loaded = Stage1GlobalStats.load(str(cache_path))
        if loaded.num_channels == schema.stage1_num_channels:
            return loaded

    stats = compute_stage1_global_stats(
        dataset_root,
        episode_indices,
        schema,
        median_kernel=s1_cfg.median_kernel,
        savgol_window=s1_cfg.savgol_window,
        savgol_polyorder=s1_cfg.savgol_polyorder,
        k_residual=s1_cfg.k_residual,
        k_accel=s1_cfg.k_accel,
        k_jerk=s1_cfg.k_jerk,
        percentile_floor=s1_cfg.percentile_floor,
        num_workers=num_workers,
        num_bins=num_bins,
        show_progress=show_progress,
        action_from_state=action_from_state,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    stats.save(str(cache_path))
    return stats
