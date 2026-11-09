from pathlib import Path
import copy
import pytest


def test_failed_result_is_written_but_never_completed_as_passed():
    from ci.acceptance.jobs import emit_test
    class Runtime:
        def __init__(self):self.rows=[]
        def emit(self,*args,**kwargs):self.rows.append((args,kwargs))
    rt=Runtime();test={'kind':'container','subject':'example/v1.0.0','identity':'a'*64}
    with pytest.raises(ValueError,match='failed'):
        emit_test(rt,test,{'passed':0,'failed':1,'exit_code':1},{})
    assert rt.rows[0][1]['status']=='failed'


def test_version_job_executes_only_assigned_mode_and_binds_package_attempt(tmp_path,monkeypatch):
    from ci.acceptance.jobs import test_operators
    from tests.ci.test_runtime import runtime,packages
    rt,client=runtime(tmp_path);packages(rt,client)
    key='example/v1.0.0';row=rt.plan['tests'][key+'/wheel'];row['identity']='a'*64
    del rt.plan['tests'][key+'/source']
    invoked=[]
    def verify(root,keys,*,modes,raise_on_failure=True):
        invoked.append((keys,modes))
        return [{'operator':key,'mode':'wheel','definition_digest':'a'*64,'passed':1,'failed':0,'exit_code':0}]
    monkeypatch.setattr('ci.acceptance.versions.verify_versions',verify)
    test_operators(rt)
    assert invoked==[([key],('wheel',))]
    import json
    payload=json.loads(rt.destination('wheel',key).read_text())['payload']
    assert payload['inputs']['packages']['producer']['job_id']=='30'


def test_version_failure_retains_assigned_report(tmp_path,monkeypatch):
    from ci.acceptance.jobs import test_operators
    from tests.ci.test_runtime import runtime,packages
    rt,client=runtime(tmp_path);packages(rt,client)
    key='example/v1.0.0';rt.plan['tests'][key+'/source']['identity']='a'*64
    del rt.plan['tests'][key+'/wheel']
    def verify(root,keys,*,modes,raise_on_failure=True):
        if raise_on_failure:raise ValueError('isolated checks failed')
        return [{'definition_digest':'a'*64,'passed':0,'failed':1,'exit_code':1}]
    monkeypatch.setattr('ci.acceptance.versions.verify_versions',verify)
    with pytest.raises(ValueError):test_operators(rt)
    import json
    assert json.loads(rt.destination('source',key).read_text())['status']=='failed'


def test_protected_container_action_only_pulls_its_declared_images(tmp_path,monkeypatch):
    from ci.acceptance.jobs import test_images
    class Runtime:
        root=tmp_path
        spec={'versions':['example/v2.0.0']}
        plan={'operators':{'example/v2.0.0':{}}}
        def evidence(self):return object()
        def image(self,key,evidence):return {'image':{'reference':key+'@sha256:abc'},'producer':{'record_digest':'sha256:def'}}
    rt=Runtime();seen=[]
    monkeypatch.setattr('ci.build.image_handoff.pull_image',lambda image:seen.append(image['reference']) or image['reference'])
    monkeypatch.setattr('ci.acceptance.jobs.execute_image_tests',lambda rt,images:seen.append(sorted(images)))
    test_images(rt)
    assert seen==['example/v2.0.0@sha256:abc',['example/v2.0.0']]


def test_repository_check_does_not_import_all_version_declarations(tmp_path,monkeypatch):
    from ci import ROOT
    from ci.acceptance.jobs import check_code
    class Runtime:
        root=ROOT;plan={'checks':{'template':False}}
        def destination(self,*args):return tmp_path/'checks.json'
        def emit(self,*args,**kwargs):self.result=args[2]
    rt=Runtime()
    monkeypatch.setattr('ci.acceptance.pytest_runner.run_tests',lambda *a,**k:{'passed':1,'failed':0,'exit_code':0})
    def forbidden(*args,**kwargs):raise AssertionError('declaration must run under its own SDK')
    monkeypatch.setattr('tools.check_operators.check',forbidden)
    monkeypatch.setattr('tools.export_registration.registration',forbidden)
    check_code(rt)
    assert rt.result['result']['passed']==1
