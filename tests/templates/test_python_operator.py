"""Template I/O behavior through the real SDK and fixed entrypoint."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ci import ROOT
import os

def environment():
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}
    if os.getenv('OPERATOR_TEMPLATE_MODE')=='wheel':env.pop('PYTHONPATH',None)
    else:env['PYTHONPATH']=str(ROOT/'templates/python-operator/src')
    return env


TEMPLATE = ROOT / "templates/python-operator"


def invoke_template(tmp_path, record, parameters=None, *, io_only=False):
    payload = tmp_path / "record.json"
    payload.write_text(json.dumps(record))
    request = tmp_path / "request.json"
    value = {
        "input_files": {"record": [{"fixture_path": str(payload)}]},
        "workspace": str(tmp_path / "work"),
    }
    if parameters is not None:
        value["parameter_overrides"] = parameters
    request.write_text(json.dumps(value))
    env = environment()
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] += ":" + str(TEMPLATE / "src")
    if io_only:
        # Exercise the new I/O API with a literal business decision and real
        # SDK validation/publication, independently of the algorithm.
        script = """
import importlib.util
from pathlib import Path
import sys
from operator_sdk.v2.startup import main
assert importlib.util.find_spec("quality_gate.io") is not None, "template I/O module missing"
from quality_gate.io import read_record, write_decision, report_quality

def execute(ctx):
    record = read_record(ctx)
    decision = {"record_id": record["record_id"], "keep": False}
    write_decision(ctx, decision)
    report_quality(ctx, record, decision)

main(declaration_path=Path(sys.argv[2]), execute=execute,
     argv=["--local-request-file", sys.argv[1]])
"""
        command = [sys.executable, "-c", script, str(request), str(TEMPLATE / "operator.yaml")]
    else:
        command = [sys.executable, str(TEMPLATE / "main.py"), "--local-request-file", str(request)]
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)


def assert_published(tmp_path, process, *, keep, verdict):
    assert process.returncode == 0, (process.stdout, process.stderr)
    assert json.loads(process.stdout)["validation"] == "passed"
    published = tmp_path / "work/published"
    assert json.loads((published / "result.json").read_text()) == {
        "decision": {"record_id": "r1", "keep": keep}
    }
    assert json.loads((published / "quality.json").read_text())["summary"] == {
        "total": 1, "passed": int(keep), "rejected": int(not keep), "unknown": 0, "error_records": 0
    }
    records = [json.loads(line) for line in (published / "quality_records.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["details"]["record_id"] == "r1" and "record_id" not in records[0]
    assert records[0]["decision"] == verdict
    if verdict == "REJECT":
        assert records[0]["reasons"] == [
            {"code": "BELOW_THRESHOLD", "message": "quality score is below threshold"}
        ]
    else:
        assert not records[0].get("reasons")
    assert not (published / "error.json").exists()
    logs = [json.loads(line) for line in (published / "operator.log").read_text().splitlines()]
    assert logs == json.loads(process.stdout)["logs"]
    assert all(row["execution"]["operator_code"] == "record_quality_gate" for row in logs)


@pytest.mark.parametrize("score, parameters, keep, verdict", [
    pytest.param(0.9, None, True, "PASS", id="pass"),
    pytest.param(0.8, None, True, "PASS", id="threshold-boundary"),
    pytest.param(0.7, None, False, "REJECT", id="reject"),
    pytest.param(0.9, {"threshold": 0.95}, False, "REJECT", id="effective-threshold"),
])
def test_template_runs_with_consistent_business_and_quality_outputs(tmp_path, score, parameters, keep, verdict):
    process = invoke_template(tmp_path, {"record_id": "r1", "quality_score": score}, parameters)
    assert_published(tmp_path, process, keep=keep, verdict=verdict)


@pytest.mark.parametrize("record", [
    pytest.param({"record_id": "r1", "quality_score": "0.9"}, id="wrong-type"),
    pytest.param({"record_id": "r1", "quality_score": 1.1}, id="out-of-range"),
    pytest.param({"record_id": "r1"}, id="missing-score"),
])
def test_template_invalid_input_publishes_only_failure(tmp_path, record):
    process = invoke_template(tmp_path, record)
    assert process.returncode == 1, (process.stdout, process.stderr)
    assert json.loads(process.stdout)["validation"] == "failed"
    published = tmp_path / "work/published"
    assert (published / "error.json").is_file()
    assert (published / "operator.log").read_bytes() == b""
    for name in ("result.json", "quality.json", "quality_records.jsonl"):
        assert not (published / name).exists()


def test_template_io_preserves_business_id_and_maps_quality_identity(tmp_path):
    process = invoke_template(tmp_path, {"record_id": "r1", "quality_score": 0.2}, io_only=True)
    assert_published(tmp_path, process, keep=False, verdict="REJECT")
