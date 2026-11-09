"""B02 admission failures must preserve every record and fail damaged batches."""
from types import SimpleNamespace
import json

from mcap_stats_global.core import GlobalStatsOperator
from pretraining_data_cleaning_common.contracts import McapEpisodeRef, NormalizedBatchRef, NormalizedRecord
from pretraining_data_cleaning_common.handoff import make_spec


def test_input_rejection_is_retained_without_fitting(tmp_path):
    ref = McapEpisodeRef(7, tmp_path, tmp_path / "input.mcap", "input.mcap")
    batch = NormalizedBatchRef(tmp_path / "manifest.json", (NormalizedRecord(ref, None, input_error="bad input"),))
    spec = make_spec("jindian_marvin", tmp_path, tmp_path / "out", 1)
    spec.output_root.mkdir()
    result = GlobalStatsOperator().run(batch, spec, SimpleNamespace())
    assert result.status == "empty"
    assert result.fit_records == ({"episode_index": 7, "fit_status": "excluded", "reason": "bad input"},)
    saved = json.loads((spec.output_root / "statistics_context.json").read_text())
    assert saved["included_episodes"] == 0 and saved["excluded_episodes"] == 1


def test_damaged_intermediate_cannot_be_reported_as_an_empty_success(tmp_path):
    ref = McapEpisodeRef(7, tmp_path, tmp_path / "input.mcap", "input.mcap")
    batch = NormalizedBatchRef(tmp_path / "manifest.json", (NormalizedRecord(ref, tmp_path / "missing.npz"),))
    spec = make_spec("jindian_marvin", tmp_path, tmp_path / "out", 1)
    spec.output_root.mkdir()
    result = GlobalStatsOperator().run(batch, spec, SimpleNamespace())
    assert result.status == "failed"
    assert "normalized input unavailable" in result.error
