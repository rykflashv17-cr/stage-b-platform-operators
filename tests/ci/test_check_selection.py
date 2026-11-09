"""The Git planner and MR checks consume the same ownership rules."""
import pytest

from ci.plan.changes import make_plan, changed_paths
from ci.plan.checks import select_checks
from tests.ci.test_change_plan import history, commit


def test_publishing_tool_only_does_not_rebuild(history):
    root, base, active = history
    path = root / "ci/publish/images.py"
    path.write_text(path.read_text() + "\n# publishing-only update\n")
    commit(root)
    assert make_plan(root, base, active.values(), base)["build"] == []
    checks = select_checks(root, changed_paths(root, base), {})
    assert checks["wheels"] == checks["containers"] == []
    assert checks["tests"]


def test_source_owner_is_same_but_mr_policy_avoids_containers(history):
    root, base, active = history
    key = next(iter(active.values()))
    path = next((root / "operators" / key.split('/v')[0] / "src").rglob("*.py"))
    path.write_text(path.read_text() + "\n# algorithm update\n")
    commit(root)
    assert make_plan(root, base, active.values(), base)["build"] == [key]
    checks = select_checks(root, changed_paths(root, base), {})
    assert checks["wheels"] == [key]
    assert checks["containers"] == []


def test_undeclared_new_tool_fails_before_image_work(history):
    root, base, active = history
    (root / "ci/unknown-tool.py").write_text("# executable input\n")
    commit(root)
    with pytest.raises(ValueError, match="unclassified"):
        make_plan(root, base, active.values(), base)


def test_failed_inactive_version_is_not_hidden_by_next_document_commit(history):
    root, delivered, active = history
    path = root / "operators/mcap_delivery_accepted/src/mcap_delivery_accepted/archive.py"
    path.write_text(path.read_text() + "\n# not delivered\n")
    failed = commit(root)
    (root / "README.md").write_text("documentation after failed delivery\n")
    commit(root)
    plan = make_plan(root, delivered, active.values(), failed)
    assert "mcap_delivery_accepted/v5.0.0" in plan["build"]
    assert plan["publish"] == []


def test_container_test_only_selects_image_check_without_wheel_rebuild():
    from ci import ROOT
    key='mcap_delivery_accepted/v5.0.0'
    checks=select_checks(ROOT,['operators/'+key.split('/v')[0]+'/tests/container/test_entry.py'],{})
    assert checks['wheels']==[] and checks['containers']==[key]


def test_workflow_test_selects_declared_members_without_fixed_operator_codes():
    from ci import ROOT
    from ci.plan.projects import operator_projects
    checks=select_checks(ROOT,['tests/pipelines/pretraining_data_cleaning/test_container_pipeline.py'],{})
    assert checks['wheels']==[]
    assert set(checks['containers'])=={f'{p.name}/v{p.version}' for p in operator_projects(ROOT)}|{'mcap_delivery_accepted/v4.0.1'}


def test_test_readme_is_documentation_on_protected_branch_too():
    from ci import ROOT
    checks=select_checks(ROOT,['operators/mcap_delivery_accepted/tests/container/README.md'],{})
    assert checks['wheels']==checks['containers']==[]
