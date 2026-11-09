"""MR risk policy, independent of the protected delivery comparison baseline."""
from ci.plan.impact import classify


def select_checks(root, changed_paths, plan):
    from pathlib import Path
    from ci.plan.projects import shared_projects, dependencies, operator_projects
    shared=shared_projects(root)
    current={f"{p.name}/v{p.version}" for p in operator_projects(root)}
    packages=set()
    wheels, containers, tests, reasons = set(), set(), set(), {}
    for path in sorted(set(changed_paths)):
        impact = classify(root, path)
        if impact.category == "documentation":
            continue
        for owner in shared:
            paths=[p.path.relative_to(root).as_posix()+'/' for p in [*dependencies(root,owner),owner]]
            if any(path.startswith(prefix) for prefix in paths) or impact.category in {'build-tool','check-tool'}:
                packages.add(owner.distribution)
        reasons[path] = {"category": impact.category, "versions": list(impact.versions)}
        if impact.category!='container-test':wheels.update(set(impact.versions) & current)
        if impact.container:
            containers.update(impact.versions)
        if impact.category != "recipe":
            tests.add("repository")
    return {"wheels": sorted(wheels), "tests": sorted(tests), "containers": sorted(containers),
            "recipes": sorted(plan.get("foundations", [])), "reasons": reasons, "packages":sorted(packages)}
