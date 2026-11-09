"""Multi-node stats map-reduce helpers (pass1 / prepare_pass2 / pass2 / finalize)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from robot_data_processing.schema import DatasetSchema
from robot_data_processing.stage1_stats import (
    compute_stage1_stats_pass1,
    compute_stage1_stats_pass2,
    finalize_stage1_stats,
    load_stage1_stats_partial,
    merge_stage1_stats_pass1,
    merge_stage1_stats_pass2,
    save_stage1_stats_partial,
)
from robot_data_processing.stages.state_action_temporal_alignment import (
    StateActionAlignConfig,
    resolve_p4_plan,
)
from robot_data_processing.state_action_lag_stats import (
    compute_lag_partial,
    load_lag_partial,
    merge_lag_partials,
    save_lag_partial,
)
from robot_data_processing.stats import (
    compute_global_stats_pass1,
    compute_global_stats_pass2,
    finalize_global_stats,
    load_global_stats_partial,
    merge_global_stats_pass1,
    merge_global_stats_pass2,
    save_global_stats_partial,
)

STATS_PHASES = ("pass1", "prepare_pass2", "pass2", "finalize")


def stats_partial_paths(stats_cache_dir: Path, shard_id: int) -> dict[str, Path]:
    root = Path(stats_cache_dir)
    partials = root / "partials"
    bounds = root / "bounds"
    return {
        "global_pass1": partials / f"global_pass1_shard{shard_id}.npz",
        "stage1_pass1": partials / f"stage1_pass1_shard{shard_id}.npz",
        "lag": partials / f"lag_shard{shard_id}.npz",
        "global_pass2": partials / f"global_pass2_shard{shard_id}.npz",
        "stage1_pass2": partials / f"stage1_pass2_shard{shard_id}.npz",
        "global_bounds": bounds / "global_bounds.npz",
        "stage1_bounds": bounds / "stage1_bounds.npz",
        "global_stats": root / "global_stats.npz",
        "stage1_stats": root / "stage1_global_stats.npz",
        "lag_stats": root / "state_action_lag.npz",
    }


def _load_all_shards(pattern_fn, num_shards: int) -> list:
    out = []
    missing = []
    for k in range(num_shards):
        path = pattern_fn(k)
        if not path.exists():
            missing.append(str(path))
            continue
        out.append(path)
    if missing:
        raise FileNotFoundError(f"Missing stats partials ({len(missing)}): {missing[:3]}...")
    return out


def run_stats_phase(
    *,
    phase: str,
    stats_cache_dir: Path,
    dataset_root: Path,
    episode_indices: list[int],
    schema: DatasetSchema,
    num_workers: int,
    num_bins: int,
    s1_cfg: Any,
    align_cfg: StateActionAlignConfig,
    shard_id: int | None,
    num_shards: int | None,
    show_progress: bool = True,
    action_from_state: bool = False,
) -> dict[str, Any]:
    """Execute one distributed-stats phase. Returns a small summary dict."""
    phase = str(phase).lower()
    if phase not in STATS_PHASES:
        raise ValueError(f"Unsupported stats_phase={phase!r}; expected one of {STATS_PHASES}")

    cache_dir = Path(stats_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    do_lag = (
        schema.embodiment in ("humanoid", "robomind_ur")
        and bool(align_cfg.enabled)
        and resolve_p4_plan(align_cfg).compute_stats
    )

    if phase in ("pass1", "pass2"):
        if shard_id is None or num_shards is None:
            raise ValueError(f"stats_phase={phase} requires shard_id and num_shards")
        paths = stats_partial_paths(cache_dir, int(shard_id))
        eps = list(episode_indices)
        summary: dict[str, Any] = {
            "phase": phase,
            "shard_id": int(shard_id),
            "num_shards": int(num_shards),
            "num_episodes": len(eps),
        }

        if phase == "pass1":
            g1 = compute_global_stats_pass1(
                dataset_root,
                eps,
                schema=schema,
                num_workers=num_workers,
                num_bins=num_bins,
                show_progress=show_progress,
                action_from_state=action_from_state,
            )
            save_global_stats_partial(paths["global_pass1"], g1)
            s1 = compute_stage1_stats_pass1(
                dataset_root,
                eps,
                schema,
                median_kernel=s1_cfg.median_kernel,
                savgol_window=s1_cfg.savgol_window,
                savgol_polyorder=s1_cfg.savgol_polyorder,
                num_workers=num_workers,
                show_progress=show_progress,
                action_from_state=action_from_state,
            )
            save_stage1_stats_partial(paths["stage1_pass1"], s1)
            if do_lag:
                lag = compute_lag_partial(
                    dataset_root,
                    eps,
                    schema,
                    align_cfg,
                    num_workers=num_workers,
                    show_progress=show_progress,
                )
                save_lag_partial(paths["lag"], lag)
            summary["wrote"] = {
                "global_pass1": str(paths["global_pass1"]),
                "stage1_pass1": str(paths["stage1_pass1"]),
                "lag": str(paths["lag"]) if do_lag else None,
            }
            return summary

        # pass2
        if not paths["global_bounds"].exists() or not paths["stage1_bounds"].exists():
            raise FileNotFoundError(
                f"Bounds missing under {cache_dir / 'bounds'}; run prepare_pass2 first"
            )
        g_bounds = load_global_stats_partial(paths["global_bounds"])
        s_bounds = load_stage1_stats_partial(paths["stage1_bounds"])
        g2 = compute_global_stats_pass2(
            dataset_root,
            eps,
            g_bounds,
            schema=schema,
            num_workers=num_workers,
            num_bins=num_bins,
            show_progress=show_progress,
            action_from_state=action_from_state,
        )
        save_global_stats_partial(paths["global_pass2"], g2)
        s2 = compute_stage1_stats_pass2(
            dataset_root,
            eps,
            s_bounds,
            schema,
            median_kernel=s1_cfg.median_kernel,
            savgol_window=s1_cfg.savgol_window,
            savgol_polyorder=s1_cfg.savgol_polyorder,
            num_workers=num_workers,
            num_bins=num_bins,
            show_progress=show_progress,
            action_from_state=action_from_state,
        )
        save_stage1_stats_partial(paths["stage1_pass2"], s2)
        summary["wrote"] = {
            "global_pass2": str(paths["global_pass2"]),
            "stage1_pass2": str(paths["stage1_pass2"]),
        }
        return summary

    if num_shards is None or num_shards < 1:
        raise ValueError(f"stats_phase={phase} requires num_shards >= 1")

    # prepare_pass2 / finalize — rank0, no per-shard episode scan required
    paths0 = stats_partial_paths(cache_dir, 0)
    if phase == "prepare_pass2":
        g_paths = _load_all_shards(
            lambda k: stats_partial_paths(cache_dir, k)["global_pass1"], int(num_shards)
        )
        s_paths = _load_all_shards(
            lambda k: stats_partial_paths(cache_dir, k)["stage1_pass1"], int(num_shards)
        )
        g_bounds = merge_global_stats_pass1([load_global_stats_partial(p) for p in g_paths])
        s_bounds = merge_stage1_stats_pass1([load_stage1_stats_partial(p) for p in s_paths])
        save_global_stats_partial(paths0["global_bounds"], g_bounds)
        save_stage1_stats_partial(paths0["stage1_bounds"], s_bounds)
        return {
            "phase": phase,
            "num_shards": int(num_shards),
            "wrote": {
                "global_bounds": str(paths0["global_bounds"]),
                "stage1_bounds": str(paths0["stage1_bounds"]),
            },
            "num_frames": int(g_bounds["num_frames"]),
            "num_episodes": int(g_bounds["num_episodes"]),
        }

    # finalize
    g_paths = _load_all_shards(
        lambda k: stats_partial_paths(cache_dir, k)["global_pass2"], int(num_shards)
    )
    s_paths = _load_all_shards(
        lambda k: stats_partial_paths(cache_dir, k)["stage1_pass2"], int(num_shards)
    )
    if not paths0["global_bounds"].exists() or not paths0["stage1_bounds"].exists():
        raise FileNotFoundError("Bounds missing; run prepare_pass2 before finalize")
    g_bounds = load_global_stats_partial(paths0["global_bounds"])
    s_bounds = load_stage1_stats_partial(paths0["stage1_bounds"])
    g_hist = merge_global_stats_pass2([load_global_stats_partial(p) for p in g_paths])
    s_hist = merge_stage1_stats_pass2([load_stage1_stats_partial(p) for p in s_paths])
    global_stats = finalize_global_stats(
        g_bounds,
        g_hist,
        num_bins=num_bins,
        num_episodes=int(g_bounds.get("num_episodes", g_hist.get("num_episodes", 0))),
    )
    stage1_stats = finalize_stage1_stats(
        s_bounds,
        s_hist,
        k_residual=s1_cfg.k_residual,
        k_accel=s1_cfg.k_accel,
        k_jerk=s1_cfg.k_jerk,
        percentile_floor=s1_cfg.percentile_floor,
        num_bins=num_bins,
        num_episodes=int(s_bounds.get("num_episodes", s_hist.get("num_episodes", 0))),
    )
    global_stats.save(str(paths0["global_stats"]))
    stage1_stats.save(str(paths0["stage1_stats"]))

    lag_path = None
    if do_lag:
        lag_files = []
        for k in range(int(num_shards)):
            p = stats_partial_paths(cache_dir, k)["lag"]
            if not p.exists():
                raise FileNotFoundError(f"Missing lag partial: {p}")
            lag_files.append(load_lag_partial(p))
        lag_stats = merge_lag_partials(lag_files, schema=schema, cfg=align_cfg)
        lag_stats.save(str(paths0["lag_stats"]))
        lag_path = str(paths0["lag_stats"])

    return {
        "phase": phase,
        "num_shards": int(num_shards),
        "wrote": {
            "global_stats": str(paths0["global_stats"]),
            "stage1_stats": str(paths0["stage1_stats"]),
            "lag_stats": lag_path,
        },
        "num_frames": global_stats.num_frames,
        "num_episodes": global_stats.num_episodes,
    }
