"""One file ownership table for checks and immutable build input identities.

Rules describe effects, not pipeline policy: a source change affects an image,
but an MR need not build that image. Unknown executable inputs fail closed.
"""
from dataclasses import dataclass
from pathlib import Path
import tomllib


BUILD_TOOLS = frozenset({
    "ci/serialization.py", "ci/build/jobs.py",
    "ci/__init__.py", "ci/build/__init__.py", "ci/base_images/__init__.py", "ci/plan/__init__.py",
    "ci/plan/projects.py", "ci/plan/impact.py", "ci/build/context.py", "ci/build/packages.py", "ci/build/build-constraints.txt",
    "ci/build/images.py", "ci/build/wheelhouse.py", "ci/build/wheel_metadata.py",
    "ci/build/sdk_wheel.py", "ci/build/dependency_wheels.py", "ci/build/downloads.py", "ci/build/bases.py", "ci/build/identity.py", "ci/base_images/config.py", "ci/base_images/manage.py",
    ".dockerignore",
})
CHECK_TOOLS = frozenset({
    "ci/plan/history.py", "ci/archived-versions.json", "ci/plan/checks.py", "ci/plan/release_scope.py", "ci/plan/entry.py", "ci/run.py", "ci/runtime.py", "ci/acceptance/jobs.py", "ci/acceptance/pytest_runner.py", "ci/build/metadata_environment.py", "ci/base_images/planning.py", "ci/base_images/jobs.py", "ci/scripts/job.sh", "ci/plan/__init__.py", "ci/plan/changes.py", "tools/check_operators.py",
    "ci/build/environment.py", "ci/acceptance/packages.py", "ci/plan/packages.py",
    "ci/acceptance/versions.py", "ci/acceptance/probe.py",
    "ci/plan/images.py", "tools/export_registration.py", "ci/scripts/plan.sh",
    "ci/runtime.py", "ci/build/jobs.py", "ci/build/bootstrap_uv.py",
    "ci/scripts/uv.sh", "ci/release-versions.yaml", ".gitlab-ci.yml",
    ".gitignore", ".python-version", "Makefile", "pyproject.toml", "uv.lock",
    "ci/tooling/pyproject.toml", "ci/tooling/uv.lock",
    "ci/plan/execution.py", "ci/plan/test_sets.py", "ci/plan/pipeline.py", "ci/plan/compose.py", "ci/registry/gitlab.py", "ci/registry/__init__.py",
    "ci/acceptance/__init__.py", "ci/acceptance/containers.py", "tools/__init__.py",
})
RELEASE_TOOLS = frozenset({
    "ci/publish/gitlab_releases.py", "ci/publish/operator_releases.py", "ci/publish/operator_sources.py", "ci/publish/package_releases.py",
    "ci/publish/packages.py", "ci/publish/package_records.py", "ci/publish/tags.py", "ci/publish/delivery.py", "ci/plan/evidence.py", "ci/publish/images.py", "ci/publish/mirror.py",
    "ci/plan/baseline.py", "ci/publish/manifest.py", "ci/scripts/oras.sh",
    "ci/publish/delivery.py", "ci/publish/delivery.py",
    "ci/registry/handoff.py", "ci/registry/harbor.py", "ci/registry/records.py",
    "ci/build/image_handoff.py",
    "ci/publish/__init__.py",
})


# Shared selection/fingerprint authority for the code that executes a test mode.
_ACCEPTANCE_COMMON = frozenset({
    'ci/acceptance/jobs.py', 'ci/runtime.py', 'ci/plan/execution.py',
    'ci/plan/test_sets.py', 'ci/scripts/job.sh', 'ci/scripts/uv.sh',
    'ci/build/environment.py', 'ci/tooling/uv.lock',
})
ACCEPTANCE_INPUTS = {
    mode: _ACCEPTANCE_COMMON | frozenset(
        {'ci/acceptance/containers.py', 'ci/acceptance/pytest_runner.py'}
        if mode in {'container','pipeline'} else
        {'ci/acceptance/versions.py', 'ci/acceptance/probe.py',
         'tools/check_operators.py', 'tools/export_registration.py'})
    for mode in ('source','wheel','container','pipeline')
}


