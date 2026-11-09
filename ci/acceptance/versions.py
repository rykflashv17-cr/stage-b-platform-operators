"""Run each exact version from source and wheel in separate locked environments."""
from ci import ROOT
import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import hashlib

from ci.plan.projects import dependencies, operator_projects, wheel_paths
from ci.build.sdk_wheel import INDEX


def run_probe(*, python, test_path, sources, modules, report, project_directory=None, distributions=None, helpers=(), check_declaration=False):
    report = Path(report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    config = report.with_suffix(".request.json")
    paths = test_path if isinstance(test_path, (list, tuple)) else [test_path]
    config.write_text(json.dumps({"test_paths": [str(Path(p).resolve()) for p in paths], "sources": list(map(str, sources)),
                                  "helpers": list(map(str, helpers)),
                                  "declaration":str(Path(project_directory)/"operator.yaml") if check_declaration else None,
                                  "modules": {m: str(p) for m, p in modules.items()}, "report": str(report),
                                  "distributions":distributions or {}}))
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "CI_OPERATOR_PLAN"}}
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    if project_directory is not None:
        env['OPERATOR_TEST_PROJECT']=str(project_directory)
    if sources:
        # The -I probe ignores this; explicit entrypoint subprocesses in source
        # tests inherit only this exact version's dependency closure.
        env["PYTHONPATH"] = os.pathsep.join(map(str, sources))
    with report.with_suffix(".log").open("w") as output:
        process = subprocess.run([str(python), "-I", str(Path(__file__).with_name("probe.py")), str(config)],
                                 cwd=report.parent, env=env, stdout=output, stderr=subprocess.STDOUT)
    if not report.is_file():
        raise ValueError(f"version test did not produce a report (exit {process.returncode}): {report}")
    result = json.loads(report.read_text())
    if result["exit_code"] != process.returncode:
        raise ValueError("test exit code differs from report")
    return result


def verify_versions(root, keys, *, modes=("source", "wheel"), raise_on_failure=True):
    root = Path(root).resolve()
    projects = {f"{p.name}/v{p.version}": p for p in operator_projects(root)}
    results = []
    for key in sorted(set(keys)):
        project = projects[key]
        owners = [*dependencies(root, project), project]
        for mode in modes:
            if mode not in {"source", "wheel"}:
                raise ValueError(f"unknown version test mode: {mode}")
            from ci.plan.test_sets import version_test
            required=version_test(root,key,mode)
            tests=[root/path for path in required.get('paths',[required['path']])]
            if mode == "wheel":
                from ci.build.context import verify_project_wheel
                for owner in owners:
                    verify_project_wheel(root, owner)
            directory = root / "build/version-tests" / key / mode
            environment = root / ".ci-envs" / key / mode
            python = environment / "bin/python"
            env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(environment)}
            subprocess.run(["uv", "sync", "--project", str(project.path), "--frozen", "--no-install-local",
                            "--no-default-groups", "--default-index", INDEX], env=env, check=True)
            # pytest is tooling, never installed from the working tree or from a
            # sibling's environment. Runtime dependencies remain project-locked.
            subprocess.run(["uv", "pip", "install", "--python", str(python), "--default-index", INDEX,
                            "pytest==8.4.2"], check=True)
            wheels = wheel_paths(root, project)
            from ci.build.dependency_wheels import locked_wheels, verify_wheel
            pins = locked_wheels(root, project.path)
            for pin in pins:
                verify_wheel(root/'build/wheels'/pin.filename,pin)
            local_names = {p.wheel for p in owners}
            installed = wheels if mode == 'wheel' else [w for w in wheels if w.name not in local_names]
            subprocess.run(["uv", "pip", "install", "--python", str(python), "--no-deps", "--reinstall",
                            *map(str, installed)], check=True)
            sources = [p.path / "src" for p in owners] if mode == "source" else []
            site = Path(subprocess.check_output([str(python), "-I", "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
            modules = {m: (p.path / "src" if mode == "source" else site) for p in owners for m in p.modules}
            with tempfile.TemporaryDirectory(prefix='operator-installed-tests-') as scratch:
                # The wheel mode has no business source checkout: only the test
                # harness and the two image entry files are copied outside Git.
                harness=project.path
                if mode=='wheel':
                    harness=Path(scratch)
                    for name in ('main.py','operator.yaml'):
                        shutil.copy2(project.path/name,harness/name)
                    shutil.copytree(tests[0],harness/'tests',ignore=shutil.ignore_patterns('__pycache__'))
                    tests=[harness/'tests']
                result = run_probe(python=python, test_path=tests, sources=sources, modules=modules,
                    report=directory / "result.json",project_directory=harness,helpers=[root] if mode=='source' else [],
                    check_declaration=mode=='source',
                    distributions={**{p.name:p.version for p in pins if mode=='wheel' or p.filename not in local_names},
                                   **({p.distribution:p.version for p in owners} if mode=='wheel' else {})})
            results.append({"operator": key, "mode": mode, "definition_digest":required['identity'],
                "wheels":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in wheels} if mode=='wheel' else {},**result})
            print(json.dumps(results[-1]), flush=True)
    if raise_on_failure and any(row["exit_code"] for row in results):
        raise ValueError("isolated version checks failed; see build/version-tests/<version>/<mode>/result.log")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keys", nargs="*")
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    root = ROOT
    keys = args.keys
    if args.plan:
        from ci.plan.images import checked_versions
        plan = json.loads(args.plan.read_text())
        keys = checked_versions(plan, merge_request=os.environ.get("CI_PIPELINE_SOURCE") == "merge_request_event")
    verify_versions(root, keys)
