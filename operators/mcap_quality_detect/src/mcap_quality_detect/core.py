from __future__ import annotations


import hashlib


import json


import os


import time


from pathlib import Path


import numpy as np


from pretrain_cleaning_common.contracts import (
    McapEpisodeRef, QualityDecision, NormalizedEpisode, SourceIdentity,
    StatisticsBundle, StatisticsContext, NormalizedBatchRef, NormalizedRecord, frame_ranges, quality_timeline,
)


from pretrain_cleaning_common.spec import McapSpec, build_adapter, setup_imports


from pretrain_cleaning_common.storage import save_episode, load_episode, save_batch


from pretrain_cleaning_common.measurement import coverage_policy, measurement_distances


from pretrain_cleaning_common.filesystem import capture_source_identity, write_json_durable


from pretrain_cleaning_common.reporting import error_record, json_safe, finish_quality




def _gripper_gap(episode: NormalizedEpisode, spec: McapSpec) -> tuple[np.ndarray, dict]:
    """Measure timestamp coverage without declaring missing readings valid."""
    bad = np.zeros(episode.num_frames, dtype=bool)
    config = spec.gripper_gap
    if config is None:
        return bad, {"status": "disabled", "detected_ranges": []}
    ranges = []
    counts = {}
    for side, (topic, distances) in measurement_distances(episode, config).items():
        side_bad = np.ones(episode.num_frames, dtype=bool) if distances is None else distances > config.max_nearest_gap_ns
        bad |= side_bad
        counts[side] = int(side_bad.sum())
        for interval in frame_ranges(side_bad, episode.frame_times_ns):
            start, end = interval["start_frame"], interval["end_frame_exclusive"]
            ranges.append({
                **interval, "side": side, "topic": topic,
                "max_nearest_gap_seconds": float(distances[start:end].max() / 1e9) if distances is not None else None,
            })
    return bad, {
        "status": "rejected" if bad.any() else "passed",
        "bad_frames": int(bad.sum()), "invalid_frames_by_side": counts,
        "max_nearest_gap_seconds": config.max_nearest_gap_ns / 1e9,
        "detected_ranges": ranges,
    }


def _stage_result(mask: np.ndarray, times: np.ndarray, *, discard: bool = False,
                  reasons=(), **metrics) -> dict:
    return {
        "status": "rejected" if discard or mask.any() else "passed",
        "discard": bool(discard), "bad_frames": int(np.count_nonzero(mask)),
        "reasons": list(reasons), "detected_ranges": frame_ranges(mask, times),
        **metrics,
    }


