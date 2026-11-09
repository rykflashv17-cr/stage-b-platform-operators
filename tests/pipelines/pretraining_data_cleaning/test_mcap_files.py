from ci import ROOT
"""Registration and ingress use the same file shapes exercised by Pipeline."""
from pathlib import Path
import pytest
import yaml
from pretraining_data_cleaning_common.archive import read_bundle

from tests.pipelines.pretraining_data_cleaning.mcap_files import pack_episode, registration
from tests.ci.helpers import selected_project


def test_registration_uses_physical_artifacts_without_changing_business_names():
    root = ROOT
    previous = None
    for code in ("mcap_read_normalize","mcap_stats_global","mcap_quality_detect","mcap_delivery_accepted"):
        path = selected_project(code) / 'operator.yaml'
        doc = registration(path)
        inp, out = doc["ioDefinition"]["inputs"][0], doc["ioDefinition"]["outputs"][0]
        assert doc["executionScope"] == "BATCH" and path.parent.name == doc['operatorCode']
        assert inp["kind"] == out["kind"] == "ARTIFACT" and out["type"] == "string"
        if previous:
            assert (inp["name"],inp["type"]) == (previous,"string")
        else:
            assert inp["name"] == "raw_files" and inp["minItems"] == 1
            assert inp["type"] == "array" and inp["items"]["type"] == "string" and "kind" not in inp["items"]
        previous = out["name"]


def test_registration_derives_file_and_value_ports_from_an_unrelated_operator(tmp_path):
    root = ROOT
    declaration = yaml.safe_load((root / "templates/python-operator/operator.yaml").read_text())
    declaration.update(code="capture_transfer_outputs", image="registry.test/capture_transfer_outputs:" + declaration["operatorVersion"],
                       inputs=[{"name": "frames", "type": "array", "items": {"type": "string", "kind": "ARTIFACT"}},
                               {"name": "tags", "type": "array", "items": {"type": "string"}}],
                       outputs=[{"name": "note", "type": "string"}])
    path = tmp_path / "operator.yaml"
    path.write_text(yaml.safe_dump(declaration))
    doc = registration(path)["ioDefinition"]
    frames, tags = doc["inputs"]
    assert (frames["kind"], frames["type"], frames["items"]["type"]) == ("ARTIFACT", "array", "string")
    assert tags.get("kind", "VALUE") == "VALUE" and tags["items"]["type"] == "string"
    assert doc["outputs"][0].get("kind", "VALUE") == "VALUE"


def test_pack_episode_only_selects_one_file_and_required_sidecar(tmp_path):
    root = tmp_path / "data"
    episode = root / "task/ep"
    episode.mkdir(parents=True)
    mcap = episode / "episode.mcap"
    mcap.write_bytes(b"selected")
    (root/"unselected.mcap").write_bytes(b"excluded")
    pack = tmp_path/"source.zip"
    with pytest.raises(ValueError):
        pack_episode(pack, root, mcap, "abc_130k", 7, "dataset", "train", "pick")
    assert not pack.exists()
    (episode/"eef_pose.parquet").write_bytes(b"sidecar")
    pack_episode(pack, root, mcap, "abc_130k", 7, "dataset", "train", "pick")
    result = tmp_path / "check"
    val = read_bundle(pack,result)
    assert val == dict(dataset_root="dataset",episode_index=7,mcap_path="dataset/task/ep/episode.mcap",
                       rel_path="task/ep",part="train",task="pick")
    assert sorted(p.name for p in result.rglob("*") if p.is_file())==["eef_pose.parquet","episode.mcap"]
