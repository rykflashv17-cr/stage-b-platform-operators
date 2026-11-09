from __future__ import annotations

import json
import multiprocessing as mp
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from robot_data_processing.ignore_list import (
    filter_episode_indices,
    load_ignore_episode_list,
    load_ignore_episode_lists,
    write_ignore_episode_list,
)
from robot_data_processing.loader import (
    episode_parquet_path,
    load_quality_arrays,
)
from robot_data_processing.mask import (
    build_frame_keep_mask,
    compute_per_joint_action_zero_exclude,
    compute_per_joint_stage1_exclude,
)
from robot_data_processing.preprocess import (
    expand_compact_exclude_to_action,
)
from robot_data_processing.transforms import robomind_ur_compact_teleop
from robot_data_processing.lerobot_export import export_lerobot_from_results, verify_lerobot_alignment
from robot_data_processing.report import (
    build_quality_report,
    write_exclusion_log,
    write_optimal_lags,
    write_quality_report,
)
from robot_data_processing.schema import DatasetSchema, schema_from_yaml
from robot_data_processing.stage1_stats import Stage1GlobalStats, load_or_compute_stage1_stats
from robot_data_processing.state_action_lag_stats import load_or_compute_action_state_lag
from robot_data_processing.stages.stage1_sudden_change import (
    EEF_SPIKE,
    GRIPPER_QUICK_REVERSAL,
    EefSpikeConfig,
    GripperFaultConfig,
    RotationJointConfig,
    Stage1Config,
    run_stage1,
)
from robot_data_processing.stages.stage2_trend_alignment import (
    Stage2Config,
    canonical_to_stage2_fields,
    run_stage2_on_fields,
)
from robot_data_processing.stages.stage3_extreme_value import Stage3Config, run_stage3
from robot_data_processing.stages.stage4_static_interval import Stage4Config, run_stage4
from robot_data_processing.stages.stage5_frame_alignment import (
    Stage5Config,
    read_stage5_raw_context,
    run_stage5,
)
from robot_data_processing.stages.state_action_temporal_alignment import (
    StateActionAlignConfig,
    alignment_metadata,
    parse_temporal_align_config,
    resolve_alignment_lag,
    resolve_p4_plan,
    resolve_state_action_delay,
)
from robot_data_processing.stats import load_or_compute_stats
from robot_data_processing.types import EpisodeResult, GlobalStats


@dataclass
class PipelineConfig:
    dataset_root: Path
    output_dir: Path
    output_mode: str
    schema: DatasetSchema
    num_workers: int = 128
    min_episode_length: int = 120
    stage1: Stage1Config | None = None
    stage2: Stage2Config | None = None
    stage3: Stage3Config | None = None
    stage4: Stage4Config | None = None
    stage5: Stage5Config | None = None
    state_action_alignment: StateActionAlignConfig | None = None
    alignment_lag: int | None = None
    stats_recompute: bool = True
    stats_cache_path: Path | None = None
    stage1_stats_recompute: bool = True
    stage1_stats_cache_path: Path | None = None
    state_action_lag_recompute: bool = True
    state_action_lag_cache_path: Path | None = None
    stats_num_bins: int = 65536
    total_episodes: int | None = None
    action_zero_epsilon: float = 1e-4
    stage1_post_zero_grace_frames: int = 0
    discard_short_prefix: bool = True
    # Drop trailing rows whose source clock (extra_info.timestamps) repeats (dt == 0).
    trim_tail_repeat_frames: bool = False
    # Prefix truncation also drops this many frames before the first abnormal frame: their
    # recorded action targets the abnormal frame (action[t] ~ state[t + lag]).
    prefix_truncate_margin_frames: int = 0
    recompute_video_stats: bool = False
    export_workers: int | None = None
    parallel_videos: bool = True
    skip_alignment_verify: bool = False
    ignore_episodes_path: Path | None = None  # singular (compat); prefer ignore_episodes_paths
    ignore_episodes_paths: list[Path] = field(default_factory=list)


_WORKER_STATE: dict[str, Any] = {}


def _init_worker(
    global_stats: GlobalStats,
    stage1_stats: Stage1GlobalStats,
    pipe_cfg: PipelineConfig,
    align_stats: Any = None,
) -> None:
    _WORKER_STATE["stats"] = global_stats
    _WORKER_STATE["stage1_stats"] = stage1_stats
    _WORKER_STATE["cfg"] = pipe_cfg
    _WORKER_STATE["align_stats"] = align_stats


