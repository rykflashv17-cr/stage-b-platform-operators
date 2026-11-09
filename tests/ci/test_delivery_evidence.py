import copy
import pytest

KEY='example/v1.0.0'
DIGEST='sha256:'+'a'*64


class Runtime:
    def assert_producers_current(self):pass
    def __init__(self):
        self.plan={'commit':'c'*40,'operators':{KEY:{'source':{'kind':'build'}}},
            'tests':{KEY+'/container':{'kind':'container','subject':KEY,'identity':'b'*64,'action':'run',
                'job':'test-image','versions':{'example':KEY}}},'jobs':{},'publication_versions':[KEY],
                'execution':{'project_id':'395','parent_pipeline_id':'10','source':'push','ref':'develop'}}
        self.digest='d'*64;self.pipeline='20'
        self.image_value={'image':{'digest':DIGEST,'reference':'harbor.striding.internal/data/data-operators/example@'+DIGEST},
            'producer':{'job':'build-image','job_id':'30','pipeline_id':'20'},'wheels':{'example.whl':'e'*64},
            'inputs':{'packages':{'producer':{'job':'build-packages','job_id':'29','pipeline_id':'20'},
                                  'payload':{'wheels':{'example.whl':'e'*64}}}},'source_commit':'c'*40}
        self.test={'producer':{'job':'test-image','job_id':'31','pipeline_id':'20'},'payload':{
            'definition_digest':'b'*64,'result':{'passed':1,'failed':0,'exit_code':0},
            'inputs':{'images':{KEY:{'image':self.image_value['image'],'producer':self.image_value['producer']}}}}}
    def image(self,*args):return copy.deepcopy(self.image_value)
    def consume(self,*args):return copy.deepcopy(self.test)
    def packages(self,**kw):return copy.deepcopy(self.image_value['inputs']['packages'])


def test_acceptance_is_bound_to_actual_latest_image_attempt():
    from ci.publish.delivery import accepted
    rt=Runtime()
    images,tests=accepted(rt,None)
    assert images[KEY]['producer']['job_id']=='30'
    rt.image_value=copy.deepcopy(rt.image_value)
    rt.image_value['producer']['job_id']='35'
    with pytest.raises(ValueError,match='image input'):accepted(rt,None)


def test_failed_empty_or_changed_test_cannot_publish():
    from ci.publish.delivery import accepted
    for update in [{'definition_digest':'f'*64},{'result':{'passed':0,'failed':0,'exit_code':0}}]:
        rt=Runtime();rt.test['payload'].update(update)
        with pytest.raises(ValueError):accepted(rt,None)


def test_reused_component_keeps_original_build_commit():
    from ci.publish.delivery import components
    rt=Runtime();rt.plan['components']={}
    rt.image_value['source_commit']='f'*40
    result=components(rt,{KEY:rt.image_value},now=lambda:'2026-11-05T00:00:00Z')
    assert result[KEY]['sourceCommit']=='f'*40
    assert result[KEY]['buildVersion']=='1.0.0'


def test_delivery_receipt_preserves_successful_inactive_versions_and_requires_acr():
    from ci.publish.delivery import delivery_versions
    rt=Runtime();rt.plan.update(retained_versions=[KEY,'example/v2.0.0'],delivery_baseline={'versions':{
        'example/v2.0.0':{'tag':'2.0.0-dev.7','input_digest':'e'*64,'image_digest':DIGEST,'harbor':True,'acr':True}}})
    rt.plan['operators'][KEY].update(delivery_tag='1.0.0-dev.10',mirror_tags=['1.0.0-dev.10'],
        build_identity={'digest':'f'*64})
    harbor={'payload':{'images':{KEY:rt.image_value}}}
    with pytest.raises(ValueError,match='ACR'):delivery_versions(rt,harbor,None)
    acr={'payload':{'versions':{KEY:{'tag':'1.0.0-dev.10','digest':DIGEST}}}}
    result=delivery_versions(rt,harbor,acr)
    assert len(result)==2 and result['example/v2.0.0']['tag']=='2.0.0-dev.7'
    assert result[KEY]['tag']=='1.0.0-dev.10' and result[KEY]['acr'] is True


def test_delivery_receipt_cannot_mark_missing_retained_version_complete():
    from ci.publish.delivery import delivery_versions
    rt=Runtime();rt.plan.update(retained_versions=[KEY,'example/v2.0.0'],delivery_baseline=None)
    rt.plan['operators'][KEY].update(delivery_tag='1.0.0-dev.10',mirror_tags=[],build_identity={'digest':'f'*64})
    with pytest.raises(ValueError,match='ACR|coverage'):
        delivery_versions(rt,{'payload':{'images':{KEY:rt.image_value}}},None)


