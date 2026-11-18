"""Joint-target freshness is quality evidence, not a sparse-input read error.

Only temporary synthetic MCAP files are created here. Actual user captures are
audited separately and are never added to this repository or changed by tests.
"""
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
from mcap.reader import make_reader
from mcap.writer import Writer

from mcap_quality_detect.core import QualityDetectionOperator
from mcap_read_normalize.core import McapReadOperator
from mcap_read_normalize.io import read_raw_files, write_normalized_episode_ref
from mcap_stats_global.core import GlobalStatsOperator
from pretrain_cleaning_common.artifact import unpack_artifact
from pretrain_cleaning_common.handoff import load_normalized
from pretrain_cleaning_common.measurement import joint_target_distances
from pretrain_cleaning_common.spec import quality_config
from pretrain_cleaning_common.storage import load_episode
from tests.pipelines.pretraining_data_cleaning import test_single_file_business as business


TARGET = "/command/marvin/joint_target"
LIMIT_NS = 100_000_000


def target_fixture(path, *, offset_ns=0, missing_gripper=False, include_target=True):
    """Write real CDR MCAP messages with exact integer target timestamp offsets."""
    business.marvin_fixture(path, missing_left_topic=missing_gripper)
    prepared = path.with_suffix(".prepared.mcap")
    target_count = 0
    with path.open("rb") as source, prepared.open("xb") as destination:
        writer = Writer(destination)
        writer.start()
        channels = {}
        for schema, channel, message in make_reader(source).iter_messages():
            # The pinned synthetic fixture uses raw CDR channels without a schema.
            if schema is not None:
                raise AssertionError("unexpected synthetic fixture schema")
            if channel.topic not in channels:
                channels[channel.topic] = writer.register_channel(channel.topic, channel.message_encoding, 0)
            timestamp = int(message.log_time)
            if channel.topic == TARGET:
                keep = include_target and target_count % 2 == 0
                target_count += 1
                if not keep:
                    continue
                timestamp += offset_ns
            writer.add_message(channels[channel.topic], timestamp, message.data, timestamp)
        writer.finish()
    prepared.replace(path)
    return np.arange(160, dtype=np.int64) * 40_000_000 + 1_000_000_000 + offset_ns


class JointTargetCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="joint-target-coverage-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def _normalize(self, path, key, workspace, *, successful=True):
        ctx, outputs = business.file_context(workspace, {"raw_files": [path]}, keys=[key])
        spec, refs, metadata = read_raw_files(ctx, ctx.workspace.path("normalization"))
        batch = McapReadOperator().run_batch(refs, spec, quality_config(spec))
        record, = batch.records
        if successful:
            self.assertIsNone(record.input_error)
        write_normalized_episode_ref(ctx, batch, metadata)
        folder = unpack_artifact(outputs["normalized_episode_ref"], "normalized_batch",
                                 workspace / "roundtrip")
        restored, _ = load_normalized(folder, "jindian_marvin")
        return restored, spec

    def _pipeline(self, workspace, paths, keys):
        previous = self.root
        self.root = workspace
        try:
            return business.SingleFileBusinessPipelineTests.run_pipeline(self, paths, keys)
        finally:
            self.root = previous

    def test_integer_nanosecond_boundary_and_source_facts_survive_normalization(self):
        for sign in (1, -1):
            for gap in (LIMIT_NS - 1, LIMIT_NS, LIMIT_NS + 1):
                with self.subTest(direction=sign, nearest_gap_ns=gap):
                    case = self.root / f"boundary-{sign}-{gap}"
                    path = case / "source.mcap"
                    target_times = target_fixture(path, offset_ns=sign * gap)
                    batch, spec = self._normalize(path, "source.mcap", case / "b1")
                    episode = load_episode(batch.records[0])
                    np.testing.assert_array_equal(episode.valid_measurement_times_ns[TARGET], target_times)
                    np.testing.assert_array_equal(episode.frame_times_ns,
                                                 np.arange(160, dtype=np.int64) * 40_000_000 + 1_000_000_000)
                    distances = joint_target_distances(episode, spec.joint_target_gap)
                    self.assertEqual(int(distances.max()), gap)
                    expected_mask = distances > LIMIT_NS
                    self.assertEqual(int(expected_mask.sum()), int(gap > LIMIT_NS))

                    stats_output = case / "statistics"
                    stats_output.mkdir()
                    stats = GlobalStatsOperator()
                    with patch.object(stats, "_fit", wraps=stats._fit) as fitted:
                        context = stats.run(batch, replace(spec, output_root=stats_output),
                                            quality_config(spec), recompute=True)
                    self.assertEqual(context.joint_target_gap_policy, (TARGET, LIMIT_NS))
                    fit, = context.fit_records
                    if gap <= LIMIT_NS:
                        self.assertEqual(context.status, "ready", context.error)
                        self.assertEqual(fit["fit_status"], "included")
                        self.assertEqual(fitted.call_count, 1)
                        self.assertIsNone(QualityDetectionOperator().precheck(episode, spec))
                    else:
                        self.assertEqual(context.status, "empty")
                        self.assertEqual(fit["fit_status"], "excluded")
                        self.assertEqual(fit["reason"], "joint target coverage insufficient for statistics")
                        self.assertEqual(fitted.call_count, 0)
                        decision = QualityDetectionOperator().precheck(episode, spec)
                        self.assertTrue(decision.reject_file)
                        self.assertEqual(decision.diagnostic_errors, {})
                        np.testing.assert_array_equal(decision.bad_mask, expected_mask)
                        report = decision.stage_results["joint_target_gap"]
                        self.assertEqual(report["status"], "rejected")
                        self.assertEqual(report["bad_frames"], 1)
                        self.assertEqual(report["topic"], TARGET)
                        self.assertEqual(report["max_nearest_gap_seconds"], 0.1)
                        self.assertEqual(report["max_observed_nearest_gap_seconds"], gap / 1e9)
                        interval, = report["detected_ranges"]
                        bad_index = 0 if sign == 1 else episode.num_frames - 1
                        self.assertEqual(interval["start_frame"], bad_index)
                        self.assertEqual(interval["end_frame_exclusive"], bad_index + 1)
                        self.assertEqual(interval["start_time_ns"], int(episode.frame_times_ns[bad_index]))
                        self.assertEqual(interval["end_time_ns"], int(episode.frame_times_ns[bad_index]) + 40_000_000)
                        self.assertTrue(all(decision.stage_results[stage]["status"] == "skipped"
                                            for stage in ("s1", "s2", "s3", "s4")))

    def test_mixed_batch_excludes_sparse_targets_without_changing_clean_statistics_or_stages(self):
        clean, sparse = self.root / "raw/clean.mcap", self.root / "raw/sparse.mcap"
        target_fixture(clean)
        target_fixture(sparse, offset_ns=LIMIT_NS + 1)
        _, baseline, baseline_decisions, _, _, _ = self._pipeline(
            self.root / "clean-only", [clean], ["clean.mcap"])
        fitted_sources = []
        original_fit = GlobalStatsOperator._fit

        def fit(instance, batch, spec, cfg, records, *, recompute=False):
            fitted_sources.append([str(record.ref.mcap_path) for record in records])
            return original_fit(instance, batch, spec, cfg, records, recompute=recompute)

        with patch.object(GlobalStatsOperator, "_fit", new=fit):
            _, mixed, decisions, _, summary, delivery = self._pipeline(
                self.root / "mixed", [sparse, clean], ["sparse.mcap", "clean.mcap"])
        self.assertEqual(fitted_sources, [["/raw/clean.mcap"]])
        self.assertEqual(mixed.statistics.global_stats.num_episodes, 1)
        fits = {record["episode_index"]: record for record in mixed.fit_records}
        self.assertEqual(fits[0]["fit_status"], "included")
        self.assertEqual(fits[1]["reason"], "joint target coverage insufficient for statistics")
        for name in ("global_stats", "stage1_stats", "alignment_stats"):
            expected, actual = getattr(baseline.statistics, name), getattr(mixed.statistics, name)
            self.assertEqual(type(expected), type(actual))
            if expected is not None:
                for field in fields(expected):
                    np.testing.assert_array_equal(getattr(actual, field.name), getattr(expected, field.name),
                                                  err_msg=f"{name}.{field.name}")
        self.assertEqual(mixed.statistics.alignment_lag, baseline.statistics.alignment_lag)
        by_source = {row["source"]: row for row in decisions}
        for key in ("status", "reject_file", "detected_bad_frames", "reasons"):
            self.assertEqual(by_source["clean.mcap"][key], baseline_decisions[0][key])
        for stage in ("s1", "s2", "s3", "s4"):
            self.assertEqual(by_source["clean.mcap"]["stage_results"][stage],
                             baseline_decisions[0]["stage_results"][stage])
        self.assertEqual(by_source["sparse.mcap"]["status"], "rejected")
        self.assertEqual(by_source["sparse.mcap"]["diagnostic_errors"], {})
        self.assertEqual((summary["kept_files"], summary["rejected_files"], summary["failed_episodes"]), (1, 1, 0))
        self.assertEqual([row["business"]["source_ref"]["logical_key"] for row in delivery["files"]], ["clean.mcap"])

    def test_all_sparse_target_batch_needs_no_fit_and_delivers_an_empty_archive(self):
        paths = [self.root / "raw/front.mcap", self.root / "raw/back.mcap"]
        target_fixture(paths[0], offset_ns=LIMIT_NS + 1)
        target_fixture(paths[1], offset_ns=-(LIMIT_NS + 1))
        with patch.object(GlobalStatsOperator, "_fit", side_effect=AssertionError("sparse targets must not fit")) as fitted:
            _, context, decisions, _, summary, delivery = self._pipeline(
                self.root / "all-sparse", paths, ["front.mcap", "back.mcap"])
        self.assertEqual(fitted.call_count, 0)
        self.assertEqual(context.status, "empty")
        self.assertIsNone(context.statistics)
        self.assertEqual(summary["rejected_files"], 2)
        self.assertEqual(summary["failed_episodes"], 0)
        self.assertTrue(all(row["diagnostic_errors"] == {} for row in decisions))
        self.assertTrue(all(row["stage_results"]["joint_target_gap"]["status"] == "rejected" for row in decisions))
        self.assertEqual(delivery["files"], [])

    def test_target_and_gripper_coverage_reasons_are_both_retained(self):
        source = self.root / "raw/both.mcap"
        target_fixture(source, offset_ns=LIMIT_NS + 1, missing_gripper=True)
        _, _, decisions, _, summary, delivery = self._pipeline(
            self.root / "both", [source], ["both.mcap"])
        row, = decisions
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["diagnostic_errors"], {})
        self.assertTrue(any(reason.startswith("joint_target_gap:invalid_frames=") for reason in row["reasons"]))
        self.assertTrue(any(reason.startswith("gripper_gap:invalid_frames=") for reason in row["reasons"]))
        self.assertEqual(row["stage_results"]["joint_target_gap"]["status"], "rejected")
        self.assertEqual(row["stage_results"]["gripper_gap"]["status"], "rejected")
        self.assertEqual(summary["failed_episodes"], 0)
        self.assertEqual(delivery["files"], [])

    def test_missing_target_metadata_is_an_error_and_old_statistics_policy_is_rejected(self):
        source = self.root / "raw/clean.mcap"
        target_fixture(source)
        batch, spec = self._normalize(source, "clean.mcap", self.root / "b1")
        episode = load_episode(batch.records[0])
        old_episode = replace(episode, valid_measurement_times_ns={
            topic: values for topic, values in episode.valid_measurement_times_ns.items() if topic != TARGET
        })
        with self.assertRaisesRegex(ValueError, "measurement timestamps"):
            joint_target_distances(old_episode, spec.joint_target_gap)
        with self.assertRaisesRegex(ValueError, "measurement timestamps"):
            QualityDetectionOperator().precheck(old_episode, spec)
        output = self.root / "statistics"
        output.mkdir()
        context = GlobalStatsOperator().run(batch, replace(spec, output_root=output), quality_config(spec), recompute=True)
        self.assertEqual(context.status, "ready", context.error)
        with self.assertRaisesRegex(ValueError, "target.*policy|policy.*target"):
            QualityDetectionOperator().run_batch(replace(context, joint_target_gap_policy=None), spec, quality_config(spec))

    def test_empty_target_topic_remains_a_read_error(self):
        source = self.root / "raw/empty-target.mcap"
        target_fixture(source, include_target=False)
        batch, _ = self._normalize(source, "empty-target.mcap", self.root / "b1", successful=False)
        record, = batch.records
        self.assertIsNone(record.data_path)
        self.assertIsNotNone(record.input_error)
        self.assertIn(TARGET, record.input_error)


if __name__ == "__main__":
    unittest.main()