def process_episode(episode_index: int, pipe_cfg: PipelineConfig | None = None) -> EpisodeResult:
    cfg = pipe_cfg or _WORKER_STATE["cfg"]
    stats: GlobalStats = _WORKER_STATE.get("stats")  # type: ignore[assignment]
    stage1_stats: Stage1GlobalStats = _WORKER_STATE.get("stage1_stats")  # type: ignore[assignment]
    schema = cfg.schema
    if stats is None and cfg is not None:
        raise RuntimeError("Global stats not initialized in worker")
    if stage1_stats is None and cfg is not None:
        raise RuntimeError("Stage1 global stats not initialized in worker")

    path = episode_parquet_path(cfg.dataset_root, episode_index)
    from robot_data_processing.adapter_hooks import has_adapter_quality_loader

    if not path.exists() and not has_adapter_quality_loader():
        return EpisodeResult(
            episode_index=episode_index,
            num_frames=0,
            discard=True,
            discard_reasons=["missing_parquet"],
            step_validity_mask=np.array([], dtype=np.int8),
        )

    data = load_quality_arrays(cfg.dataset_root, episode_index, schema=schema)
    state = data["state"]
    action = data["action"]
    num_frames = state.shape[0]

    s1_cfg = cfg.stage1 or Stage1Config()
    s2_cfg = cfg.stage2 or Stage2Config()
    s3_cfg = cfg.stage3 or Stage3Config()
    s4_cfg = cfg.stage4 or Stage4Config()
    s5_cfg = cfg.stage5 or Stage5Config()
    align_cfg = cfg.state_action_alignment or StateActionAlignConfig()

    action_for_masks = action
    if schema.embodiment == "robomind_ur":
        _, action_compact = robomind_ur_compact_teleop(state, action)
        action_for_masks = action_compact

    startup_exclude_compact = compute_per_joint_action_zero_exclude(action_for_masks, cfg.action_zero_epsilon)
    stage1_exclude_compact = compute_per_joint_stage1_exclude(
        action_for_masks, cfg.action_zero_epsilon, cfg.stage1_post_zero_grace_frames
    )
    if schema.embodiment == "robomind_ur":
        startup_exclude = expand_compact_exclude_to_action(startup_exclude_compact, action.shape[1])
        stage1_exclude = expand_compact_exclude_to_action(stage1_exclude_compact, action.shape[1])
    else:
        startup_exclude = startup_exclude_compact
        stage1_exclude = stage1_exclude_compact

    s1 = run_stage1(
        state,
        action,
        s1_cfg,
        schema=schema,
        startup_exclude_per_joint=stage1_exclude,
        global_stats=stage1_stats,
    )
    # Stage2 DA on standard-key fields (eef pose + arm joints); may fill action.eef
    # using global P4 lag_mean (cfg.alignment_lag), not per-episode joint lag.
    stage2_fields = canonical_to_stage2_fields(
        state, action, embodiment=schema.embodiment
    )
    align_stats = _WORKER_STATE.get("align_stats")
    align_lag = (
        cfg.alignment_lag
        if cfg.alignment_lag is not None
        else resolve_alignment_lag(align_cfg, align_stats)
    )
    s2 = run_stage2_on_fields(stage2_fields, s2_cfg, fill_lag=align_lag)
    s3 = run_stage3(
        state, action, stats, s3_cfg, schema=schema, startup_exclude_per_joint=startup_exclude
    )

    abnormal = s1.abnormal_frames.copy()
    reasons: list[str] = list(s1.discard_reasons)

    if s2.discard:
        abnormal[:] = True
        reasons.extend(f"stage2:{r}" for r in s2.discard_reasons)
    else:
        abnormal |= s3.remove_frames
        reasons.extend(f"stage3:{r}" for r in s3.discard_reasons)

    margin = int(cfg.prefix_truncate_margin_frames)
    if margin > 0 and abnormal.any():
        first_bad = int(np.flatnonzero(abnormal)[0])
        # Frame 0 is never dropped (it defines the episode frame).
        abnormal[max(1, first_bad - margin) : first_bad] = True
    prefix_mask = build_frame_keep_mask(num_frames, abnormal)
    valid_end = int(prefix_mask.sum()) if prefix_mask.any() else 0
    # Stage whose first abnormal frame starts the prefix truncation (frames >= valid_end).
    prefix_trigger = None
    if not s2.discard and valid_end < num_frames:
        firsts = {
            name: int(np.flatnonzero(flags)[0])
            for name, flags in (("stage1", s1.abnormal_frames), ("stage3", s3.remove_frames))
            if flags.any()
        }
        prefix_trigger = min(firsts, key=firsts.get) if firsts else None
        if prefix_trigger == "stage1" and s1.rotation_joint_first_frame == firsts["stage1"]:
            prefix_trigger = "stage1_rotation_joint"
    s4_remove = np.zeros(num_frames, dtype=bool)
    if valid_end > 0:
        s4 = run_stage4(state[:valid_end], action[:valid_end], s4_cfg)
        s4_remove[:valid_end] = s4.remove_frames
    else:
        s4 = run_stage4(state[:0], action[:0], s4_cfg)
    # Trailing rows whose source clock repeats (dt == 0) are dropped; frame tail_start - 1 is kept.
    tail_start = num_frames
    if cfg.trim_tail_repeat_frames and num_frames > 1:
        ts = _source_clock(path, episode_index, num_frames)
        if ts is not None:
            tail_start = tail_repeat_start(ts)
    tail_remove = np.zeros(num_frames, dtype=bool)
    tail_remove[tail_start:] = True
    step_validity_mask = build_frame_keep_mask(num_frames, abnormal, s4_remove | tail_remove)
    kept_frames = int(step_validity_mask.sum())
    first_removed = int(np.flatnonzero(step_validity_mask == 0)[0]) if kept_frames < num_frames else None

    discard = bool(s2.discard) or bool(s3.discard)
    # Other stage1 findings only truncate the prefix; a gripper that undoes an open/close
    # within a few frames drops the whole episode (stage1.gripper_fault.quick_reversal_discard).
    # Same for a single-frame EEF position spike (stage1.eef_spike).
    if any(r.startswith((GRIPPER_QUICK_REVERSAL, EEF_SPIKE)) for r in s1.discard_reasons):
        discard = True
    if cfg.discard_short_prefix and kept_frames < cfg.min_episode_length:
        discard = True
        reasons.append(f"kept_frames={kept_frames}<{cfg.min_episode_length}")
    if first_removed is not None:
        reasons.append(f"first_removed_frame={first_removed}")

    s5_meta: dict[str, Any] = {"stage5_applied": False}
    if s5_cfg.enabled:
        raw_ctx = read_stage5_raw_context(path, schema, s5_cfg)
        s5 = run_stage5(cfg.dataset_root, episode_index, schema, raw_ctx, s5_cfg)
        s5_meta = {
            "stage5_applied": s5.applied,
            "stage5_reference_frame": s5_cfg.reference_frame,
            "stage5_reference_forward": s5.reference_forward_xyz.tolist(),
            "stage5_rotation_correction_euler_xyz": list(s5_cfg.rotation_correction_euler_xyz),
            "aligned_state_shape": list(s5.aligned_state.shape),
        }

    align_meta = alignment_metadata(align_cfg, align_stats, align_lag)

    return EpisodeResult(
        episode_index=episode_index,
        num_frames=num_frames,
        discard=discard,
        discard_reasons=reasons,
        step_validity_mask=step_validity_mask,
        first_abnormal_frame=first_removed,
        stage1_flagged_frames=s1.flagged_count,
        stage2_da_mean=s2.da_mean,
        stage2_da_per_dim=s2.da_per_dim,
        stage2_lags=s2.lags,
        stage3_excluded_frames=s3.excluded_count,
        stage4_removed_frames=s4.removed_count,
        metadata={
            "stage1_hard_limit": s1.hard_limit_hit,
            "stage1_gripper_faults": s1.gripper_faults,
            "stage1_rotation_joint_steps": s1.rotation_joint_steps,
            "valid_prefix": kept_frames,
            # Frames [prefix_valid_end, T) are prefix-truncated (stage1/3); the other dropped
            # frames are stage4 static-run shortening inside the prefix.
            "prefix_valid_end": valid_end,
            "prefix_truncate_trigger": prefix_trigger,
            # Frames [tail_repeat_start, T) repeat the source clock (dt == 0) and are dropped.
            "tail_repeat_start": tail_start if tail_start < num_frames else None,
            "kept_frames": kept_frames,
            "low_valid_prefix": kept_frames < cfg.min_episode_length,
            "stage4_static_runs_shortened": s4.static_runs_shortened,
            "stage4_static_frames_removed": s4.static_frames_removed,
            "startup_exclude_per_joint_frames": int(startup_exclude.sum()),
            "stage1_exclude_frames": int(stage1_exclude.sum()),
            "stage1_post_zero_grace_frames": cfg.stage1_post_zero_grace_frames,
            "embodiment": schema.embodiment,
            "state_dim": schema.pipeline_state_dim,
            "action_dim": schema.pipeline_action_dim,
            "stage2_dim_names": s2.dim_names,
            "stage2_filled_action_eef": s2.filled_action_eef,
            "stage2_fill_lags": s2.fill_lags,
            "stage2_episode_lag": s2.episode_lag,
            "stage2_lag_mean": s2.lag_mean,
            "stage2_lag_mode": s2.lag_mode,
            "stage2_lag_source": s2.lag_source,
            **s5_meta,
            **align_meta,
        },
    )