class QualityDetectionOperator:
    """Gripper coverage and Stage1–4; one failure rejects the whole source MCAP."""

    def precheck(self, episode: NormalizedEpisode, spec: McapSpec) -> QualityDecision | None:
        """A coverage failure needs no fitted statistics or numerical scoring."""
        if not episode.num_frames:
            raise ValueError("cannot judge an episode without quality frames")
        mask, gap = _gripper_gap(episode, spec)
        if not mask.any():
            return None
        stages = {"gripper_gap": gap}
        stages.update({name: {"status": "skipped", "reason": "gripper_gap_rejected_entire_file", "detected_ranges": []}
                       for name in ("s1", "s2", "s3", "s4")})
        return QualityDecision(
            episode.ref, episode.frame_times_ns, mask, True,
            (f"gripper_gap:invalid_frames={int(mask.sum())}",), stages,
            episode.source_identity,
        )

    def run(self, episode: NormalizedEpisode, spec: McapSpec, cfg,
            statistics: StatisticsBundle | None) -> QualityDecision:
        early = self.precheck(episode, spec)
        if early is not None:
            return early
        if not np.isfinite(episode.state).all() or not np.isfinite(episode.action).all():
            raise ValueError("non-finite quality values without a diagnosed gripper coverage gap")
        if statistics is None:
            raise ValueError("global statistics unavailable for numerical judgment")
        setup_imports(spec)
        from robot_data_processing.mask import compute_per_joint_action_zero_exclude, compute_per_joint_stage1_exclude
        from robot_data_processing.preprocess import expand_compact_exclude_to_action
        from robot_data_processing.transforms import robomind_ur_compact_teleop
        action_for_masks = episode.action
        if cfg.schema.embodiment == "robomind_ur":
            _, action_for_masks = robomind_ur_compact_teleop(episode.state, episode.action)
        startup = compute_per_joint_action_zero_exclude(action_for_masks, cfg.action_zero_epsilon)
        stage1_exclude = compute_per_joint_stage1_exclude(action_for_masks, cfg.action_zero_epsilon, cfg.stage1_post_zero_grace_frames)
        if cfg.schema.embodiment == "robomind_ur":
            startup = expand_compact_exclude_to_action(startup, episode.action.shape[1])
            stage1_exclude = expand_compact_exclude_to_action(stage1_exclude, episode.action.shape[1])
        from robot_data_processing.stages.stage1_sudden_change import Stage1Config, run_stage1
        from robot_data_processing.stages.stage2_trend_alignment import (
            Stage2Config, canonical_to_stage2_fields, run_stage2_on_fields,
        )
        from robot_data_processing.stages.stage3_extreme_value import Stage3Config, run_stage3
        from robot_data_processing.stages.stage4_static_interval import Stage4Config, run_stage4

        n, times = episode.num_frames, episode.frame_times_ns
        reports = {"gripper_gap": {"status": "passed" if spec.gripper_gap else "disabled",
                                    "bad_frames": 0, "detected_ranges": []}}
        masks, diagnostic_errors = [], {}

        def run_s2():
            if cfg.schema.embodiment == "jindian_marvin":
                fields = {
                    "observation.state.arm.left.joint_position": episode.state[:, :7],
                    "observation.state.arm.right.joint_position": episode.state[:, 7:14],
                    "action.arm.left.joint_position": episode.action[:, :7],
                    "action.arm.right.joint_position": episode.action[:, 7:14],
                }
            else:
                fields = canonical_to_stage2_fields(episode.state, episode.action, embodiment=cfg.schema.embodiment)
            return run_stage2_on_fields(fields, cfg.stage2 or Stage2Config(), fill_lag=statistics.alignment_lag)

        stage4_enabled = True
        def run_s4():
            nonlocal stage4_enabled
            config = cfg.stage4 or Stage4Config()
            stage4_enabled = bool(config.enabled)
            # Evaluate the full episode; no prefix is salvaged or exported.
            return run_stage4(episode.state, episode.action, config)

        # Each successful stage is recorded before the next is attempted. Even
        # optional metric extraction cannot erase a validated reject/mask.
        stages = (
            ("s1", lambda: run_stage1(
                episode.state, episode.action, cfg.stage1 or Stage1Config(),
                schema=cfg.schema, startup_exclude_per_joint=stage1_exclude,
                global_stats=statistics.stage1_stats),
             lambda r: r.abnormal_frames, lambda r: r.discard, lambda r: r.discard_reasons,
             lambda r: dict(flagged_count=int(r.flagged_count), hard_limit_hit=bool(r.hard_limit_hit),
                            gripper_faults=dict(r.gripper_faults), rotation_joint_steps=int(r.rotation_joint_steps))),
            ("s2", run_s2, lambda r: np.full(n, bool(r.discard), dtype=bool),
             lambda r: r.discard, lambda r: r.discard_reasons,
             lambda r: dict(da_mean=r.da_mean, da_per_dim=r.da_per_dim, dim_names=r.dim_names,
                            lags=r.lags, episode_lag=r.episode_lag, lag_source=r.lag_source)),
            ("s3", lambda: run_stage3(
                episode.state, episode.action, statistics.global_stats, cfg.stage3 or Stage3Config(),
                schema=cfg.schema, startup_exclude_per_joint=startup),
             lambda r: r.remove_frames, lambda r: r.discard, lambda r: r.discard_reasons,
             lambda r: dict(excluded_count=int(r.excluded_count))),
            ("s4", run_s4, lambda r: r.remove_frames, lambda r: False, lambda r: (),
             lambda r: dict(removed_count=int(r.removed_count), static_runs_shortened=int(r.static_runs_shortened),
                            static_frames_removed=int(r.static_frames_removed))),
        )
        for index, (name, invoke, get_mask, get_discard, get_reasons, get_metrics) in enumerate(stages):
            try:
                outcome = invoke()
                mask = np.asarray(get_mask(outcome), dtype=bool)
                if mask.shape != (n,):
                    raise ValueError(f"{name} returned a mask with the wrong length")
                if name == "s4" and not stage4_enabled and mask.any():
                    raise ValueError("disabled s4 returned removal frames")
                reports[name] = _stage_result(mask, times, discard=bool(get_discard(outcome)))
                masks.append(mask)
                if name == "s4" and not stage4_enabled:
                    reports[name]["status"] = "disabled"
                reports[name]["reasons"] = list(get_reasons(outcome))
                reports[name].update(get_metrics(outcome))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                diagnostic_errors[name] = error
                reports.setdefault(name, {"status": "error", "detected_ranges": []})["error"] = error
                for following, *_ in stages[index + 1:]:
                    reports[following] = {"status": "skipped", "reason": f"{name}_diagnostic_error",
                                          "detected_ranges": []}
                break
        reasons = []
        for name, result in reports.items():
            if result["status"] != "rejected":
                continue
            reasons.append(f"{name}:bad_frames={result['bad_frames']}")
            reasons.extend(f"{name}:{reason}" for reason in result.get("reasons", ()))
        bad = np.logical_or.reduce(masks) if masks else np.zeros(n, dtype=bool)
        return QualityDecision(
            episode.ref, times, bad, bool(reasons), tuple(reasons), reports,
            episode.source_identity, diagnostic_errors,
        )

    def run_batch(self, context: StatisticsContext, spec, cfg, *, started=None, check_cancelled=None):
        """B03: final judgments and a durable handoff; no source file mutation."""
        if context.coverage_policy != coverage_policy(spec.gripper_gap):
            raise ValueError("coverage policy changed between statistics and quality")
        started = time.perf_counter() if started is None else started
        ordered = []
        for record in context.batch.records:
            if check_cancelled is not None:
                check_cancelled()
            if record.input_error:
                ordered.append(error_record(record, record.input_error))
                continue
            try:
                episode = load_episode(record)
                decision = self.run(episode, spec, cfg, context.statistics)
                result = json_safe(decision.to_record())
                diagnostic_error = bool(result.get("diagnostic_errors"))
                result.update({"source_identity": json_safe(record.source_identity),
                               "deletion_eligible": record.source_identity is not None and record.deletion_blocked_reason is None and not diagnostic_error,
                               "deletion_blocked_reason": "quality diagnostics failed; source deletion blocked" if diagnostic_error else record.deletion_blocked_reason})
                ordered.append(result)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                if context.error:
                    detail += f"; global statistics failed: {context.error}"
                ordered.append(error_record(record, detail))
        return finish_quality(ordered, context, spec, started)

