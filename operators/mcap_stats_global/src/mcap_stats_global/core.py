from __future__ import annotations


import hashlib


import json


import os


import time


from pathlib import Path


import numpy as np


from pretraining_data_cleaning_common.contracts import (
    McapEpisodeRef, QualityDecision, NormalizedEpisode, SourceIdentity,
    StatisticsBundle, StatisticsContext, NormalizedBatchRef, NormalizedRecord, frame_ranges, quality_timeline,
)


from pretraining_data_cleaning_common.spec import McapSpec, build_adapter, setup_imports


from pretraining_data_cleaning_common.storage import save_episode, load_episode, save_batch


from pretraining_data_cleaning_common.measurement import coverage_policy, measurement_distances


from pretraining_data_cleaning_common.filesystem import capture_source_identity, write_json_durable


from pretraining_data_cleaning_common.reporting import error_record, json_safe, finish_quality




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

    def run(self, batch: NormalizedBatchRef, spec: McapSpec, cfg, *, recompute=False, check_cancelled=None) -> StatisticsContext:
        """B02: select fit inputs and fit statistics; retain every batch record."""
        fit_records, eligible = [], []
        error = None
        for record in batch.records:
            if check_cancelled is not None:
                check_cancelled()
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
            if check_cancelled is not None:
                check_cancelled()
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
                "input_root": str(spec.input_root), "quality_config_sha256": hashlib.sha256(spec.quality_yaml.read_bytes()).hexdigest(),
            }, indent=2), encoding="utf-8")
            return StatisticsBundle(
                global_stats, stage1_stats, align_stats,
                resolve_alignment_lag(align_cfg, align_stats), cache,
            )
        finally:
            adapter_hooks._ADAPTER = previous_adapter
            adapter_hooks._PACKAGE_DIR = previous_package

