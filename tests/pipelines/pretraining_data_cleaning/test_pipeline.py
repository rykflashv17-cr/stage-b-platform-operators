from ci import ROOT
"""Separate-process acceptance with the fixed real numerical engine."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import mkdtemp
from zipfile import ZipFile

import pytest
import yaml
from tests.support.pretraining_samples import write_abc
from importlib.metadata import version as installed_version

CODES=["mcap_read_normalize","mcap_stats_global","mcap_quality_detect","mcap_delivery_accepted"]

VERSIONS = {code: ('5.0.0' if code==CODES[3] else '4.0.1') for code in CODES}


def project(code):
    return ROOT / 'operators' / code


def environment():
    # Every subprocess uses its own declared SDK/dependency environment.
    for code in CODES:
        assert (project(code)/'.venv/bin/python').is_file(), 'Run make setup OPERATOR='+code
    env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"}
    env.pop("PYTHONPATH",None)
    return env

def call(code, directory, inputs, parameters, store, roots, *, expected_code=0, storage_files=None):
    directory.mkdir()
    request={"input_files":inputs,"parameter_overrides":parameters,"workspace":str(directory / "work"),"artifact_store":str(store),"read_roots":list(map(str,roots))}
    if storage_files is not None:request["storage_files"]=storage_files
    file=directory / "request.json";file.write_text(json.dumps(request))
    process=subprocess.run([str(project(code)/".venv/bin/python"),"-I",str(project(code) / "main.py"),"--local-request-file",str(file)],env=environment(),capture_output=True,text=True,timeout=90)
    assert process.returncode==expected_code,(process.stdout,process.stderr)
    report = json.loads(process.stdout)
    log = directory / "work/published/operator.log"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["sequence"] for e in entries] == list(range(len(entries)))
    assert all(e["execution"]["operator_code"] == code for e in entries)
    if expected_code == 0:
        assert entries and entries == report["logs"]
    return report

def descriptor(report,name):
    saved=report["outputs"][name][0]
    return [{"fixture_path":saved["path"],"local_reference":saved["local_reference"]}]

def published_file(saved, store):
    """Import a single-file output as a consumer would, including after docker cp."""
    from operator_sdk.storage import LocalArtifactStore
    from operator_sdk.v2.local import run_local

    repository = LocalArtifactStore(store)
    reference = saved["local_reference"]
    folder = Path(mkdtemp(prefix="read-output-", dir=Path(store).parent))
    consumer = yaml.safe_load((project(CODES[3]) / "operator.yaml").read_text())
    consumer.pop("delivery",None)
    consumer.update(code="mcap_inspect_file", image="registry.test/mcap_inspect_file:" + consumer["operatorVersion"],
                    inputs=[{"name": "result_file", "kind": "ARTIFACT", "type": "string"}], outputs=[])
    declaration = folder / "operator.yaml"
    declaration.write_text(yaml.safe_dump(consumer))
    materialized = []
    run_local(declaration_path=declaration, execute=lambda ctx: materialized.append(ctx.inputs.file("result_file")),
              input_files={"result_file": [{"local_reference": reference, "fixture_path": repository.resolve(reference).path}]},
              workspace=folder / "work", artifact_store=repository)
    path, = materialized
    return path


def check_package(path, kind, destination, raw_digests):
    """Inspect every published ZIP member, including any outside the verified payload."""
    from pretraining_data_cleaning_common.artifact import unpack_artifact

    with ZipFile(path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            suffix = Path(member.filename).suffix.lower()
            assert suffix not in {".mcap", ".parquet"}, member.filename
            if kind == "quality_batch":
                assert suffix in {".json", ".jsonl"}, member.filename
            body = archive.read(member)
            assert hashlib.sha256(body).hexdigest() not in raw_digests, member.filename
    return unpack_artifact(path, kind, destination)


def raw_inputs(fixture, keys):
    fixture.write_text(json.dumps(list(keys)))
    return {"raw_files": [{"fixture_path": str(fixture)}]}


def marvin_source(path,**kwargs):
    reference=ROOT / "tests/fixtures/pretraining_data_cleaning_reference"
    spec=importlib.util.spec_from_file_location("pretraining_fixture_mcap",reference / "tests/test_mcap_integration.py")
    module=importlib.util.module_from_spec(spec)
    sys.path.insert(0,str(reference))
    try: spec.loader.exec_module(module)
    finally: sys.path.remove(str(reference))
    return module.write_marvin_mcap(path,**kwargs)

@pytest.mark.parametrize("case",["marvin","gaps","abc","local_file_output"])
def test_four_independent_processes_preserve_inputs_and_complete_reports(tmp_path,case):
    source="abc_130k" if case=="abc" else "jindian_marvin"
    root=tmp_path / "sources";root.mkdir()
    if case=="abc": write_abc(root)
    else:
        marvin_source(root / "clean.mcap")
        if case=="gaps": marvin_source(root / "gap.mcap",missing_left_topic=True)
    before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()}
    episode_count = len(list(root.rglob("*.mcap")))
    store=tmp_path / "store"
    roots=[root]
    params={"source":source,"workers":1}
    if case == "local_file_output":
        from operator_sdk.storage import LocalArtifactStore
        from operator_sdk.v2.local import run_local

        producer = yaml.safe_load((project(CODES[0]) / "operator.yaml").read_text())
        producer.update(code="capture_collect_files", inputs=[], parameters=[], outputs=producer["inputs"],
                        image="registry.test/capture_collect_files:" + producer["operatorVersion"])
        declaration = tmp_path / "producer.yaml"
        declaration.write_text(yaml.safe_dump(producer))

        def produce(ctx):
            path = ctx.workspace.path("clean.mcap")
            path.write_bytes(before["clean.mcap"])
            ctx.outputs.files("raw_files", [path])

        saved = run_local(declaration_path=declaration, execute=produce, input_files={},
                          workspace=tmp_path / "producer", artifact_store=LocalArtifactStore(store))
        inputs = {"raw_files": descriptor(saved, "raw_files")}
        roots = []
    else:
        inputs = raw_inputs(tmp_path / "input.json", sorted(before, reverse=True))
    b1=call(CODES[0],tmp_path / "b1",inputs,params,store,roots)
    b2=call(CODES[1],tmp_path / "b2",{"normalized_batch_ref":descriptor(b1,"normalized_batch_ref")},params,store,[])
    b3=call(CODES[2],tmp_path / "b3",{"statistics_context":descriptor(b2,"statistics_context")},params,store,[])
    b4=call(CODES[3],tmp_path / "b4",{"batch_quality_result":descriptor(b3,"batch_quality_result")},{},store,[root],storage_files={key:str(root/key) for key in before if key.endswith(".mcap")})
    expected={"marvin":(1,0),"local_file_output":(1,0),"gaps":(1,1),"abc":(0,1)}[case]
    raw_digests = {hashlib.sha256(data).hexdigest() for data in before.values()}
    normalized, statistics, judgments = [
        check_package(published_file(report["outputs"][name][0], store), kind, tmp_path / kind, raw_digests)
        for report, name, kind in [(b1, "normalized_batch_ref", "normalized_batch"),
                                   (b2, "statistics_context", "statistics_context"),
                                   (b3, "batch_quality_result", "quality_batch")]
    ]
    final = published_file(b4["outputs"]["archive"][0], store)
    accepted = [] if case == "abc" else [{
        "episode_index": 0,
        "source_ref": {"attempt_id": b1["execution"]["attempt_id"], "input_name": "raw_files",
                       "input_index": sorted(before, reverse=True).index("clean.mcap"),
                       "logical_key": "clean.mcap", "sha256": hashlib.sha256(before["clean.mcap"]).hexdigest()},
    }]
    from tests.pipelines.pretraining_data_cleaning.container_acceptance import require_delivery_archive
    require_delivery_archive(final,b1['execution']['attempt_id'],sorted(before,reverse=True),
        {k:hashlib.sha256(v).hexdigest() for k,v in before.items()},[] if case=='abc' else [(0,'clean.mcap')])
    assert before=={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob("*") if p.is_file()}
    quality=json.loads((tmp_path / "b3/work/published/quality.json").read_text())
    assert quality["summary"]["total"]==episode_count
    assert quality["summary"]["passed"]==expected[0]
    assert quality["summary"]["rejected"]==expected[1]
    for stage in ("b1","b2","b4"):
        assert not (tmp_path / stage / "work/published/quality.json").exists()
    assert set(json.loads((tmp_path / "b3/work/published/result.json").read_text()))=={"batch_quality_result"}
    if case in ("marvin", "gaps"):
        # Exact original-source comparison with the real pinned engine. ABC has an
        # explicit source interface defect, covered separately by timeline tests.
        reference=ROOT / "tests/fixtures/pretraining_data_cleaning_reference"
        from pretraining_data_cleaning_engine import root as engine_root
        engine=engine_root()
        env={**os.environ,"PYTHONPATH":str(reference),"PYTHONDONTWRITEBYTECODE":"1"}
        output=tmp_path / "reference"
        process=subprocess.run([sys.executable,"-m","stage_b_mcap_v4.cli","quality","--config",str(reference / "configs/jindian_marvin.yaml"),"--repo-root",str(engine),"--input-root",str(root),"--output-root",str(output)],env=env,capture_output=True,text=True,timeout=90)
        assert process.returncode==0,(process.stdout,process.stderr)
        # Compare all engine verdict evidence. Source locations and file operation
        # diagnostics belong to the old delivery contract and vary by workspace.
        fields = ("episode_index", "status", "reject_file", "reasons", "num_frames", "detected_bad_frames",
                  "detected_ranges", "effective_removed_ranges", "stage_results", "diagnostic_errors")
        def decisions(directory):
            return [{key: row[key] for key in fields}
                    for line in (directory / "decisions.jsonl").read_text().splitlines()
                    for row in [json.loads(line)]]
        assert decisions(judgments) == decisions(output)
        import numpy as np
        for actual, baseline in [(normalized, output / "normalized"), (statistics / "cache", output / "cache")]:
            expected_files = {p.name for p in baseline.glob("*.npz")}
            assert expected_files and {p.name for p in actual.glob("*.npz")} == expected_files
            for filename in expected_files:
                with np.load(actual / filename, allow_pickle=False) as got, np.load(baseline / filename, allow_pickle=False) as reference:
                    assert set(got.files) == set(reference.files)
                    for key in got.files:
                        np.testing.assert_array_equal(got[key], reference[key])


def test_input_error_causes_quality_failure_without_success_result(tmp_path):
    root=tmp_path / "sources";root.mkdir()
    bad=root / "bad.mcap";bad.write_bytes(b"broken source")
    store=tmp_path / "store";params={"source":"jindian_marvin","workers":1}
    b1=call(CODES[0],tmp_path / "b1",raw_inputs(tmp_path / "input.json", ["bad.mcap"]),params,store,[root])
    b2=call(CODES[1],tmp_path / "b2",{"normalized_batch_ref":descriptor(b1,"normalized_batch_ref")},params,store,[root])
    result=call(CODES[2],tmp_path / "b3",{"statistics_context":descriptor(b2,"statistics_context")},params,store,[root],expected_code=1)
    assert result["validation"]=="failed"
    assert not (tmp_path / "b3/work/published/result.json").exists()
    assert not (tmp_path / "b3/work/published/quality.json").exists()
    assert (tmp_path / "b3/work/published/error.json").exists()
    assert bad.read_bytes()==b"broken source"

def test_statistics_fit_failure_ends_node_without_success_result(tmp_path,monkeypatch):
    from operator_sdk.v2.local import run_local
    from operator_sdk.storage import LocalArtifactStore
    monkeypatch.syspath_prepend(str(project(CODES[1])/'src'))
    from mcap_stats_global.operator import GlobalStatsOperator
    from mcap_stats_global.core import GlobalStatsOperator as Business
    root=tmp_path / "sources";root.mkdir();marvin_source(root / "clean.mcap")
    store=tmp_path / "store";params={"source":"jindian_marvin","workers":1}
    b1=call(CODES[0],tmp_path / "b1",raw_inputs(tmp_path / "input.json", ["clean.mcap"]),params,store,[root])
    def failed(*args,**kwargs):raise OSError("injected statistics failure")
    monkeypatch.setattr(Business,"_fit",failed)
    with pytest.raises(RuntimeError,match="statistics failed"):
        run_local(declaration_path=project(CODES[1]) / "operator.yaml",execute=GlobalStatsOperator().execute,input_files={"normalized_batch_ref":descriptor(b1,"normalized_batch_ref")},workspace=tmp_path / "failed",parameter_overrides=params,artifact_store=LocalArtifactStore(store),read_roots=[root])
    assert not (tmp_path / "failed/published/result.json").exists()
    assert (tmp_path / "failed/published/error.json").exists()


def test_abc_pairs_by_directory_and_indexes_by_sorted_key(tmp_path):
    root = tmp_path / "sources"
    first, _, _ = write_abc(root, start=3_000_000_000)
    first.parent.rename(first.parent.with_name("episode_001"))
    write_abc(root)
    parent = "data/train/pick/"
    keys = [parent + "episode_001/eef_pose.parquet", parent + "episode_000/episode.mcap",
            parent + "episode_001/episode.mcap", parent + "episode_000/eef_pose.parquet"]
    params = {"source": "abc_130k", "workers": 1}
    store = tmp_path / "store"
    for idx, selection in enumerate([keys, list(reversed(keys))]):
        report = call(CODES[0], tmp_path / f"b1-{idx}", raw_inputs(tmp_path / "keys.json", selection),
                      params, store, [root])
        output = report["outputs"]["normalized_batch_ref"][0]
        folder = check_package(published_file(output, store), "normalized_batch", tmp_path / f"batch-{idx}", set())
        records = json.loads((folder / "manifest.json").read_text())["records"]
        assert [(r["ref"]["episode_index"], r["ref"]["rel_path"], r["ref"]["part"], r["ref"]["task"])
                for r in records] == [(i, parent + f"episode_{i:03}", "train", "pick") for i in range(2)]
        assert all(r["data_file"] and not r["input_error"] for r in records)


def test_only_explicit_files_are_read(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    marvin_source(root / "selected.mcap")
    (root / "unselected.mcap").write_bytes(b"not selected")
    report = call(CODES[0], tmp_path / "b1", raw_inputs(tmp_path / "keys.json", ["selected.mcap"]),
                  {"source": "jindian_marvin", "workers": 1}, tmp_path / "store", [root])
    saved, = report["outputs"]["normalized_batch_ref"]
    folder = check_package(published_file(saved, tmp_path / "store"), "normalized_batch", tmp_path / "batch", set())
    records = json.loads((folder / "manifest.json").read_text())["records"]
    assert [(r["ref"]["episode_index"], r["ref"]["rel_path"]) for r in records] == [(0, "selected.mcap")]


@pytest.mark.parametrize("source,keys", [
    ("jindian_marvin", []),
    ("jindian_marvin", ["clean.mcap", "clean.mcap"]),
    ("jindian_marvin", ["../outside.mcap"]),
    ("jindian_marvin", ["clean.mcap", "data/train/pick/episode_000/eef_pose.parquet"]),
    ("abc_130k", ["data/train/pick/episode_000/episode.mcap"]),
    ("abc_130k", ["data/train/pick/episode_000/eef_pose.parquet"]),
])
def test_invalid_raw_files_fail_without_publishing_a_batch(tmp_path, source, keys):
    root = tmp_path / "sources"
    write_abc(root)
    marvin_source(root / "clean.mcap")
    marvin_source(tmp_path / "outside.mcap")
    report = call(CODES[0], tmp_path / "b1", raw_inputs(tmp_path / "keys.json", keys),
                  {"source": source, "workers": 1}, tmp_path / "store", [root], expected_code=1)
    assert report["validation"] == "failed"
    published = tmp_path / "b1/work/published"
    assert (published / "error.json").is_file()
    assert not (published / "result.json").exists()
    assert not (published / "quality.json").exists()
