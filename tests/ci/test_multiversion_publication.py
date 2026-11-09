"""A publication records independent versions, without erasing older releases."""
import copy
import json

import pytest
import yaml

from ci.publish import manifest as pp
from tests.ci.test_publication import CODE, COMMIT, FakeRegistry, ci_env, component, document, repo, git_commit_all


def v3_document(branch='main'):
    value=document(component(), sourceBranch=branch, releaseLine=branch)
    value['schemaVersion']=3
    first={'code':CODE, **value['components'][CODE]}
    second={**copy.deepcopy(first), 'buildVersion':'3.0.0', 'digest':'sha256:'+'b'*64}
    value['components']=[first,second]
    return value


def test_v3_validates_two_versions_of_one_code_and_rejects_duplicate_identity():
    value=v3_document()
    assert pp.validate_document(value)==value
    value['components'].append(copy.deepcopy(value['components'][0]))
    with pytest.raises(pp.PublicationError):pp.validate_document(value)


def test_v3_rejects_repository_mismatch_and_unknown_version():
    for change in ({'code':'other'}, {'buildVersion':'03.0.0'}, {'code':None}):
        value=v3_document();value['components'][1].update(change)
        with pytest.raises(pp.PublicationError):pp.validate_document(value)


def test_fixed_release_routes_to_its_own_v4_publication():
    repository,tag,line,commit,_=pp.validate_ci(ci_env('release'))
    assert repository==pp.PUBLICATION_PREFIX+'operators'
    assert tag=='v4-release-p00000000000000004711-'+COMMIT[:12]
    assert line=='release' and commit==COMMIT
    with pytest.raises(pp.PublicationError):pp.validate_ci(ci_env('release/v4.0.1'))


def test_v4_canonical_bytes_do_not_depend_on_component_input_order(tmp_path):
    value=v3_document()
    args=('main',COMMIT,value['pipeline'],None)
    raw=pp.build_document(*args,value['components'])
    assert raw==pp.build_document(*args,list(reversed(value['components'])))
    assert yaml.safe_load(raw)['schemaVersion']==4
    tag='v4-main-p00000000000000004711-'+COMMIT[:12]
    receipt=pp.package_publication(tmp_path/'layout',tag,raw)
    envelope=json.loads(pp.verify_layout(tmp_path/'layout',receipt,tag,raw))
    assert envelope['artifactType']=='application/vnd.striding.data-platform.publication.v4'
    assert envelope['layers'][0]['mediaType']=='application/vnd.striding.data-platform.publication.v4+yaml'


def test_internal_exact_version_key_cannot_disagree_with_component():
    value=v3_document();row={k:v for k,v in value['components'][0].items() if k!='code'}
    with pytest.raises(pp.PublicationError,match='version'):
        pp.build_document('main',COMMIT,value['pipeline'],None,{CODE+'/v9.0.0':row})


@pytest.mark.parametrize('field',['projectId','pipelineId'])
def test_pipeline_identifiers_must_be_strings(field):
    value=v3_document();value['pipeline'][field]=4711
    with pytest.raises(pp.PublicationError):pp.validate_document(value)


def test_v2_artifact_is_read_without_rewriting_original_bytes(tmp_path):
    value=document(component())
    raw=yaml.safe_dump(value,sort_keys=False).encode()
    tag='v2-develop-p00000000000000004711-'+COMMIT[:12]
    receipt=pp.package_publication(tmp_path/'legacy',tag,raw)
    envelope=json.loads(pp.verify_layout(tmp_path/'legacy',receipt,tag,raw))
    assert envelope['artifactType']=='application/vnd.striding.data-platform.publication.v2'
    registry=FakeRegistry()
    client=pp.Oras('oras','config',tmp_path/'scratch',registry.run).bind(pp.PUBLICATION_PREFIX+'operators-develop')
    client.publish(tmp_path/'legacy',tag,receipt['artifactDigest'],json.dumps(envelope,sort_keys=True,separators=(',',':')).encode(),raw)
    actual,original=pp.fetch_publication(client,client.target,receipt['artifactDigest'])
    assert actual==value and original==raw


def test_v3_rejects_an_oci_envelope_claiming_v2(tmp_path):
    value=v3_document();raw=yaml.safe_dump(value).encode()
    tag='v3-main-p00000000000000004711-'+COMMIT[:12]
    receipt=pp.package_publication(tmp_path/'layout',tag,raw)
    registry=FakeRegistry();repository=pp.PUBLICATION_PREFIX+'operators'
    envelope=json.loads((tmp_path/'layout/blobs/sha256'/receipt['artifactDigest'][7:]).read_bytes())
    envelope['artifactType']='application/vnd.striding.data-platform.publication.v2'
    encoded=json.dumps(envelope).encode();digest=pp.digest(encoded)
    registry.values[repository+'@'+digest]=encoded
    registry.values[repository+'@'+envelope['layers'][0]['digest']]=raw
    client=pp.Oras('oras','config',tmp_path/'scratch',registry.run)
    with pytest.raises(pp.PublicationError):pp.fetch_publication(client,repository,digest)


