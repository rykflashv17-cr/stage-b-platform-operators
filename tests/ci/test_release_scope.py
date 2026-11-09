import copy
from types import SimpleNamespace

import pytest

from tests.ci.test_delivery_plan import repository, Evidence, saved, execution
from ci.plan.compose import compose
from ci.publish.delivery import components, delivery_versions


def scope(root, keys):
    (root/'ci/release-versions.yaml').write_text(''.join('- '+key+'\n' for key in keys))


def plan(root, evidence, branch='main', baseline=None):
    return compose(root,commit='a'*40,execution=execution(ref=branch),
        checks={'wheels':[p.name+'/v'+p.version for p in __import__('ci.plan.projects',fromlist=['operator_projects']).operator_projects(root)],'containers':[],'extra':False},
        evidence=evidence,baseline=baseline or {'parent':None,'components':{}})


@pytest.mark.parametrize('branch',['main','release'])
def test_release_and_main_can_publish_two_versions_of_one_operator(tmp_path,branch):
    from tests.ci.test_current_source import historical_fixture
    evidence=historical_fixture(tmp_path)
    p=plan(tmp_path,evidence,branch)
    assert set(p['operators'])=={'example_read_data/v1.0.0','example_read_data/v2.0.0'}
    assert p['operators']['example_read_data/v1.0.0']['source']['kind']=='registry'
    assert p['operators']['example_read_data/v2.0.0']['publish_tags']==['2.0.0' if branch=='main' else '2.0.0-rc.10']



@pytest.mark.parametrize('branch',['main','release'])
def test_release_scope_excludes_unapproved_version_from_publication(tmp_path,branch):
    repository(tmp_path);scope(tmp_path,['example/v2.0.0'])
    evidence=Evidence();saved(tmp_path,evidence)
    p=plan(tmp_path,evidence,branch)
    assert set(p['operators'])=={'example/v2.0.0'}
    assert p['checks']['wheels']==['example/v2.0.0']


@pytest.mark.parametrize('keys',[['example/v9.0.0'],['example/v1.0.0','example/v1.0.0']])
def test_unknown_or_duplicate_release_versions_fail_before_registry_lookup(tmp_path,keys):
    repository(tmp_path);scope(tmp_path,keys);evidence=Evidence()
    with pytest.raises(ValueError):plan(tmp_path,evidence)
    assert evidence.calls==[]


def test_cumulative_components_keep_removed_version_and_original_source_commit():
    row={'buildVersion':'1.0.0','digest':'sha256:'+'a'*64,'sourceCommit':'a'*40,'repositories':{'original':True}}
    rt=SimpleNamespace(plan={'components':{'example/v1.0.0':row},'publication_versions':['example/v2.0.0'],
                             'execution':{'ref':'main'}})
    images={'example/v2.0.0':{'image':{'digest':'sha256:'+'b'*64},'source_commit':'b'*40}}
    result=components(rt,images,now=lambda:'2026-11-06T00:00:00Z')
    assert result['example/v1.0.0']==row
    assert result['example/v2.0.0']['sourceCommit']=='b'*40


def test_formal_version_digest_cannot_be_replaced():
    rt=SimpleNamespace(plan={'components':{'example/v1.0.0':{'buildVersion':'1.0.0','digest':'sha256:'+'a'*64}},
                            'publication_versions':['example/v1.0.0'],'execution':{'ref':'main'}})
    with pytest.raises(ValueError,match='conflict'):
        components(rt,{'example/v1.0.0':{'image':{'digest':'sha256:'+'b'*64},'source_commit':'b'*40}})


def test_compatibility_helper_is_tested_but_not_published(tmp_path):
    import shutil
    import yaml
    repository(tmp_path);scope(tmp_path,['example_old/v1.0.0'])
    helper=tmp_path/'operators/helper'
    shutil.copytree(tmp_path/'operators/example_old',helper)
    path=helper/'pyproject.toml';path.write_text(path.read_text().replace('example_old','helper'))
    (helper/'operator.yaml').write_text('code: helper\noperatorVersion: 1.0.0\n')
    flow=tmp_path/'tests/pipelines/pair';flow.mkdir(parents=True)
    (flow/'test_pair.py').write_text('def test_pair(): pass\n')
    (flow/'definition.yaml').write_text(yaml.safe_dump({
        'requirements':{'platform':'linux/amd64','runner_tags':['software'],'environment':'ci/tooling/uv.lock'},
        'combinations':{'checked':{'versions':{'primary':'example_old/v1.0.0','helper':'helper/v1.0.0'},
                                    'tests':['test_pair.py'],'parameters':{}}},'container_checks':{}}))
    p=plan(tmp_path,Evidence())
    helper_row=p['operators']['helper/v1.0.0']
    assert helper_row['delivery_tag'] is None
    assert helper_row['publish_tags']==helper_row['mirror_tags']==[]
    assert 'pipeline:pair/checked' in p['tests']
    assert p['retained_versions']==p['publication_versions']==['example_old/v1.0.0']
    images={key:{'image':{'digest':'sha256:'+'a'*64},'source_commit':'b'*40} for key in p['operators']}
    assert set(components(SimpleNamespace(plan=p),images))=={'example_old/v1.0.0'}
    receipt=delivery_versions(SimpleNamespace(plan=p),{'payload':{'images':images}},
        {'payload':{'versions':{'example_old/v1.0.0':{'tag':'1.0.0','digest':'sha256:'+'a'*64}}}})
    assert set(receipt)=={'example_old/v1.0.0'}


def test_removed_source_keeps_cumulative_manifest_and_delivery_history(tmp_path):
    import shutil
    from tests.ci.test_delivery_plan import DIGEST
    repository(tmp_path);scope(tmp_path,['example/v2.0.0'])
    evidence=Evidence();delivery=saved(tmp_path,evidence)
    shutil.rmtree(tmp_path/'operators/example_old')
    for key,row in delivery['versions'].items():
        row['tag']=key.split('/v')[1]
        evidence.tags['harbor.striding.internal/data/data-operators/'+key.split('/')[0],row['tag']]=DIGEST
    previous={('example_old' if v=='1.0.0' else 'example')+'/v'+v:{'buildVersion':v,'digest':DIGEST} for v in ['1.0.0','2.0.0']}
    p=compose(tmp_path,commit='a'*40,execution=execution(ref='main'),
        checks={'wheels':[],'containers':[],'extra':False},evidence=evidence,
        baseline={'parent':None,'schema_version':4,'components':previous},delivery=delivery)
    assert evidence.calls==['example/v2.0.0']
    assert not p['operators'] and not any(p['publication'].values())
    assert components(SimpleNamespace(plan=p),{})==previous
    assert delivery_versions(SimpleNamespace(plan=p),{'payload':{'images':{}}},None)==delivery['versions']