def rotation_joint_config(raw: dict[str, Any] | None) -> RotationJointConfig:
    """``stage1.rotation_joint`` yaml block -> RotationJointConfig."""
    raw = dict(raw or {})
    arms = tuple(
        (tuple(int(d) for d in a["joints"]), tuple(int(d) for d in a["rotation_6d"]))
        for a in raw.pop("arms", None) or []
    )
    return RotationJointConfig(arms=arms, **raw)


def tail_repeat_start(ts: np.ndarray) -> int:
    """First row after the last clock change: rows [start, T) repeat row start - 1 (dt == 0)."""
    ts = np.asarray(ts).reshape(-1)
    j = ts.size - 1
    while j > 0 and ts[j] == ts[j - 1]:
        j -= 1
    return j + 1 if ts.size else 0


def _source_clock(path: Path, episode_index: int, num_frames: int) -> np.ndarray | None:
    """``extra_info.timestamps`` of one episode (int64), or None if absent / length differs."""
    import pyarrow.parquet as pq

    from robot_data_processing.schema import SOURCE_TIMESTAMPS_FIELD

    if not Path(path).exists():
        return None
    names = set(pq.ParquetFile(path).schema_arrow.names)
    if SOURCE_TIMESTAMPS_FIELD not in names:
        return None
    filters = [("episode_index", "=", int(episode_index))] if "episode_index" in names else None
    col = pq.read_table(path, columns=[SOURCE_TIMESTAMPS_FIELD], filters=filters).column(0).to_pylist()
    ts = np.asarray(col, dtype=np.int64).reshape(len(col), -1)[:, 0]
    return ts if ts.size == num_frames else None