def save_snapshot(tmp_path, registry, *, branch, commit, pipeline, version, image_digest, schema=2):
    value=document(component(),sourceBranch=branch,releaseLine=branch,sourceCommit=commit,
        pipeline={'projectId':'4242','pipelineId':str(pipeline),'pipelineUrl':pp.PIPELINE_URL_PREFIX+str(pipeline)})
    value['components'][CODE].update(buildVersion=version,digest=image_digest,sourceCommit=commit)
    if schema==3:
        value['schemaVersion']=3
        value['components']=[{'code':CODE,**value['components'][CODE]}]
    raw=yaml.safe_dump(value,sort_keys=False).encode()
    tag=f'v{schema}-{branch}-p{pipeline:020d}-{commit[:12]}'
    layout=tmp_path/f'layout-{pipeline}'
    receipt=pp.package_publication(layout,tag,raw)
    repository=pp.PUBLICATION_PREFIX+('operators-develop' if branch=='develop' else 'operators')
    client=pp.Oras('oras','config',tmp_path/'scratch',registry.run).bind(repository)
    encoded=(layout/'blobs/sha256'/receipt['artifactDigest'][7:]).read_bytes()
    client.publish(layout,tag,receipt['artifactDigest'],encoded,raw)
    return client,receipt


def test_first_v3_baseline_recovers_versions_displaced_in_older_v2_snapshots(repo,tmp_path):
    import subprocess
    from ci.plan.baseline import registry_baseline
    first=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    (repo/'change').write_text('next');second=git_commit_all(repo,'next')
    registry=FakeRegistry()
    save_snapshot(tmp_path,registry,branch='main',commit=first,pipeline=10,version='2.0.0',image_digest='sha256:'+'a'*64)
    client,receipt=save_snapshot(tmp_path,registry,branch='main',commit=second,pipeline=11,version='3.0.0',image_digest='sha256:'+'b'*64)
    baseline=registry_baseline(client,'main',second,'12','4242',repo)
    assert set(baseline['components'])=={CODE+'/v2.0.0',CODE+'/v3.0.0'}
    assert baseline['components'][CODE+'/v2.0.0']['sourceCommit']==first
    assert baseline['parent']['artifactDigest']==receipt['artifactDigest']
    assert baseline['schema_version']==2


@pytest.mark.parametrize('branch,conflict',[('main',True),('develop',False)])
def test_bootstrap_rejects_formal_replacement_but_keeps_latest_candidate(repo,tmp_path,branch,conflict):
    import subprocess
    from ci.plan.baseline import registry_baseline
    first=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    (repo/'change').write_text('next');second=git_commit_all(repo,'next')
    registry=FakeRegistry()
    save_snapshot(tmp_path,registry,branch=branch,commit=first,pipeline=10,version='2.0.0',image_digest='sha256:'+'a'*64)
    client,_=save_snapshot(tmp_path,registry,branch=branch,commit=second,pipeline=11,version='2.0.0',image_digest='sha256:'+'b'*64)
    if conflict:
        with pytest.raises(pp.PublicationError,match='conflict'):registry_baseline(client,branch,second,'12','4242',repo)
    else:
        assert registry_baseline(client,branch,second,'12','4242',repo)['components'][CODE+'/v2.0.0']['digest']=='sha256:'+'b'*64


@pytest.mark.parametrize('version',[3,4])
def test_documented_example_has_two_independently_addressable_versions(version):
    from ci import ROOT
    value=yaml.safe_load((ROOT/f'docs/examples/operator-publication-v{version}.yaml').read_text())
    assert set(pp.component_index(value))=={'mcap_delivery_accepted/v4.0.1','mcap_delivery_accepted/v5.0.0'}
    assert value['schemaVersion']==version and value['sourceBranch']=='main'


def test_history_scan_limit_fails_instead_of_discarding_older_versions(tmp_path):
    from ci.plan.baseline import registry_baseline
    class ManyTags:
        def repo_tags(self,repository):
            return [f'v2-main-p{i:020d}-'+COMMIT[:12] for i in range(1,4098)]
    with pytest.raises(pp.PublicationError,match='history-scan-limit'):
        registry_baseline(ManyTags(),'main',COMMIT,'5000','4242',tmp_path)


