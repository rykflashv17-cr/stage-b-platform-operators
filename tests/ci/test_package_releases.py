"""Exercise the publisher at its HTTP boundary, including partially saved releases."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
from urllib.parse import unquote, urlsplit, parse_qs

import pytest

from tests.ci.test_package_records import record
from tests.ci.test_nexus_package_records import nexus


@pytest.fixture
def registry():
    state = {'tag': None, 'release': None, 'writes': [], 'requests': [], 'private_tokens': [], 'failure': None, 'lose_response': False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def handle_request(self):
            path = unquote(self.path)
            method = self.command
            state['requests'].append((method, path, self.headers.get('JOB-TOKEN')))
            state['private_tokens'].append(self.headers.get('PRIVATE-TOKEN'))
            body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.headers.get('Content-Length') else None
            if method != 'GET': state['writes'].append((method, path, body))
            failure = state['failure']
            if failure and (failure[0] == method or failure[0] == '*'):
                self.send_response(failure[1]); self.end_headers(); self.wfile.write(b'private diagnostic'); return
            if '/repository/tags/' in path:
                value = state['tag']
            elif method == 'POST' and path.endswith('/releases'):
                if state['release'] is not None:
                    self.send_response(409); self.end_headers(); return
                if state['tag'] is None:
                    state['tag'] = {'name': body['tag_name'], 'message': body['tag_message'], 'commit': {'id': body['ref']}}
                state['release'] = {**body, 'commit': state['tag']['commit'], '_links': {'self': 'https://gitlab.striding.internal/release'}}
                for i, link in enumerate(state['release']['assets']['links'], 1): link['id'] = i
                value = state['release']
                if state['lose_response']:
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
            elif method == 'PUT':
                state['release'].update(body); value = state['release']
            elif method == 'POST' and path.endswith('/assets/links'):
                value = {**body, 'id': len(state['release']['assets']['links']) + 1}
                state['release']['assets']['links'].append(value)
            else:
                value = state['release']
            self.send_response(404 if value is None else (201 if method == 'POST' else 200))
            self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        do_GET = do_POST = do_PUT = handle_request

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True); worker.start()
    from ci.publish.package_releases import PackageReleases
    client = PackageReleases(f'http://127.0.0.1:{server.server_port}/api/v4', 'test-job-token', '395')
    try: yield client, state
    finally: server.shutdown(); server.server_close(); worker.join()


def test_create_uses_original_nexus_commit_path_and_wheel_and_retry_is_read_only(registry):
    client, state = registry
    saved = {**record(), 'source_path': 'packages/legacy-core-v1.0.0'}
    result = client.sync(saved, notes='修复文件读取；保持输入输出兼容。')
    assert result['tag_name'] == 'packages/example-core/v1.0.0'
    assert state['tag']['commit']['id'] == 'a' * 40
    assert 'c' * 64 in state['tag']['message']
    release = state['release']
    assert release['name'] == '[Package][Stable] example-core v1.0.0'
    assert 'pip install example-core==1.0.0' in release['description']
    assert '修复文件读取' in release['description']
    assert '/-/jobs/30' in release['description'] and '/-/pipelines/10' in release['description']
    urls = {row['name']: row['url'] for row in release['assets']['links']}
    assert urls['wheel'] == saved['url']
    assert urls['完整发布记录'] == 'https://nexus.striding.internal/repository/Data/operators/package-releases/example-core/1.0.0/release.json'
    parsed = urlsplit(urls['本包源码（tar.gz）'])
    assert parsed.path == '/api/v4/projects/395/repository/archive.tar.gz'
    assert parse_qs(parsed.query) == {'sha': ['a' * 40], 'path': ['packages/legacy-core-v1.0.0']}
    writes = deepcopy(state['writes'])
    client.sync(saved, notes='重试时的说明不能覆盖原说明')
    assert state['writes'] == writes
    assert all(token == 'test-job-token' for _, _, token in state['requests'])


def test_wrong_existing_tag_is_rejected_before_any_release_write(registry):
    client, state = registry
    state['tag'] = {'name': 'packages/example-core/v1.0.0', 'commit': {'id': 'd' * 40}}
    with pytest.raises(ValueError, match='tag.*commit'): client.sync(record())
    assert state['writes'] == []


def test_partial_release_gets_only_missing_metadata_and_assets_and_preserves_manual_content(registry):
    client, state = registry
    client.sync(record())
    state['release']['description'] = '团队补充的版本说明。'
    state['release']['assets']['links'] = [{'id': 99, 'name': '说明', 'url': 'https://example.com/notes', 'link_type': 'other'}]
    state['writes'].clear()
    client.sync(record())
    assert state['release']['description'].startswith('团队补充的版本说明。')
    assert len(state['release']['assets']['links']) == 4
    assert [method for method, _, _ in state['writes']] == ['PUT', 'POST', 'POST', 'POST']
    state['writes'].clear(); client.sync(record())
    assert state['writes'] == []


@pytest.mark.parametrize('kind,name,version,expected', [
    ('package', 'example-core', '1.0.0', '[Package][Stable] example-core v1.0.0'),
    ('package', 'example-core', '1.0.0.dev2', '[Package][Dev] example-core v1.0.0.dev2'),
    ('package', 'example-core', '1.0.0rc1', '[Package][RC] example-core v1.0.0rc1'),
    ('package', 'example-core', '1.0.0rc1.dev2', '[Package][Dev] example-core v1.0.0rc1.dev2'),
    ('package', 'example-core', '1.0.0a1', '[Package][Alpha] example-core v1.0.0a1'),
    ('package', 'example-core', '1.0.0b1', '[Package][Beta] example-core v1.0.0b1'),
    ('operator', 'mcap_read_normalize', '2.0.0', '[Operator][Stable] mcap_read_normalize v2.0.0'),
    ('operator', 'mcap_read_normalize', '2.0.0-dev.123', '[Operator][Dev] mcap_read_normalize v2.0.0-dev.123'),
    ('operator', 'mcap_read_normalize', '2.0.0-rc.123', '[Operator][RC] mcap_read_normalize v2.0.0-rc.123'),
])
def test_release_title_identifies_resource_and_actual_version_phase(kind, name, version, expected):
    from ci.publish.tags import release_title
    assert release_title(kind, name, version) == expected


def test_known_legacy_title_and_heading_migrate_without_recreating_release(registry):
    client, state = registry; client.sync(record(), notes='人工版本说明。')
    state['release']['name'] = 'example-core 1.0.0'
    state['release']['description'] = state['release']['description'].replace('## 安装与来源', '### 安装与来源')
    state['release']['evidences'] = [{'sha': 'original-snapshot'}]
    original_tag = deepcopy(state['tag']); original_assets = deepcopy(state['release']['assets'])
    state['writes'].clear(); client.sync(record())
    assert [method for method, _, _ in state['writes']] == ['PUT']
    assert state['release']['name'] == '[Package][Stable] example-core v1.0.0'
    assert state['release']['description'].startswith('人工版本说明。')
    assert '\n## 安装与来源\n' in state['release']['description']
    assert state['release']['evidences'] == [{'sha': 'original-snapshot'}]
    assert state['tag'] == original_tag and state['release']['assets'] == original_assets
    state['writes'].clear(); client.sync(record())
    assert state['writes'] == []


def test_title_migration_still_rejects_conflicting_provenance_before_write(registry):
    client, state = registry; client.sync(record()); state['writes'].clear()
    state['release']['name'] = 'example-core 1.0.0'
    state['release']['description'] = state['release']['description'].replace('c' * 64, 'd' * 64)
    with pytest.raises(ValueError, match='conflict'): client.sync(record())
    assert state['writes'] == []


@pytest.mark.parametrize('damage', ['digest', 'asset', 'commit'])
def test_conflicting_release_is_not_silently_overwritten(registry, damage):
    client, state = registry; client.sync(record()); state['writes'].clear()
    if damage == 'digest': state['release']['description'] = state['release']['description'].replace('c' * 64, 'e' * 64)
    if damage == 'asset': state['release']['assets']['links'][0]['url'] = 'https://invalid.example/wheel'
    if damage == 'commit': state['release']['commit'] = {'id': 'd' * 40}
    with pytest.raises(ValueError, match='conflict|commit'): client.sync(record())
    assert state['writes'] == []


@pytest.mark.parametrize('code', [401, 403, 429, 500, 302])
def test_http_failure_is_not_reported_as_missing_or_success_and_does_not_leak_body(registry, code):
    client, state = registry; state['failure'] = ('GET', code)
    with pytest.raises(ValueError) as caught: client.sync(record())
    assert str(code) in str(caught.value)
    assert 'private diagnostic' not in str(caught.value) and 'test-job-token' not in str(caught.value)
    assert state['writes'] == []


def test_write_failure_keeps_nexus_identity_and_retry_completes_existing_release(registry):
    client, state = registry; client.sync(record())
    state['release']['assets']['links'].pop()
    state['failure'] = ('POST', 503)
    with pytest.raises(ValueError): client.sync(record())
    assert state['tag']['commit']['id'] == 'a' * 40
    state['failure'] = None; client.sync(record())
    assert len(state['release']['assets']['links']) == 3


def test_lost_create_response_is_recovered_by_read_only_retry(registry):
    client, state = registry; state['lose_response'] = True
    with pytest.raises(ValueError): client.sync(record())
    assert state['release']['tag_name'] == 'packages/example-core/v1.0.0'
    state['writes'].clear(); state['lose_response'] = False
    client.sync(record())
    assert state['writes'] == []


def test_backfill_reads_authoritative_nexus_record_and_verifies_wheel_before_gitlab(monkeypatch):
    from ci.publish.package_releases import backfill
    from tests.ci.test_package_records import Store
    store = Store(); store.create(record()); events = []
    class Client:
        def sync(self, row, **kwargs): events.append(row); return {'tag_name': 'packages/example-core/v1.0.0'}
    monkeypatch.setattr('ci.publish.packages.remote_digest', lambda pin, **kwargs: 'c' * 64)
    backfill(Client(), store, 'example-core', '1.0.0')
    assert events == [record()]
    monkeypatch.setattr('ci.publish.packages.remote_digest', lambda pin, **kwargs: 'd' * 64)
    with pytest.raises(ValueError, match='wheel'): backfill(Client(), store, 'example-core', '1.0.0')
    assert len(events) == 1


def test_backfill_uses_nexus_read_credentials_for_both_record_and_wheel(nexus, monkeypatch):
    import base64
    import hashlib
    from ci.publish.package_releases import backfill
    from ci.publish.package_records import PackageRecords
    saved = {**record(), 'sha256': hashlib.sha256(b'original wheel').hexdigest()}
    nexus.rows['/repository/Data/operators/package-releases/example-core/1.0.0/release.json'] = json.dumps(saved).encode()
    nexus.rows[urlsplit(saved['url']).path] = b'original wheel'
    monkeypatch.delenv('NEXUS_PUBLISH_USERNAME'); monkeypatch.delenv('NEXUS_PUBLISH_PASSWORD')
    class Client:
        def sync(self, row, **kwargs): return row
    assert backfill(Client(), PackageRecords(), 'example-core', '1.0.0') == saved
    assert len(nexus.requests) == 2
    assert all(auth == 'Basic ' + base64.b64encode(b'reader:read-secret').decode() for _, _, auth in nexus.requests)
