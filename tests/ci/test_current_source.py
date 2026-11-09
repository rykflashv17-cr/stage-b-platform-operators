from pathlib import Path
import json
import pytest
from tests.ci.test_project_discovery import project
from ci.plan.projects import operator_projects


def current(root, code='example_read_data', version='2.0.0'):
    p=project(root,'operators/'+code,code.replace('_','-'),version)
    (p.path/'operator.yaml').write_text('code: '+code+'\noperatorVersion: "'+version+'"\n')
    return p


def test_flat_source_discovers_code_and_version_from_declaration(tmp_path):
    current(tmp_path)
    rows=operator_projects(tmp_path)
    assert [(p.name,p.version,p.path.relative_to(tmp_path).as_posix()) for p in rows]==[
        ('example_read_data','2.0.0','operators/example_read_data')]


@pytest.mark.parametrize('field,value',[('code','another_read_data'),('operatorVersion','1.0.0')])
def test_flat_source_rejects_identity_conflict(tmp_path,field,value):
    p=current(tmp_path)
    body={'code':'example_read_data','operatorVersion':'2.0.0'};body[field]=value
    (p.path/'operator.yaml').write_text(json.dumps(body))
    with pytest.raises(ValueError,match='identity'):operator_projects(tmp_path)


def test_version_directories_are_rejected_in_current_tree(tmp_path):
    project(tmp_path,'operators/example_read_data/v1.0.0','example-read-data')
    with pytest.raises(ValueError,match='current source'):operator_projects(tmp_path)


def test_archived_version_loader_keeps_only_exact_immutable_references(tmp_path):
    from ci.plan.history import load_history
    (tmp_path/'ci').mkdir()
    row={'build':{'input_digest':'a'*64,'record_digest':'sha256:'+'b'*64},
         'source':{'commit':'c'*40,'path':'operators/example_read_data/v1.0.0','definition_sha256':'d'*64},
         'tests':{mode:{'definition_digest':'e'*64,'record_digest':'sha256:'+'f'*64} for mode in ('source','wheel','container')}}
    path=tmp_path/'ci/archived-versions.json';path.write_text(json.dumps({'example_read_data/v1.0.0':row}))
    assert load_history(tmp_path,{})=={'example_read_data/v1.0.0':row}
    with pytest.raises(ValueError,match='current'):load_history(tmp_path,{'example_read_data/v1.0.0':object()})
    row['build']['record_digest']='latest';path.write_text(json.dumps({'example_read_data/v1.0.0':row}))
    with pytest.raises(ValueError,match='archived'):load_history(tmp_path,{})


def historical_fixture(root):
    from tests.ci.test_delivery_plan import Evidence, BASE, DIGEST
    p=current(root)
    (p.path/'Dockerfile').write_text('FROM '+BASE+'\n');(p.path/'uv.lock').write_text('version=1\n')
    for mode in ('source','wheel','container'):
        path=p.path/'tests'/mode/'test_entry.py';path.parent.mkdir(parents=True);path.write_text('def test_ok(): assert True\n')
    (root/'ci').mkdir(exist_ok=True)
    (root/'ci/release-versions.yaml').write_text('- example_read_data/v1.0.0\n- example_read_data/v2.0.0\n')
    key='example_read_data/v1.0.0'
    row={'build':{'input_digest':'a'*64,'record_digest':'sha256:'+'b'*64},
         'source':{'commit':'c'*40,'path':'operators/'+key,'definition_sha256':'d'*64},
         'tests':{mode:{'definition_digest':'e'*64,'record_digest':'sha256:'+'f'*64} for mode in ('source','wheel','container')}}
    (root/'ci/archived-versions.json').write_text(json.dumps({key:row}))
    import subprocess,hashlib
    from tests.ci.test_publication import git_commit_all
    old=root/'operators'/key;old.mkdir(parents=True)
    raw=b'code: example_read_data\noperatorVersion: 1.0.0\n';(old/'operator.yaml').write_bytes(raw)
    subprocess.run(['git','init','-q',str(root)],check=True)
    commit=git_commit_all(root,'original definition')
    (old/'operator.yaml').unlink();old.rmdir()
    row['source'].update(commit=commit,definition_sha256=hashlib.sha256(raw).hexdigest())
    (root/'ci/archived-versions.json').write_text(json.dumps({key:row}))
    evidence=Evidence()
    evidence.read_build=lambda k,i,d:{'operator':k,'input_digest':i,'record_digest':d,
        'provenance':{'commit':commit,'ref':'develop'},'wheels':{'old.whl':'d'*64},
        'image':{'repository':'harbor.striding.internal/data/data-operators/example_read_data','digest':DIGEST}}
    evidence.read_test=lambda required,digest:{**required,'record_digest':digest,'result':{'passed':1,'failed':0,'exit_code':0}}
    return evidence


