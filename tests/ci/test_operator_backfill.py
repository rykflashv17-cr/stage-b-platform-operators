"""Recover only Release presentation from an exact completed branch delivery."""
import json,subprocess
from copy import deepcopy
from types import SimpleNamespace
import pytest,yaml
from tests.ci.test_package_releases import registry
from tests.ci.test_operator_releases import operator_client


def context(root,monkeypatch,*,branch='main',image_tag='2.0.0'):
    from ci.publish import manifest as pp
    from tests.ci.test_publication import git_commit_all
    folder=root/'operators/example_read_data';folder.mkdir(parents=True)
    folder.joinpath('operator.yaml').write_text('code: example_read_data\noperatorVersion: 2.0.0\n')
    subprocess.run(['git','init','-q',str(root)],check=True);commit=git_commit_all(root,'original source')
    key='example_read_data/v2.0.0';image='sha256:'+'b'*64
    component={'buildVersion':'2.0.0','sourceCommit':commit,'digest':image,
        'repositories':{'local-harbor':{'role':'canonical','repository':'harbor.striding.internal/data/data-operators/example_read_data','verifiedAt':'2026-11-06T00:00:00Z'}}}
    raw=pp.build_document(branch,commit,{'projectId':'395','pipelineId':'30','pipelineUrl':pp.PIPELINE_URL_PREFIX+'30'},None,{key:component})
    document=yaml.safe_load(raw)
    receipt={'format_version':1,'kind':'delivery','ref':branch,'versions':{key:{'input_digest':'e'*64,'image_digest':image,'tag':image_tag,'harbor':True,'acr':True}},
        'provenance':{'project_id':'395','parent_pipeline_id':'30','pipeline_id':'31','commit':commit,'ref':branch,'source':'push'}}
    build={'image':{'digest':image},'input_digest':'e'*64,'record_digest':'sha256:'+'d'*64,
        'provenance':{'commit':commit,'pipeline_id':'20','parent_pipeline_id':'10'}}
    calls=[]
    class Registry:
        def client(self,repository):return self
        def resolve(self,*args):return image if len(args)==2 else 'sha256:'+'a'*64
        def read_record(self,repository,tag):
            assert tag==f'delivery-{branch}-p30';return deepcopy(receipt)
    evidence=SimpleNamespace(root=root,registry=Registry(),commit=commit,receipt=receipt,build=build,calls=calls,
        verify=lambda row:calls.append(deepcopy(row)),find_build=lambda *a:deepcopy(build),read_build=lambda *a:deepcopy(build))
    monkeypatch.setattr(pp,'fetch_publication',lambda *a:(deepcopy(document),raw))
    return evidence,document,raw,key


@pytest.mark.parametrize('problem',[None,'candidate','wrong_parent','wrong_commit','unmirrored','wrong_image','wrong_acr','lost_source','untrusted'])
@pytest.mark.parametrize('branch,image_tag',[('develop','2.0.0-dev.12'),('release','2.0.0-rc.13'),('main','2.0.0')])
def test_backfill_requires_exact_snapshot_receipt_and_original_build(tmp_path,monkeypatch,registry,problem,branch,image_tag):
    from ci.publish.operator_releases import backfill
    evidence,document,raw,key=context(tmp_path,monkeypatch,branch=branch,image_tag=image_tag);client,state=operator_client(registry)
    remote=lambda ref:'sha256:'+'b'*64
    if problem=='candidate':document.update(sourceBranch='release' if branch=='develop' else 'develop',releaseLine='release' if branch=='develop' else 'develop')
    if problem=='wrong_parent':evidence.receipt['provenance']['parent_pipeline_id']='32'
    if problem=='wrong_commit':evidence.receipt['provenance']['commit']='f'*40
    if problem=='unmirrored':evidence.receipt['versions'][key]['acr']=False
    if problem=='wrong_image':evidence.build['image']['digest']='sha256:'+'c'*64
    if problem=='wrong_acr':remote=lambda ref:'sha256:'+'c'*64
    if problem=='lost_source':evidence.build['provenance']['commit']='f'*40
    if problem=='untrusted':evidence.verify=lambda row:(_ for _ in ()).throw(ValueError('untrusted producer'))
    if problem:
        with pytest.raises(ValueError):backfill(client,evidence,key,'sha256:'+'a'*64,remote,record_digest='sha256:'+'d'*64,branch=branch)
        assert state['writes']==[]
    else:
        evidence.find_build=lambda *a:(_ for _ in ()).throw(AssertionError('frozen record required'))
        backfill(client,evidence,key,'sha256:'+'a'*64,remote,record_digest='sha256:'+'d'*64,branch=branch)
        assert state['tag']['commit']['id']==evidence.commit and len(evidence.calls)==1
        before=deepcopy(state['writes'])
        backfill(client,evidence,key,'sha256:'+'a'*64,remote,record_digest='sha256:'+'d'*64,branch=branch)
        assert state['writes']==before
        assert '版本发布清单' in {a['name'] for a in state['release']['assets']['links']}


@pytest.mark.parametrize('branch,image_tag,stage',[
    ('develop','2.0.0-dev.12','Dev'),('release','2.0.0-rc.13','RC')])
def test_backfill_candidate_uses_frozen_receipt_tag(tmp_path,monkeypatch,registry,branch,image_tag,stage):
    from ci.publish.operator_releases import backfill
    evidence,document,raw,key=context(tmp_path,monkeypatch,branch=branch,image_tag=image_tag)
    client,state=operator_client(registry)
    def resolve_acr(ref):
        assert ref.endswith(':'+image_tag)
        return 'sha256:'+'b'*64
    backfill(client,evidence,key,'sha256:'+'a'*64,resolve_acr,record_digest='sha256:'+'d'*64,branch=branch)
    assert state['release']['tag_name']==f'operators/example_read_data/v{image_tag}'
    assert state['release']['name']==f'[Operator][{stage}] example_read_data v{image_tag}'
    before=deepcopy(state['writes'])
    backfill(client,evidence,key,'sha256:'+'a'*64,resolve_acr,record_digest='sha256:'+'d'*64,branch=branch)
    assert state['writes']==before
    evidence.receipt['versions'][key]['tag']='2.0.0'
    with pytest.raises(ValueError):
        backfill(client,evidence,key,'sha256:'+'a'*64,resolve_acr,record_digest='sha256:'+'d'*64,branch=branch)
    assert state['writes']==before
