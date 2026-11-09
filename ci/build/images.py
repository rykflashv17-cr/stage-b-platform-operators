"""Build operator images offline using the exact foundations in their Dockerfiles."""
from ci import ROOT
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import tempfile

from ci.build.context import stage
from ci.plan.projects import operator_projects
from ci.build import bases as operator_bases
from ci.build import wheelhouse


def build_plan(root, requested=()):
    projects = {f"{p.name}/v{p.version}": p for p in operator_projects(root)}
    plan = []
    for key in requested or projects:
        if key not in projects:
            raise ValueError(f"unknown operator version {key}")
        project = projects[key]
        refs, runtime = operator_bases.parse_dockerfile((project.path / "Dockerfile").read_text())
        plan.append((key, project, refs, runtime))
    return plan


def identity(ref):
    return subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", ref],
                                   text=True).strip()


def build(root, plan, tag_suffix, *, bootstrap=False, no_cache=False, wheel_cache=None, frozen_bases=None):
    bases, targets = {}, {}
    for _, _, refs, runtime in plan:
        for ref in refs:
            if ref not in bases:
                bases[ref] = (operator_bases.materialize(ref,frozen_bases[ref]) if frozen_bases is not None else
                              operator_bases.resolve(root, ref, bootstrap=bootstrap))
        if runtime not in targets:
            targets[runtime] = wheelhouse.probe_target(bases[runtime]["image_id"])
    counts = Counter(project.name for _, project, _, _ in plan)
    results = {}
    for key, project, refs, runtime in plan:
        tag = f"local/{project.name}:v{project.version}-{tag_suffix}"
        with tempfile.TemporaryDirectory(prefix="operator-build-context-") as scratch:
            context = Path(scratch) / "context"
            manifest = stage(root, key, context, bases=bases, target=targets[runtime], cache=wheel_cache)
            subprocess.run(["docker", "build", "--platform", operator_bases.PLATFORM, "--network", "none",
                            *(["--no-cache"] if no_cache else []), "--pull=false", "-f", str(context / "Dockerfile"),
                            "-t", tag, str(context)], check=True)
        for ref in refs:
            base = bases[ref]
            checked = base["reference"] if base["bootstrap"] or frozen_bases is not None else ref
            if identity(checked) != base["image_id"] or identity(base["reference"]) != base["image_id"]:
                raise RuntimeError(f"{key}: foundation image changed during build: {ref}")
        result_key = project.name if counts[project.name] == 1 else key
        results[result_key] = {"image": identity(tag), "tag": tag, "operator": key,
                               "platform": operator_bases.PLATFORM, "bases": manifest["bases"],
                               "build_inputs": manifest}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, help="Incremental exact-version plan")
    parser.add_argument("--operator", action="append", help="Exact <code>/v<version>; default: all registered versions")
    parser.add_argument("--tag", default="local-validation")
    parser.add_argument("--output", type=Path, default=Path("build/container-images.json"))
    parser.add_argument("--bootstrap-runtime", action="store_true", help="Allow the verified first release seed in local/MR builds")
    parser.add_argument("--no-cache", action="store_true", help="Ignore Docker build layer cache")
    parser.add_argument("--wheel-cache", type=Path, help="Hash-checked wheel download cache (default: build/locked-wheel-cache)")
    args = parser.parse_args()
    options = dict(bootstrap=args.bootstrap_runtime, no_cache=args.no_cache, wheel_cache=args.wheel_cache)
    root = ROOT
    if args.plan:
        from ci.plan.images import load_plan
        plan = load_plan(args.plan, root)
        if args.operator:
            parser.error("--operator and --plan are mutually exclusive")
        built = build(root, build_plan(root, plan["build"]), args.tag, **options) if plan["build"] else {}
        by_key = {row["operator"]: row for row in built.values()}
        results = dict(by_key)
        for key in plan["publication_versions"]:
            if key in by_key:
                results[key] = by_key[key]
            else:
                row = plan["components"][key]
                ref = row["repositories"]["local-harbor"]["repository"] + "@" + row["digest"]
                subprocess.run(["docker", "pull", "--platform", "linux/amd64", ref], check=True)
                results[key] = {"operator": key, "image": ref, "image_id": identity(ref),
                                 "inherited": row, "platform": "linux/amd64"}
    else:
        results = build(root, build_plan(root, args.operator), args.tag, **options)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({"built": len(built) if args.plan else len(results), "resolved": len(results), "local_only": True, "report": str(args.output)}))


if __name__ == "__main__":
    main()
