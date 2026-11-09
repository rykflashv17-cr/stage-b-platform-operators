from pathlib import Path

import pytest
import yaml

from tools.export_registration import registration
from tests.ci.helpers import selected_project

ROOT = Path(__file__).resolve().parents[2]


def test_registration_uses_fields_for_unrelated_operator(tmp_path):
    raw = yaml.safe_load((ROOT / "templates/python-operator/operator.yaml").read_text())
    raw.update(code="capture_read_sample", image="registry.example/data/capture_read_sample:1.0.0",
               inputs=[{"name":"raw_files","type":"array","minItems":1,
                        "items":{"type":"string","kind":"ARTIFACT"}},
                       {"name":"level","type":"integer","minimum":0}],
               outputs=[{"name":"batch","type":"object"},
                        {"name":"report","type":"string","kind":"ARTIFACT"}])
    path = tmp_path / "operator.yaml"
    path.write_text(yaml.safe_dump(raw))
    doc = registration(path)
    files, level = doc["ioDefinition"]["inputs"]
    assert doc["executionScope"] == "DATA"
    assert files["kind"] == "ARTIFACT" and files["items"]["type"] == "string"
    assert files["minItems"] == 1 and "kind" not in files["items"]
    assert level.get("kind", "VALUE") == "VALUE" and level["minimum"] == 0
    batch, report = doc["ioDefinition"]["outputs"]
    assert (batch["type"], batch.get("kind", "VALUE")) == ("object", "VALUE")
    assert (report["type"], report["kind"]) == ("string", "ARTIFACT")

    raw["batchMode"] = "chunk"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="chunk"):
        registration(path)


@pytest.mark.parametrize("field", [
    {"type": "object", "kind": "ARTIFACT"},
    {"type": "array", "kind": "ARTIFACT", "items": {"type": "string"}},
    {"type": "array", "items": {"type": "object", "kind": "ARTIFACT"}},
])
def test_registration_rejects_structured_artifacts(tmp_path, field):
    raw = yaml.safe_load((ROOT / "templates/python-operator/operator.yaml").read_text())
    raw["outputs"] = [{"name": "result", **field}]
    path = tmp_path / "operator.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="ARTIFACT"):
        registration(path)


def test_mcap_batch_handoffs_are_files_in_the_author_contract():
    names = ["raw_files", "normalized_batch_ref", "statistics_context",
             "batch_quality_result", "archive"]
    codes = ["mcap_read_normalize", "mcap_stats_global", "mcap_quality_detect", "mcap_delivery_accepted"]
    for i, code in enumerate(codes):
        path = selected_project(code) / 'operator.yaml'
        raw = yaml.safe_load(path.read_text())
        assert path.parent.name == raw['code']
        assert registration(path)["operatorVersion"] == raw['operatorVersion']
        assert [row["name"] for row in raw["inputs"]] == [names[i]]
        assert [row["name"] for row in raw["outputs"]] == [names[i + 1]]
        fields = raw["outputs"] + (raw["inputs"] if i else [])
        for field in fields:
            assert (field["type"], field["kind"]) == ("string", "ARTIFACT")
            assert not {"properties", "required"} & field.keys()
