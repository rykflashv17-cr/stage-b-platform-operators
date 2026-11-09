import json

from ci.publish.package_records import PackageRecords

import pytest


def record():
    return {'name':'example-core','version':'1.0.0','filename':'example_core-1.0.0-py3-none-any.whl',
            'source_path':'packages/example-core','source_commit':'a'*40,'source_digest':'b'*64,
            'sha256':'c'*64,'url':'https://nexus.striding.internal/repository/pypi-internal/packages/example-core/1.0.0/example_core-1.0.0-py3-none-any.whl',
            'build_producer':{'job':'build-packages','job_id':'30','pipeline_id':'20'},'parent_pipeline_id':'10'}


class Store(PackageRecords):
    def __init__(self):
        super().__init__(writable=True)
        self.rows={}
    def _request(self,method,url,body=None):
        if method=='GET':return json.dumps(self.rows[url]).encode() if url in self.rows else None
        if url in self.rows:raise ValueError('immutable conflict')
        self.rows[url]=json.loads(body)
        return b''


def test_package_record_survives_without_any_git_tag_or_gitlab_client():
    from ci.publish.package_records import PackageRecords
    store=Store()
    assert store.read('example-core','1.0.0') is None
    saved=record()
    assert store.create(saved)==saved
    assert store.read('example-core','1.0.0')==saved
    assert store.read('example-core','2.0.0') is None


def test_retry_preserves_original_producer_and_rejects_changed_bytes_or_source():
    from ci.publish.package_records import PackageRecords
    store=Store();saved=record();store.create(saved)
    retry={**saved,'source_commit':'d'*40,'parent_pipeline_id':'11'}
    assert store.create(retry)==saved
    for field,value in [('sha256','e'*64),('source_digest','f'*64),('source_path','packages/another')]:
        with pytest.raises(ValueError,match='conflict'):store.create({**saved,field:value})
    assert store.read('example-core','1.0.0')==saved


@pytest.mark.parametrize('field,value',[
    ('source_commit','invalid'),('sha256','x'*64),('source_path','../outside'),
    ('filename','other-1.0.0-py3-none-any.whl'),('url','https://other.invalid/wheel'),
    ('build_producer',{}),('parent_pipeline_id','0'),
])
def test_malformed_published_record_is_an_error_not_a_missing_version(field,value):
    from ci.publish.package_records import PackageRecords
    store=Store();store.create(record())
    row=next(iter(store.rows.values()));row[field]=value
    with pytest.raises(ValueError):store.read('example-core','1.0.0')


@pytest.mark.parametrize('name',['example_core','Example_Core','example.core'])
def test_python_distribution_aliases_share_record_and_remain_valid_in_planning(tmp_path,name):
    from ci.publish.package_records import PackageRecords
    from ci.plan.packages import plan_packages
    from tests.ci.test_project_discovery import project
    store=Store()
    project(tmp_path,'packages/example-core',name)
    assert plan_packages(tmp_path,[name],{'source':'merge_request_event','ref':'feature/example'},store)==[name]
    saved={**record(),'name':name,'url':record()['url'].replace('/example-core/','/'+name+'/')}
    assert store.create(saved)==saved
    for alias in ('example-core','example_core','Example.Core'):
        assert store.read(alias,'1.0.0')==saved
    assert len(store.rows)==1


@pytest.mark.parametrize('valid',[True,False])
def test_publish_packages_preserves_nexus_on_gitlab_failure_and_retries_original_record(tmp_path,monkeypatch,valid):
    import hashlib
    import subprocess
    from tests.ci.test_project_discovery import project
    from tests.ci.test_shared_packages import wheel
    from ci.publish.packages import publish_packages
    from ci.publish.package_records import PackageRecords
    p=project(tmp_path,'packages/example-core','example-core')
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    subprocess.run(['git','add','packages'],cwd=tmp_path,check=True)
    subprocess.run(['git','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture'],cwd=tmp_path,check=True)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=tmp_path,text=True).strip()
    directory=tmp_path/'build/wheels';directory.mkdir(parents=True)
    archive=wheel(directory);digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    inventory={'producer':{'job':'build-packages','job_id':'30','pipeline_id':'20'},'payload':{'wheels':{p.wheel:digest}}}
    result={'name':p.distribution,'version':p.version,'wheels':{p.wheel:digest},
            'results':[{'mode':mode,'passed':1,'failed':0,'exit_code':0} for mode in ('source','wheel')]}
    if not valid:result['results'][1]['exit_code']=1
    store=Store();uploads=[];emitted=[];synced=[]
    class Releases:
        def sync(self,record,**kwargs):
            assert store.read('example-core','1.0.0')==record
            synced.append(record)
            if len(synced)==1:raise ValueError('GitLab unavailable')
            return {'tag_name':'packages/example-core/v1.0.0','source_commit':record['source_commit']}
    class Runtime:
        root=tmp_path
        plan={'commit':commit,'execution':{'source':'push','ref':'develop','protected':True,'parent_pipeline_id':'10'}}
        spec={'packages':['example-core']}
        def packages(self):return inventory
        def consume(self,*args):return {'payload':{'inputs':{'packages':inventory},'result':result}}
        def assert_producers_current(self):pass
        def emit(self,kind,subject,payload):emitted.append(payload)
    monkeypatch.setattr('ci.publish.package_records.PackageRecords',lambda **kwargs:store)
    monkeypatch.setattr('ci.publish.packages.publish_wheel',lambda path,pin:uploads.append((path,pin)))
    monkeypatch.setattr('ci.publish.package_releases.PackageReleases.environment',lambda:Releases())
    if not valid:
        with pytest.raises(ValueError,match='incomplete'):publish_packages(Runtime())
        assert uploads==[] and store.rows=={} and emitted==[]
        return
    with pytest.raises(ValueError,match='GitLab unavailable'):publish_packages(Runtime())
    assert emitted==[]
    saved=store.read('example-core','1.0.0')
    assert saved['source_commit']==commit and saved['sha256']==digest
    assert len(uploads)==1 and uploads[0][0]==archive
    Runtime.plan['commit']='d'*40
    publish_packages(Runtime())
    assert synced==[saved,saved]
    assert emitted[0]['packages']==[saved]
    assert emitted[0]['gitlab_releases'][0]['source_commit']==commit
