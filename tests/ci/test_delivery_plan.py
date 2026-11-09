from pathlib import Path
import copy
import json

import pytest

from ci.serialization import hash_json


BASE='harbor.striding.internal/data/data-operators/base/runtime/python312-cpu:1.0.0'
DIGEST='sha256:'+'a'*64


class Evidence:
    def __init__(self):self.builds={};self.tests={};self.tags={};self.calls=[]
    def freeze_base(self,ref):return {'digest':DIGEST,'reference':ref.rsplit(':',1)[0]+'@'+DIGEST}
    def find_build(self,key,digest):
        self.calls.append(key)
        return self.builds.get((key,digest))
    def find_test(self,required):return self.tests.get(hash_json(required))
    def resolve_tag(self,repository,tag):return self.tags.get((repository,tag))


def repository(root):
    from tests.ci.test_project_discovery import project
    for version in ['1.0.0','2.0.0']:
        code='example_old' if version=='1.0.0' else 'example'
        p=project(root,'operators/'+code,code,version)
        (p.path/'Dockerfile').write_text('FROM '+BASE+'\n')
        (p.path/'uv.lock').write_text('version=1\n')
        (p.path/'src/example').mkdir(parents=True);(p.path/'src/example/__init__.py').write_text('VALUE=1\n')
        for mode in ['source','wheel','container']:
            path=p.path/'tests'/mode/'test_example.py';path.parent.mkdir(parents=True);path.write_text('def test_ok(): pass\n')
    (root/'ci').mkdir();(root/'ci/release-versions.yaml').write_text('- example_old/v1.0.0\n')


def execution(source='push',ref='develop'):
    return {'project_id':'395','parent_pipeline_id':'10','plan_job_id':'11','source':source,'ref':ref,'protected':source!='merge_request_event'}


def saved(root,evidence):
    from ci.build.identity import build_identity
    from ci.plan.test_sets import version_test
    delivery={'format_version':1,'versions':{}}
    for v in ['1.0.0','2.0.0']:
        key=('example_old' if v=='1.0.0' else 'example')+'/v'+v;identity=build_identity(root,key,{BASE:evidence.freeze_base(BASE)})['digest']
        image={'repository':'harbor.striding.internal/data/data-operators/'+key.split('/')[0],'digest':DIGEST,
               'config_digest':DIGEST,'platform':'linux/amd64','reference':'harbor.striding.internal/data/data-operators/example@'+DIGEST}
        evidence.builds[key,identity]={'operator':key,'input_digest':identity,'image':image,
            'record_digest':'sha256:'+'e'*64,'harbor_tag':v+'-dev.7','provenance':{'ref':'develop','commit':'b'*40}}
        evidence.tags[image['repository'],v+'-dev.7']=DIGEST
        delivery['versions'][key]={'input_digest':identity,'image_digest':DIGEST,'tag':v+'-dev.7','harbor':True,'acr':True}
        for mode in ['source','wheel','container']:
            t=version_test(root,key,mode)
            required={'format_version':1,'kind':'test','subject':key,'mode':mode,'definition_digest':t['identity'],'images':{key:DIGEST}}
            evidence.tests[hash_json(required)]={**required,'record_digest':'sha256:'+'f'*64,'result':{'passed':1,'failed':0,'exit_code':0}}
    return delivery


def compose(root,execution_value,evidence,delivery=None,checks=None):
    from ci.plan.compose import compose
    return compose(root,commit='a'*40,execution=execution_value,
        checks=checks or {'wheels':[],'containers':[],'extra':False},evidence=evidence,
        baseline={'parent':{'artifactDigest':'sha256:'+'d'*64,'sourceCommit':'b'*40},'schema_version':4,
                  'components':{('example_old' if v=='1.0.0' else 'example')+'/v'+v:{'buildVersion':v,'digest':DIGEST} for v in ('1.0.0','2.0.0')}},delivery=delivery)


def test_ordinary_mr_needs_no_registry_or_image_work(tmp_path):
    repository(tmp_path);evidence=Evidence()
    p=compose(tmp_path,execution('merge_request_event','feature/change'),evidence,
        checks={'wheels':['example_old/v1.0.0'],'containers':[],'extra':False})
    assert not p['operators'] and not evidence.calls
    assert {row['kind'] for row in p['tests'].values()}=={'source','wheel'}


def test_delivered_document_only_change_does_no_build_or_delivery(tmp_path):
    repository(tmp_path);evidence=Evidence();delivery=saved(tmp_path,evidence)
    p=compose(tmp_path,execution(),evidence,delivery)
    assert not p['operators'] and not p['tests']
    assert p['publication']=={'write_manifest':False,'write_delivery_receipt':False}


