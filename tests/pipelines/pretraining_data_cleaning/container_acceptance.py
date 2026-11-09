"""Run four isolated images against generated real MCAP fixtures, without network."""
from ci import ROOT
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from zipfile import ZipFile
from operator_sdk.v2.declaration import load_operator
CODES = ("mcap_read_normalize", "mcap_stats_global", "mcap_quality_detect", "mcap_delivery_accepted")
from ci.plan.projects import operator_projects
from ci.build.sdk_wheel import locked_sdk

def write_fixture(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o644)  # docker cp assigns root ownership; the operator runs as uid 10001.


def read_report(process, case, code, expected):
    detail = f"stderr: {process.stderr[-2400:]}\nstdout: {process.stdout[-2400:]}"
    if process.returncode != expected:
        raise RuntimeError(f"{case}/{code}: expected exit {expected}, got {process.returncode}: {detail}")
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{case}/{code}: no valid JSON report (exit {process.returncode}): {detail}") from error


def require_failure_report(published):
    assert (published / "error.json").is_file()
    assert not (published / "result.json").exists()
    assert not (published / "quality.json").exists()
    assert not (published / "quality_records.jsonl").exists()


def require_delivery_manifest(manifest, attempt, raw_keys, digests, expected):
    accepted = [
        {"episode_index": index, "source_ref": {
            "attempt_id": attempt, "input_name": "raw_files", "input_index": raw_keys.index(key),
            "logical_key": key, "sha256": digests[key],
        }}
        for index, key in expected
    ]
    assert manifest == {"format": "mcap_delivery_manifest_v1", "accepted": accepted}


def source_access(index, archive_delivery, input_root, raw_keys, volume, case):
    readable = index == 0 or (index == 3 and archive_delivery)
    config = {"read_roots": [input_root] if readable else []}
    if index == 3 and archive_delivery:
        config["storage_files"] = {key: input_root + "/" + key for key in raw_keys if key.endswith(".mcap")}
    mounts = (["--mount", f"type=volume,src={volume},dst={input_root},volume-subpath={case}/inputs,readonly"]
              if readable else ["--tmpfs", f"{input_root}:ro,nosuid,size=1m"])
    return config, mounts


def require_delivery_archive(path, attempt, raw_keys, digests, expected):
    files = []
    with ZipFile(path) as archive:
        for index, key in expected:
            relative = f"episode_{index:03d}/" + Path(key).name
            body = archive.read(relative)
            assert hashlib.sha256(body).hexdigest() == digests[key]
            files.append({"delivery_path": relative, "size_bytes": len(body), "sha256": digests[key],
                          "business": {"episode_index": index, "source_ref": {
                              "attempt_id": attempt, "input_name": "raw_files", "input_index": raw_keys.index(key),
                              "logical_key": key, "sha256": digests[key],
                          }}})
        files.sort(key=lambda item: item["delivery_path"].encode("utf-8"))
        assert len(archive.namelist()) == len(files) + 1
        assert set(archive.namelist()) == {"delivery_manifest.json", *(item["delivery_path"] for item in files)}
        assert json.loads(archive.read("delivery_manifest.json")) == {"format": "delivery_manifest_v1", "files": files}


