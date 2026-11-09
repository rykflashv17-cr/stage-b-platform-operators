"""Four fresh Pipeline processes, connected only by Storage-signed files."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
from uuid import UUID
from zipfile import ZipFile

import grpc
import pytest
from operator_sdk._proto import storage_contract_pb2 as pb
from operator_sdk._proto import storage_contract_pb2_grpc as rpc
from operator_sdk.pipeline_storage import Storage
from tests.support.pretraining_samples import write_abc
from tests.pipelines.pretraining_data_cleaning.test_pipeline import CODES, VERSIONS, check_package, environment, marvin_source, project


from tests.support.storage_service import remote, location


def consume_verified(storage, grants_by_attempt, ref, destination):
    # The file location comes from the saved B01 input binding. The manifest
    # supplies neither credentials nor a URL that the consumer could follow.
    selected = grants_by_attempt[ref["attempt_id"], ref["input_name"]]
    key, granted_location = selected[ref["input_index"]]
    if key != ref["logical_key"]:
        raise ValueError("logical key differs from the frozen input binding")
    storage.download(granted_location, key, destination)
    body = destination.read_bytes()
    if hashlib.sha256(body).hexdigest() != ref["sha256"]:
        destination.unlink()
        raise ValueError("source checksum changed since B01")
    return body


@pytest.mark.parametrize("case", [
    "clean", "mixed", "abc", "empty", "upload_error", "sidecar_missing", "mispaired_abc", "split_location",
    "mixed_file_types", "duplicate_key", "changed_source", "tampered_artifact", "same_basename",
])
def test_four_jobs_over_storage(remote, tmp_path, case):
    endpoint, objects, calls, consumer_grants = remote
    dataset = tmp_path / "fixture"
    dataset.mkdir()
    is_abc = case in {"abc", "sidecar_missing", "mispaired_abc", "split_location"}
    if is_abc:
        mcap, sidecar, _ = write_abc(dataset)
        keys = [sidecar.relative_to(dataset).as_posix(), mcap.relative_to(dataset).as_posix()]
        if case == "sidecar_missing":
            keys.pop(0)
        elif case == "mispaired_abc":
            moved = sidecar.parent.with_name("episode_001") / sidecar.name
            moved.parent.mkdir()
            sidecar.rename(moved)
            keys[0] = moved.relative_to(dataset).as_posix()
    elif case == "same_basename":
        keys = ["z/episode.mcap", "a/episode.mcap"]
        for i, key in enumerate(keys):
            (dataset / key).parent.mkdir()
            marvin_source(dataset / key, missing_left_topic=bool(i))
    elif case == "empty":
        keys = []
    else:
        marvin_source(dataset / "one.mcap")
        keys = ["one.mcap"]
        if case == "mixed":
            marvin_source(dataset / "gap.mcap", missing_left_topic=True)
            keys.insert(0, "gap.mcap")
        elif case == "mixed_file_types":
            _, sidecar, _ = write_abc(dataset)
            keys.append(sidecar.relative_to(dataset).as_posix())
        elif case == "duplicate_key":
            keys.append("one.mcap")

    # An ABC sidecar must share its primary file's storage location as well as its directory.
    buckets = ["other" if case == "split_location" and i > 0 else "raw" for i in range(len(keys))]
    originals = {f"{bucket}/{key}": (dataset / key).read_bytes()
                 for key, bucket in zip(keys, buckets, strict=True)}
    keys = [bucket + "/" + key for bucket, key in zip(buckets, keys, strict=True)]
    objects.update(originals)
    if case == "upload_error":
        objects["deny"] = b""
    ids = {k:str(UUID(int=i+1)) for i,k in enumerate(["tenantId","projectId","taskId","batchId"])}
    outs = ["normalized_batch_ref","statistics_context","batch_quality_result","archive"]
    expected_episodes = {0: "REJECT" if is_abc else "PASS"}
    if case in {"mixed", "same_basename"}:
        expected_episodes = {0: "REJECT", 1: "PASS"}  # gap.mcap sorts ahead of one.mcap
    value = keys
    grants_by_attempt = {}
    locations = [location(bucket) for bucket in buckets]

    for i, code in enumerate(CODES):
        stage, attempt = str(UUID(int=10+i)), str(UUID(int=20+i))
        prefix = "/".join([ids["tenantId"],ids["projectId"],"pipeline",ids["taskId"],ids["batchId"],"batch",stage,code,attempt])
        inp = "raw_files" if i==0 else outs[i-1]
        ctx = dict(**ids, **location("out"), stageExecutionId=stage,attemptId=attempt,executionScope="BATCH",
                   nodeCode=code,outputPrefix=prefix,storageEndpoint=endpoint,serviceCode="data-platform-operator-runtime",
                   serviceCredential="fixture-only",inputArtifactLocations={inp:locations if i==0 else location("out")})
        params = {inp:value}
        if i == 0:
            grants_by_attempt[(attempt, inp)] = tuple(zip(params[inp], ctx["inputArtifactLocations"][inp], strict=True))
            consumer_grants.update((bucket, key) for bucket, key in zip(buckets, keys, strict=True))
        calls_before = len(calls)
        if i < 3:
            params.update(source="abc_130k" if is_abc else "jindian_marvin",workers=1)
        job = tmp_path/("job-"+str(i))
        job.mkdir()
        env = dict(environment(),PIPELINE_NODE_PARAMS=json.dumps(params),PIPELINE_EXECUTION_CONTEXT=json.dumps(ctx),
                   TMPDIR=str(job),PYTHONDONTWRITEBYTECODE="1")
        command = [str(project(code)/".venv/bin/python"), "-I", str(project(code) / "main.py")]
        if image_tag := os.getenv("MCAP_TEST_IMAGE_TAG"):
            command = ["docker", "run", "--rm", "--platform", "linux/amd64", "--read-only", "--user", "10001:10001",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,size=1g",
                       "-e", "PIPELINE_NODE_PARAMS", "-e", "PIPELINE_EXECUTION_CONTEXT", "local/"+code+":"+image_tag]
        proc = subprocess.run(command,
                              cwd=job,env=env,capture_output=True,text=True,timeout=150)
        result_key=prefix+"/result.json"
        b01_failure = case in {"empty", "upload_error", "sidecar_missing", "mispaired_abc", "split_location", "mixed_file_types", "duplicate_key"}
        if (i == 0 and b01_failure) or (i == 1 and case == "tampered_artifact") or (i == 3 and case == "changed_source"):
            assert proc.returncode != 0 and result_key not in objects, (proc.stdout,proc.stderr)
            assert prefix+"/error.json" in objects
            assert prefix+"/quality.json" not in objects
            assert json.loads(proc.stdout)["validation"]=="failed"
            if case == "upload_error":
                assert any(op == "UPLOAD" and key.endswith(".zip") for op, key in calls)
            if case == "changed_source":
                assert objects["raw/one.mcap"] == b"changed after B01"
            else:
                assert all(objects[key] == body for key, body in originals.items())
            return
        assert proc.returncode==0, (proc.stdout,proc.stderr)
        result=json.loads(objects[result_key])
        assert set(result)=={outs[i]}
        value=result[outs[i]]
        assert isinstance(value,str) and value.startswith(prefix+"/") and value in objects
        stage_calls = calls[calls_before:]
        downloads = [key for operation, key in stage_calls if operation == "DOWNLOAD"]
        if i==3:
            expected_downloads=[params[inp]]+[k for k in originals if k.endswith('.mcap') and ('gap' not in k) and (case!='same_basename' or k.startswith('raw/z/')) and not is_abc]
        else:expected_downloads=sorted(originals) if i==0 else [params[inp]]
        assert sorted(downloads)==sorted(expected_downloads)
        uploads = {key for operation, key in stage_calls if operation == "UPLOAD"}
        allowed = {value, result_key, prefix+"/operator.log"}
        if i == 2:
            allowed.update({prefix+"/quality.json", prefix+"/quality_records.jsonl"})
        assert uploads == allowed
        if i < 3:
            archive = tmp_path / f"published-{i}.zip"
            archive.write_bytes(objects[value])
            check_package(archive, ("normalized_batch", "statistics_context", "quality_batch")[i], tmp_path / f"unpacked-{i}",
                          {hashlib.sha256(source).hexdigest() for source in originals.values()})
        if i == 0 and case == "changed_source":
            objects["raw/one.mcap"] = b"changed after B01"
        if i == 0 and case == "tampered_artifact":
            # Rebuild a valid ZIP with a changed NPZ but the original digest index.
            buffer = io.BytesIO()
            with ZipFile(archive) as old, ZipFile(buffer, "w") as new:
                victim = next(n for n in old.namelist() if n.endswith(".npz"))
                for name in old.namelist():
                    new.writestr(name, b"corrupt" if name == victim else old.read(name))
            objects[value] = buffer.getvalue()
        assert prefix+"/operator.log" in objects
        if i==2:
            q=json.loads(objects[prefix+"/quality.json"])
            assert q["format_version"] == "2" and q["summary"]["total"] == len(expected_episodes)
            rows = [json.loads(line) for line in objects[prefix+"/quality_records.jsonl"].splitlines()]
            assert all("record_id" not in row for row in rows)
            assert {row["details"]["episode_index"]:row["decision"] for row in rows} == expected_episodes
    assert not any(p.name.startswith("operator-pipeline-") for p in tmp_path.rglob("*"))
    with ZipFile(io.BytesIO(objects[value])) as archive:
        manifest=json.loads(archive.read('delivery_manifest.json'))
    attempt = str(UUID(int=20))
    expected = []
    if not is_abc:
        episode_index = 1 if case in {"mixed", "same_basename"} else 0
        input_index = 1 if case == "mixed" else 0
        key = keys[input_index]
        original_key = key
        expected = [{"episode_index": episode_index,
                     "source_ref": {"attempt_id": attempt, "input_name": "raw_files", "input_index": input_index,
                                    "logical_key": key, "sha256": hashlib.sha256(originals[original_key]).hexdigest()}}]
    assert manifest['format']=='delivery_manifest_v1'
    assert [row['business'] for row in manifest['files']]==expected
    consumer = dict(serviceCode="data-platform-operator-runtime", serviceCredential="delivery-consumer",
                    storageEndpoint="127.0.0.1:" + endpoint.rsplit(":", 1)[1])
    with Storage(consumer) as storage:
        for row in (item["business"] for item in manifest["files"]):
            ref = row["source_ref"]
            target = tmp_path / "downloaded.mcap"
            if case == "changed_source":
                with pytest.raises(ValueError, match="checksum"):
                    consume_verified(storage, grants_by_attempt, ref, target)
                assert not target.exists()
                assert objects["raw/one.mcap"] == b"changed after B01"
            else:
                assert consume_verified(storage, grants_by_attempt, ref, target) == originals[original_key]
    if case != "changed_source":
        assert all(objects[key] == body for key, body in originals.items())


def test_batch_verdict_without_episodes_over_storage(remote, tmp_path):
    """A whole-call check can run before a dataset has episode boundaries."""
    import yaml
    endpoint, objects, _, _ = remote
    ids = {k:str(UUID(int=i+41)) for i,k in enumerate(["tenantId","projectId","taskId","batchId"])}
    stage, attempt = str(UUID(int=55)), str(UUID(int=56))
    prefix = "/".join([ids["tenantId"],ids["projectId"],"pipeline",ids["taskId"],ids["batchId"],"batch",stage,"capture_check_clock",attempt])
    context = dict(**ids, **location("out"), stageExecutionId=stage, attemptId=attempt, executionScope="BATCH",
                   nodeCode="capture_check_clock", outputPrefix=prefix, storageEndpoint=endpoint,
                   serviceCode="data-platform-operator-runtime", serviceCredential="fixture-only", inputArtifactLocations={})
    raw = yaml.safe_load((project(CODES[2]) / "operator.yaml").read_text())
    raw.update(code="capture_check_clock", image="registry.test/data/capture_check_clock:" + VERSIONS[CODES[2]], outputs=[], parameters=[],
               inputs=[{"name":"timestamps","type":"array","items":{"type":"integer"}}])
    (tmp_path/"operator.yaml").write_text(yaml.safe_dump(raw))
    entry = tmp_path/"main.py"
    entry.write_text("""from pathlib import Path
