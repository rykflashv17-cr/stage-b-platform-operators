"""B01 format normalization, B02 global statistics and B03 quality judgment."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np

from .contracts import (
    McapEpisodeRef, QualityDecision, NormalizedEpisode, SourceIdentity,
    StatisticsBundle, StatisticsContext, NormalizedBatchRef, NormalizedRecord, frame_ranges, quality_timeline,
)
from .spec import McapSpec, build_adapter, setup_imports
from .storage import save_episode, load_episode, save_batch
from .measurement import coverage_policy, measurement_distances
from .deletion import capture_source_identity, write_json_durable
from .reporting import error_record, json_safe, finish_quality
from .integrity import validate_mcap


def _read_quality_data(ref: McapEpisodeRef, spec: McapSpec, adapter, schema):
    """Format conversion owned exclusively by B01.

    The adapter owns row selection/alignment, so only it can supply the final
    row timestamps. Never reconstruct them from message counts or a stride.
    """
    from robot_data_processing.normalize.standard_types import EpisodeRef

    timeline_reader = getattr(adapter, "to_quality_timeline", None)
    if not callable(timeline_reader):
        raise ValueError(
            "adapter must implement to_quality_timeline(standard) with exact "
            "frame_times_ns; message-count/stride timestamp inference is not supported"
        )
    source_ref = EpisodeRef(
        episode_index=ref.episode_index, dataset_root=ref.dataset_root,
        part=ref.part or None, task=ref.task or None,
        rel_path=(Path(os.path.relpath(ref.mcap_path, ref.dataset_root)).as_posix()
                  if spec.package_dir.name == "jindian_marvin" else ref.rel_path),
    )
    raw = adapter.load_raw(source_ref)
    standard = adapter.to_standard_episode(source_ref, raw, {
        "dataset_root": str(ref.dataset_root), "source_episode_index": ref.episode_index,
        "input_format": "pseudo_v3", "identity_p1": True,
    })
    state, action = (np.asarray(x, dtype=np.float64) for x in adapter.to_quality_arrays(standard))
    if state.ndim != 2 or action.ndim != 2 or state.shape[0] != action.shape[0]:
        raise ValueError("state/action must be aligned two-dimensional arrays")
    if schema is not None:
        expected = (schema.pipeline_state_dim, schema.pipeline_action_dim)
        if (state.shape[1], action.shape[1]) != expected:
            raise ValueError(f"quality dimensions {(state.shape, action.shape)} != {expected}")
    if not len(state):
        raise ValueError("MCAP contains no quality frames")
    times, measurements = quality_timeline(timeline_reader(standard), len(state))
    return state, action, times, measurements


class McapReadOperator:
    """B01: decode once and persist normalized data facts for the whole batch."""

    def run(self, ref: McapEpisodeRef, spec: McapSpec, cfg, adapter) -> NormalizedEpisode:
        identity = SourceIdentity.capture(ref.mcap_path)
        validate_mcap(ref.mcap_path)
        setup_imports(spec)
        state, action, times, measurements = _read_quality_data(ref, spec, adapter, cfg.schema)
        if SourceIdentity.capture(ref.mcap_path) != identity:
            raise ValueError("source MCAP changed while reading")
        return NormalizedEpisode(ref, state, action, times, identity, measurements)

    def run_batch(self, refs, spec, cfg, adapter=None) -> NormalizedBatchRef:
        ids = [ref.episode_index for ref in refs]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate episode index in input batch")
        folder = (spec.output_root / "normalized").resolve()
        folder.mkdir(parents=True, exist_ok=False)
        if refs and adapter is None:
            adapter = build_adapter(spec)
        records = []
        for position, ref in enumerate(refs):
            identity, blocked = None, None
            try:
                identity = SourceIdentity(**capture_source_identity(ref.mcap_path, spec.input_root))
            except (OSError, ValueError) as exc:
                blocked = f"{type(exc).__name__}: {exc}"
            try:
                episode = self.run(ref, spec, cfg, adapter)
                if identity is not None and SourceIdentity(**capture_source_identity(ref.mcap_path, spec.input_root)) != identity:
                    raise ValueError("source identity changed during normalization")
            except Exception as exc:
                records.append(NormalizedRecord(ref, None, identity, f"{type(exc).__name__}: {exc}", blocked))
                continue
            # Storage failures abort B01; they are not mislabeled as bad input.
            path = folder / f"episode_{position:08d}.npz"
            save_episode(path, episode)
            records.append(NormalizedRecord(ref, path, identity, deletion_blocked_reason=blocked))
        batch = NormalizedBatchRef(folder / "manifest.json", tuple(records))
        save_batch(batch)
        return batch


class _NormalizedQualityHook:
    """Upstream statistics can read normalized arrays, never a source adapter."""

    def __init__(self, records):
        self.records = {r.ref.episode_index: r for r in records}

    def load_quality_arrays(self, root, episode_index, schema=None, **kwargs):
        episode = load_episode(self.records[int(episode_index)])
        if schema is not None and (episode.state.shape[1], episode.action.shape[1]) != (
            schema.pipeline_state_dim, schema.pipeline_action_dim
        ):
            raise ValueError("normalized arrays mismatch statistics schema")
        if not np.isfinite(episode.state).all() or not np.isfinite(episode.action).all():
            raise ValueError("non-finite arrays cannot be used to fit global statistics")
        return {"state": episode.state, "action": episode.action}


class GlobalStatsOperator:
    """B02 fits upstream statistics using only B01's normalized artifacts."""

    def run(self, batch: NormalizedBatchRef, spec: McapSpec, cfg, *, recompute=False) -> StatisticsContext:
        """B02: select fit inputs and fit statistics; retain every batch record."""
        fit_records, eligible = [], []
        error = None
        for record in batch.records:
            reason = record.input_error
            if reason is None:
                try:
                    episode = load_episode(record)
                    # This is statistical input admission, not a quality verdict.
                    if spec.gripper_gap is not None:
                        for _, distance in measurement_distances(episode, spec.gripper_gap).values():
                            if distance is None or np.any(distance > spec.gripper_gap.max_nearest_gap_ns):
                                reason = "measurement coverage insufficient for statistics"
                                break
                    if reason is None and (not np.isfinite(episode.state).all() or not np.isfinite(episode.action).all()):
                        reason = "non-finite numerical statistics input"
                except Exception as exc:
                    reason = f"normalized input unavailable: {type(exc).__name__}: {exc}"
                    # A damaged intermediate invalidates the shared fit, unlike
                    # an explicitly recorded B01 input error or coverage exclusion.
                    error = reason
            fit_records.append({"episode_index": record.ref.episode_index,
                                "fit_status": "excluded" if reason else "included", "reason": reason})
            if reason is None:
                eligible.append(record)
        statistics = None
        status = "failed" if error else "empty"
        if eligible and error is None:
            try:
                statistics = self._fit(batch, spec, cfg, eligible, recompute=recompute)
                status = "ready"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                status = "failed"
        context = StatisticsContext(batch, status, statistics, tuple(fit_records), coverage_policy(spec.gripper_gap), error)
        write_json_durable(spec.output_root / "statistics_context.json", {
            "status": status, "error": error, "normalized_manifest": str(batch.manifest_path),
            "coverage_policy": context.coverage_policy, "fit_records": fit_records,
            "included_episodes": len(eligible), "excluded_episodes": len(batch.records) - len(eligible),
        })
        return context

    def _fit(self, batch, spec, cfg, records, *, recompute=False) -> StatisticsBundle:
        setup_imports(spec)
        from robot_data_processing import adapter_hooks
        from robot_data_processing.stage1_stats import load_or_compute_stage1_stats
        from robot_data_processing.state_action_lag_stats import load_or_compute_action_state_lag
        from robot_data_processing.stages.stage1_sudden_change import Stage1Config
        from robot_data_processing.stages.state_action_temporal_alignment import (
            StateActionAlignConfig, resolve_alignment_lag,
        )
        from robot_data_processing.stats import load_or_compute_stats

        previous_adapter = adapter_hooks.get_adapter()
        previous_package = getattr(adapter_hooks, "_PACKAGE_DIR", None)
        adapter_hooks._ADAPTER = _NormalizedQualityHook(records)
        adapter_hooks._PACKAGE_DIR = spec.package_dir
        try:
            cache = spec.output_root / "cache"
            cache.mkdir(parents=True, exist_ok=True)
            ids = [record.ref.episode_index for record in records]
            fingerprint = hashlib.sha256()
            fingerprint.update(b"stage_b_v4_normalized_serial_v1")
            fingerprint.update(json.dumps(spec.quality_overrides, sort_keys=True).encode())
            fingerprint.update(str(spec.input_root.resolve()).encode())
            fingerprint.update(spec.quality_yaml.read_bytes())
            fingerprint.update(repr(spec.gripper_gap).encode())
            fingerprint.update(batch.manifest_path.read_bytes())
            for record in records:
                with record.data_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        fingerprint.update(chunk)
            signature = fingerprint.hexdigest()
            manifest = cache / "source_fingerprint.json"
            if not recompute:
                try:
                    cached = json.loads(manifest.read_text(encoding="utf-8"))
                except (FileNotFoundError, ValueError):
                    cached = {}
                recompute = cached.get("signature") != signature
            global_stats = load_or_compute_stats(
                batch.manifest_path.parent, cache / "global_stats.npz", ids, cfg.schema,
                recompute=recompute, num_workers=spec.workers,
                num_bins=cfg.stats_num_bins, show_progress=True,
            )
            if global_stats.num_frames <= 0:
                raise ValueError("statistics engine returned no fitted frames")
            stage1_stats = load_or_compute_stage1_stats(
                batch.manifest_path.parent, cache / "stage1_global_stats.npz", ids,
                cfg.schema, cfg.stage1 or Stage1Config(), recompute=recompute,
                num_workers=spec.workers, num_bins=cfg.stats_num_bins, show_progress=True,
            )
            align_cfg = cfg.state_action_alignment or StateActionAlignConfig()
            align_stats = load_or_compute_action_state_lag(
                batch.manifest_path.parent, cache / "state_action_lag.npz", ids, cfg.schema,
                align_cfg, recompute=recompute, num_workers=spec.workers, show_progress=True,
            )
            manifest.write_text(json.dumps({
                "signature": signature, "episodes": len(records),
                "input_root": str(spec.input_root), "quality_yaml": str(spec.quality_yaml),
            }, indent=2), encoding="utf-8")
            return StatisticsBundle(
                global_stats, stage1_stats, align_stats,
                resolve_alignment_lag(align_cfg, align_stats), cache,
            )
        finally:
            adapter_hooks._ADAPTER = previous_adapter
            adapter_hooks._PACKAGE_DIR = previous_package


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

    def run_batch(self, context: StatisticsContext, spec, cfg, *, started=None):
        """B03: final judgments and a durable handoff; no source file mutation."""
        if context.coverage_policy != coverage_policy(spec.gripper_gap):
            raise ValueError("coverage policy changed between statistics and quality")
        started = time.perf_counter() if started is None else started
        ordered = []
        for record in context.batch.records:
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
