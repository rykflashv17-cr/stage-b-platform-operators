"""Stage one immutable local build context from exact project-owned artifacts."""
from ci import ROOT
import argparse
import json
from pathlib import Path
import shutil
import tempfile
import zipfile
import tomllib
from email.parser import BytesParser
from ci.build import bases as operator_bases
from ci.build import wheelhouse
from ci.build.sdk_wheel import verify as verify_sdk, locked_sdk
from ci.build.wheel_metadata import metadata_errors
from ci.plan.projects import operator_projects, dependencies, source_files, wheel_paths, build_inputs, read_project


def verify_project_wheel(root, project):
    wheel = Path(root) / "build/wheels" / project.wheel
    expected = {p.relative_to(project.path / "src").as_posix(): p.read_bytes()
                for p in source_files(project) if p.is_relative_to(project.path / "src")}
    with zipfile.ZipFile(wheel) as archive:
        records = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        if len(records) != 1:
            raise ValueError(f"{project.distribution}: ambiguous wheel metadata")
        metadata = BytesParser().parsebytes(archive.read(records[0]))
        installed = dict(name=metadata["Name"], version=metadata["Version"],
                         requires_python=metadata.get("Requires-Python"),
                         requires_dist=metadata.get_all("Requires-Dist") or [],
                         provides_extra=metadata.get_all("Provides-Extra") or [])
        errors = metadata_errors(tomllib.loads((project.path / "pyproject.toml").read_text())["project"], installed)
        if errors:
            raise ValueError("; ".join(errors))
        actual = {n: archive.read(n) for n in archive.namelist()
                  if n.split("/")[0] in project.modules and not n.endswith("/")}
    if actual != expected:
        raise ValueError(f"{project.distribution}: source differs from wheel; rebuild before staging")
    return wheel


def select_project(root, key):
    if key == "templates/python-operator":
        return read_project(root / key)
    projects = [p for p in operator_projects(root) if key in (p.name, f"{p.name}/v{p.version}")]
    if len(projects) != 1:
        raise ValueError("unknown or ambiguous operator; use <code>/v<version>")
    return projects[0]


def stage(root, code, destination, *, bases=None, target=None, cache=None, bootstrap=False):
    root, destination = Path(root).resolve(), Path(destination).absolute()
    project = select_project(root, code)
    if destination.exists():
        raise ValueError("build context already exists")
    for owner in [*dependencies(root, project), project]:
        verify_project_wheel(root, owner)
    wheels = wheel_paths(root, project)
    from ci.build.dependency_wheels import locked_wheels, verify_wheel
    for pin in locked_wheels(root, project.path):
        verify_wheel(root/'build/wheels'/pin.filename,pin)
    verify_sdk(wheels[0], locked_sdk(root, project.path))
    source = (project.path / "Dockerfile").read_text()
    refs, runtime = operator_bases.parse_dockerfile(source)
    if bases is None:
        bases = {ref: operator_bases.resolve(root, ref, bootstrap=bootstrap) for ref in refs}
    if target is None:
        target = wheelhouse.probe_target(bases[runtime]["image_id"])
    # Publish a complete context only after all downloads and checks succeed.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        context = Path(temp) / "context"
        context.mkdir()
        hashes = wheelhouse.prepare(project, wheels, target, context, cache or root / ".ci-cache/wheels")
        for name in ("main.py", "operator.yaml"):
            path = context / name
            shutil.copyfile(project.path / name, path)
            path.chmod(0o644)  # entrypoint and declaration must be readable by UID 10001
            hashes[name] = wheelhouse.digest(path)
        dockerfile = context / "Dockerfile"
        dockerfile.write_text(operator_bases.pin_dockerfile(source, {r: bases[r]["reference"] for r in refs}))
        hashes[dockerfile.name] = wheelhouse.digest(dockerfile)
        result = dict(code=project.name, version=project.version, inputs=build_inputs(root, project),
                      context_files=hashes, bases={r: bases[r] for r in refs})
        (context / "build-inputs.json").write_text(json.dumps(result, indent=2) + "\n")
        context.rename(destination)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-runtime", action="store_true", help="Allow the verified first release seed in local/MR builds")
    parser.add_argument("--wheel-cache", type=Path, help="Hash-checked wheel download cache (default: build/locked-wheel-cache)")
    args = parser.parse_args()
    result = stage(args.root, args.operator, args.output, bootstrap=args.bootstrap_runtime, cache=args.wheel_cache)
    print(json.dumps({"code": result["code"], "context_files": len(result["context_files"])}))

if __name__ == "__main__":
    main()
