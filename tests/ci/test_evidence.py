import copy
import pytest

from tests.ci.test_reuse_records import Records,record


class FrozenRecords(Records):
    def read_record(self,repository,reference):
        if reference.startswith('sha256:'):
            return copy.deepcopy(self.by_digest.get((repository,reference)))
        return super().read_record(repository,reference)

    def write_record(self,repository,tag,row,**kw):
        digest=super().write_record(repository,tag,row,**kw)
        if not hasattr(self,'by_digest'):self.by_digest={}
        self.by_digest[repository,digest]=copy.deepcopy(row)
        return digest


def test_build_lookup_freezes_record_not_mutable_input_alias(tmp_path):
    from ci.plan.evidence import Evidence
    from ci.registry.records import save_build,input_tag
    registry=FrozenRecords();row=record(registry);save_build(registry,row)
    evidence=Evidence(registry,None,tmp_path,'develop','a'*40,'100')
    evidence.verify=lambda provenance:None
    frozen=evidence.find_build(row['operator'],row['input_digest'])
    expected=registry.resolve(row['image']['repository'],input_tag(row['operator'],row['input_digest']))
    assert frozen['record_digest']==expected
    registry.records[row['image']['repository'],input_tag(row['operator'],row['input_digest'])]['wheels']={}
    assert evidence.read_build(row['operator'],row['input_digest'],expected)==frozen


def test_delivery_pointer_is_branch_specific_and_cannot_rollback(tmp_path,monkeypatch):
    from ci.plan.evidence import Evidence,delivery_pointer
    from ci.publish.manifest import route_for
    registry=FrozenRecords();provenance=record(registry)['provenance']
    repository=route_for('develop')[0]
    body={'format_version':1,'kind':'delivery','ref':'develop','provenance':provenance,'versions':{}}
    registry.write_record(repository,delivery_pointer('develop'),body)
    evidence=Evidence(registry,None,tmp_path,'develop','a'*40,'100');evidence.verify=lambda p:None
    monkeypatch.setattr('ci.plan.evidence.is_ancestor',lambda *args:True)
    assert evidence.delivery()['ref']=='develop'
    evidence.pipeline_id='9'
    with pytest.raises(ValueError,match='newer'):evidence.delivery()
    assert delivery_pointer('release')!=delivery_pointer('main')


def test_delivery_compare_blocks_concurrent_update_before_write(tmp_path):
    from ci.plan.evidence import Evidence,delivery_pointer
    from ci.publish.manifest import route_for
    registry=FrozenRecords();repository=route_for('develop')[0]
    evidence=Evidence(registry,None,tmp_path,'develop','a'*40,'100')
    evidence.assert_delivery_current(None)
    registry.write_record(repository,delivery_pointer('develop'),{'changed':True})
    with pytest.raises(ValueError,match='changed'):evidence.assert_delivery_current(None)


def test_pipeline_evidence_can_be_reused_across_protected_branches(tmp_path):
    from ci.plan.evidence import Evidence
    required={'mode':'pipeline','subject':'demo/default'}
    repositories={Evidence(None,None,tmp_path,branch,'a'*40,'100').test_repository(required)
        for branch in ['develop','release','main']}
    assert len(repositories)==1