def _worker_fn(episode_index: int) -> EpisodeResult:
    return process_episode(episode_index)


def run_pipeline(
    cfg: PipelineConfig,
    episode_indices: list[int],
    stats_episode_indices: list[int] | None = None,
    show_progress: bool = True,
    timings: dict[str, float] | None = None,
) -> list[EpisodeResult]:
    from tqdm import tqdm

    def _tick(name: str, t_start: float) -> float:
        dt = time.perf_counter() - t_start
        if timings is not None:
            timings[name] = round(dt, 3)
        if show_progress:
            print(f"[timing] {name}: {dt:.2f}s ({dt / 60:.2f} min)", flush=True)
        return time.perf_counter()

    pipe_t0 = time.perf_counter()
    ignore_paths = list(cfg.ignore_episodes_paths)
    if not ignore_paths and cfg.ignore_episodes_path is not None:
        ignore_paths = [cfg.ignore_episodes_path]
    ignore_set = load_ignore_episode_lists(ignore_paths)
    if ignore_set:
        before = len(episode_indices)
        episode_indices = filter_episode_indices(episode_indices, ignore_set)
        ignored_manifest = {
            "reason": "pipeline ignore list",
            "source": str(ignore_paths[0]) if len(ignore_paths) == 1 else None,
            "sources": [str(p) for p in ignore_paths],
            "ignored_count": before - len(episode_indices),
            "ignore_list_size": len(ignore_set),
            "episode_indices": sorted(ignore_set),
        }
        meta_dir = cfg.output_dir / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        with (meta_dir / "ignored_episodes.json").open("w", encoding="utf-8") as f:
            json.dump(ignored_manifest, f, indent=2, ensure_ascii=False)
        if show_progress:
            print(
                f"Ignoring {len(ignore_set)} episodes from {len(ignore_paths)} ignore list(s) "
                f"({before} -> {len(episode_indices)})"
            )

    stats_eps = stats_episode_indices or episode_indices
    stats_eps = filter_episode_indices(stats_eps, ignore_set)
    cache_path = cfg.stats_cache_path or (cfg.output_dir / "cache" / "global_stats.npz")

    t = time.perf_counter()
    global_stats = load_or_compute_stats(
        cfg.dataset_root,
        cache_path,
        stats_eps,
        schema=cfg.schema,
        recompute=cfg.stats_recompute,
        num_workers=cfg.num_workers,
        num_bins=cfg.stats_num_bins,
        show_progress=show_progress,
    )
    t = _tick("p3_global_stats", t)

    s1_cache = cfg.stage1_stats_cache_path or (cfg.output_dir / "cache" / "stage1_global_stats.npz")
    s1_cfg = cfg.stage1 or Stage1Config()
    stage1_global_stats = load_or_compute_stage1_stats(
        cfg.dataset_root,
        s1_cache,
        stats_eps,
        cfg.schema,
        s1_cfg,
        recompute=cfg.stage1_stats_recompute,
        num_workers=cfg.num_workers,
        num_bins=cfg.stats_num_bins,
        show_progress=show_progress,
    )
    t = _tick("p3_stage1_stats", t)

    align_cfg = cfg.state_action_alignment or StateActionAlignConfig()
    align_cache = cfg.state_action_lag_cache_path or (
        cfg.output_dir / "cache" / "state_action_lag.npz"
    )
    align_stats = load_or_compute_action_state_lag(
        cfg.dataset_root,
        align_cache,
        stats_eps,
        cfg.schema,
        align_cfg,
        recompute=cfg.state_action_lag_recompute,
        num_workers=cfg.num_workers,
        show_progress=show_progress,
    )
    t = _tick("p3_state_action_lag_stats", t)
    p4_plan = resolve_p4_plan(align_cfg)
    cfg.alignment_lag = resolve_alignment_lag(align_cfg, align_stats)

    ctx = mp.get_context("fork")
    results: list[EpisodeResult] = []
    with ctx.Pool(
        processes=cfg.num_workers,
        initializer=_init_worker,
        initargs=(global_stats, stage1_global_stats, cfg, align_stats),
    ) as pool:
        iterator = pool.imap_unordered(_worker_fn, episode_indices, chunksize=4)
        if show_progress:
            iterator = tqdm(iterator, total=len(episode_indices), desc="process episodes")
        for result in iterator:
            results.append(result)

    results.sort(key=lambda r: r.episode_index)
    t = _tick("p3_process_episodes", t)

    align_lag = resolve_alignment_lag(align_cfg, align_stats)
    delay_meta = resolve_state_action_delay(align_cfg, align_stats, p4_plan)

    if cfg.output_mode in ("filter", "both"):
        export_summary = export_lerobot_from_results(
            source_root=cfg.dataset_root,
            output_root=cfg.output_dir,
            schema=cfg.schema,
            results=results,
            alignment_lag=align_lag,
            alignment_enabled=p4_plan.apply_shift,
            recompute_video_stats=cfg.recompute_video_stats,
            export_workers=cfg.export_workers,
            parallel_videos=cfg.parallel_videos,
            show_progress=show_progress,
            alignment_plan=p4_plan.name,
            manual_delay=p4_plan.manual_delay,
        )
        t = _tick("p3_lerobot_export", t)
        if not cfg.skip_alignment_verify:
            alignment = verify_lerobot_alignment(
                cfg.output_dir,
                [ep.episode_index for ep in export_summary.episodes],
            )
            alignment_path = cfg.output_dir / "alignment_report.json"
            with alignment_path.open("w", encoding="utf-8") as f:
                json.dump(alignment, f, indent=2, ensure_ascii=False)
            t = _tick("p3_alignment_verify", t)
        if show_progress:
            print(
                f"Exported {export_summary.total_episodes} episodes, "
                f"{export_summary.total_frames} frames "
                f"(skipped {export_summary.skipped_episodes}, truncated {export_summary.truncated_episodes}); "
                f"P4 plan={p4_plan.name} state_action_delay={delay_meta}"
            )

    if cfg.output_mode in ("report", "both"):
        report = build_quality_report(
            results,
            stats_meta={
                "num_frames": global_stats.num_frames,
                "num_episodes": global_stats.num_episodes,
                "cache_path": str(cache_path),
                "stage1_stats_cache_path": str(s1_cache),
                "state_action_lag_cache_path": str(align_cache),
                "stage1_threshold_mode": "global",
                "embodiment": cfg.schema.embodiment,
                "state_dim": cfg.schema.pipeline_state_dim,
                "action_dim": cfg.schema.pipeline_action_dim,
            },
            config_summary={
                "output_mode": cfg.output_mode,
                "num_workers": cfg.num_workers,
                "processed_episodes": len(episode_indices),
                "validity_mask_mode": "prefix_truncate_then_static_shorten",
                "state_action_alignment_enabled": align_cfg.enabled,
                "state_action_alignment_plan": p4_plan.name,
                "state_action_alignment_apply_delay": align_cfg.apply_delay,
                "state_action_alignment_lag": align_lag,
                "state_action_delay": delay_meta,
                "stage4_max_static_duration_sec": (
                    cfg.stage4 or Stage4Config()
                ).max_static_duration_sec,
                "stage4_max_static_steps": (cfg.stage4 or Stage4Config()).resolved_max_static_steps,
                "stage5_enabled": (cfg.stage5 or Stage5Config()).enabled,
                "embodiment": cfg.schema.embodiment,
                "ignored_episodes": len(ignore_set),
                "ignore_episodes_path": str(ignore_paths[0]) if len(ignore_paths) == 1 else None,
                "ignore_episodes_paths": [str(p) for p in ignore_paths],
            },
        )
        write_quality_report(cfg.output_dir / "reports" / "quality_report.json", report)
        write_exclusion_log(cfg.output_dir / "reports" / "exclusion_log.jsonl", results)
        write_optimal_lags(cfg.output_dir / "reports" / "optimal_lags.json", results)
        t = _tick("p3_quality_report", t)

    _tick("p3_pipeline_total", pipe_t0)
    return results


