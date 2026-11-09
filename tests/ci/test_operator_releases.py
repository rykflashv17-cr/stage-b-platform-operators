from copy import deepcopy
from urllib.parse import urlsplit, parse_qs
import pytest
from tests.ci.test_package_releases import registry


def record():
    return {'code':'example_read_data','version':'2.0.0','source_commit':'a'*40,
        'source_path':'operators/example_read_data','image_digest':'sha256:'+'b'*64,
        'definition_sha256':'c'*64,'build_pipeline_id':'20','parent_pipeline_id':'10'}


def operator_client(registry):
    from ci.publish.operator_releases import OperatorReleases
    old,state=registry
    return OperatorReleases(old.api,old.token,old.project),state


def test_operator_release_uses_original_source_and_only_download_assets(registry):
    client,state=operator_client(registry);saved=record()
    client.sync(saved,notes='升级交付格式；调用方需调整输出名。')
    row=state['release']
    assert row['name']=='[Operator][Stable] example_read_data v2.0.0'
    assert row['tag_name']=='operators/example_read_data/v2.0.0'
    assert row['commit']['id']==saved['source_commit']
    assert 'harbor.striding.internal/data/data-operators/example_read_data:2.0.0' in row['description']
    assert 'registry-dp-registry.cn-beijing.cr.aliyuncs.com/data-operators/example_read_data:2.0.0' in row['description']
    links={r['name']:r['url'] for r in row['assets']['links']}
    assert set(links)=={'operator.yaml','本算子源码（tar.gz）'}
    assert parse_qs(urlsplit(links['本算子源码（tar.gz）']).query)=={'sha':['a'*40],'path':['operators/example_read_data']}
    before=deepcopy(state['writes']);client.sync(saved,notes='不得覆盖人工说明')
    assert state['writes']==before


def test_operator_release_conflict_does_not_move_tag_or_replace_image(registry):
    client,state=operator_client(registry);client.sync(record());state['writes'].clear()
    with pytest.raises(ValueError,match='conflict'):client.sync({**record(),'image_digest':'sha256:'+'e'*64})
    assert state['writes']==[]


def test_operator_release_write_failure_recovers_without_republishing_image(registry):
    client,state=operator_client(registry);state['lose_response']=True
    with pytest.raises(ValueError):client.sync(record())
    state['lose_response']=False;state['writes'].clear()
    client.sync(record());assert state['writes']==[]


def test_manifest_asset_remains_bound_to_registration_snapshot(registry):
    client,state=operator_client(registry)
    first={'name':'版本发布清单','url':'https://registry.invalid/first.yaml','link_type':'other'}
    client.sync(record(),first_assets=[first]);state['writes'].clear()
    client.sync(record(),first_assets=[{**first,'url':'https://registry.invalid/next.yaml'}])
    assert state['writes']==[]
    assert first in [{k:r[k] for k in first} for r in state['release']['assets']['links']]


@pytest.mark.parametrize('image_tag,stage',[
    ('2.0.0-dev.12','Dev'),('2.0.0-rc.13','RC')])
def test_candidate_release_identity_matches_delivered_image(registry,image_tag,stage):
    client,state=operator_client(registry)
    client.sync({**record(),'image_tag':image_tag})
    row=state['release']
    assert row['name']==f'[Operator][{stage}] example_read_data v{image_tag}'
    assert row['tag_name']==f'operators/example_read_data/v{image_tag}'
    assert f'example_read_data:{image_tag}' in row['description']
    assert row['commit']['id']=='a'*40
    before=deepcopy(state['writes'])
    client.sync({**record(),'image_tag':image_tag})
    assert state['writes']==before


@pytest.mark.parametrize('image_tag',['2.0.0rc1','2.0.0-dev.0','2.0.1-dev.12','2.0.0-ci.12','latest'])
def test_invalid_operator_release_version_fails_before_remote_write(registry,image_tag):
    client,state=operator_client(registry)
    with pytest.raises(ValueError):client.sync({**record(),'image_tag':image_tag})
    assert state['writes']==[]


@pytest.mark.parametrize('branch,image_tag,stage',[
    ('develop','2.0.0-dev.12','Dev'),('release','2.0.0-rc.13','RC')])
