import hashlib
import json
import pytest


def encoded(value):
    raw=json.dumps(value,sort_keys=True).encode()
    return 'sha256:'+hashlib.sha256(raw).hexdigest(),raw


class Registry:
    def __init__(self):
        self.tags={};self.objects={};self.tagged=[]
    def resolve(self,repository,tag):return tag if tag in self.objects else self.tags.get((repository,tag))
    def fetch(self,repository,digest,*,blob=False):return self.objects[digest]
    def tag(self,repository,digest,tag):
        self.tagged.append((repository,digest,tag));self.tags[repository,tag]=digest


def image(registry):
    config,raw=encoded({'os':'linux','architecture':'amd64','rootfs':{'type':'layers','diff_ids':[]}})
    registry.objects[config]=raw
    digest,raw=encoded({'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json',
        'config':{'mediaType':'application/vnd.oci.image.config.v1+json','digest':config,'size':len(raw)},'layers':[]})
    registry.objects[digest]=raw
    return digest,config


def test_stage_uses_existing_repository_and_actual_build_attempt():
    from ci.registry.handoff import temporary_target
    env={'CI_PIPELINE_SOURCE':'parent_pipeline','CI_COMMIT_REF_PROTECTED':'true','CI_COMMIT_BRANCH':'develop','CI_JOB_ID':'42'}
    execution={'source':'push','ref':'develop','parent_pipeline_id':'10','protected':True}
    assert temporary_target('mcap_read_normalize/v4.0.1',execution,env)==(
        'harbor.striding.internal/data/data-operators/mcap_read_normalize','4.0.1-ci.10.42')
    execution['source']='merge_request_event'
    with pytest.raises(ValueError,match='protected'):
        temporary_target('mcap_read_normalize/v4.0.1',execution,env)
    execution['source']='push'
    with pytest.raises(ValueError,match='operator'):
        temporary_target('../../elsewhere/v4.0.1',execution,env)


def test_registry_image_identity_accepts_config_or_manifest_local_ids():
    from ci.registry.handoff import inspect_manifest,check_local_image
    registry=Registry();digest,config=image(registry)
    row=inspect_manifest(registry,'harbor.striding.internal/data/data-operators/example',digest)
    assert row['digest']==digest and row['config_digest']==config
    for local_id in (config,digest):
        check_local_image({'Id':local_id,'Os':'linux','Architecture':'amd64'},row)
    with pytest.raises(ValueError,match='local image'):
        check_local_image({'Id':'sha256:'+'0'*64,'Os':'linux','Architecture':'amd64'},row)
    registry.objects[digest]+=b' '
    with pytest.raises(ValueError,match='digest'):
        inspect_manifest(registry,row['repository'],digest)


def test_image_index_requires_exactly_one_target_platform():
    from ci.registry.handoff import inspect_manifest
    registry=Registry();digest,config=image(registry)
    descriptor={'digest':digest,'size':len(registry.objects[digest]),'mediaType':'application/vnd.oci.image.manifest.v1+json',
                'platform':{'os':'linux','architecture':'amd64'}}
    index,raw=encoded({'schemaVersion':2,'mediaType':'application/vnd.oci.image.index.v1+json','manifests':[descriptor,descriptor]})
    registry.objects[index]=raw
    with pytest.raises(ValueError,match='one linux/amd64'):
        inspect_manifest(registry,'harbor.striding.internal/data/data-operators/example',index)


def test_promote_uses_exact_accepted_digest_and_never_overwrites():
    from ci.registry.handoff import promote
    registry=Registry();digest,_=image(registry)
    repository='harbor.striding.internal/data/data-operators/example'
    assert promote(registry,repository,digest,'4.0.1-dev.10')==digest
    assert promote(registry,repository,digest,'4.0.1-dev.10')==digest
    assert len(registry.tagged)==1
    registry.tags[repository,'4.0.1-dev.10']='sha256:'+'0'*64
    with pytest.raises(ValueError,match='conflict'):
        promote(registry,repository,digest,'4.0.1-dev.10')