# Old names are accepted only for removed files in a Git comparison. They are
# neither executable compatibility entrypoints nor a second ownership table.
REMOVED_TOOL_PATHS = {'ci/publish/package_tags.py': 'ci/publish/package_records.py', 'ci/active-versions.json': 'ci/release-versions.yaml', 'ci/projects.py': 'ci/plan/projects.py', 'ci/change_plan.py': 'ci/plan/changes.py', 'ci/publication_base.py': 'ci/plan/baseline.py', 'ci/incremental_images.py': 'ci/plan/images.py', 'ci/build_v4.py': 'ci/build/packages.py', 'ci/plan_wheels.py': 'ci/build/environment.py', 'ci/build_v4_images.py': 'ci/build/images.py', 'ci/build_context.py': 'ci/build/context.py', 'ci/build_identity.py': 'ci/build/identity.py', 'ci/operator_bases.py': 'ci/build/bases.py', 'ci/wheelhouse.py': 'ci/build/wheelhouse.py', 'ci/wheel_metadata.py': 'ci/build/wheel_metadata.py', 'ci/sdk_wheel.py': 'ci/build/sdk_wheel.py', 'ci/downloads.py': 'ci/build/downloads.py', 'ci/bootstrap_uv.py': 'ci/build/bootstrap_uv.py', 'ci/build-constraints.txt': 'ci/build/build-constraints.txt', 'ci/version_tests.py': 'ci/acceptance/versions.py', 'ci/version_probe.py': 'ci/acceptance/probe.py', 'ci/publish_images.py': 'ci/publish/images.py', 'ci/mirror_images.py': 'ci/publish/mirror.py', 'ci/publish_publication.py': 'ci/publish/manifest.py', 'ci/image_tags.py': 'ci/publish/tags.py', 'ci/check_v4.py': 'tools/check_operators.py', 'ci/registration.py': 'tools/export_registration.py', 'images/config.py': 'ci/base_images/config.py', 'images/manage.py': 'ci/base_images/manage.py', 'images/gitlab.sh': 'ci/base_images/jobs.py', 'images/ci.yml': 'ci/gitlab/common.yml', 'ci/verify_v4_containers.py': 'tests/pipelines/pretraining_data_cleaning/container_acceptance.py', 'ci/mcap_files.py': 'tests/pipelines/pretraining_data_cleaning/mcap_files.py', 'ci/gitlab_plan.sh': 'ci/scripts/plan.sh', 'ci/gitlab_plan_guard.sh': 'ci/runtime.py', 'ci/gitlab_uv.sh': 'ci/scripts/uv.sh', 'ci/gitlab_oras.sh': 'ci/scripts/oras.sh', 'ci/gitlab_images.sh': 'ci/build/jobs.py', 'ci/gitlab_mirror.sh': 'ci/publish/delivery.py', 'ci/gitlab_publication.sh': 'ci/publish/delivery.py'}


@dataclass(frozen=True)
class Impact:
    category: str
    versions: tuple[str, ...] = ()
    container: bool = False


def documentation(path):
    p = Path(path)
    # openspec/ holds planning artifacts (proposals, specs, CLI marker files
    # like .openspec.yaml); none of it is a build or check input.
    return p.parts[0] in {"docs", ".superpowers", "openspec"} or p.suffix.lower() in {".md", ".rst"} or p.name == "LICENSE"


def build_tool_files(root):
    return sorted(path for path in BUILD_TOOLS if (Path(root) / path).is_file())


