"""B03 rejects missing evidence and keeps original business source identity."""
import pytest

from mcap_quality_detect.io import portable_decisions


def test_decision_preserves_origin_without_leaking_local_path():
    source = {"logical_key": "capture/one.mcap", "input_index": 3}
    metadata = {"members": [{"episode_index": 7, "source": "/raw/one.mcap", "source_ref": source}]}
    decisions = portable_decisions([{"episode_index": 7, "source": "/raw/one.mcap", "status": "kept",
                                    "private_path": "/tmp/execution"}], metadata)
    assert decisions == [{"episode_index": 7, "source": "capture/one.mcap", "source_ref": source, "status": "kept"}]


@pytest.mark.parametrize("records", [[], [{"episode_index": 9, "source": "/raw/one.mcap"}],
                                     [{"episode_index": 7, "source": "/raw/other.mcap"}]])
def test_incomplete_or_mismatched_evidence_is_rejected(records):
    with pytest.raises(ValueError):
        portable_decisions(records, {"members": [{"episode_index": 7, "source": "/raw/one.mcap", "source_ref": {}}]})
