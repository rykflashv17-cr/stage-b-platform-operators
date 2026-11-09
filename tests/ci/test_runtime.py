import copy
import hashlib
import json
import pytest

from tests.ci.test_child_pipeline import plan
from ci.plan.execution import report_payload


class Client:
    def __init__(self):self.rows={};self.files={}
    def successful_job(self,pipeline,name):
        row=self.rows[name]
        if row['status']!='success':raise ValueError('latest attempt is not successful')
        return row
    def artifact(self,job,path):return self.files[str(job),path]


def runtime(tmp_path):
    from ci.plan.pipeline import assign_jobs
    from ci.runtime import Runtime
    p=assign_jobs(plan());client=Client()
    env={'CI_JOB_ID':'40','CI_PIPELINE_ID':'20','CI_JOB_NAME':'test-operators:example/v1.0.0'}
    return Runtime(tmp_path,p,'b'*64,client,env),client


def packages(rt,client,content=b'wheel',job='30'):
    path=rt.root/'build/wheels/example.whl';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
    payload={'wheels':{'example.whl':hashlib.sha256(content).hexdigest()}}
    client.rows['build-packages']={'id':job,'status':'success','pipeline':{'sha':'a'*40}}
    client.files[job,f'build/packages/{job}/packages.json']=json.dumps(report_payload(rt.plan,rt.digest,
        job_id=job,pipeline_id='20',job='build-packages',kind='packages',subject='selected',payload=payload)).encode()
    return payload


def test_packages_bind_actual_job_and_reject_stale_download_after_retry(tmp_path):
    rt,client=runtime(tmp_path);packages(rt,client)
    first=rt.packages()
    assert first['producer']['job_id']=='30'
    packages(rt,client,b'rebuilt',job='31')
    (tmp_path/'build/wheels/example.whl').write_bytes(b'wheel')
    with pytest.raises(ValueError,match='producer attempt changed'):rt.packages()


def test_report_writer_requires_exact_assignment_and_complete_modes(tmp_path):
    rt,client=runtime(tmp_path)
    with pytest.raises(ValueError,match='assigned'):rt.emit('pipeline','other',{})
    rt.emit('source','example/v1.0.0',{'passed':1})
    with pytest.raises(ValueError,match='missing'):rt.finish()
    rt.emit('wheel','example/v1.0.0',{'passed':1})
    rt.finish()
    raw=json.loads((tmp_path/'build/operators/example/1.0.0/test/40/source/acceptance.json').read_text())
    assert raw['producer']['job_id']=='40' and raw['plan']['plan_job_id']=='11'


def test_consumer_rejects_prior_success_when_latest_producer_failed(tmp_path):
    rt,client=runtime(tmp_path);packages(rt,client)
    client.rows['build-packages']['status']='failed'
    with pytest.raises(ValueError,match='latest'):rt.packages()


def test_reused_build_disappearance_is_not_silently_rebuilt(tmp_path):
    rt,client=runtime(tmp_path)
    key='example/v1.0.0'
    rt.plan['operators'][key]={'source':{'kind':'registry','record':{'record_digest':'sha256:'+'c'*64}},
        'build_identity':{'digest':'d'*64}}
    class Evidence:
        def read_build(self,*args):return None
    with pytest.raises(ValueError,match='missing'):rt.image(key,Evidence())


def test_one_consumer_cannot_mix_reports_from_retried_producer(tmp_path):
    rt,client=runtime(tmp_path);packages(rt,client)
    rt.packages()
    packages(rt,client,job='31')
    with pytest.raises(ValueError,match='producer attempt changed'):
        rt.packages()


def test_consumer_rechecks_used_producers_before_completion(tmp_path):
    rt,client=runtime(tmp_path);packages(rt,client)
    rt.packages();packages(rt,client,job='31')
    with pytest.raises(ValueError,match='producer attempt changed'):
        rt.assert_producers_current()