def test_old_release_uses_verified_artifact_without_source_or_wheel_jobs(tmp_path):
    from ci.plan.compose import compose
    from ci.plan.pipeline import assign_jobs
    from tests.ci.test_delivery_plan import execution
    evidence=historical_fixture(tmp_path)
    p=assign_jobs(compose(tmp_path,commit='a'*40,execution=execution(ref='main'),
        checks={'wheels':[],'containers':[]},evidence=evidence,baseline={'parent':None,'components':{},'schema_version':4}))
    old=p['operators']['example_read_data/v1.0.0']
    assert old['source']['kind']=='registry' and old['publish_tags']==['1.0.0']
    assert old['source']['record']['provenance']['commit']==old['archived']['source']['commit']
    assert all(t['action']=='reuse' for t in p['tests'].values() if t['subject']=='example_read_data/v1.0.0')
    assert 'test-operators:example_read_data/v1.0.0' not in p['jobs']
    assert 'example_read_data/v1.0.0' not in p['jobs']['build-packages']['versions']


def test_missing_archived_image_does_not_rebuild_current_code_as_old_version(tmp_path):
    from ci.plan.compose import compose
    from tests.ci.test_delivery_plan import execution
    evidence=historical_fixture(tmp_path);evidence.read_build=lambda *args:None
    with pytest.raises(ValueError,match='archived build/image missing'):
        compose(tmp_path,commit='a'*40,execution=execution(ref='release'),
            checks={'wheels':[],'containers':[]},evidence=evidence,baseline={'parent':None,'components':{}})


def test_archived_pointer_change_requires_container_evidence_and_combination_checks(tmp_path):
    from ci.plan.checks import select_checks
    from ci.plan.compose import compose
    from tests.ci.test_delivery_plan import execution
    evidence=historical_fixture(tmp_path)
    checks=select_checks(tmp_path,['ci/archived-versions.json'],{})
    assert 'example_read_data/v1.0.0' in checks['containers']
    assert checks['wheels']==[]
    planned=compose(tmp_path,commit='a'*40,execution=execution(source='merge_request_event'),checks=checks,
        evidence=evidence,baseline={'parent':None,'components':{}})
    assert planned['operators']['example_read_data/v1.0.0']['source']['kind']=='registry'


def test_snapshot_comparison_reads_old_version_directories_but_builds_only_current(tmp_path):
    import subprocess,shutil
    from ci.plan.changes import make_plan
    from tests.ci.test_publication import git_commit_all
    p=current(tmp_path)
    (tmp_path/'ci').mkdir()
    (tmp_path/'ci/release-versions.yaml').write_text('- example_read_data/v2.0.0\n')
    old=p.path/'v2.0.0';old.mkdir()
    for item in list(p.path.iterdir()):
        if item != old:shutil.move(str(item),old/item.name)
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    base=git_commit_all(tmp_path,'version directory')
    for item in list(old.iterdir()):shutil.move(str(item),p.path/item.name)
    old.rmdir();git_commit_all(tmp_path,'current source')
    plan=make_plan(tmp_path,base,['example_read_data/v2.0.0'])
    assert plan['build']==['example_read_data/v2.0.0']


def test_local_incremental_plan_accepts_flat_current_and_inherited_archived_version(tmp_path):
    from ci.plan.images import load_plan
    from tests.ci.test_publication import git_commit_all
    historical_fixture(tmp_path)
    commit=git_commit_all(tmp_path,'archive reference')
    old='example_read_data/v1.0.0';new='example_read_data/v2.0.0'
    plan={'commit':commit,'publication_versions':[old,new],'build':[new],'publish':[new],
        'components':{old:{'buildVersion':'1.0.0','digest':'sha256:'+'a'*64,
            'repositories':{'local-harbor':{'repository':'harbor.striding.internal/data/data-operators/example_read_data'}}}}}
    path=tmp_path/'plan.json';path.write_text(json.dumps(plan))
    assert load_plan(path,tmp_path)==plan
    plan['build'].append(old);path.write_text(json.dumps(plan))
    with pytest.raises(ValueError,match='allowlist'):load_plan(path,tmp_path)
