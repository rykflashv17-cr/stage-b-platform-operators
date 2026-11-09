from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import numpy as np
from tqdm import tqdm

from robot_data_processing.loader import (
    episode_parquet_path,
    load_quality_arrays,
    nonempty_quality_pair,
)
from robot_data_processing.schema import DatasetSchema
from robot_data_processing.stages.state_action_temporal_alignment import (
    StateActionAlignConfig,
    StateActionAlignStats,
    aggregate_lag_stats,
    alignment_state_action_pair,
    compute_episode_lags,
)


def _lag_worker(args: tuple) -> np.ndarray | None:
    root_str, ep_idx, cfg, schema = args
    from robot_data_processing.adapter_hooks import has_adapter_quality_loader

    path = episode_parquet_path(Path(root_str), ep_idx)
    if not path.exists() and not has_adapter_quality_loader():
        return None
    try:
        data = load_quality_arrays(Path(root_str), ep_idx, schema=schema)
        pair = nonempty_quality_pair(data)
        if pair is None:
            return None
        state, action = pair
        state_pair, action_pair = alignment_state_action_pair(state, action, schema)
        num_dims = state_pair.shape[1]
        if num_dims == 0 or state.shape[0] < cfg.min_active_samples + cfg.max_lag_frames + 1:
            return None
        return compute_episode_lags(state_pair, action_pair, cfg, num_dims)
    except Exception:
        return None


def compute_lag_partial(
    dataset_root: Path,
    episode_indices: list[int],
    schema: DatasetSchema,
    cfg: StateActionAlignConfig,
    num_workers: int = 128,
    show_progress: bool = True,
) -> np.ndarray:
    """Return (N, D) per-episode lag vectors for this shard (may be empty N=0)."""
    args_list = [(str(dataset_root), idx, cfg, schema) for idx in episode_indices]
    per_episode: list[np.ndarray] = []
    if not args_list:
        return np.zeros((0, schema.alignment_dim), dtype=np.float64)

    ctx = mp.get_context("fork")
    with ctx.Pool(processes=num_workers) as pool:
        iterator = pool.imap_unordered(_lag_worker, args_list, chunksize=32)
        if show_progress:
            iterator = tqdm(iterator, total=len(args_list), desc="state-action lag stats")
        for lags in iterator:
            if lags is not None:
                per_episode.append(lags)
    if not per_episode:
        return np.zeros((0, schema.alignment_dim), dtype=np.float64)
    return np.stack(per_episode, axis=0)


def merge_lag_partials(
    partials: list[np.ndarray],
    *,
    schema: DatasetSchema,
    cfg: StateActionAlignConfig,
) -> StateActionAlignStats:
    rows: list[np.ndarray] = []
    for p in partials:
        arr = np.asarray(p)
        if arr.ndim == 2 and arr.shape[0] > 0:
            rows.extend(list(arr))
    if not rows:
        num_dims = schema.alignment_dim
        return StateActionAlignStats(
            lag_mean=cfg.default_lag,
            lag_min=cfg.default_lag,
            lag_max=cfg.default_lag,
            per_dim_lag_mean=np.full(num_dims, cfg.default_lag, dtype=np.float64),
            per_dim_lag_min=np.full(num_dims, cfg.default_lag, dtype=np.float64),
            per_dim_lag_max=np.full(num_dims, cfg.default_lag, dtype=np.float64),
            num_episodes=0,
        )
    return aggregate_lag_stats(rows)


def save_lag_partial(path: Path, partial: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, lags=np.asarray(partial))


def load_lag_partial(path: Path) -> np.ndarray:
    data = np.load(Path(path))
    return data["lags"]


def compute_global_action_state_lag(
    dataset_root: Path,
    episode_indices: list[int],
    schema: DatasetSchema,
    cfg: StateActionAlignConfig,
    num_workers: int = 128,
    show_progress: bool = True,
) -> StateActionAlignStats:
    partial = compute_lag_partial(
        dataset_root,
        episode_indices,
        schema,
        cfg,
        num_workers=num_workers,
        show_progress=show_progress,
    )
    return merge_lag_partials([partial], schema=schema, cfg=cfg)


def load_or_compute_action_state_lag(
    dataset_root: Path,
    cache_path: Path,
    episode_indices: list[int],
    schema: DatasetSchema,
    cfg: StateActionAlignConfig,
    recompute: bool,
    num_workers: int,
    show_progress: bool = True,
) -> StateActionAlignStats | None:
    from robot_data_processing.stages.state_action_temporal_alignment import resolve_p4_plan

    plan = resolve_p4_plan(cfg)
    if not cfg.enabled or not plan.compute_stats:
        return None
    if schema.embodiment not in ("humanoid", "robomind_ur"):
        return None
    if cache_path.exists() and not recompute:
        loaded = StateActionAlignStats.load(str(cache_path))
        if loaded.per_dim_lag_mean.shape[0] == schema.alignment_dim:
            return loaded
    stats = compute_global_action_state_lag(
        dataset_root,
        episode_indices,
        schema,
        cfg,
        num_workers=num_workers,
        show_progress=show_progress,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    stats.save(str(cache_path))
    return stats