from operator_sdk.v2.startup import main
def check(ctx):
    times = ctx.inputs.value("timestamps")
    if any(a >= b for a, b in zip(times, times[1:])):
        ctx.quality.report_batch("REJECT", reasons=[{"code":"CLOCK_ORDER","message":"timestamps out of order"}])
    else:
        ctx.quality.report_batch("PASS")
main(declaration_path=Path(__file__).with_name("operator.yaml"), execute=check)
""")
    env = dict(os.environ, PIPELINE_NODE_PARAMS=json.dumps({"timestamps":[2,1]}),
               PIPELINE_EXECUTION_CONTEXT=json.dumps(context))
    command = [sys.executable,"-I",str(entry)]
    if tag := os.getenv("MCAP_TEST_IMAGE_TAG"):
        command = ["docker","run","--rm","--platform","linux/amd64","--read-only","--user","10001:10001",
                   "--cap-drop","ALL","--security-opt","no-new-privileges","--tmpfs","/tmp:rw,nosuid,size=256m",
                   "--mount",f"type=bind,source={entry},target=/fixture/main.py,readonly",
                   "--mount",f"type=bind,source={tmp_path / 'operator.yaml'},target=/fixture/operator.yaml,readonly",
                   "-e","PIPELINE_NODE_PARAMS","-e","PIPELINE_EXECUTION_CONTEXT","--entrypoint","python",
                   "local/"+CODES[2]+":"+tag,"-I","/fixture/main.py"]
    proc = subprocess.run(command,env=env,cwd=tmp_path,capture_output=True,text=True,timeout=90)
    assert proc.returncode == 0, (proc.stdout,proc.stderr)
    files = {k.rsplit("/",1)[-1]:v for k,v in objects.items() if k.startswith(prefix+"/")}
    assert set(files) == {"quality.json","result.json","operator.log"}
    assert json.loads(files["result.json"]) == {}
    assert json.loads(files["quality.json"]) == {"format_version":"2","decision":"REJECT","reasons":[
        {"code":"CLOCK_ORDER","message":"timestamps out of order"}]}
