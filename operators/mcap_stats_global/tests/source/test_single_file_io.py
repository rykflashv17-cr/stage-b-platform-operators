"""The B02 portable context must checksum its full cohort."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from mcap_stats_global.io import write_statistics_context
from pretrain_cleaning_common.artifact import unpack_artifact
from pretrain_cleaning_common.contracts import McapEpisodeRef, NormalizedBatchRef, NormalizedRecord, StatisticsContext
from pretrain_cleaning_common.handoff import load_context, save_selection, selection_metadata, sha256
from pretrain_cleaning_common.single_file import make_cohort, read_cohort
from pretrain_cleaning_common.storage import save_batch


class StatisticsCohortIOTests(unittest.TestCase):
    def test_empty_context_cohort_is_in_verified_file_index(self):
        with TemporaryDirectory(prefix="mcap-statistics-io-") as temporary:
            root = Path(temporary).resolve()
            normalized = root / "normalized"
            normalized.mkdir()
            ref = McapEpisodeRef(0, Path("/raw"), Path("/raw/one.mcap"), "one.mcap")
            record = NormalizedRecord(ref, None, input_error="unsupported source schema")
            batch = NormalizedBatchRef(normalized / "manifest.json", (record,))
            metadata = selection_metadata("jindian_marvin", [Path("/raw")], batch.records)
            metadata["members"][0]["source_ref"] = {
                "attempt_id": "00000000-0000-4000-8000-000000000001", "input_name": "raw_files", "input_index": 0,
                "logical_key": "one.mcap", "sha256": hashlib.sha256(b"raw").hexdigest(),
            }
            metadata["business_refs"] = [{"episode_index": 0, "dataset_root": "raw", "mcap_path": "raw/one.mcap",
                                          "rel_path": "one.mcap", "part": "", "task": ""}]
            save_batch(batch)
            save_selection(normalized, metadata)
            cohort = make_cohort(batch, metadata)
            context = StatisticsContext(batch, "empty", None, ({"episode_index": 0, "fit_status": "excluded", "reason": "input error"},), None)
            outputs = []
            ctx = SimpleNamespace(check_cancelled=lambda: None,
                                  workspace=SimpleNamespace(path=lambda name: root / name),
                                  outputs=SimpleNamespace(file=lambda name, path: outputs.append((name, path))))
            write_statistics_context(ctx, context, metadata, root / "context", cohort)
            self.assertEqual(outputs[0][0], "statistics_context")
            folder = unpack_artifact(outputs[0][1], "statistics_context", root / "restored")
            payload = json.loads((folder / "context.json").read_text())
            self.assertEqual(payload["files"]["cohort.json"], sha256(folder / "cohort.json"))
            restored, meta = load_context(folder, "jindian_marvin")
            self.assertEqual(restored.status, "empty")
            self.assertEqual(read_cohort(folder, meta), cohort)


if __name__ == "__main__":
    unittest.main()