def test_image_publication_promotes_existing_digest_without_build(tmp_path,monkeypatch):
    from ci.publish import delivery
    from tests.ci.test_reuse_records import Records,record
    registry=Records();saved=record(registry);rt=Runtime()
    rt.plan['tests']={}
    rt.plan['execution']['protected']=True;rt.plan['delivery_baseline']=None
    rt.plan['operators'][KEY]={'source':{'kind':'registry'},'publish_tags':['1.0.0-dev.10'],
        'mirror_tags':['1.0.0-dev.10'],'delivery_tag':'1.0.0-dev.10','write_records':False}
    image={**rt.image_value,'image':saved['image']}
    class Evidence:
        def __init__(self):self.registry=registry
        def assert_delivery_current(self,*args):pass
    rt.evidence=lambda **kwargs:Evidence()
    rt.emit=lambda kind,subject,payload:setattr(rt,'published',payload)
    monkeypatch.setattr(delivery,'accepted',lambda *args:({KEY:image},{}))
    monkeypatch.setattr(delivery,'assert_manifest_current',lambda *args:None)
    delivery.publish_images(rt)
    assert registry.resolve(saved['image']['repository'],'1.0.0-dev.10')==saved['image']['digest']
    assert rt.published['images'][KEY]['image']['digest']==saved['image']['digest']


def test_mr_cannot_enter_publication_or_write_evidence():
    from ci.publish.delivery import publish_images
    rt=Runtime();rt.plan['execution'].update(source='merge_request_event',protected=False)
    with pytest.raises(ValueError,match='protected'):publish_images(rt)


def test_v4_document_keeps_exact_versions_and_retry_bytes(tmp_path):
    from ci.publish.delivery import publish_document
    from ci.publish import manifest as external
    from tests.ci.test_publication import FakeRegistry
    registry=FakeRegistry();rt=Runtime();rt.root=tmp_path
    import subprocess
    from tests.ci.test_publication import make_operator,git_commit_all
    make_operator(tmp_path,'example','1.0.0')
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    commit=git_commit_all(tmp_path,'original')
    rt.plan['commit']=commit;rt.image_value['source_commit']=commit
    rt.plan.update(parent=None,components={})
    class Adapter:
        def client(self,repository):return external.Oras('oras','config',tmp_path/'oras',registry.run)
    first=publish_document(rt,Adapter(),{KEY:rt.image_value})
    second=publish_document(rt,Adapter(),{KEY:rt.image_value})
    assert first==second and len(registry.copies)==2
    assert first['tag']=='v4-develop-p00000000000000000010-'+commit[:12]
    assert (tmp_path/'build/release/manifest.yaml').is_file()
    assert not (tmp_path/'build/publication').exists()


def test_mirror_cannot_use_harbor_report_from_old_test_attempt(monkeypatch):
    from ci.publish import delivery
    rt=Runtime()
    rt.consume=lambda *args:{'producer':{'job_id':'40'},'payload':{'images':{KEY:rt.image_value},'tests':{'test':{'job_id':'31'}}}}
    monkeypatch.setattr(delivery,'accepted',lambda *a:({KEY:rt.image_value},{'test':{'producer':{'job_id':'39'}}}))
    with pytest.raises(ValueError,match='Harbor evidence'):delivery.confirmed_harbor(rt,None)


def test_delivery_pointer_retry_accepts_same_receipt_and_rejects_newer(tmp_path):
    from ci.publish.delivery import save_delivery
    from ci.plan.evidence import delivery_pointer
    from tests.ci.test_evidence import FrozenRecords
    from ci.publish.manifest import route_for
    registry=FrozenRecords();rt=Runtime();rt.plan['delivery_baseline']=None
    rt.root=tmp_path
    body={'format_version':1,'kind':'delivery','ref':'develop','versions':{},'provenance':{'parent_pipeline_id':'10'}}
    first=save_delivery(rt,registry,body)
    assert save_delivery(rt,registry,body)==first
    repository=route_for('develop')[0]
    registry.write_record(repository,delivery_pointer('develop'),{'newer':True},replace=True)
    with pytest.raises(ValueError,match='changed'):save_delivery(rt,registry,body)


def test_pipeline_with_locked_local_wheels_rejects_an_old_package_attempt():
    from ci.publish.delivery import accepted
    rt=Runtime();row=rt.plan['tests'].pop(KEY+'/container')
    row.update(kind='pipeline',subject='flow/current',requirements={'environment':'tests/pipelines/flow/uv.lock'})
    rt.plan['tests']['flow/current']=row
    rt.test['payload']['inputs']['packages']=rt.packages()
    accepted(rt,None)
    rt.test['payload']['inputs']['packages']['producer']['job_id']='28'
    with pytest.raises(ValueError,match='pipeline package'):
        accepted(rt,None)