def accept_plan(plan, images, accept):
    """Run declared MCAP combinations; preserve every exact version's report."""
    from ci.plan.test_sets import discover, select
    from ci.plan.images import selection
    selection(plan, images)
    from ci.plan.history import load_history
    current={f"{p.name}/v{p.version}" for p in operator_projects(ROOT)}
    definitions = discover(ROOT, current|set(load_history(ROOT,current)))
    names = select(definitions, set(images))
    cases = {"marvin", "gaps", "abc", "empty", "input_error"}
    result = {"local_only": True, "commit": plan["commit"],
              "publication_versions": plan["publication_versions"], "images": {},
              "cases": {case: {} for case in sorted(cases)}, "combinations": {}}
    chains = []
    for name in names:
        keys = set(definitions[name]["versions"].values())
        if (not name.startswith("pretraining_data_cleaning/") or
                not keys <= images.keys() or {key.split('/')[0] for key in keys} != set(CODES)):
            raise ValueError("MCAP acceptance requires every declared combination image")
        chains.append((name.split('/', 1)[1], {key.split('/')[0]: images[key] for key in sorted(keys)}))
    if set(images) != {row['operator'] for _,chain in chains for row in chain.values()}:
        raise ValueError("image version has no declared MCAP acceptance combination")
    for label, chain in chains:
        report = accept(chain, label)
        if set(report.get('cases', {})) != cases or set(report.get('images', {})) != set(chain):
            raise ValueError("incomplete combination acceptance")
        for code, row in report['images'].items():
            key = chain[code]['operator']
            if (row.get('operator') != key or row.get('operator_version') != key.split('/v')[1]
                    or row.get('id') != chain[code]['image']):
                raise ValueError("combination image identity mismatch")
            if key in result['images'] and result['images'][key] != row:
                raise ValueError("shared combination image evidence mismatch")
            result['images'][key] = row
        for case in cases:result['cases'][case][label] = report['cases'][case]
        result['combinations'][label] = sorted(row['operator'] for row in chain.values())
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--images",type=Path,default=ROOT / "build/container-images.json")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    images=json.loads(args.images.read_text())
    if args.plan:
        from ci.plan.images import load_plan
        plan = load_plan(args.plan, ROOT)
        with tempfile.TemporaryDirectory(prefix="incremental-acceptance-") as scratch:
            scratch = Path(scratch)
            def accept(chain, label):
                manifest, output = scratch / (label + "-images.json"), scratch / (label + ".json")
                manifest.write_text(json.dumps(chain))
                subprocess.run([sys.executable, "-m", "tests.pipelines.pretraining_data_cleaning.container_acceptance", "--images", str(manifest), "--output", str(output)], check=True, cwd=ROOT)
                return json.loads(output.read_text())
            evidence = accept_plan(plan, images, accept)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(evidence, indent=2) + "\n")
        return
    sys.path.insert(0,str(ROOT / "tests/pipelines/pretraining_data_cleaning"))
    from tests.pipelines.pretraining_data_cleaning.test_pipeline import check_package, marvin_source, published_file
    from tests.support.pretraining_samples import write_abc
    root=Path(tempfile.mkdtemp(prefix="pretraining-container-acceptance-")).resolve()
    def guest(path):return "/validation/"+path.relative_to(root).as_posix()
    def fingerprints(folder):return {p.relative_to(folder).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}
    evidence={"local_only":True,"workspace":str(root),"cases":{},"images":{}}
    # Image evidence checks dependencies and absence of sibling implementations.
    all_projects = {f"{p.name}/v{p.version}": p for p in operator_projects(ROOT)}
    from ci.plan.history import load_history
    from ci.publish.operator_sources import definition
    from types import SimpleNamespace
    import tomllib,yaml
    history=load_history(ROOT,all_projects);projects={};declarations={};sdk_versions={}
    for code in CODES:
        key=images[code]['operator']
        if key in all_projects:
            project=all_projects[key]
            raw=(project.path/'operator.yaml').read_bytes()
            sdk_versions[code]=locked_sdk(ROOT,project.path).version
        else:
            pin=history[key];source=definition(ROOT,key,pin['source']['commit']);raw=source['raw']
            if source['sha256']!=pin['source']['definition_sha256']:raise ValueError('historical declaration differs')
            metadata=tomllib.loads(subprocess.check_output(['git','show',pin['source']['commit']+':'+source['path']+'/pyproject.toml'],cwd=ROOT,text=True))['project']
            project=SimpleNamespace(name=code,version=key.rsplit('/v',1)[1],distribution=metadata['name'])
            sdk_versions[code]=next(r.split('==')[1] for r in metadata['dependencies'] if r.startswith('striding-operator-sdk=='))
        if project.name!=code:raise ValueError('image operator identity mismatch')
        projects[code]=project;declarations[code]=yaml.safe_load(raw)
    archive_delivery=declarations[CODES[3]].get('delivery')=={'type':'archive','format':'zip'}
    for code in CODES:
        image=images[code]["image"]
        project = projects[code]
        sdk_version = sdk_versions[code]
        script=f"import importlib.util,importlib.metadata,json; assert importlib.metadata.version('striding-operator-sdk')=={sdk_version!r}; assert importlib.metadata.version({project.distribution!r}) == {project.version!r}; assert importlib.util.find_spec('operator_runtime') is None; assert importlib.util.find_spec('stage_b_upstream') is None; "
        script+='; '.join("assert importlib.util.find_spec("+repr(other)+") is None" for other in CODES if other!=code)
        script+="; import numpy,pandas,pyarrow,scipy,mcap; print(json.dumps({'sdk':importlib.metadata.version('striding-operator-sdk'),'tzdata':importlib.metadata.version('tzdata')}))"
        result=subprocess.run(["docker","run","--rm","--platform","linux/amd64","--network","none","--read-only","--entrypoint","python",image,"-c",script],capture_output=True,text=True,timeout=60)
        if result.returncode:raise RuntimeError(result.stdout+result.stderr)
        evidence["images"][code]={"id":image,"operator_version":project.version,"dependencies":json.loads(result.stdout), "operator":images[code].get("operator", code + "/v" + project.version)}
        if "inherited" in images[code]:
            evidence["images"][code]["inherited"] = images[code]["inherited"]
    def copy_from_volume(anchor, source, target):
        target.parent.mkdir(parents=True,exist_ok=True)
        result=subprocess.run(["docker","cp",anchor+":"+source,str(target)],capture_output=True,text=True,timeout=60)
        if result.returncode:raise RuntimeError(result.stderr)
    def export_evidence(anchor, folder, inputs, stage):
        # Capture the actual container source directory separately to detect removed files too.
        copy_from_volume(anchor, guest(inputs), folder / "verified-inputs")
        copy_from_volume(anchor, guest(folder / stage / "published"), folder / stage / "published")
    volume="pretraining-acceptance-"+uuid.uuid4().hex
    subprocess.run(["docker","volume","create",volume],check=True,capture_output=True,timeout=30)
    anchor=None
    try:
        anchor=subprocess.check_output(["docker","run","-d","--rm","--platform","linux/amd64","--network","none","--read-only","--cap-drop","ALL","--security-opt","no-new-privileges","--user","0:0","--mount",f"type=volume,src={volume},dst=/validation","--entrypoint","python",images[CODES[0]]["image"],"-c","import time; time.sleep(900)"],text=True,timeout=30).strip()
        for case in ("marvin","gaps","abc","empty","input_error"):
            folder=root / case;folder.mkdir(mode=0o777);folder.chmod(0o777)
            inputs=folder / "inputs";inputs.mkdir()
            source="abc_130k" if case=="abc" else "jindian_marvin"
            if case=="abc":write_abc(inputs)
            elif case=="input_error":(inputs / "bad.mcap").write_bytes(b"invalid mcap")
            elif case!="empty":
                marvin_source(inputs / "clean.mcap")
                if case=="gaps":marvin_source(inputs / "gap.mcap",missing_left_topic=True)
            for path in [inputs, *inputs.rglob("*")]:
                path.chmod(0o755 if path.is_dir() else 0o644)
            before=fingerprints(inputs)
            episode_count = len(list(inputs.rglob("*.mcap")))
            raw_keys = sorted(before, reverse=True)
            payload=folder / "input.json";write_fixture(payload, raw_keys)
            subprocess.run(["docker","cp",str(folder),anchor+":/validation/"],check=True,capture_output=True,timeout=60)
            bound={"raw_files":[{"fixture_path":guest(payload)}]};steps=[]
            published = []
            for index,code in enumerate(CODES):
                access, raw_mount = source_access(index, archive_delivery, guest(inputs), raw_keys, volume, case)
                request={"input_files":bound,"parameter_overrides":{} if index==3 else {"source":source,"workers":1},"workspace":guest(folder / ("b"+str(index+1))),"artifact_store":guest(folder / "store"),**access}
                file=folder / f"request-{index}.json";write_fixture(file,request)
                subprocess.run(["docker","cp",str(file),anchor+":"+guest(file)],check=True,capture_output=True,timeout=60)
                command=["docker","run","--rm","--platform","linux/amd64","--network","none","--read-only","--tmpfs","/tmp:rw,nosuid,size=256m","--cap-drop","ALL","--security-opt","no-new-privileges","--cpus","4","--memory","8g","--pids-limit","256","--mount",f"type=volume,src={volume},dst=/validation",*raw_mount,images[code]["image"],"--local-request-file",guest(file)]
                process=subprocess.run(command,capture_output=True,text=True,timeout=180)
                (folder / f"b{index+1}.stderr.log").write_text(process.stderr)
                expected=int((case=="empty" and index==0) or (case=="input_error" and index==2))
                report=read_report(process,case,code,expected)
                steps.append({"code":code,"exit_code":process.returncode,"validation":report["validation"]})
                if expected:
                    stage = f"b{index + 1}"
                    export_evidence(anchor, folder, inputs, stage)
                    require_failure_report(folder / stage / "published")
                    break
                name=("normalized_batch_ref", "statistics_context", "batch_quality_result", "archive" if archive_delivery else "batch_delivery_result")[index]
                item, = report["outputs"][name]
                published.append(item)
                if index == 0:
                    attempt = report["execution"]["attempt_id"]
                if index < 3:
                    bound={name:[{"fixture_path":item["path"],"local_reference":item["local_reference"]}]}
                else:
                    export_evidence(anchor, folder, inputs, "b3")
            if published:
                copy_from_volume(anchor, guest(folder / "store"), folder / "store")
            for item, kind in zip(published, ("normalized_batch", "statistics_context", "quality_batch")):
                check_package(published_file(item, folder / "store"), kind, folder / kind, set(before.values()))
            if len(published) == 4:
                delivery = published_file(published[3], folder / "store")
                expected = [] if case == "abc" else [(0, "clean.mcap")]
                if archive_delivery:
                    require_delivery_archive(delivery, attempt, raw_keys, before, expected)
                else:
                    require_delivery_manifest(json.loads(delivery.read_bytes()), attempt, raw_keys, before, expected)
                quality=json.loads((folder / "b3/published/quality.json").read_text())
                assert quality["summary"]["total"] == episode_count
                assert quality["summary"]["passed"] == len(expected)
                assert quality["summary"]["rejected"] == episode_count - len(expected)
                steps[-1]["accepted"] = len(expected)
            assert fingerprints(folder / "verified-inputs")==before
            assert len(steps) == {"empty": 1, "input_error": 3}.get(case, 4)
            evidence["cases"][case]={"steps":steps,"sources_unchanged":True}
            print(json.dumps({"case":case,"steps":len(steps),"validation":"passed"}),flush=True)
    finally:
        if anchor:
            subprocess.run(["docker","rm","-f",anchor],capture_output=True,timeout=30)
        subprocess.run(["docker","volume","rm",volume],capture_output=True,timeout=30)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(evidence,indent=2)+"\n")
    print(json.dumps({"validation":"passed","report":str(args.output)}))

if __name__=="__main__":main()
