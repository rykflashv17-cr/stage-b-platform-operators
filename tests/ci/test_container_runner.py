import json
import subprocess
from pathlib import Path
import pytest


def test_timeout_cleans_container_and_volume_and_preserves_logs(tmp_path,monkeypatch):
    from ci.acceptance.containers import run_container
    calls=[]
    def command(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='run':
            raise subprocess.TimeoutExpired(argv,1,output=b'partial stdout',stderr=b'partial stderr')
        return subprocess.CompletedProcess(argv,0,stdout='anchor\n',stderr='')
    monkeypatch.setattr(subprocess,'run',command)
    request=tmp_path/'input/request.json';request.parent.mkdir();request.write_text('{}')
    with pytest.raises(TimeoutError):run_container('example@sha256:'+'a'*64,request,tmp_path/'result',1)
    assert (tmp_path/'result/stderr.log').read_text()=='partial stderr'
    assert any(row[1:3]==['rm','-f'] and 'operator-check-' in row[-1] for row in calls)
    assert any(row[1:3]==['volume','rm'] for row in calls)
    assert not any('build' in row for row in calls)


def test_nonzero_exit_remains_a_testable_result(tmp_path,monkeypatch):
    from ci.acceptance.containers import run_container
    def command(argv,**kwargs):
        return subprocess.CompletedProcess(argv,7 if argv[1]=='run' else 0,
            stdout='{"validation":"failed"}' if argv[1]=='run' else 'anchor\n',stderr='diagnostic')
    monkeypatch.setattr(subprocess,'run',command)
    request=tmp_path/'input/request.json';request.parent.mkdir();request.write_text('{}')
    result=run_container('example@sha256:'+'a'*64,request,tmp_path/'result',1)
    assert result['exit_code']==7 and json.loads(result['stdout'])['validation']=='failed'


def test_private_fixture_permissions_allow_real_nonroot_image(tmp_path,monkeypatch):
    from ci.acceptance.containers import run_container
    request=tmp_path/'input/request.json';request.parent.mkdir(mode=0o700)
    request.write_text('{}');request.chmod(0o600)
    nested=request.parent/'fixtures';nested.mkdir(mode=0o700)
    sample=nested/'input.bin';sample.write_bytes(b'fixture');sample.chmod(0o600)
    def command(argv,**kwargs):
        if argv[1]=='cp' and argv[2]==str(request.parent):
            assert request.stat().st_mode & 0o004
            assert nested.stat().st_mode & 0o005 == 0o005
            assert sample.stat().st_mode & 0o004
        if argv[1]=='run':
            assert '--user' not in argv
        return subprocess.CompletedProcess(argv,0,stdout='{}',stderr='')
    monkeypatch.setattr(subprocess,'run',command)
    run_container('example@sha256:'+'a'*64,request,tmp_path/'result',1)
