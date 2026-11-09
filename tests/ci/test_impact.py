from pathlib import Path

import pytest

from ci.plan.projects import build_inputs, operator_projects
from ci.plan.impact import affected_versions, build_tool_files, classify
from ci.plan.checks import select_checks
from tests.ci.test_project_discovery import project


@pytest.fixture
def repository(tmp_path):
    for version in ("1.0.0", "2.0.0"):
        owner = project(tmp_path, f"operators/example_read_data_{version[0]}", "example-operator-"+version[0], version)
        (owner.path / "src").mkdir()
        (owner.path / "src/main.py").write_text("value = 1\n")
    for filename in ("build/context.py", "publish/images.py", "publish/mirror.py"):
        path=tmp_path / "ci" / filename
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text("# initial\n")
    return tmp_path


def test_source_checks_keep_both_changed_versions_without_container_work(repository):
    paths = [f"operators/example_read_data_{version[0]}/src/main.py" for version in ("1.0.0", "2.0.0")]
    result = select_checks(repository, paths, {})
    assert result["wheels"] == ["example_read_data_1/v1.0.0", "example_read_data_2/v2.0.0"]
    assert result["containers"] == []


@pytest.mark.parametrize("name", ["Dockerfile", "pyproject.toml", "uv.lock", "main.py", "operator.yaml"])
def test_environment_and_entrypoint_changes_select_exact_version(repository, name):
    result = select_checks(repository, [f"operators/example_read_data_2/{name}"], {})
    assert result["containers"] == ["example_read_data_2/v2.0.0"]


@pytest.mark.parametrize("name", ["src/example/install.sh", "src/example/default.yaml", "src/example/model.bin"])
def test_packaged_resources_and_install_scripts_require_container_checks(repository, name):
    result = select_checks(repository, [f"operators/example_read_data_2/{name}"], {})
    assert result["containers"] == ["example_read_data_2/v2.0.0"]


def test_shared_packaged_resource_selects_all_consumers(repository):
    project(repository, "packages/config", "shared-config", "1.0.0")
    for path in (repository / "operators").glob("*/pyproject.toml"):
        path.write_text(path.read_text().replace("dependencies = []", 'dependencies = ["shared-config==1.0.0"]'))
    checks = select_checks(repository, ["packages/config/src/shared_config/default.yaml"], {})
    assert checks["containers"] == ["example_read_data_1/v1.0.0", "example_read_data_2/v2.0.0"]


def test_publication_change_does_not_invalidate_build_but_build_tool_does(repository):
    projects = operator_projects(repository)
    before = [build_inputs(repository, p) for p in projects]
    (repository / "ci/publish/images.py").write_text("# publish changed\n")
    assert [build_inputs(repository, p) for p in projects] == before
    assert affected_versions(repository, ["ci/publish/images.py"]) == set()
    assert select_checks(repository, ["ci/publish/images.py"], {})["containers"] == []
    (repository / "ci/build/context.py").write_text("# build changed\n")
    assert all(build_inputs(repository, p) != old for p, old in zip(projects, before))
    assert affected_versions(repository, ["ci/build/context.py"]) == {
        "example_read_data_1/v1.0.0", "example_read_data_2/v2.0.0"}
    assert "ci/build/context.py" in build_tool_files(repository)


def test_packaged_readme_and_deleted_source_belong_to_version(repository):
    path = repository / "operators/example_read_data_1/pyproject.toml"
    path.write_text(path.read_text().replace('[project]', '[project]\nreadme="README.md"'))
    checks = select_checks(repository, ["operators/example_read_data_1/README.md",
                                         "operators/example_read_data_2/src/deleted.py"], {})
    assert checks["wheels"] == ["example_read_data_1/v1.0.0", "example_read_data_2/v2.0.0"]
    assert classify(repository, "docs/guide.md").category == "documentation"
    assert classify(repository, "openspec/changes/example-change/.openspec.yaml").category == "documentation"


def test_unclassified_ci_or_build_file_is_not_silently_skipped(repository):
    for path in ("ci/new_builder.py", "vendor/tool", "new-build-tool.sh"):
        with pytest.raises(ValueError, match="unclassified"):
            classify(repository, path)


def test_test_only_change_requires_checks_but_not_wheel_or_container(repository):
    checks = select_checks(repository, ["tests/ci/test_release.py"], {})
    assert checks["tests"]
    assert checks["wheels"] == checks["containers"] == []


def test_shared_test_change_selects_only_compatible_consumer_versions(repository):
    project(repository, "packages/shared-new", "shared-config", "2.0.0")
    for version in ("1.0.0","2.0.0"):
        path=repository/f"operators/example_read_data_{version[0]}/pyproject.toml"
        path.write_text(path.read_text().replace("dependencies = []",f'dependencies = ["{("archived-config" if version=="1.0.0" else "shared-config")}=={version}"]'))
    checks=select_checks(repository,["packages/shared-new/tests/source/test_contract.py"],{})
    assert checks['wheels']==['example_read_data_2/v2.0.0']
    assert checks['containers']==[]
    assert affected_versions(repository,["packages/shared-new/tests/source/test_contract.py"])==set()
