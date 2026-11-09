"""Repository-discovered projects keep independent build inputs and wheel closures."""
from pathlib import Path
import shutil
import tomllib
import pytest
from ci.plan.projects import operator_projects, dependencies, wheel_paths, boundary_errors
from tests.ci.helpers import selected_project

ROOT = Path(__file__).resolve().parents[2]
PROJECTS = operator_projects(ROOT)

def test_discovered_projects_have_exact_sdk_and_owned_dependencies():
    projects = operator_projects(ROOT)
    assert projects
    assert len({(p.name, p.version) for p in projects}) == len(projects)
    for project in projects:
        owners=dependencies(ROOT,project)
        assert len({p.distribution for p in owners})==len(owners)
        metadata = tomllib.loads((project.path / 'pyproject.toml').read_text())['project']
        sdk, = [r for r in metadata['dependencies'] if r.startswith('striding-operator-sdk==')]
        assert wheel_paths(ROOT, project)[0].name == f"striding_operator_sdk-{sdk.split('==')[1]}-py3-none-any.whl"
    assert boundary_errors(ROOT) == []

def test_missing_exact_sdk_dependency_cannot_stage_implicit_version(tmp_path):
    from ci.plan.projects import read_project
    source = selected_project('mcap_read_normalize')
    shutil.copytree(source, tmp_path / "project")
    metadata = tmp_path / "project/pyproject.toml"
    text = metadata.read_text()
    assert 'striding-operator-sdk==' in text
    metadata.write_text(text.replace('striding-operator-sdk==', 'striding-operator-sdk>='))
    with pytest.raises(ValueError, match="exact SDK"):
        wheel_paths(ROOT, read_project(tmp_path / "project"))


@pytest.mark.parametrize("key, relative", [
    (p.path.relative_to(ROOT),next((p.path/'src').rglob('*.py')).relative_to(p.path)) for p in PROJECTS])
def test_private_edit_only_changes_own_build_inputs(tmp_path, key, relative):
    from ci.plan.projects import build_inputs, operator_projects
    # No Git, environment or global build cache may determine a build fingerprint.
    for directory in ("operators", "packages", "templates"):
        shutil.copytree(ROOT / directory, tmp_path / directory, ignore=shutil.ignore_patterns("__pycache__", ".venv"))
    projects = operator_projects(tmp_path)
    before = {(p.name, p.version): build_inputs(tmp_path, p) for p in projects}
    export = next(p for p in projects if p.path.relative_to(tmp_path) == key)
    algorithm = export.path / relative
    assert algorithm.is_file(), f"missing operator-owned implementation: {algorithm}"
    algorithm.write_text(algorithm.read_text() + "\n# private implementation change\n")
    after = {(p.name, p.version): build_inputs(tmp_path, p) for p in projects}
    assert before[export.name, export.version] != after[export.name, export.version]
    assert all(before[p.name, p.version] == after[p.name, p.version] for p in projects if p != export)


@pytest.fixture
def prepared_context(context_project, tmp_path, monkeypatch):
    """Exercise real local wheel, lock and context handling with a tiny registry fixture."""
    import hashlib
    from io import BytesIO
    from zipfile import ZipFile
    from ci.build import wheelhouse
    from ci.build.context import stage
    from ci.build.bases import parse_dockerfile
    from packaging.markers import default_environment

    root, project = context_project
    # When CI has no operator selections conftest copies the template one level deeper.
    if project.distribution == "example-quality-gate":
        (root / "operators/build").symlink_to(root / "build", target_is_directory=True)
    _, runtime = parse_dockerfile((project.path / "Dockerfile").read_text())
    ref = runtime.rsplit(":", 1)[0] + "@sha256:" + "1" * 64
    target = {"markers": {**default_environment(), "python_full_version": "3.12.14"},
              "tags": ["py3-none-any"]}
    remaining = [p for p in wheelhouse.export_lock(project.path)["packages"] if "archive" in p]
    buf = BytesIO()
    with ZipFile(buf, "w") as archive:
        archive.writestr("registry_fixture/__init__.py", "")
    data = buf.getvalue()
    filename = "registry_fixture-1.2.3-py3-none-any.whl"
    remaining.append({"name": "registry-fixture", "version": "1.2.3",
                      "wheels": [{"url": "https://nexus.striding.internal/repository/pypi/" + filename,
                                  "hashes": {"sha256": hashlib.sha256(data).hexdigest()}}]})
    monkeypatch.setattr(wheelhouse, "export_lock", lambda directory: {"packages": remaining})
    monkeypatch.setattr(wheelhouse, "urlopen", lambda url, timeout: BytesIO(data))
    def run(context):
        return stage(root, f"{project.name}/v{project.version}", context,
                     bases={runtime: {"reference": ref}}, target=target)
    return root, project, run, filename