def test_v3_tag_cannot_disguise_a_v2_snapshot_and_bypass_history_merge(repo,tmp_path):
    import subprocess
    from ci.plan.baseline import registry_baseline
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    registry=FakeRegistry()
    client,receipt=save_snapshot(tmp_path,registry,branch='main',commit=commit,pipeline=10,
        version='2.0.0',image_digest='sha256:'+'a'*64)
    registry.values[client.target+':v3-main-p00000000000000000010-'+commit[:12]]=registry.values[client.target+'@'+receipt['artifactDigest']]
    registry.tags[client.target].append('v3-main-p00000000000000000010-'+commit[:12])
    with pytest.raises(pp.PublicationError):registry_baseline(client,'main',commit,'12','4242',repo)


def test_malformed_v3_snapshot_does_not_become_empty_first_publication(repo,tmp_path):
    import subprocess
    from ci.plan.baseline import registry_baseline
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    registry=FakeRegistry();repository=pp.PUBLICATION_PREFIX+'operators'
    raw=b'{}';registry.values[repository+':v3-main-p00000000000000000010-'+commit[:12]]=raw
    registry.values[repository+'@'+pp.digest(raw)]=raw
    registry.tags[repository]=['v3-main-p00000000000000000010-'+commit[:12]]
    client=pp.Oras('oras','config',tmp_path/'scratch',registry.run)
    with pytest.raises(pp.PublicationError):registry_baseline(client,'main',commit,'12','4242',repo)


def test_first_v4_publication_carries_v3_history_forward(repo,tmp_path):
    import subprocess
    from types import SimpleNamespace
    from ci.plan.baseline import registry_baseline
    from ci.plan.projects import operator_projects
    from ci.publish.delivery import publish_document
    from tests.ci.test_publication import CODE, FakeRegistry, REPO_DEVELOP, image_manifest
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    registry=FakeRegistry()
    _,receipt=save_snapshot(tmp_path,registry,branch='develop',commit=commit,pipeline=10,
        version='2.0.0',image_digest='sha256:'+'a'*64,schema=3)
    work=tmp_path/'out';work.mkdir(parents=True)
    client=pp.Oras('oras',work/'auth.json',work/'scratch',registry.run)
    baseline=registry_baseline(client,'develop',commit,'4711','4242',repo)
    assert baseline['schema_version']==3
    assert baseline['parent']['artifactDigest']==receipt['artifactDigest']
    versions=[p.name+'/v'+p.version for p in operator_projects(repo)]
    _,image_digest=image_manifest()
    images={key:{'image':{'digest':image_digest},'source_commit':commit} for key in versions}
    rt=SimpleNamespace(root=repo,plan={'execution':{'ref':'develop','parent_pipeline_id':'4711',
        'project_id':'4242'},'commit':commit,'publication_versions':versions,**baseline})
    publish_document(rt,SimpleNamespace(client=lambda _: client),images)
    document=yaml.safe_load((repo/'build/release/manifest.yaml').read_bytes())
    assert document['schemaVersion']==4 and document['sourceBranch']=='develop'
    assert document['parentPublication']['artifactDigest']==receipt['artifactDigest']
    row=document['components'][0]
    assert (row['code'],row['buildVersion'])==(CODE,'2.0.0')
    assert row['operatorYaml']['repository']==pp.DEFINITIONS_PREFIX+CODE
    assert registry.tags[pp.DEFINITIONS_PREFIX+CODE]==['sha256-'+pp.digest((repo/'operators'/CODE/'operator.yaml').read_bytes())[7:]]
    assert json.loads((repo/'build/release/receipt.json').read_text())['tag'].startswith('v4-develop-')
    # The next pipeline discovers the v4 line directly and keeps the reference.
    baseline2=registry_baseline(client,'develop',commit,'4800','4242',repo)
    assert baseline2['schema_version']==4
    assert baseline2['components'][CODE+'/v2.0.0']['operatorYaml']==row['operatorYaml']


def test_future_tags_do_not_hide_the_previous_v3_baseline(repo,tmp_path):
    import subprocess
    from ci.plan.baseline import registry_baseline
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    registry=FakeRegistry()
    client,_=save_snapshot(tmp_path,registry,branch='main',commit=commit,pipeline=10,
        version='2.0.0',image_digest='sha256:'+'a'*64,schema=3)
    for pipeline in range(100,121):
        registry.values[client.target+f':v3-main-p{pipeline:020d}-'+commit[:12]]=b'{}'
        registry.tags[client.target].append(f'v3-main-p{pipeline:020d}-'+commit[:12])
    assert set(registry_baseline(client,'main',commit,'12','4242',repo)['components'])=={CODE+'/v2.0.0'}