def test_candidate_sync_preserves_reused_tag_and_delivery_notes_snapshot(tmp_path,monkeypatch,registry,branch,image_tag,stage):
    from types import SimpleNamespace
    from tests.ci.test_operator_backfill import context
    from tests.ci.test_publication import git_commit_all
    from ci.publish import operator_releases as mod
    evidence,document,raw,key=context(tmp_path,monkeypatch,branch=branch,image_tag=image_tag)
    original=evidence.commit
    notes=tmp_path/'docs/releases/operators/example_read_data/2.0.0.md'
    notes.parent.mkdir(parents=True)
    notes.write_text('## 变更说明\n\n- 本轮输入验证。\n\n## 兼容性\n\n- 保持输入格式。\n')
    delivery_commit=git_commit_all(tmp_path,'delivery notes')
    notes.write_text('不应读取工作区的新说明')
    client,state=operator_client(registry)
    monkeypatch.setattr(mod.OperatorReleases,'environment',lambda env:client)
    rt=SimpleNamespace(root=tmp_path,env={},plan={'commit':delivery_commit,
        'execution':{'ref':branch,'source':'push','protected':True,'parent_pipeline_id':'30'},
        'operators':{key:{'delivery_tag':image_tag}},'publication_versions':[key]})
    images={key:{'producer':{'record_digest':'sha256:'+'d'*64},'input_digest':'e'*64,
        'source_commit':original,'image':evidence.build['image']}}
    mod.sync_releases(rt,evidence,images,{'layerDigest':'sha256:'+'f'*64})
    row=state['release']
    assert row['tag_name']==f'operators/example_read_data/v{image_tag}'
    assert row['name']==f'[Operator][{stage}] example_read_data v{image_tag}'
    assert row['commit']['id']==original
    assert '本轮输入验证' in row['description'] and '工作区' not in row['description']
    link=next(a['url'] for a in row['assets']['links'] if a['name']=='版本发布清单')
    assert ('operators-develop/' if branch=='develop' else 'operators/') in link
    before=deepcopy(state['writes'])
    mod.sync_releases(rt,evidence,images,{'layerDigest':'sha256:'+'f'*64})
    assert state['writes']==before


@pytest.mark.parametrize('branch,protected,source,image_tag',[
    ('feature/test',True,'push','2.0.0-dev.12'),
    ('develop',False,'push','2.0.0-dev.12'),
    ('develop',True,'merge_request_event','2.0.0-dev.12'),
    ('develop',True,'push','2.0.0'),
    ('release',True,'push','2.0.0-dev.12'),
    ('main',True,'push','2.0.0-rc.13')])
def test_sync_rejects_wrong_release_branch_or_untrusted_execution(tmp_path,monkeypatch,registry,branch,protected,source,image_tag):
    from types import SimpleNamespace
    from tests.ci.test_operator_backfill import context
    from ci.publish import operator_releases as mod
    evidence,document,raw,key=context(tmp_path,monkeypatch)
    client,state=operator_client(registry)
    monkeypatch.setattr(mod.OperatorReleases,'environment',lambda env:client)
    rt=SimpleNamespace(root=tmp_path,env={},plan={'commit':evidence.commit,
        'execution':{'ref':branch,'protected':protected,'source':source},
        'operators':{key:{'delivery_tag':image_tag}},'publication_versions':[key]})
    images={key:{'producer':{'record_digest':'sha256:'+'d'*64},'input_digest':'e'*64,
        'source_commit':evidence.commit,'image':evidence.build['image']}}
    with pytest.raises(ValueError):mod.sync_releases(rt,evidence,images)
    assert state['writes']==[]


