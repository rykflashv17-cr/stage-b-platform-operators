"""Single-MCAP selection against real, cohort-bound numeric ZIP handoffs.

These tests use only temporary synthetic arrays. They exercise B03 I/O and the
business quality implementation without requiring a running platform or SDK.
"""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import sys

import numpy as np

from pretrain_cleaning_engine import root as engine_root
from pretrain_cleaning_common.artifact import pack_artifact, unpack_artifact
from pretrain_cleaning_common.contracts import (
    McapEpisodeRef, NormalizedBatchRef, NormalizedEpisode, NormalizedRecord,
    StatisticsBundle, StatisticsContext,
)
from pretrain_cleaning_common.handoff import (
    load_context, make_handoff_spec, save_context, save_selection, selection_metadata, sha256,
)
from pretrain_cleaning_common.reporting import read_quality_batch
from pretrain_cleaning_common.single_file import merge_normalized_episodes, read_cohort, write_cohort
from pretrain_cleaning_common.spec import quality_config
from pretrain_cleaning_common.storage import save_batch, save_episode

from mcap_quality_detect import io
from mcap_quality_detect.core import QualityDetectionOperator


class _Context:
    """The file/workspace/report interfaces used by the I/O helpers, not an SDK mock."""

    def __init__(self, folder, files):
        folder.mkdir()
        self.parameters = {"source": "jindian_marvin", "workers": 1}
        self.inputs = SimpleNamespace(file=files.__getitem__)
        self.workspace = SimpleNamespace(path=lambda name: folder / name)
        self.published = {}
        self.outputs = SimpleNamespace(file=lambda name, path: self.published.setdefault(name, path))
        self.check_cancelled = lambda: None
        self.quality_records = []
        self.report_calls = 0

        def report(rows):
            self.report_calls += 1
            self.quality_records.extend(rows)

        self.quality = SimpleNamespace(report_items=report)


class SingleFileQualityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.serial = 0

    def _single(self, key, attempt, *, value=0.2, gap=False, input_error=None):
        self.serial += 1
        folder = self.folder / f"single-{self.serial}"
        folder.mkdir()
        ref = McapEpisodeRef(0, Path("/raw"), Path("/raw") / key, key)
        path = None
        if input_error is None:
            path = folder / "episode_00000000.npz"
            times = np.array([1_000_000_000, 1_200_000_000], dtype=np.int64)
            measurements = {
                "/raw/das/left/state": np.array([], dtype=np.int64) if gap else times,
                "/raw/das/right/state": times,
            }
            save_episode(path, NormalizedEpisode(
                ref, np.full((2, 16), value), np.full((2, 16), value), times,
                valid_measurement_times_ns=measurements,
            ))
        batch = NormalizedBatchRef(folder / "manifest.json", (NormalizedRecord(ref, path, input_error=input_error),))
        save_batch(batch)
        metadata = selection_metadata("jindian_marvin", [Path("/raw")], batch.records)
        metadata["members"][0]["source_ref"] = {
            "attempt_id": f"00000000-0000-4000-8000-{attempt:012d}",
            "input_name": "raw_files", "input_index": 0, "logical_key": key,
            "sha256": hashlib.sha256(key.encode()).hexdigest(),
        }
        metadata["business_refs"] = [{
            "episode_index": 0, "dataset_root": "raw", "mcap_path": "raw/" + key,
            "rel_path": key, "part": "", "task": "",
        }]
        save_selection(folder, metadata)
        archive = folder.with_suffix(".zip")
        pack_artifact(archive, "normalized_batch", folder)
        return batch, metadata, archive

    def _statistics(self, items, *, statistics_offset=0):
        self.serial += 1
        batch, metadata, cohort = merge_normalized_episodes(
            [(batch, metadata) for batch, metadata, _ in items],
            self.folder / f"merged-{self.serial}", "jindian_marvin",
        )
        sys.path.insert(0, str(engine_root() / "qwen-manip-preprocess/src"))
        from robot_data_processing.types import GlobalStats
        from robot_data_processing.stage1_stats import Stage1GlobalStats
        cache = self.folder / f"cache-{self.serial}"
        cache.mkdir()
        count = sum(record.input_error is None for record in batch.records)
        global_stats = GlobalStats(*[np.arange(16, dtype=float) + statistics_offset for _ in range(8)],
                                   2 * count, count)
        stage1_stats = Stage1GlobalStats(*[np.arange(16, dtype=float) for _ in range(3)], 2 * count, count, 16)
        bundle = StatisticsBundle(global_stats, stage1_stats, None, 0, cache)
        context = StatisticsContext(batch, "ready", bundle, tuple({
            "episode_index": record.ref.episode_index,
            "fit_status": "excluded" if record.input_error else "included",
            "reason": "input_error" if record.input_error else None,
        } for record in batch.records), ("/raw/das/left/state", "/raw/das/right/state", 600_000_000))
        folder = self.folder / f"statistics-{self.serial}"
        save_context(context, folder, metadata)
        write_cohort(folder, cohort)
        # A statistics payload covers cohort.json with its internal file index,
        # as well as the independent outer archive checksums.
        payload = json.loads((folder / "context.json").read_text())
        payload["files"]["cohort.json"] = sha256(folder / "cohort.json")
        (folder / "context.json").write_text(json.dumps(payload))
        archive = folder.with_suffix(".zip")
        pack_artifact(archive, "statistics_context", folder)
        return archive, cohort

    def _context(self, episode_archive, statistics_archive):
        self.serial += 1
        return _Context(self.folder / f"workspace-{self.serial}", {
            "normalized_episode_ref": episode_archive, "statistics_context": statistics_archive,
        })

    def test_selects_cohort_identity_and_reuses_full_statistics_without_refit(self):
        target = self._single("z.mcap", 2)
        other = self._single("a.mcap", 1)
        statistics, cohort = self._statistics([target, other])
        ctx = self._context(target[2], statistics)
        loaded = []

        def capture(*args):
            result = load_context(*args)
            loaded.append(result[0])
            return result

        with patch.object(io, "load_context", side_effect=capture):
            context, metadata = io.read_statistics_context(ctx)
        full = loaded[0]
        self.assertEqual([r.ref.episode_index for r in full.batch.records], [0, 1])
        self.assertEqual([r.ref.episode_index for r in context.batch.records], [1])
        self.assertIs(context.statistics, full.statistics)
        self.assertEqual(context.statistics.global_stats.num_episodes, 2)
        self.assertEqual(context.coverage_policy, full.coverage_policy)
        self.assertEqual(context.fit_records, (full.fit_records[1],))
        self.assertEqual(metadata["members"][0]["source_ref"], target[1]["members"][0]["source_ref"])
        self.assertEqual(len(metadata["members"]), 1)
        self.assertEqual(len(metadata["business_refs"]), 1)
        self.assertEqual(metadata["_cohort"], cohort)
        self.assertEqual(len(cohort["members"]), 2)
        self.assertEqual(metadata["_statistics_context_digest"],
                         sha256(ctx.workspace.path("statistics-input") / "payload/context.json"))

    def test_same_cohort_with_changed_statistics_has_a_different_context_digest(self):
        target = self._single("a.mcap", 1)
        original_statistics, original_cohort = self._statistics([target])
        different_statistics, different_cohort = self._statistics([target], statistics_offset=1)
        _, first = io.read_statistics_context(self._context(target[2], original_statistics))
        _, second = io.read_statistics_context(self._context(target[2], different_statistics))
        self.assertEqual(original_cohort, different_cohort)
        self.assertEqual(first["_cohort"], second["_cohort"])
        self.assertNotEqual(first["_statistics_context_digest"], second["_statistics_context_digest"])

    def test_rejects_source_from_another_cohort(self):
        included = self._single("a.mcap", 1)
        foreign = self._single("z.mcap", 2)
        statistics, _ = self._statistics([included])
        with self.assertRaisesRegex(ValueError, "not a unique member"):
            io.read_statistics_context(self._context(foreign[2], statistics))

    def test_rejects_same_source_reference_with_changed_normalized_data(self):
        included = self._single("a.mcap", 1)
        altered = self._single("a.mcap", 1, value=0.9)
        self.assertEqual(included[1]["members"][0]["source_ref"], altered[1]["members"][0]["source_ref"])
        statistics, _ = self._statistics([included])
        with self.assertRaisesRegex(ValueError, "normalized content differs"):
            io.read_statistics_context(self._context(altered[2], statistics))

    def test_rejects_changed_frozen_origin_even_when_numeric_data_match(self):
        included = self._single("a.mcap", 1)
        another_attempt = self._single("a.mcap", 2)
        statistics, _ = self._statistics([included])
        with self.assertRaisesRegex(ValueError, "not a unique member"):
            io.read_statistics_context(self._context(another_attempt[2], statistics))

    def test_refuses_multi_episode_target_before_loading_statistics(self):
        first, second = self._single("a.mcap", 1), self._single("z.mcap", 2)
        batch, _, _ = merge_normalized_episodes(
            [(item[0], item[1]) for item in [first, second]], self.folder / "many", "jindian_marvin")
        archive = self.folder / "many.zip"
        pack_artifact(archive, "normalized_batch", batch.manifest_path.parent)
        ctx = self._context(archive, self.folder / "does-not-exist.zip")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            io.read_statistics_context(ctx)

    def test_quality_artifact_contains_one_selection_and_complete_cohort(self):
        target = self._single("z.mcap", 2)
        statistics, cohort = self._statistics([self._single("a.mcap", 1), target])
        ctx = self._context(target[2], statistics)
        _, metadata = io.read_statistics_context(ctx)
        decisions = io.portable_decisions([{
            "episode_index": 1, "source": "/raw/z.mcap", "status": "rejected", "reject_file": True,
            "reasons": ["s3:abnormal frame"], "diagnostic_errors": {},
        }], metadata)
        io.report_quality(ctx, decisions)
        io.write_episode_quality_result(ctx, decisions, metadata, "ready")
        self.assertEqual(ctx.report_calls, 1)
        self.assertEqual(ctx.quality_records[0]["decision"], "REJECT")
        self.assertEqual(set(ctx.published), {"episode_quality_result"})
        payload = unpack_artifact(ctx.published["episode_quality_result"], "quality_batch", self.folder / "restored")
        self.assertEqual(read_cohort(payload), cohort)
        selection = json.loads((payload / "selection.json").read_text())
        self.assertEqual(selection, {"source": "jindian_marvin", "members": [{
            "episode_index": 1, "source_ref": target[1]["members"][0]["source_ref"],
        }]})
        self.assertEqual(len((payload / "decisions.jsonl").read_text().splitlines()), 1)
        summary = json.loads((payload / "quality_summary.json").read_text())
        self.assertEqual(summary["statistics_context_digest"], metadata["_statistics_context_digest"])
        self.assertNotIn("_cohort", selection)
        self.assertNotIn("/raw/", (payload / "decisions.jsonl").read_text())

    def test_real_quality_rejects_only_selected_gap_episode(self):
        target = self._single("z.mcap", 2, gap=True)
        statistics, _ = self._statistics([self._single("a.mcap", 1), target])
        ctx = self._context(target[2], statistics)
        context, metadata = io.read_statistics_context(ctx)
        output = ctx.workspace.path("quality")
        output.mkdir()
        spec = make_handoff_spec(ctx, "statistics_context", metadata, output)
        result = QualityDetectionOperator().run_batch(context, spec, quality_config(spec))
        _, summary, records = read_quality_batch(result.manifest_path)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(summary["episodes"], 1)
        self.assertEqual(summary["rejected_files"], 1)
        self.assertEqual(summary["failed_episodes"], 0)
        self.assertEqual(records[0]["episode_index"], 1)
        self.assertEqual(records[0]["stage_results"]["gripper_gap"]["status"], "rejected")
        self.assertEqual(records[0]["stage_results"]["s1"]["status"], "skipped")

    def test_input_error_is_not_a_quality_rejection(self):
        target = self._single("z.mcap", 2, input_error="unsupported MCAP schema")
        statistics, _ = self._statistics([self._single("a.mcap", 1), target])
        ctx = self._context(target[2], statistics)
        context, metadata = io.read_statistics_context(ctx)
        output = ctx.workspace.path("quality")
        output.mkdir()
        spec = make_handoff_spec(ctx, "statistics_context", metadata, output)
        result = QualityDetectionOperator().run_batch(context, spec, quality_config(spec))
        _, summary, records = read_quality_batch(result.manifest_path)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(summary["failed_episodes"], 1)
        self.assertEqual(summary["rejected_files"], 0)
        self.assertEqual(records[0]["status"], "error")
        self.assertIn("unsupported MCAP schema", records[0]["error"])
        self.assertEqual(ctx.published, {})


if __name__ == "__main__":
    unittest.main()
