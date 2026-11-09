import hashlib
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest


@pytest.fixture
def api_server():
    routes, seen = {}, []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get('JOB-TOKEN')))
            status, body, headers = routes.get(self.path, (404, {}, {}))
            raw = json.dumps(body).encode()
            self.send_response(status)
            for name, value in headers.items(): self.send_header(name, value)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    yield f'http://127.0.0.1:{server.server_port}', routes, seen
    server.shutdown(); thread.join(); server.server_close()


def client(server):
    from ci.registry.gitlab import GitLab
    return GitLab(server, 'job-secret', '395')


def row(id, name='test-operators:a/v1.0.0', status='success', pipeline=20):
    return {'id': id, 'name': name, 'status': status, 'pipeline': {'id': pipeline, 'project_id': 395, 'sha': 'a'*40}}


def test_latest_failed_attempt_cannot_fall_back_to_older_success(api_server):
    base, routes, seen = api_server
    routes['/projects/395/pipelines/20/jobs?include_retried=true&per_page=100&page=1'] = (200, [row(32, status='failed')], {'X-Next-Page':'2', 'X-Total':'2'})
    routes['/projects/395/pipelines/20/jobs?include_retried=true&per_page=100&page=2'] = (200, [row(31)], {'X-Next-Page':'', 'X-Total':'2'})
    with pytest.raises(ValueError, match='latest.*failed'):
        client(base).successful_job('20', 'test-operators:a/v1.0.0')
    assert len(seen) == 2 and all(token == 'job-secret' for _, token in seen)


@pytest.mark.parametrize('status', [401,403,404,500])
def test_metadata_errors_are_not_treated_as_empty_results(api_server, status):
    base, routes, _ = api_server
    routes['/projects/395/pipelines/20'] = (status, {'message':'denied'}, {})
    with pytest.raises(ValueError, match=str(status)):
        client(base).pipeline('20')


def test_truncated_pagination_is_rejected(api_server):
    base, routes, _ = api_server
    routes['/projects/395/pipelines/20/jobs?include_retried=true&per_page=100&page=1'] = (200, [row(31)], {'X-Total':'2','X-Next-Page':''})
    with pytest.raises(ValueError, match='pagination'):
        client(base).jobs('20')


def test_recreated_child_is_rejected_but_job_retry_in_original_child_is_allowed(api_server):
    base, routes, _ = api_server
    routes['/projects/395/pipelines/10/trigger_jobs?include_retried=true&per_page=100&page=1'] = (200, [
        {**row(12, 'run-jobs', pipeline=10), 'downstream_pipeline': {'id':21,'project_id':395,'sha':'a'*40}},
        {**row(11, 'run-jobs', pipeline=10), 'downstream_pipeline': {'id':20,'project_id':395,'sha':'a'*40}},
    ], {'X-Next-Page':'','X-Total':'2'})
    assert client(base).first_child('10','20','a'*40) == '20'
    with pytest.raises(ValueError, match='new parent pipeline'):
        client(base).first_child('10','21','a'*40)


def test_retried_parent_plan_cannot_replace_frozen_bytes(tmp_path):
    from ci.plan.execution import read_frozen_plan
    body = {'format_version':2, 'commit':'a'*40, 'execution':{'project_id':'395','parent_pipeline_id':'10','plan_job_id':'11'}}
    p=tmp_path/'plan.json';p.write_text(json.dumps(body))
    digest=hashlib.sha256(p.read_bytes()).hexdigest()
    assert read_frozen_plan(p, digest, '11')['commit']=='a'*40
    body['execution']['plan_job_id']='12';p.write_text(json.dumps(body))
    with pytest.raises(ValueError, match='frozen plan'):
        read_frozen_plan(p,digest,'11')


def test_report_consumer_rejects_wrong_attempt_plan_or_subject(tmp_path):
    from ci.plan.execution import report_payload, check_report
    plan={'commit':'a'*40,'execution':{'project_id':'395','parent_pipeline_id':'10','plan_job_id':'11'}}
    report=report_payload(plan,'b'*64,job_id='31',pipeline_id='20',job='test-operators:a/v1.0.0',kind='wheel',subject='a/v1.0.0',payload={'passed':3})
    assert check_report(report,plan,'b'*64,job_id='31',pipeline_id='20',job=report['producer']['job'],kind='wheel',subject='a/v1.0.0') == {'passed':3}
    for field, value in [('job_id','32'),('subject','a/v2.0.0'),('plan_sha256','c'*64)]:
        args=dict(job_id='31',pipeline_id='20',job=report['producer']['job'],kind='wheel',subject='a/v1.0.0')
        digest='b'*64
        if field=='plan_sha256':digest=value
        else:args[field]=value
        with pytest.raises(ValueError, match='report identity'):
            check_report(report,plan,digest,**args)