@pytest.mark.parametrize('branch,image_tag',[('develop','1.0.0-dev.10'),('release','1.0.0-rc.10'),('main','1.0.0')])
def test_manifest_then_release_failure_can_retry_without_build_or_mirror(tmp_path,monkeypatch,branch,image_tag):
    from types import SimpleNamespace
    from ci.publish import delivery,operator_releases
    from ci.publish.manifest import route_for
    from ci.plan.evidence import delivery_pointer
    from tests.ci.test_delivery_evidence import Runtime,KEY,DIGEST
    from tests.ci.test_evidence import FrozenRecords
    rt=Runtime();rt.root=tmp_path;rt.env={}
    rt.plan.update(components={},parent=None,delivery_baseline=None,retained_versions=[KEY],
        publication={'write_manifest':True},jobs={'mirror-operator-images':{}})
    rt.plan['execution'].update(ref=branch,protected=True)
    rt.plan['operators'][KEY].update(delivery_tag=image_tag,mirror_tags=[image_tag],build_identity={'digest':'e'*64})
    registry=FrozenRecords();rt.evidence=lambda **kw:SimpleNamespace(registry=registry)
    harbor={'producer':{'job_id':'50','job':'publish-operator-images'},'payload':{'images':{KEY:rt.image_value}}}
    acr={'producer':{'job_id':'51','job':'mirror-operator-images'},'payload':{'harbor_producer':harbor['producer'],'versions':{KEY:{'tag':image_tag,'digest':DIGEST}}}}
    rt.consume=lambda *a:acr
    monkeypatch.setattr(delivery,'assert_manifest_current',lambda *a:None)
    monkeypatch.setattr(delivery,'confirmed_harbor',lambda *a:harbor)
    events=[]
    monkeypatch.setattr(delivery,'publish_document',lambda *a:events.append('manifest') or {'layerDigest':DIGEST})
    def failed(*args):
        assert (tmp_path/'build/release/delivery.json').is_file()
        assert registry.resolve(route_for(branch)[0],delivery_pointer(branch))
        events.append('release');raise ValueError('GitLab unavailable')
    monkeypatch.setattr(operator_releases,'sync_releases',failed)
    rt.emit=lambda *a:events.append('complete')
    # ACR mismatch must stop before either external document or Release writes.
    acr['payload']['versions'][KEY]['digest']='sha256:'+'f'*64
    with pytest.raises(ValueError,match='ACR'):delivery.publish_manifest(rt)
    assert events==[]
    acr['payload']['versions'][KEY]['digest']=DIGEST
    with pytest.raises(ValueError,match='GitLab unavailable'):delivery.publish_manifest(rt)
    assert events==['manifest','release']
    monkeypatch.setattr(operator_releases,'sync_releases',lambda *a:events.append('release') or [])
    delivery.publish_manifest(rt)
    assert events==['manifest','release','manifest','release','complete']


def test_sync_uses_frozen_build_record_and_neutral_manifest_label(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from ci.publish import operator_releases as mod
    from tests.ci.test_operator_backfill import context
    evidence,document,raw,key=context(tmp_path,monkeypatch)
    client=SimpleNamespace(sync=lambda saved,**kw:kw)
    monkeypatch.setattr(mod.OperatorReleases,'environment',lambda env:client)
    evidence.find_build=lambda *a:(_ for _ in ()).throw(AssertionError('mutable alias must not be read'))
    rt=SimpleNamespace(root=tmp_path,env={},plan={'commit':evidence.commit,'execution':{'ref':'main','source':'push','protected':True},'operators':{key:{'delivery_tag':'2.0.0'}},'publication_versions':[key]})
    images={key:{'producer':{'record_digest':'sha256:'+'d'*64},'input_digest':'e'*64,
        'source_commit':evidence.build['provenance']['commit'],'image':evidence.build['image']}}
    result=mod.sync_releases(rt,evidence,images,{'layerDigest':'sha256:'+'f'*64})
    assert result[0]['first_assets'][0]['name']=='版本发布清单'


def test_local_release_client_authenticates_provenance_reads_with_private_token(registry):
    from ci.publish.operator_releases import OperatorReleases
    old,state=registry
    client=OperatorReleases(old.api,'local-api-token','395',token_header='PRIVATE-TOKEN')
    client.sync(record())
    value,_=client.get('/projects/395/repository/tags/operators%2Fexample_read_data%2Fv2.0.0')
    assert value['commit']['id']==record()['source_commit']
    assert state['private_tokens'] and all(token=='local-api-token' for token in state['private_tokens'])
    assert all(row[2] is None for row in state['requests'])
