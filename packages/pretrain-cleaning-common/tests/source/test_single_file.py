"""Business handoff checks independent of SDK and remote package installation."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from pretrain_cleaning_common.contracts import McapEpisodeRef, NormalizedBatchRef, NormalizedEpisode, NormalizedRecord
from pretrain_cleaning_common.handoff import save_selection, selection_metadata, validate_source_members
from pretrain_cleaning_common.single_file import (
    cohort_member, merge_normalized_episodes, normalized_record_digest,
    read_cohort, validate_single_episode, write_cohort,
)
from pretrain_cleaning_common.storage import load_episode, save_batch, save_episode


def singleton(folder, key, attempt, *, source="jindian_marvin", slot=0, error=None):
    folder.mkdir(parents=True)
    virtual = Path("/raw") / key
    relative = str(Path(key).parent) if source == "abc_130k" else key
    ref = McapEpisodeRef(0, Path("/raw"), virtual, relative)
    path = None
    if error is None:
        path = folder / "episode_00000000.npz"
        width = 32 if source == "abc_130k" else 16
        values = np.full((2, width), 0.2)
        save_episode(path, NormalizedEpisode(ref, values, values, np.array([2**53 + 1, 2**53 + 101], dtype=np.int64)))
    record = NormalizedRecord(ref, path, input_error=error)
    batch = NormalizedBatchRef(folder / "manifest.json", (record,))
    metadata = selection_metadata(source, [Path("/raw")], batch.records)
    metadata["members"][0]["source_ref"] = {
        "attempt_id": f"00000000-0000-4000-8000-{attempt:012x}", "input_name": "raw_files", "input_index": slot,
        "logical_key": key, "sha256": hashlib.sha256(key.encode()).hexdigest(),
    }
    metadata["business_refs"] = [{"episode_index": 0, "dataset_root": "raw", "mcap_path": str(virtual.relative_to("/")),
                                  "rel_path": relative, "part": "", "task": ""}]
    save_batch(batch)
    save_selection(folder, metadata)
    return batch, metadata


class SingleFileHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="mcap-single-handoff-")
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_independent_attempts_join_in_key_order_and_keep_frozen_slots(self):
        z = singleton(self.root / "z", "z/episode.mcap", 1)
        a = singleton(self.root / "a", "a/episode.mcap", 2)
        batch, metadata, cohort = merge_normalized_episodes([z, a], self.root / "merged", "jindian_marvin")
        self.assertEqual([r.ref.episode_index for r in batch.records], [0, 1])
        self.assertEqual([m["source_ref"]["logical_key"] for m in metadata["members"]], ["a/episode.mcap", "z/episode.mcap"])
        self.assertEqual([m["source_ref"]["input_index"] for m in metadata["members"]], [0, 0])
        self.assertEqual([r.data_path.name for r in batch.records], ["episode_00000000.npz", "episode_00000001.npz"])
        self.assertEqual(normalized_record_digest(a[0].records[0]), cohort["members"][0]["normalized_digest"])
        self.assertEqual(load_episode(batch.records[0]).frame_times_ns[0], 2**53 + 1)
        self.assertEqual(cohort_member(cohort, a[1]["members"][0]["source_ref"])["episode_index"], 0)
        self.assertEqual([m["source_episode_index"] for m in metadata["members"]], [0, 0])

    def test_repeated_logical_key_and_repeated_handoff_are_rejected(self):
        one = singleton(self.root / "one", "one.mcap", 1)
        other = singleton(self.root / "other", "one.mcap", 2)
        for index, items in enumerate(([one, other], [one, one])):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                merge_normalized_episodes(items, self.root / f"bad-{index}", "jindian_marvin")

    def test_mixed_profile_and_non_singleton_are_rejected(self):
        one = singleton(self.root / "one", "one.mcap", 1)
        abc = singleton(self.root / "abc", "data/train/task/e/episode.mcap", 2, source="abc_130k", slot=1)
        with self.assertRaisesRegex(ValueError, "mixed source"):
            merge_normalized_episodes([one, abc], self.root / "bad", "jindian_marvin")
        with self.assertRaises(ValueError):
            validate_single_episode(replace(one[0], records=()), one[1])

    def test_abc_primary_slot_one_survives_separate_attempts(self):
        items = [singleton(self.root / str(i), f"data/train/task/e{i}/episode.mcap", i + 1,
                           source="abc_130k", slot=1) for i in range(2)]
        batch, metadata, cohort = merge_normalized_episodes(items, self.root / "merged", "abc_130k")
        self.assertEqual([m["source_ref"]["input_index"] for m in metadata["members"]], [1, 1])
        self.assertEqual(len(cohort["members"]), 2)
        self.assertEqual(load_episode(batch.records[0]).state.shape, (2, 32))
        self.assertFalse(list((self.root / "merged").rglob("*.parquet")))

    def test_unreadable_episode_remains_in_cohort_without_numeric_data(self):
        item = singleton(self.root / "input", "input.mcap", 1, error="unsupported source schema")
        batch, _, cohort = merge_normalized_episodes([item], self.root / "merged", "jindian_marvin")
        self.assertIsNone(batch.records[0].data_path)
        self.assertEqual(batch.records[0].input_error, "unsupported source schema")
        self.assertEqual(cohort["members"][0]["normalized_digest"], normalized_record_digest(item[0].records[0]))

    def test_cohort_checks_full_context_content_and_its_own_identity(self):
        item = singleton(self.root / "input", "one.mcap", 1)
        batch, metadata, cohort = merge_normalized_episodes([item], self.root / "merged", "jindian_marvin")
        context = self.root / "context"
        context.mkdir()
        shutil.copytree(batch.manifest_path.parent, context / "normalized")
        write_cohort(context, cohort)
        self.assertEqual(read_cohort(context, metadata), cohort)
        data = context / "normalized" / batch.records[0].data_path.name
        with data.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(ValueError, "content|members"):
            read_cohort(context, metadata)
        corrupt = {**cohort, "cohort_id": "0" * 64}
        (context / "cohort.json").write_text(json.dumps(corrupt))
        with self.assertRaisesRegex(ValueError, "checksum"):
            read_cohort(context)

    def test_frozen_slot_uniqueness_is_per_attempt_and_still_bounded(self):
        one = singleton(self.root / "one", "one.mcap", 1)
        two = singleton(self.root / "two", "two.mcap", 2)
        members = [one[1]["members"][0], {**two[1]["members"][0], "episode_index": 1}]
        self.assertEqual(len(validate_source_members("jindian_marvin", members)), 2)
        bad = {**members[1], "source_ref": {**members[1]["source_ref"], "attempt_id": members[0]["source_ref"]["attempt_id"]}}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_source_members("jindian_marvin", [members[0], bad])
        bad = {**members[1], "source_ref": {**members[1]["source_ref"], "input_index": 1}}
        with self.assertRaisesRegex(ValueError, "out-of-range"):
            validate_source_members("jindian_marvin", [members[0], bad])


if __name__ == "__main__":
    unittest.main()
