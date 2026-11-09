"""Prepare only the exact operator wheels consumed by the version-scoped CI jobs."""
from ci import ROOT
import argparse
import json
import os
from pathlib import Path
import subprocess
import tomllib

from ci.build.packages import build, verify_local_locks
from ci.plan.images import load_plan, checked_versions
from ci.plan.projects import operator_projects, read_project, shared_projects, dependencies
from ci.build.sdk_wheel import locked_sdk, prepare_sdk

PLAN = ROOT / "build/plan.json"
WHEELS = ROOT / "build/wheels"


def tool_directory(root):
    configured = root / "ci/tooling"
    return configured if (configured / "pyproject.toml").exists() else root


def tools(root):
    metadata = tomllib.loads((tool_directory(root) / "pyproject.toml").read_text())
    pins = set(metadata["project"]["dependencies"])
    return [p for p in shared_projects(root) if p.distribution + "==" + p.version in pins]


def selected_projects(root, keys, *, template=True, environments=(), packages=()):
    from ci.plan.test_sets import inside
    projects = {f"{p.name}/v{p.version}": p for p in operator_projects(root)}
    shared = {p.distribution:p for p in shared_projects(root)}
    selected = [*tools(root), *(projects[k] for k in keys), *(shared[name] for name in packages)]
    if template:selected.append(read_project(root / 'templates/python-operator'))
    available={(p.distribution,p.version):p for p in [*shared_projects(root),*projects.values()]}
    for environment in environments:
        folder=inside(root,environment).parent
        metadata=tomllib.loads((folder/'pyproject.toml').read_text())
        lock=tomllib.loads((folder/'uv.lock').read_text())
        sources=metadata.get('tool',{}).get('uv',{}).get('sources',{})
        for row in lock['package']:
            if row['name'] not in sources or row['name']=='striding-operator-sdk':continue
            identity=(row['name'],row['version'])
            if identity in available:
                selected.append(available[identity])
            else:
                from ci.build.dependency_wheels import locked_wheels
                if not any((pin.name,pin.version)==identity for pin in locked_wheels(root,folder)):
                    raise ValueError('workflow lock references an unknown local distribution')
    return list({(p.distribution, p.version): p for owner in selected
                   for p in [*dependencies(root, owner), owner]}.values())


def prepare(root, keys, *, template=True, environments=(), packages=()):
    from ci.build.context import verify_project_wheel
    from ci.build.wheel_metadata import verify_wheel_lock

    owners=selected_projects(root,keys,template=template,environments=environments,packages=packages)
    output = root / "build/wheels"
    pins = {locked_sdk(root, tool_directory(root)),*(locked_sdk(root,(root/path).parent) for path in environments)}
    for owner in owners:
        metadata = tomllib.loads((owner.path / "pyproject.toml").read_text())
        if "striding-operator-sdk" in metadata.get("tool", {}).get("uv", {}).get("sources", {}):
            pins.add(locked_sdk(root, owner.path))
    for pin in pins:
        prepare_sdk(root, pin)
    from ci.build.dependency_wheels import prepare_external
    external = prepare_external(root, [tool_directory(root), *(p.path for p in owners),
        *((root/path).parent for path in environments)], owners,
        offline=os.environ.get('UV_OFFLINE','').lower() in {'1','true'})
    workspace_lock = tomllib.loads((tool_directory(root) / "uv.lock").read_text())
    for owner in owners:
        build(owner, output, os.environ.get("UV_OFFLINE", "").lower() in {"1", "true"})
        verify_project_wheel(root, owner)
        verify_local_locks(owner.path)
        # The dev workspace can pin one version of a distribution; additional
        # versions still get wheel/source verification and their own frozen lock.
        if any(p["name"] == owner.distribution and p["version"] == owner.version
               for p in workspace_lock["package"]):
            verify_wheel_lock(output / owner.wheel, workspace_lock, owner.distribution, owner.version)
    for environment in environments:verify_local_locks((root/environment).parent)
    return sorted(set(external + [output/pin.filename for pin in pins]+[output/owner.wheel for owner in owners]))


def environment():
    # Tooling has a separate coherent SDK pin. Historical operator locks are untouched.
    python = ROOT / ".venv/bin/python"
    subprocess.run(["uv", "sync", "--project", str(tool_directory(ROOT)), "--frozen", "--no-editable"],
                   env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(ROOT / ".venv")}, check=True)


def verify_installed(plan):
    from ci.acceptance.versions import verify_versions
    keys = checked_versions(plan, merge_request=os.environ.get("CI_PIPELINE_SOURCE") == "merge_request_event")
    return verify_versions(ROOT, keys)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "environment", "verify-installed"))
    parser.add_argument('--package', action='append', dest='packages', help='Prepare a current shared package and its dependencies')
    args = parser.parse_args()
    if args.packages:
        if args.action!='prepare':parser.error('--package requires prepare')
        prepare(ROOT,[],template=False,packages=args.packages)
        return
    if args.action == "environment":
        environment()
        return
    plan = load_plan(PLAN, ROOT)
    if args.action == "prepare":
        prepare(ROOT, checked_versions(plan, merge_request=os.environ.get("CI_PIPELINE_SOURCE") == "merge_request_event"))
    elif args.action == "environment":
        environment()
    else:
        verify_installed(plan)


if __name__ == "__main__":
    main()
