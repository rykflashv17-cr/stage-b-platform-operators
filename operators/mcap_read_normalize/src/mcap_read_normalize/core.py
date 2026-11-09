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


from pretraining_data_cleaning_common.spec import McapSpec, setup_imports
from .source_adapters import build_adapter


from pretraining_data_cleaning_common.storage import save_episode, load_episode, save_batch


from pretraining_data_cleaning_common.measurement import coverage_policy, measurement_distances


from pretraining_data_cleaning_common.filesystem import capture_source_identity, write_json_durable


from pretraining_data_cleaning_common.reporting import error_record, json_safe, finish_quality


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

    def run_batch(self, refs, spec, cfg, adapter=None, *, check_cancelled=None) -> NormalizedBatchRef:
        ids = [ref.episode_index for ref in refs]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate episode index in input batch")
        folder = (spec.output_root / "normalized").resolve()
        folder.mkdir(parents=True, exist_ok=False)
        if refs and adapter is None:
            adapter = build_adapter(spec)
        records = []
        for position, ref in enumerate(refs):
            if check_cancelled is not None:
                check_cancelled()
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

