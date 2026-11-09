"""Publish the accepted Linux images to Harbor and record remote manifest digests."""
from ci import ROOT
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

import yaml

from ci.publish.tags import publication_tag
from ci.plan.projects import operator_projects
from ci.base_images.manage import remote

PREFIX = "harbor.striding.internal/data/data-operators/"


def release_target(code, version, image):
    expected = f"{PREFIX}{code}:{version}"
    if image != expected:
        raise ValueError(f"{code}: operator.yaml image must be {expected}")
    return expected


def remote_artifact(target):
    metadata = remote(target)
    if metadata is None:
        return None
    repository = target.rsplit(":", 1)[0]
    digests = [d for d in metadata["RepoDigests"] if d.startswith(repository + "@sha256:")]
    if len(digests) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", digests[0].split("@")[1]):
        raise ValueError(f"ambiguous remote digest for {target}")
    return {"image_id": metadata["Id"], "reference": digests[0]}


def publish(plan, *, lookup=remote_artifact, run=subprocess.run):
    # Check every tag before the first push. Remote tag immutability also
    # protects the interval between inspection and push from other writers.
    remote = {tag: lookup(tag) for tag in plan}
    for tag, existing in remote.items():
        if existing is not None and existing["image_id"] != plan[tag]:
            raise ValueError(f"refusing to overwrite {tag}")
    results = {}
    for tag, image in plan.items():
        if remote[tag] is None:
            run(["docker", "tag", image, tag], check=True)
            run(["docker", "push", tag], check=True, timeout=900)
            remote[tag] = lookup(tag)
        if remote[tag] is None or remote[tag]["image_id"] != image:
            raise ValueError(f"{tag}: published image differs from accepted image")
        results[tag] = remote[tag]
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gitlab", action="store_true", help="Derive each tag from the GitLab branch and pipeline ID")
    mode.add_argument("--candidate-tag", help="Explicit legacy candidate tag for standalone publication")
    args = parser.parse_args()
    if args.candidate_tag and not re.fullmatch(r"candidate-[a-f0-9]{8,64}-p[0-9]+", args.candidate_tag):
        parser.error("invalid candidate tag")
    root = ROOT
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if args.gitlab:
        if os.environ.get("CI_COMMIT_REF_PROTECTED") != "true" or os.environ.get("CI_COMMIT_SHA") != commit:
            parser.error("GitLab publication requires the protected pipeline commit")
    images = json.loads(args.images.read_text())
    acceptance = json.loads(args.acceptance.read_text())
    projects = operator_projects(root)
    incremental = None
    if args.plan:
        from ci.plan.images import load_plan, selection, shipping_keys
        incremental = load_plan(args.plan, root)
        selection(incremental, images)
        if acceptance.get("commit") != commit or acceptance.get("publication_versions") != incremental["publication_versions"]:
            raise ValueError("acceptance plan provenance mismatch")
        projects = [p for p in projects if f"{p.name}/v{p.version}" in shipping_keys(incremental, os.environ.get("CI_COMMIT_BRANCH", "main"))]
    if set(acceptance["images"]) != set(images):
        raise ValueError("incomplete image acceptance coverage")
    if set(acceptance["cases"]) != {"marvin", "gaps", "abc", "empty", "input_error"}:
        raise ValueError("incomplete container acceptance cases")
    counts = {}
    for project in projects:counts[project.name] = counts.get(project.name, 0) + 1
    plan = {}
    for project in projects:
        code = project.name
        decl = yaml.safe_load((project.path / "operator.yaml").read_text())
        if (decl["code"], decl["operatorVersion"]) != (code, project.version):
            raise ValueError(f"{code}: inconsistent declaration identity")
        target = release_target(code, project.version, decl["image"])
        key = f"{code}/v{project.version}"
        label = key if incremental or key in images else code
        if counts[code] > 1 and label == code:
            raise ValueError("multiple versions require exact image keys")
        accepted = acceptance.get("images", {}).get(label, {})
        image = images[label]["image"]
        if incremental and (images[label]["operator"] != key or
                            accepted.get("operator") != key or accepted.get("operator_version") != project.version):
            raise ValueError("accepted operator version mismatch")
        if image != accepted.get("id") or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError(f"{code}: image was not accepted")
        tag = args.candidate_tag or project.version
        if args.gitlab:
            tag = publication_tag(project.version, os.environ["CI_COMMIT_BRANCH"], os.environ["CI_PIPELINE_ID"])
        plan[target.rsplit(":", 1)[0] + ":" + tag] = image
    result = {"commit": commit, "images": publish(plan)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
