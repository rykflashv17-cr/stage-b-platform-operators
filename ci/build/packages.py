"""Prepare locked SDK and project wheels for CI or an editable operator environment."""
from ci import ROOT
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib

from ci.plan.projects import dependencies, operator_projects, read_project, shared_projects
from ci.build.sdk_wheel import INDEX, locked_sdk, prepare_sdk



def refresh_local_lock(project):
    metadata = tomllib.loads((Path(project) / "pyproject.toml").read_text())
    refresh = []
    for name, source in metadata.get("tool", {}).get("uv", {}).get("sources", {}).items():
        if str(source.get("path", "")).endswith(".whl"):
            refresh.extend(["--upgrade-package", name])
    subprocess.run(["uv", "lock", "--offline", *refresh], cwd=project, check=True)


def verify_local_locks(directory):
    lock = tomllib.loads((directory / "uv.lock").read_text())
    metadata = tomllib.loads((directory / "pyproject.toml").read_text())
    for name, source in metadata.get("tool", {}).get("uv", {}).get("sources", {}).items():
        if not source.get("path", "").endswith(".whl"):
            continue
        wheel = directory / source["path"]
        packages = [p for p in lock["package"] if p["name"] == name and p.get("source") == source]
        digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
        if len(packages) != 1 or not any(w.get("filename") == wheel.name and w["hash"] == digest
                                        for w in packages[0]["wheels"]):
            raise ValueError(f"{directory}: {wheel.name} differs from committed uv.lock")


def build(project, wheels, offline):
    constraints = Path(__file__).with_name("build-constraints.txt")
    cmd = ["uv", "build", "--wheel", "--no-sources", "--out-dir", str(wheels),
           "--build-constraints", str(constraints), "--require-hashes", "--default-index", INDEX]
    if offline:
        cmd.append("--offline")
    subprocess.run(cmd, cwd=project.path, check=True)


def sdk_pins(root, projects):
    pins = {}
    for project in projects:
        pin = locked_sdk(root, project.path)
        if pin.version in pins and pins[pin.version] != pin:
            raise ValueError("conflicting SDK wheel hashes for one version")
        pins[pin.version] = pin
    return list(pins.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--sdk-wheel", type=Path, help="Use a pre-downloaded SDK wheel; verify against uv.lock")
    source.add_argument("--sdk-project", type=Path, help="Build the pinned SDK from an explicit source checkout")
    parser.add_argument("--operator", help="Prepare only this operator and install its source editable")
    parser.add_argument("--offline", action="store_true", help="Use previously prepared SDK and uv cache")
    parser.add_argument("--refresh-local-locks", action="store_true", help="Explicitly refresh rebuilt project wheel locks")
    args = parser.parse_args()
    if args.refresh_local_locks and args.operator:
        parser.error("refresh locks with the full repository build")
    root = ROOT
    wheels = root / "build/wheels"
    projects = operator_projects(root)
    target = None
    template = read_project(root / "templates/python-operator")
    if args.operator:
        matches = ([template] if args.operator == "templates/python-operator" else
                   [p for p in projects if p.name == args.operator or f"{p.name}/v{p.version}" == args.operator])
        if len(matches) != 1:
            parser.error("unknown or ambiguous operator; specify <code>/v<version>")
        target = matches[0]
    operators = [target] if target else [*projects, template]
    pins = sdk_pins(root, operators)
    root_pin = locked_sdk(root, root)
    if not target and root_pin not in pins:
        pins.append(root_pin)
    pin = locked_sdk(root, target.path) if target else root_pin
    if args.sdk_wheel and args.sdk_wheel.name not in {p.filename for p in pins}:
        parser.error("SDK wheel does not match any selected project")
    if args.sdk_project:
        project = read_project(args.sdk_project.resolve())
        if project.wheel != pin.filename:
            parser.error("SDK project version differs from selected operator lock")
    sdk_files = []
    for selected in pins:
        source = args.sdk_wheel if args.sdk_wheel and args.sdk_wheel.name == selected.filename else None
        if args.offline and source is None and not (wheels / selected.filename).exists() and not args.sdk_project:
            parser.error("offline SDK wheel is missing: " + selected.filename)
        if args.sdk_project and target is None and selected == pin:
            with tempfile.TemporaryDirectory() as tmp:
                build(project, Path(tmp), args.offline)
                prepared = prepare_sdk(root, selected, wheel=Path(tmp) / selected.filename)
        else:
            prepared = prepare_sdk(root, selected, wheel=source)
        sdk_files.append(prepared)
    sdk = wheels / pin.filename
    owners = list({(p.distribution, p.version): p for owner in operators
                   for p in [*dependencies(root, owner), owner]}.values())
    from ci.build.dependency_wheels import prepare_external
    external = prepare_external(root, [*(p.path for p in owners), *([root] if not target else [])],
                                owners, offline=args.offline)
    for p in owners:
        build(p, wheels, args.offline)
        if args.refresh_local_locks:
            refresh_local_lock(p.path)
        verify_local_locks(p.path)
    if target:
        verify_local_locks(target.path)
        command = ["uv", "sync", "--frozen"]
        command.extend(["--offline"] if args.offline else ["--default-index", INDEX])
        env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(target.path / ".venv")}
        subprocess.run(command, cwd=target.path, env=env, check=True)
        if args.sdk_project:
            command = ["uv", "pip", "install", "--python", str(target.path / ".venv/bin/python"),
                       "--no-deps", "--editable", str(project.path)]
            command.extend(["--offline"] if args.offline else ["--default-index", INDEX])
            subprocess.run(command, check=True)
    else:
        if args.refresh_local_locks:
            refresh_local_lock(root)
        verify_local_locks(root)
        manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [*external, *sdk_files, *(wheels / owner.wheel for owner in owners)]}
        (wheels / "sha256.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"sdk": sdk.name, "built_projects": len(owners), "wheel_directory": str(wheels)}))


if __name__ == "__main__":
    main()
