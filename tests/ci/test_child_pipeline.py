import copy
import pytest


def plan(source='push',ref='develop'):
    key='example/v1.0.0'
    return {'format_version':2,'commit':'a'*40,'execution':{'project_id':'395','parent_pipeline_id':'10',
        'plan_job_id':'11','source':source,'ref':ref,'protected':source!='merge_request_event'},
      'operators':{key:{'source':{'kind':'build'},'publish_tags':['1.0.0-dev.10'] if source!='merge_request_event' else [],
                        'mirror_tags':['1.0.0-dev.10'] if source!='merge_request_event' else [],'write_records':source!='merge_request_event'}},
      'checks':{'wheels':[key],'extra':False},'tests':{
        key+'/'+mode:{'kind':mode,'subject':key,'action':'run','requirements':{'platform':'linux/amd64','runner_tags':['software'],'environment':'ci/tooling/uv.lock'}}
        for mode in ('source','wheel','container')},'base_images':{},
      'publication':{'write_manifest':source!='merge_request_event','write_delivery_receipt':source!='merge_request_event'}}


def test_protected_builds_and_tests_download_only_their_producer_records():
    from ci.plan.pipeline import assign_jobs,render
    p=assign_jobs(plan());child=render(p,'b'*64)
    build='build-operator-images:example/v1.0.0';test='test-operator-images:example/v1.0.0'
    assert any(row.get('job')==build and row.get('artifacts') for row in child[test]['needs'])
    assert not any(row.get('job')==build for row in child['mirror-operator-images']['needs'])
    assert child['variables']['PLAN_JOB_ID']=='11' and child['variables']['PLAN_SHA256']=='b'*64
    assert all('.tar' not in path for row in child.values() if isinstance(row,dict)
               for path in row.get('artifacts',{}).get('paths',[]))


def test_mr_groups_build_and_test_without_write_environment():
    from ci.plan.pipeline import assign_jobs,render
    p=assign_jobs(plan('merge_request_event','feature/example'));child=render(p,'b'*64)
    assert not any(name.startswith(('build-operator-images','publish-','mirror-')) for name in p['jobs'])
    assert any(name.startswith('check-images:') for name in p['jobs'])
    assert all('environment' not in child[name] for name in p['jobs'])
    assert p['tests']['example/v1.0.0/source']['job']==p['tests']['example/v1.0.0/wheel']['job']


def test_main_metadata_only_still_requires_manual_gate():
    from ci.plan.pipeline import assign_jobs,render
    p=plan(ref='main');p['operators']={};p['checks']={'wheels':[],'extra':False};p['tests']={}
    p['publication']={'write_manifest':False,'write_delivery_receipt':True}
    child=render(assign_jobs(p),'b'*64)
    assert child['publish-operator-images']['when']=='manual'
    assert child['publish-operator-images']['allow_failure'] is False
    assert any(row.get('job')=='publish-operator-images' for row in child['publish-operator-manifest']['needs'])


def test_docs_only_generates_lightweight_success_child():
    from ci.plan.pipeline import assign_jobs
    p=plan();p.update(operators={},tests={},checks={'wheels':[],'extra':False},publication={'write_manifest':False,'write_delivery_receipt':False})
    assert set(assign_jobs(p)['jobs'])=={'check-plan'}


def test_mr_cannot_smuggle_publication_actions():
    from ci.plan.pipeline import assign_jobs
    p=plan('merge_request_event','feature/example');p['publication']['write_manifest']=True
    with pytest.raises(ValueError,match='MR'):
        assign_jobs(p)


def test_missing_source_and_duplicate_test_assignment_fail_closed():
    from ci.plan.pipeline import assign_jobs,render
    p=plan();p['operators']={}
    with pytest.raises(ValueError,match='image source'):
        assign_jobs(p)
    p=assign_jobs(plan());p['jobs']['test-operator-images:example/v1.0.0']['needs'].append('absent')
    with pytest.raises(ValueError,match='missing dependency'):
        render(p,'b'*64)


def test_reused_images_with_workflow_checks_prepare_only_workflow_packages():
    from ci.plan.pipeline import assign_jobs
    p=plan();p['checks']['wheels']=[]
    p['operators']['example/v1.0.0']['source']={'kind':'registry','record':{}}
    p['tests']={'pipeline:demo/default':{'kind':'pipeline','subject':'demo/default','action':'run',
        'requirements':{'environment':'tests/pipelines/demo/uv.lock'},'versions':{'a':'example/v1.0.0'}}}
    result=assign_jobs(p)
    packages=result['jobs']['build-packages']
    assert packages['versions']==[]
    assert packages['environments']==['tests/pipelines/demo/uv.lock']
    assert 'build-packages' in result['jobs']['test-pipelines:demo/default']['needs']


def test_cache_writers_are_scoped_by_preparation_and_exact_version():
    from ci.plan.pipeline import assign_jobs,render
    frozen=assign_jobs(plan());child=render(frozen,'a'*64)
    assert child['build-packages']['variables']['UV_CACHE_POLICY']=='pull-push'
    version=child['test-operators:example/v1.0.0']['cache']
    assert version['policy']=='pull-push'
    assert version['key']['files'][0]=='operators/example/uv.lock'
    image=child['build-operator-images:example/v1.0.0']['cache']
    assert image['paths']==['.ci-cache/wheels/']
    assert image['policy']=='pull-push'
    assert 'cache' not in child['publish-operator-images']