def load_config(yaml_path: Path) -> dict:
    with yaml_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_ignore_episodes_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    repo_root = Path(__file__).resolve().parents[2]  # qwen-manip-preprocess/
    candidate = repo_root / path
    if candidate.exists():
        return candidate
    # Fallback: Dataclean repo root (parent of qwen-manip-preprocess)
    dataclean_root = repo_root.parent
    alt = dataclean_root / path
    if alt.exists():
        return alt
    return candidate


def _resolve_ignore_episode_paths(
    pipe: dict[str, Any],
    overrides: dict[str, Any],
) -> list[Path]:
    raw = overrides.get("ignore_episodes_paths", pipe.get("ignore_episodes_paths"))
    if raw is None:
        single = overrides.get("ignore_episodes_path", pipe.get("ignore_episodes_path"))
        raw = [single] if single is not None else []
    elif isinstance(raw, (str, Path)):
        raw = [raw]
    paths: list[Path] = []
    for item in raw:
        resolved = _resolve_ignore_episodes_path(item)
        if resolved is not None:
            paths.append(resolved)
    return paths


def _joint_limits(raw: Any) -> tuple[tuple[float, float], ...] | None:
    """``hard_limits.joint_limits``: one [lo, hi] per joint (joint_indices order), or None."""
    if not raw:
        return None
    return tuple((float(lo), float(hi)) for lo, hi in raw)


