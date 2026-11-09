import pytest
from tests.ci.gitlab_probe import make_plan
from ci.plan.pipeline import render


def test_fault_exercises_keep_generated_dependencies_without_external_writers():
    execution = {'source':'merge_request_event','protected':False,'project_id':'395',
                 'parent_pipeline_id':'10','plan_job_id':'11','ref':'refs/merge-requests/1/head'}
    plan = make_plan(execution, 'a'*40, 'retry')
    rendered = render(plan, 'b'*64)
    assert len(plan['jobs']) == 7
    assert all(row['action'] == 'check-plan' for row in plan['jobs'].values())
    assert rendered['publish-operator-images']['when'] == 'manual'
    assert rendered['publish-operator-images']['allow_failure'] is False
    assert rendered['mirror-operator-images']['needs'][1]['job'] == 'publish-operator-images'
    assert not any('environment' in rendered[name] for name in plan['jobs'])
    with pytest.raises(ValueError, match='MR'):
        make_plan({**execution,'source':'push'}, 'a'*40, 'retry')
