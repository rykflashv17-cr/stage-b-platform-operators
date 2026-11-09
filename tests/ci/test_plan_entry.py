import json
from pathlib import Path
import subprocess
import sys

import pytest


def test_document_mr_plan_generates_a_valid_no_work_child_without_registry(tmp_path):
    from ci.plan.entry import generate
    from tests.ci.test_delivery_plan import repository,execution
    repository(tmp_path)
    env=execution('merge_request_event','feature/docs')
    result=generate(tmp_path,commit='a'*40,execution=env,paths=['README.md'],
        base_scope={'foundations':[]},evidence=None,baseline=None)
    assert list(result['jobs'])==['check-plan']
    assert not result['operators'] and not result['tests']


def test_ordinary_source_plan_keeps_declared_modes_without_registry(tmp_path):
    from ci.plan.entry import generate
    from tests.ci.test_delivery_plan import repository,execution
    repository(tmp_path)
    result=generate(tmp_path,commit='a'*40,execution=execution('merge_request_event','feature/source'),
        paths=['operators/example/v2.0.0/src/example/__init__.py'],base_scope={'foundations':[]},evidence=None,baseline=None)
    assert result['checks']['wheels']==['example/v2.0.0']
    assert result['jobs']['test-operators:example/v2.0.0']['needs']==['build-packages']
    assert not result['operators']


def test_freeze_bytes_match_child_and_reject_dirty_plan_identity(tmp_path):
    from ci.plan.entry import write_plan
    from ci.plan.execution import read_frozen_plan
    import yaml
    plan={'format_version':2,'commit':'a'*40,'execution':{'plan_job_id':'11','parent_pipeline_id':'10','source':'merge_request_event'},'jobs':{}}
    output=tmp_path/'build'
    digest=write_plan(plan,output)
    child=yaml.safe_load((output/'child.yml').read_text())
    assert child['variables']['PLAN_SHA256']==digest
    report=json.loads((output/'checks/planning.json').read_text())
    assert report['plan_sha256']==digest and report['commit']==plan['commit']
    assert json.loads((output/'base-images.json').read_text())=={'foundations':{}}
    assert read_frozen_plan(output/'plan.json',digest,'11')==plan
    (output/'plan.json').write_text(json.dumps(plan)+' ')
    with pytest.raises(ValueError,match='digest'):read_frozen_plan(output/'plan.json',digest,'11')


def test_planning_imports_do_not_load_sdk_pytest_or_algorithm_packages():
    root=Path(__file__).resolve().parents[2]
    process=subprocess.run([sys.executable,'-c',
        'import ci.plan.entry,sys; forbidden={"pytest","operator_sdk","numpy","mcap","pretraining_data_cleaning_common"}; '
        'assert not forbidden.intersection(sys.modules), forbidden.intersection(sys.modules)'],cwd=root,capture_output=True,text=True)
    assert process.returncode==0,process.stderr


def test_template_regression_change_cannot_omit_template_preparation(tmp_path):
    from ci.plan.entry import generate
    from tests.ci.test_delivery_plan import repository,execution
    repository(tmp_path)
    result=generate(tmp_path,commit='a'*40,execution=execution('merge_request_event','feature/template'),
        paths=['tests/templates/test_python_operator.py'],base_scope={'foundations':[]},evidence=None,baseline=None)
    assert result['checks']['template'] is True
    assert result['jobs']['build-packages']['template'] is True
    assert result['jobs']['check-code']['needs']==['build-packages']
