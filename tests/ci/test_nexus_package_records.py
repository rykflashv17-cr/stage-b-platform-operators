import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Thread
from types import SimpleNamespace

import pytest

from tests.ci.test_package_records import record

PATH='/repository/Data/operators/package-releases/example-core/1.0.0/release.json'


@pytest.fixture
def nexus(monkeypatch,tmp_path):
    state=SimpleNamespace(rows={},requests=[],get_status=None,put_status=None,lose_response=False,corrupt_write=False)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            state.requests.append(('GET',self.path,self.headers.get('Authorization')))
            data=state.rows.get(self.path)
            self.send_response(state.get_status or (200 if data is not None else 404));self.end_headers()
            if data is not None:self.wfile.write(data)
        def do_PUT(self):
            state.requests.append(('PUT',self.path,self.headers.get('Authorization')))
            body=self.rfile.read(int(self.headers['Content-Length']))
            if state.put_status:
                self.send_response(state.put_status);self.end_headers();return
            state.rows[self.path]=b'{}' if state.corrupt_write else body
            if state.lose_response:
                self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
            self.send_response(201);self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True);worker.start()
    connection=http.client.HTTPConnection
    def connect(host,*,timeout,context):
        assert host=='nexus.striding.internal' and timeout>0 and context.check_hostname
        return connection('127.0.0.1',server.server_port,timeout=timeout)
    monkeypatch.setattr(http.client,'HTTPSConnection',connect)
    for key,value in {'NEXUS_READ_USERNAME':'reader','NEXUS_READ_PASSWORD':'read-secret',
                      'NEXUS_PUBLISH_USERNAME':'publisher','NEXUS_PUBLISH_PASSWORD':'write-secret'}.items():monkeypatch.setenv(key,value)
    netrc=tmp_path/'netrc';netrc.write_text('machine nexus.striding.internal login local password local-secret\n')
    netrc.chmod(0o600);monkeypatch.setenv('NETRC',str(netrc))
    try:yield state
    finally:server.shutdown();server.server_close();worker.join()


def test_requested_path_read_and_publish_credentials_without_harbor(nexus):
    from ci.publish.package_records import PackageRecords,location
    writer=PackageRecords(writable=True);reader=PackageRecords()
    assert reader.read('example-core','1.0.0') is None
    assert writer.create(record())==record()
    assert reader.read('Example_Core','1.0.0')==record()
    assert json.loads(nexus.rows[PATH])==record()
    assert location('example_core','1.0.0')=='https://nexus.striding.internal'+PATH
    assert nexus.requests[0]==('GET',PATH,'Basic '+base64.b64encode(b'reader:read-secret').decode())
    assert ('PUT',PATH,'Basic '+base64.b64encode(b'publisher:write-secret').decode()) in nexus.requests


def test_read_only_client_cannot_publish(nexus):
    from ci.publish.package_records import PackageRecords
    with pytest.raises(ValueError,match='read.only'):PackageRecords().create(record())
    assert nexus.rows=={} and nexus.requests==[]


@pytest.mark.parametrize('status',[401,403,429,500,302])
def test_errors_and_redirects_are_not_missing_records(nexus,status):
    from ci.publish.package_records import PackageRecords
    nexus.get_status=status
    with pytest.raises(ValueError,match=str(status)):PackageRecords().read('example-core','1.0.0')
    assert len(nexus.requests)==1


@pytest.mark.parametrize('payload',[b'not JSON',b'[]',b'x'*65537],ids=['invalid-json','array','oversize'])
def test_invalid_or_oversized_response_fails(nexus,payload):
    from ci.publish.package_records import PackageRecords
    nexus.rows[PATH]=payload
    with pytest.raises(ValueError):PackageRecords().read('example-core','1.0.0')


def test_oversized_record_is_rejected_before_upload(nexus):
    from ci.publish.package_records import PackageRecords
    with pytest.raises(ValueError):PackageRecords(writable=True).create({**record(),'extra':'x'*65537})
    assert nexus.rows=={}


def test_timeout_is_reported_without_credentials(nexus,monkeypatch):
    from ci.publish.package_records import PackageRecords
    def fail(*args,**kwargs):raise TimeoutError('secret remote diagnostic')
    monkeypatch.setattr(http.client.HTTPConnection,'request',fail)
    with pytest.raises(ValueError) as caught:PackageRecords().read('example-core','1.0.0')
    assert 'secret' not in str(caught.value)


def test_lost_write_response_requires_matching_readback(nexus):
    from ci.publish.package_records import PackageRecords
    nexus.lose_response=True
    assert PackageRecords(writable=True).create(record())==record()


def test_write_readback_must_match(nexus):
    from ci.publish.package_records import PackageRecords
    nexus.corrupt_write=True
    with pytest.raises(ValueError):PackageRecords(writable=True).create(record())


def test_write_failure_can_be_retried_without_false_success(nexus):
    from ci.publish.package_records import PackageRecords
    store=PackageRecords(writable=True);nexus.put_status=403
    with pytest.raises(ValueError,match='403'):store.create(record())
    assert store.read('example-core','1.0.0') is None
    nexus.put_status=None
    assert store.create(record())==record()


def test_netrc_fallback_and_partial_credentials(nexus,monkeypatch):
    from ci.publish.package_records import PackageRecords
    for key in ('NEXUS_READ_USERNAME','NEXUS_READ_PASSWORD'):monkeypatch.delenv(key)
    assert PackageRecords().read('example-core','1.0.0') is None
    assert nexus.requests[-1][2]=='Basic '+base64.b64encode(b'local:local-secret').decode()
    monkeypatch.setenv('NEXUS_READ_USERNAME','partial')
    with pytest.raises(ValueError,match='credentials'):PackageRecords().read('example-core','1.0.0')
    assert len(nexus.requests)==1


def test_package_only_mr_needs_nexus_but_not_registry():
    from ci.plan.entry import needs_registry
    assert not needs_registry({'containers':[],'packages':['example-core']},{'foundations':[]},'merge_request_event')
    assert needs_registry({'containers':['operator/v1.0.0'],'packages':[]},{'foundations':[]},'merge_request_event')


@pytest.mark.parametrize('name,version',[('../escape','1.0.0'),('example','../1'),('example',None)])
def test_invalid_identity_never_reaches_server(nexus,name,version):
    from ci.publish.package_records import PackageRecords
    with pytest.raises(ValueError):PackageRecords().read(name,version)
    assert nexus.requests==[]
