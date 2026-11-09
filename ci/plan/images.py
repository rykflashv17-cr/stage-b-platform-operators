"""Validated optional incremental image execution plan."""
import json
from pathlib import Path
import re
import subprocess


def load_plan(path, root, *, commit=None):
    plan = json.loads(Path(path).read_text())
    commit = commit or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if plan["commit"] != commit:
        raise ValueError("image plan does not match checked out commit")
    from ci.plan.projects import operator_projects
    projects = {f"{p.name}/v{p.version}": p for p in operator_projects(root)}
    from ci.plan.release_scope import load_versions
    selected = plan["publication_versions"]
    if selected != load_versions(root, projects):
        raise ValueError("image plan release versions differ from authoritative scope")
    semver = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    for key, project in projects.items():
        if not re.fullmatch(semver, project.version) or project.path.name != project.name:
            raise ValueError("noncanonical current operator directory")
    for field in ("build", "publish"):
        keys = plan[field]
        if not isinstance(keys, list) or len(keys) != len(set(keys)) or not set(keys) <= projects.keys():
            raise ValueError("invalid exact image allowlist: " + field)
    if not set(plan["publish"]) <= set(plan["build"]) & set(selected):
        raise ValueError("publication must select built release versions")
    for key in selected:
        code=key.split('/')[0]
        if key not in plan["build"]:
            row = plan["components"][key]
            if row["buildVersion"] != key.rsplit("/v",1)[1]:
                raise ValueError("inherited component version mismatch")
            ref = row["repositories"]["local-harbor"]["repository"]
            if ref != "harbor.striding.internal/data/data-operators/" + code or not re.fullmatch(r"sha256:[a-f0-9]{64}", row["digest"]):
                raise ValueError("invalid inherited canonical digest")
    return plan


def selection(plan, images):
    expected = set(plan["publication_versions"]) | set(plan["build"])
    if set(images) != expected:
        raise ValueError("unexpected image manifest coverage")
    for key in expected:
        if images[key]["operator"] != key:
            raise ValueError("exact image version mismatch")
    for label, row in images.items():
        key = row["operator"]
        if key in plan["build"]:
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", row.get("image", "")):
                raise ValueError("built image must use the accepted local image ID")
        else:
            parent = plan["components"][label]
            ref = parent["repositories"]["local-harbor"]["repository"] + "@" + parent["digest"]
            if row.get("image") != ref or row.get("inherited") != parent:
                raise ValueError("inherited image does not match canonical parent digest")


def shipping_keys(plan, branch):
    """Develop ships changed versions; release/main honor the approved scope."""
    return plan["build"] if branch == "develop" else plan["publish"]


def mr_container_plan(plan):
    """Build risky versions plus missing workflow peers, not every source change."""
    selected = set(plan["checks"]["containers"])
    for key in plan["publication_versions"]:
        parent = plan.get("components", {}).get(key)
        if not parent or parent["buildVersion"] != key.split("/v", 1)[1]:
            selected.add(key)
    if not selected <= set(plan["build"]) | set(plan.get("checks", {}).get("wheels", [])):
        raise ValueError("container verification requires an unprepared version wheel")
    return {**plan, "build": sorted(selected), "publish": sorted(set(plan["publish"]) & selected)}


def checked_versions(plan, *, merge_request):
    """Include every newly built peer without expanding ordinary source MRs."""
    if not merge_request:
        return sorted(set(plan["build"]) | set(plan.get("checks", {}).get("wheels", [])))
    selected = set(plan.get("checks", {}).get("wheels", plan["build"]))
    if plan.get("checks", {}).get("containers"):
        selected.update(mr_container_plan(plan)["build"])
    return sorted(selected)


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Count the exact versions to push or mirror.")
    parser.add_argument("--shipping-count", type=Path, required=True)
    args = parser.parse_args()
    plan = load_plan(args.shipping_count, Path.cwd(), commit=os.environ["CI_COMMIT_SHA"])
    print(len(shipping_keys(plan, os.environ["CI_COMMIT_BRANCH"])))
