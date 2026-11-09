import copy
import hashlib
import json
import pytest

from tests.ci.test_image_handoff import Registry,image


class Records(Registry):
    def __init__(self):super().__init__();self.records={};self.writes=[]
    def read_record(self,repository,tag):return copy.deepcopy(self.records.get((repository,tag)))
    def write_record(self,repository,tag,row,*,replace=False):
        self.writes.append((repository,tag));self.records[repository,tag]=copy.deepcopy(row)
        digest='sha256:'+hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()
        self.tags[repository,tag]=digest
        return digest
    def alias_record(self,repository,source,tag):
        self.records[repository,tag]=copy.deepcopy(self.records[repository,source])
        self.tags[repository,tag]=self.tags[repository,source]


def record(registry):
    digest,config=image(registry)
    return {'format_version':1,'kind':'build','operator':'example/v1.0.0','input_digest':'b'*64,
      'image':{'repository':'harbor.striding.internal/data/data-operators/example','digest':digest,
        'config_digest':config,'platform':'linux/amd64'},'wheels':{'example.whl':'c'*64},
      'provenance':{'project_id':'395','parent_pipeline_id':'10','pipeline_id':'20','commit':'a'*40,
        'source':'push','ref':'develop','jobs':[{'id':'31','name':'build-operator-images:example/v1.0.0'}]}}


def test_build_input_lookup_works_without_wheels_and_after_artifact_expiry():
    from ci.registry.records import save_build,find_build
    registry=Records();row=record(registry)
    save_build(registry,row)
    seen=[]
    found=find_build(registry,'example/v1.0.0','b'*64,verify_provenance=lambda p:seen.append(p))
    assert found['image']['digest']==row['image']['digest']
    assert seen==[row['provenance']]
    assert find_build(registry,'example/v1.0.0','d'*64,verify_provenance=lambda p:None) is None


def test_same_input_pointer_is_preserved_and_corruption_is_not_a_cache_miss():
    from ci.registry.records import save_build,find_build,input_tag
    registry=Records();row=record(registry);save_build(registry,row)
    repository=row['image']['repository'];tag=input_tag(row['operator'],row['input_digest'])
    previous=copy.deepcopy(registry.records[repository,tag])
    # Nonreproducible rebuild may be recorded, but cannot replace the valid lookup.
    other=copy.deepcopy(row);other['wheels']={'example.whl':'d'*64}
    with pytest.raises(ValueError,match='conflict'):
        save_build(registry,other)
    assert registry.records[repository,tag]==previous
    registry.records[repository,tag]['input_digest']='e'*64
    with pytest.raises(ValueError,match='input identity'):
        find_build(registry,row['operator'],row['input_digest'],verify_provenance=lambda p:None)


def test_test_record_matches_exact_definition_and_images():
    from ci.registry.records import save_test,find_test
    registry=Records();build=record(registry)
    test={'format_version':1,'kind':'test','subject':'example/v1.0.0','mode':'container','definition_digest':'d'*64,
        'images':{'example/v1.0.0':build['image']['digest']},'provenance':build['provenance'],
        'result':{'passed':3,'failed':0,'exit_code':0}}
    repository=build['image']['repository']
    save_test(registry,repository,test)
    assert find_test(registry,repository,test,verify_provenance=lambda p:None)['result']['passed']==3
    test['definition_digest']='e'*64
    assert find_test(registry,repository,test,verify_provenance=lambda p:None) is None


def test_record_failures_never_allow_mr_or_failed_tests():
    from ci.registry.records import save_build,save_test
    registry=Records();row=record(registry);row['provenance']['source']='merge_request_event'
    with pytest.raises(ValueError,match='protected'):
        save_build(registry,row)
    test={'format_version':1,'kind':'test','subject':'example/v1.0.0','mode':'container','definition_digest':'d'*64,
        'images':{'example/v1.0.0':row['image']['digest']},'provenance':record(registry)['provenance'],
        'result':{'passed':3,'failed':1,'exit_code':1}}
    with pytest.raises(ValueError,match='passed'):
        save_test(registry,row['image']['repository'],test)
    assert not registry.writes


def test_interrupted_input_alias_is_repaired_from_new_parent_candidate(monkeypatch):
    from ci.registry.records import save_build,input_tag
    registry=Records();first=record(registry);first['harbor_tag']='1.0.0-dev.10'
    alias=registry.alias_record
    def fail(*args):raise OSError('connection interrupted after immutable write')
    monkeypatch.setattr(registry,'alias_record',fail)
    with pytest.raises(OSError):save_build(registry,first)
    second=copy.deepcopy(first);second['harbor_tag']='1.0.0-dev.12';second['provenance']['parent_pipeline_id']='12'
    monkeypatch.setattr(registry,'alias_record',alias)
    recovered=save_build(registry,second)
    assert recovered==first
    assert registry.read_record(first['image']['repository'],input_tag(first['operator'],first['input_digest']))==first