def test_inactive_acr_failure_recovers_by_old_proven_tag_without_rebuild(tmp_path):
    repository(tmp_path);evidence=Evidence();delivery=saved(tmp_path,evidence)
    del delivery['versions']['example/v2.0.0']
    p=compose(tmp_path,execution(),evidence,delivery)
    row=p['operators']['example/v2.0.0']
    assert row['source']['kind']=='registry' and row['publish_tags']==[] and row['mirror_tags']==['2.0.0-dev.7']
    assert p['publication']=={'write_manifest':False,'write_delivery_receipt':True}


def test_only_container_test_change_retests_without_new_image_or_tag(tmp_path):
    repository(tmp_path);evidence=Evidence();delivery=saved(tmp_path,evidence)
    (tmp_path/'operators/example/tests/container/test_example.py').write_text('def test_more(): pass\n')
    p=compose(tmp_path,execution(),evidence,delivery)
    row=p['operators']['example/v2.0.0']
    assert row['source']['kind']=='registry' and not row['publish_tags'] and not row['mirror_tags'] and row['write_records']
    assert [r['kind'] for r in p['tests'].values() if r['action']=='run']==['container']
    assert not p['publication']['write_manifest']


def test_main_promotes_only_selected_versions_with_manual_delivery_plan(tmp_path):
    repository(tmp_path);evidence=Evidence();saved(tmp_path,evidence)
    p=compose(tmp_path,execution(ref='main'),evidence)
    assert set(p['operators'])=={'example_old/v1.0.0'} and evidence.calls==['example_old/v1.0.0']
    assert p['operators']['example_old/v1.0.0']['publish_tags']==['1.0.0']


@pytest.mark.parametrize('accepted', [False, True])
def test_main_filters_unselected_versions_from_full_release_diff(tmp_path, accepted):
    from ci.plan.pipeline import assign_jobs

    repository(tmp_path);evidence=Evidence()
    if accepted:saved(tmp_path,evidence)
    checks={'wheels':['example_old/v1.0.0','example/v2.0.0'],
            'containers':['example_old/v1.0.0','example/v2.0.0'],
            'tests':['repository'],'extra':True}
    p=compose(tmp_path,execution(ref='main'),evidence,checks=checks)
    assert set(p['operators'])=={'example_old/v1.0.0'}
    assert {row['subject'] for row in p['tests'].values()}=={'example_old/v1.0.0'}
    assert all(p['checks'][mode]==['example_old/v1.0.0'] for mode in ('wheels','containers'))
    assert p['checks']['tests']==['repository']
    assert checks['wheels']==['example_old/v1.0.0','example/v2.0.0']
    jobs=assign_jobs(p)['jobs']
    assert jobs['publish-operator-images']['manual'] is True
    assert 'check-code' in jobs
    assert all('example/v2.0.0' not in job.get('versions',[]) for job in jobs.values())


@pytest.mark.parametrize('source,ref', [('push','develop'),
                                      ('merge_request_event','main')])
def test_non_main_delivery_and_mr_keep_unselected_version_checks(tmp_path, source, ref):
    repository(tmp_path);evidence=Evidence()
    keys=['example_old/v1.0.0','example/v2.0.0']
    p=compose(tmp_path,execution(source,ref),evidence,
              checks={'wheels':keys,'containers':keys,'extra':False})
    assert set(p['operators'])==set(keys)
    assert {row['subject'] for row in p['tests'].values()}==set(keys)
    assert p['checks']['wheels']==sorted(keys)


def test_old_manifest_without_build_evidence_requires_fresh_validation(tmp_path):
    repository(tmp_path);evidence=Evidence()
    p=compose(tmp_path,execution(),evidence)
    assert all(row['source']['kind']=='build' for row in p['operators'].values())
    assert len(p['tests'])==6 and all(t['action']=='run' for t in p['tests'].values())


def test_denied_lookup_is_not_silently_rebuilt(tmp_path):
    repository(tmp_path);evidence=Evidence()
    def deny(*args):raise ValueError('registry HTTP 403')
    evidence.find_build=deny
    with pytest.raises(ValueError,match='403'):compose(tmp_path,execution(),evidence)


def test_selecting_already_delivered_version_retains_its_evidence_for_manifest(tmp_path):
    repository(tmp_path);evidence=Evidence();delivery=saved(tmp_path,evidence)
    (tmp_path/'ci/release-versions.yaml').write_text('- example/v2.0.0\n')
    p=compose(tmp_path,execution(),evidence,delivery)
    assert not p['publication']['write_manifest']
    assert not p['operators'] and not p['tests']