def classify(root, path, *, resolve_owners=True):
    from ci.plan.projects import dependencies, operator_projects, readme_file

    root, parts = Path(root), Path(path).parts
    if not parts or Path(path).is_absolute() or ".." in parts:
        raise ValueError(f"invalid change path: {path}")
    projects = {f"{p.name}/v{p.version}": p for p in operator_projects(root)}
    if path in REMOVED_TOOL_PATHS and not (root / path).exists():
        return classify(root, REMOVED_TOOL_PATHS[path], resolve_owners=resolve_owners)
    if path in BUILD_TOOLS:
        return Impact("build-tool", tuple(sorted(projects)), True)
    execution_modes={mode for mode,paths in ACCEPTANCE_INPUTS.items() if path in paths}
    if execution_modes:
        category='check-tool' if execution_modes & {'source','wheel'} else 'container-test'
        return Impact(category,tuple(sorted(projects)),bool(execution_modes & {'container','pipeline'}))
    if path in {"ci/archived-versions.json", "ci/plan/history.py"}:
        from ci.plan.history import load_history
        return Impact("container-test", tuple(sorted(load_history(root,projects))), True)
    if path in CHECK_TOOLS or path.startswith("ci/gitlab/") and path.endswith(".yml") and path != "ci/gitlab/common.yml":
        versions = tuple(sorted(projects)) if path in {
            "ci/build/environment.py", "ci/acceptance/versions.py", "ci/acceptance/probe.py", "ci/release-versions.yaml",
            "ci/tooling/pyproject.toml", "ci/tooling/uv.lock", "tools/check_operators.py", "tools/export_registration.py"} else ()
        return Impact("check-tool", versions)
    if path in RELEASE_TOOLS:
        return Impact("release-tool")
    if path in {"ci/base_images/jobs.py", "ci/gitlab/common.yml"}:
        return Impact("recipe")
    if parts[0] in {"operators", "packages"}:
        is_operator = parts[0] == "operators"
        prefix = Path(*parts[:2])
        metadata = root / prefix / "pyproject.toml"
        readme = readme_file(tomllib.loads(metadata.read_text())) if metadata.exists() else None
        tail = Path(path).relative_to(prefix).as_posix()
        if tail.startswith(("tests/", "examples/")):
            if documentation(path):return Impact('documentation')
            versions = tuple(sorted(key for key,p in projects.items() if p.name==parts[1])) if is_operator else tuple(sorted(
                key for key,p in projects.items() if resolve_owners and
                any(dep.path.relative_to(root)==prefix for dep in dependencies(root,p))))
            if tail.startswith('tests/container/'):return Impact('container-test',versions,True)
            return Impact("tests", versions)
        if documentation(path) and not tail.startswith("src/") and tail != readme:
            return Impact("documentation")
        versions = {key for key,p in projects.items() if p.name==parts[1]} if is_operator else {
            key for key, p in projects.items()
            if resolve_owners and any(dep.path.relative_to(root) == prefix for dep in dependencies(root, p))}
        risk = tail in {"Dockerfile", "main.py", "operator.yaml", "uv.lock", "pyproject.toml"}
        # Packaged scripts, native libraries, configuration and other resources
        # need a real image check; a source-only import cannot prove inclusion.
        if tail.startswith("src/") and Path(tail).suffix not in {".py", ".pyi"}:
            risk = True
        # Deletion/rename still maps to its former owning version/package.
        return Impact("project", tuple(sorted(versions)), risk)
    if path in {"images/profiles.toml", "images/pins.json"}:
        return Impact("legacy-bases")
    if parts[0] == "images":
        return Impact("recipe")
    if len(parts)>3 and parts[:2]==('tests','pipelines') and not documentation(path):
        if not resolve_owners:return Impact('container-test')
        from ci.plan.test_sets import discover
        from ci.plan.history import load_history
        definitions=discover(root,set(projects)|set(load_history(root,projects)))
        versions={key for row in definitions.values() if row['workflow']==parts[2] for key in row['versions'].values()}
        return Impact('container-test',tuple(sorted(versions)),bool(versions))
    if path.startswith("tests/support/") and not documentation(path):
        return Impact("tests",tuple(sorted(projects)),True)
    if parts[0] in {"tests", "examples", "templates"}:
        return Impact("documentation" if documentation(path) else "tests")
    if documentation(path):
        return Impact("documentation")
    raise ValueError(f"unclassified CI/build input; register ownership in ci/plan/impact.py: {path}")


def affected_versions(root, paths):
    return {key for path in paths for impact in [classify(root, path)]
            if impact.category in {"project", "build-tool"} for key in impact.versions}