def test_runtime_checks_real_parent_child_and_producer(api_server):
    from ci.plan.execution import validate_runtime
    base, routes, _ = api_server
    plan={'commit':'a'*40,'execution':{'project_id':'395','parent_pipeline_id':'10',
        'plan_job_id':'11','source':'push','ref':'develop','protected':True}}
    env={'CI_PIPELINE_ID':'20','CI_JOB_ID':'31','CI_JOB_NAME':'build-packages',
         'PLAN_SHA256':'b'*64,'CI_COMMIT_REF_PROTECTED':'true'}
    parent={'id':10,'project_id':395,'sha':'a'*40,'ref':'develop','source':'push'}
    routes['/projects/395/pipelines/10']=(200,parent,{})
    routes['/projects/395/pipelines/20']=(200,{**parent,'id':20,'source':'parent_pipeline'}, {})
    routes['/job']=(200,row(31,'build-packages','running'),{})
    routes['/projects/395/jobs/11']=(401,{'message':'Unauthorized'}, {})
    routes['/projects/395/pipelines/10/jobs?include_retried=true&per_page=100&page=1']=(200,[row(11,'plan-changes',pipeline=10)],{'X-Next-Page':'','X-Total':'1'})
    routes['/projects/395/pipelines/10/trigger_jobs?include_retried=true&per_page=100&page=1']=(200,[
        {**row(12,'run-jobs',pipeline=10),'downstream_pipeline':{'id':20,'project_id':395,'sha':'a'*40}}],
        {'X-Next-Page':'','X-Total':'1'})
    assert validate_runtime(plan,'b'*64,client=client(base),env=env)['id']==31
    # A successful plan from another project must not establish identity.
    routes['/projects/395/pipelines/10/jobs?include_retried=true&per_page=100&page=1'][1][0]['pipeline']['project_id']=396
    with pytest.raises(ValueError,match='producer|metadata identity'):
        validate_runtime(plan,'b'*64,client=client(base),env=env)


def test_consumer_downloads_only_latest_successful_attempt(api_server):
    from ci.plan.execution import read_report
    base,routes,_=api_server
    plan={'commit':'a'*40,'execution':{'project_id':'395','parent_pipeline_id':'10','plan_job_id':'11'}}
    routes['/projects/395/pipelines/20/jobs?include_retried=true&per_page=100&page=1']=(200,
        [row(31),row(32)],{'X-Next-Page':'','X-Total':'2'})
    from ci.plan.execution import report_payload
    old=report_payload(plan,'b'*64,job_id='31',pipeline_id='20',job=row(31)['name'],kind='wheel',subject='a/v1.0.0',payload={})
    routes['/projects/395/jobs/32/artifacts/build/reports/32/wheel.json']=(200,old,{})
    with pytest.raises(ValueError,match='report identity'):
        read_report(client(base),plan,'b'*64,pipeline_id='20',job=row(31)['name'],
                    path='build/reports/{job_id}/wheel.json',kind='wheel',subject='a/v1.0.0')


def test_record_provenance_uses_exact_job_from_bounded_pipeline_listing(api_server):
    from ci.registry.records import verify_provenance
    base,routes,seen = api_server
    parent={'id':10,'project_id':395,'sha':'a'*40,'ref':'develop','source':'push'}
    routes['/projects/395/pipelines/10']=(200,parent,{})
    routes['/projects/395/pipelines/20']=(200,{**parent,'id':20,'source':'parent_pipeline'}, {})
    routes['/projects/395/pipelines/10/trigger_jobs?include_retried=true&per_page=100&page=1']=(200,[
        {**row(12,'run-jobs',pipeline=10),'downstream_pipeline':{'id':20,'project_id':395,'sha':'a'*40}}],{'X-Next-Page':'','X-Total':'1'})
    routes['/projects/395/pipelines/20/jobs?include_retried=true&per_page=100&page=1']=(200,
        [row(31,'build-image'),row(32,'build-image','failed')],{'X-Next-Page':'','X-Total':'2'})
    value={'project_id':'395','parent_pipeline_id':'10','pipeline_id':'20','commit':'a'*40,
        'source':'push','ref':'develop','jobs':[{'id':'31','name':'build-image'}]}
    verify_provenance(value,client(base))
    assert not any(path.startswith('/projects/395/jobs/') for path,_ in seen)
    value['jobs'][0]['id']='33'
    with pytest.raises(ValueError):verify_provenance(value,client(base))