def test_operator_context_has_only_owned_wheel_and_shared_dependencies(tmp_path, prepared_context):
    import zipfile
    root, project, run, registry_wheel = prepared_context
    key = f'{project.name}/v{project.version}'
    result = run(tmp_path / "context")
    wheels = sorted((tmp_path / "context/wheelhouse").rglob("*.whl"))
    assert {p.name for p in wheels} == {p.name for p in wheel_paths(root, project)} | {registry_wheel}
    manifest = (tmp_path / "context/requirements.install.txt").read_text().splitlines()
    assert len(manifest) == len(wheels) and all(" --hash=sha256:" in line for line in manifest)
    members = {m.split("/")[0] for p in wheels for m in zipfile.ZipFile(p).namelist()}
    assert set(project.modules) <= members
    sibling_modules = {m for p in operator_projects(ROOT) if p.name != project.name for m in p.modules}
    assert not sibling_modules & members
    assert not {'ci','tests','tools'} & members
    assert all(not path.startswith('operators/') or path.startswith(f'operators/{key.split("/v")[0]}/') for path in result['inputs'])

def test_stale_private_wheel_cannot_be_used_to_build_an_image(tmp_path, context_project):
    import pytest
    from ci.build.context import stage
    root, project = context_project
    path = next((project.path / 'src').rglob('*.py'))
    path.write_text(path.read_text() + "\n# unbuilt business edit\n")
    with pytest.raises(ValueError, match="source differs from wheel"):
        stage(root, f'{project.name}/v{project.version}', tmp_path / "context")
    assert not (tmp_path / "context").exists()

def test_project_metadata_changes_require_a_new_wheel(tmp_path, context_project):
    from ci.plan.projects import read_project
    from ci.build.context import verify_project_wheel
    import pytest
    root, target = context_project
    original = target.path
    shutil.copytree(original, tmp_path / 'project')
    project = read_project(tmp_path / 'project')
    wheels = tmp_path / 'build/wheels'
    wheels.mkdir(parents=True)
    shutil.copyfile(root / 'build/wheels' / project.wheel, wheels / project.wheel)
    pyproject = project.path / 'pyproject.toml'
    original_text = pyproject.read_text()
    metadata = tomllib.loads(original_text)['project']
    sdk, = [r for r in metadata['dependencies'] if r.startswith('striding-operator-sdk==')]
    for old, new in [(sdk, 'striding-operator-sdk==99.0.0'),
                     (metadata['requires-python'], metadata['requires-python'] + ',!=3.12.1')]:
        assert old in original_text and old != new
        pyproject.write_text(original_text.replace(old, new))
        with pytest.raises(ValueError, match='metadata'):
            verify_project_wheel(tmp_path, project)

def test_wrong_wheel_identity_cannot_pass_matching_source(tmp_path, context_project):
    import zipfile
    import pytest
    from ci.plan.projects import read_project
    from ci.build.context import verify_project_wheel
    root, target = context_project
    original = target.path
    shutil.copytree(original, tmp_path / 'project')
    project = read_project(tmp_path / 'project')
    wheel = tmp_path / 'build/wheels' / project.wheel
    wheel.parent.mkdir(parents=True)
    with zipfile.ZipFile(root / 'build/wheels' / project.wheel) as source:
        members = {n: source.read(n) for n in source.namelist()}
    metadata = next(n for n in members if n.endswith('.dist-info/METADATA'))
    for before, after in [(f'Name: {project.distribution}'.encode(), b'Name: wrong-distribution'),
                          (f'Version: {project.version}'.encode(), b'Version: 9.0.0')]:
        assert before in members[metadata]
        changed = {**members, metadata: members[metadata].replace(before, after)}
        with zipfile.ZipFile(wheel, 'w') as output:
            for name, raw in changed.items():
                output.writestr(name, raw)
        with pytest.raises(ValueError, match='metadata'):
            verify_project_wheel(tmp_path, project)


def test_private_runner_umask_does_not_hide_entrypoint_from_nonroot(tmp_path, prepared_context):
    import os
    _, _, run, _ = prepared_context
    context = tmp_path / "context"
    old = os.umask(0o077)
    try:
        run(context)
    finally:
        os.umask(old)
    for name in ("main.py", "operator.yaml"):
        assert (context / name).stat().st_mode & 0o777 == 0o644

def test_context_fixture_does_not_require_selected_business_wheels(monkeypatch, tmp_path, request):
    import json

    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"build": ["mcap_delivery_accepted/v4.0.1", "mcap_read_normalize/v4.0.1"],
                               "checks": {"wheels": ["mcap_read_normalize/v4.0.1"], "containers": []}}))
    monkeypatch.setenv("CI_OPERATOR_PLAN", str(plan))
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "merge_request_event")
    _, project = request.getfixturevalue("context_project")
    assert project.name == "record_quality_gate"
    assert project.version == "1.0.0"
    assert dependencies(project.path.parents[2], project)==[]
