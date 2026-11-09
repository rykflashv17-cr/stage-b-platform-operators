import json
from types import SimpleNamespace

import pytest
import yaml
from operator_sdk.contracts import load
from operator_sdk.v2.local import run_local
from operator_sdk.v2.reporting import QualityReport
import os
from pathlib import Path
PROJECT=Path(os.environ["OPERATOR_TEST_PROJECT"])
from tests.support.quality import source_ref


def quality_case():
    members = [{"episode_index": i, "source": "/raw/" + p, "source_ref": source_ref(n, p)}
               for n, (i, p) in enumerate([(17, "a.mcap"), (3, "b.mcap")])]
    return {"members": members}


def test_decisions_match_input_episodes_by_identity_not_position(tmp_path):
    from mcap_quality_detect.io import portable_decisions, report_quality
    rows = [{"episode_index": 3, "source": "/raw/b.mcap", "status": "rejected", "reject_file": True},
            {"episode_index": 17, "source": "/raw/a.mcap", "status": "kept", "reject_file": False,
             "source_identity": {"st_ino": 9}, "deletion_eligible": True}]
    decisions = portable_decisions(rows, quality_case())
    q = QualityReport("quality", tmp_path, lambda: None)
    report_quality(SimpleNamespace(quality=q), decisions)
    details = [json.loads(line) for line in (tmp_path / "quality_records.jsonl").read_text().splitlines()]
    assert [(r["details"]["episode_index"], r["decision"]) for r in details] == [(3, "REJECT"), (17, "PASS")]
    assert all("record_id" not in r for r in details)
    assert decisions[1]["source"] == "a.mcap"
    assert decisions[1]["source_ref"] == source_ref(0, "a.mcap")
    assert "source_identity" not in decisions[1] and "deletion_eligible" not in decisions[1]


@pytest.mark.parametrize("pairs", [[(17, "a")], [(17, "a"), (17, "a")], [(17, "b"), (3, "a")],
                                  [(17, "a"), (99, "b")], [(True, "a"), (3, "b")]])
def test_invalid_episode_coverage_blocks_submission(tmp_path, pairs):
    from mcap_quality_detect.io import portable_decisions, report_quality
    rows = [dict(episode_index=i, source=f"/raw/{p}.mcap", status="kept") for i, p in pairs]
    declaration = load(PROJECT / "operator.yaml")
    declaration.update(inputs=[], outputs=[], parameters=[])
    path = tmp_path / "operator.yaml"
    path.write_text(yaml.safe_dump(declaration))
    with pytest.raises(ValueError):
        run_local(declaration_path=path, input_files={}, workspace=tmp_path / "attempt",
                  execute=lambda ctx: report_quality(ctx, portable_decisions(rows, quality_case())))
    published = tmp_path / "attempt/published"
    assert (published / "error.json").is_file()
    for absent in ("result.json", "quality.json", "quality_records.jsonl"):
        assert not (published / absent).exists()