def pipeline_config_from_yaml(yaml_cfg: dict, overrides: dict | None = None) -> PipelineConfig:
    overrides = overrides or {}
    dataset_root = Path(overrides.get("dataset_root", yaml_cfg["dataset"]["root"]))
    output_dir = Path(overrides.get("output_dir", "./output"))
    schema = schema_from_yaml(yaml_cfg)
    s1 = yaml_cfg["stage1"]
    s2 = yaml_cfg["stage2"]
    s3 = yaml_cfg["stage3"]
    pipe = yaml_cfg.get("pipeline", {})
    s4 = yaml_cfg.get("stage4", {})
    s5 = yaml_cfg.get("stage5", {})
    sa = yaml_cfg.get("state_action_alignment", {})
    ta = overrides.get("temporal_align") or yaml_cfg.get("temporal_align") or {}

    gripper_state = tuple(s3["exempt_dims"].get("gripper_state", list(schema.gripper_indices)))
    gripper_action = tuple(s3["exempt_dims"].get("gripper_action", list(schema.gripper_indices)))
    rpy_state = tuple(s3["exempt_dims"].get("rpy_state", list(schema.rpy_indices)))
    rpy_action = tuple(s3["exempt_dims"].get("rpy_action", list(schema.rpy_indices)))

    align_cfg = parse_temporal_align_config(ta, sa, overrides=overrides)
    dataset_fps = float(overrides.get("fps", yaml_cfg.get("dataset", {}).get("fps", 30)))

    s4_duration = float(
        overrides.get(
            "stage4_max_static_duration_sec",
            s4.get("max_static_duration_sec", 1.0),
        )
    )
    if "stage4_max_static_steps" in overrides:
        s4_steps_override: int | None = int(overrides["stage4_max_static_steps"])
    elif "max_static_steps" in s4:
        s4_steps_override = int(s4["max_static_steps"])
    else:
        s4_steps_override = None

    return PipelineConfig(
        dataset_root=dataset_root,
        output_dir=output_dir,
        output_mode=overrides.get("output_mode", pipe.get("output_mode", "report")),
        schema=schema,
        num_workers=int(overrides.get("num_workers", pipe.get("num_workers", 128))),
        min_episode_length=int(overrides.get("min_episode_length", pipe.get("min_episode_length", 120))),
        action_zero_epsilon=float(overrides.get("action_zero_epsilon", pipe.get("action_zero_epsilon", 1e-4))),
        stage1_post_zero_grace_frames=int(
            overrides.get("stage1_post_zero_grace_frames", pipe.get("stage1_post_zero_grace_frames", 0))
        ),
        discard_short_prefix=bool(overrides.get("discard_short_prefix", pipe.get("discard_short_prefix", True))),
        trim_tail_repeat_frames=bool(
            overrides.get("trim_tail_repeat_frames", pipe.get("trim_tail_repeat_frames", False))
        ),
        prefix_truncate_margin_frames=int(
            overrides.get("prefix_truncate_margin_frames", pipe.get("prefix_truncate_margin_frames", 0))
        ),
        recompute_video_stats=bool(overrides.get("recompute_video_stats", pipe.get("recompute_video_stats", False))),
        export_workers=(
            int(overrides["export_workers"])
            if overrides.get("export_workers") is not None
            else (int(pipe["export_workers"]) if pipe.get("export_workers") is not None else None)
        ),
        parallel_videos=bool(overrides.get("parallel_videos", pipe.get("parallel_videos", True))),
        skip_alignment_verify=bool(
            overrides.get("skip_alignment_verify", pipe.get("skip_alignment_verify", False))
        ),
        ignore_episodes_paths=(ignore_paths := _resolve_ignore_episode_paths(pipe, overrides)),
        ignore_episodes_path=ignore_paths[0] if ignore_paths else None,
        state_action_alignment=align_cfg,
        state_action_lag_recompute=bool(
            overrides.get("state_action_lag_recompute", sa.get("stats", {}).get("recompute", True))
        ),
        state_action_lag_cache_path=Path(
            overrides.get(
                "state_action_lag_cache_path",
                output_dir / sa.get("stats", {}).get("cache_path", "cache/state_action_lag.npz"),
            )
        ),
        stage1=Stage1Config(
            median_kernel=s1["smoothing"]["median_kernel"],
            savgol_window=s1["smoothing"]["savgol_window"],
            savgol_polyorder=s1["smoothing"]["savgol_polyorder"],
            k_residual=s1["thresholds"]["k_residual"],
            k_accel=s1["thresholds"]["k_accel"],
            k_jerk=s1["thresholds"]["k_jerk"],
            percentile_floor=s1["thresholds"]["percentile_floor"],
            joint_abs_max=s1["hard_limits"]["joint_abs_max"],
            joint_limits=_joint_limits(s1["hard_limits"].get("joint_limits")),
            joint_limit_tolerance=float(s1["hard_limits"].get("joint_limit_tolerance", 0.0)),
            ee_position_max=s1["hard_limits"]["ee_position_max"],
            rpy_abs_max=s1["hard_limits"].get("rpy_abs_max", 3.14159265),
            gripper_max=s1["hard_limits"]["gripper_max"],
            frame_max_cluster=s1["exclusion"]["frame_removal"]["max_cluster_length"],
            frame_abnormal_min_cluster=s1["exclusion"]["frame_removal"].get(
                "min_cluster_length_for_abnormal", 1
            ),
            episode_min_cluster=s1["exclusion"]["episode_discard"]["min_cluster_length"],
            min_cluster_frame_jump=s1["exclusion"]["episode_discard"].get("min_cluster_frame_jump", 0.08),
            on_hard_limit_violation=s1["exclusion"]["episode_discard"]["on_hard_limit_violation"],
            exempt_state_dims=tuple((s1.get("exempt_dims") or {}).get("state", ())),
            exempt_action_dims=tuple((s1.get("exempt_dims") or {}).get("action", ())),
            gripper_fault=GripperFaultConfig(**(s1.get("gripper_fault") or {})),
            eef_spike=EefSpikeConfig(**(s1.get("eef_spike") or {})),
            rotation_joint=rotation_joint_config(s1.get("rotation_joint")),
        ),
        stage2=Stage2Config(
            median_kernel=s2["smoothing"]["median_kernel"],
            savgol_window=s2["smoothing"]["savgol_window"],
            savgol_polyorder=s2["smoothing"]["savgol_polyorder"],
            max_lag_frames=s2["alignment"]["max_lag_frames"],
            diff_epsilon=float(overrides.get("stage2_diff_epsilon", s2["alignment"]["diff_epsilon"])),
            exact_shift_tolerance=float(s2["alignment"].get("exact_shift_tolerance", 1e-6)),
            min_active_samples=s2["alignment"]["min_active_samples"],
            da_per_dim=s2["thresholds"]["da_per_dim"],
            da_episode_mean=s2["thresholds"]["da_episode_mean"],
            action_type=s2["action_type"],
            fill_missing_action_eef=bool(
                overrides.get(
                    "stage2_fill_missing_action_eef",
                    s2.get("fill_missing_action_eef", True),
                )
            ),
        ),
        stage3=Stage3Config(
            alpha=s3["alpha"],
            gripper_state_indices=gripper_state,
            gripper_action_indices=gripper_action,
            rpy_state_indices=rpy_state,
            rpy_action_indices=rpy_action,
            joint_limits=tuple(s3["hard_limits"]["joint"]),
            joint_limits_per_joint=_joint_limits(s3["hard_limits"].get("joint_limits")),
            joint_limit_tolerance=float(s3["hard_limits"].get("joint_limit_tolerance", 0.0)),
            ee_xyz_limits=tuple(s3["hard_limits"]["ee_xyz"]),
            rpy_limits=tuple(s3["hard_limits"].get("rpy", [-3.15, 3.15])),
            gripper_limits=tuple(s3["hard_limits"]["gripper"]),
            min_episode_length=int(
                (s3.get("exclusion") or {}).get(
                    "min_episode_length",
                    overrides.get("min_episode_length", pipe.get("min_episode_length", 120)),
                )
            ),
        ),
        stage4=Stage4Config(
            max_static_duration_sec=s4_duration,
            fps=dataset_fps,
            max_static_steps=s4_steps_override,
            enabled=bool(overrides.get("stage4_enabled", s4.get("enabled", True))),
            change_epsilon=float(overrides.get("stage4_change_epsilon", s4.get("change_epsilon", 0.0))),
        ),
        stage5=Stage5Config(
            enabled=bool(overrides.get("stage5_enabled", s5.get("enabled", False))),
            reference_frame=s5.get("reference_frame", "camera_top_frame0"),
            rotation_correction_euler_xyz=tuple(
                s5.get("rotation_correction_euler_xyz", [0.0, 0.0, 0.0])
            ),
            egodex_extrinsics_column=s5.get(
                "egodex_extrinsics_column", "observation.camera_extrinsics_world"
            ),
            humanoid_calibration_relpath=s5.get(
                "humanoid_calibration_relpath",
                "parameters/chunk-{chunk:03d}/episode_{episode:06d}/calibration_bundle_optimized.json",
            ),
            humanoid_camera_extrinsic_key=s5.get(
                "humanoid_camera_extrinsic_key", "camera_front_to_arm_left"
            ),
        ),
        stats_recompute=bool(overrides.get("stats_recompute", s3["stats"]["recompute"])),
        stats_cache_path=Path(overrides.get("stats_cache_path", output_dir / s3["stats"]["cache_path"])),
        stage1_stats_recompute=bool(overrides.get("stage1_stats_recompute", s1["stats"].get("recompute", True))),
        stage1_stats_cache_path=Path(
            overrides.get("stage1_stats_cache_path", output_dir / s1["stats"].get("cache_path", "cache/stage1_global_stats.npz"))
        ),
        stats_num_bins=int(s3["stats"]["num_histogram_bins"]),
        total_episodes=yaml_cfg["dataset"].get("total_episodes"),
    )