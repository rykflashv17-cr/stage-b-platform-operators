"""Run an actual image entrypoint in a fresh named volume on any Docker Runner.

The request and its fixtures live beside request_path. Fixture requests refer
to this directory as /validation/job; the image is never replaced by host code.
Only SDK invocation transport is handled here, not business assertions.
"""
from pathlib import Path
import subprocess
import uuid

GUEST='/validation/job'


def run_container(image, request_path, output_dir, timeout_seconds):
    request_path=Path(request_path).resolve();output_dir=Path(output_dir).resolve()
    if not request_path.is_file() or not isinstance(timeout_seconds,int) or timeout_seconds<1:
        raise ValueError('invalid container test request')
    output_dir.mkdir(parents=True,exist_ok=True)
    # docker cp preserves directory permissions; the real image's user must be
    # able to create an execution workspace even with a remote Docker daemon.
    request_path.parent.chmod(0o777)
    # Runner umask may create 0600 files. docker cp makes them root-owned;
    # fixture inputs must remain readable by the image's configured user.
    for path in request_path.parent.rglob('*'):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)
    token='operator-check-'+uuid.uuid4().hex
    volume,anchor,container=token+'-data',token+'-anchor',token+'-run'
    def run(*args,**kwargs):
        return subprocess.run(['docker',*args],capture_output=True,text=True,timeout=kwargs.pop('timeout',60),**kwargs)
    created=False
    try:
        run('volume','create',volume,check=True);created=True
        run('create','--name',anchor,'--platform','linux/amd64','--network','none','--read-only',
            '--mount',f'type=volume,src={volume},dst=/validation','--entrypoint','python',image,'-c','pass',check=True)
        run('cp',str(request_path.parent),anchor+':'+GUEST,check=True)
        argv=['run','--name',container,'--platform','linux/amd64','--network','none','--read-only',
              '--tmpfs','/tmp:rw,nosuid,size=256m','--cap-drop','ALL','--security-opt','no-new-privileges',
              '--cpus','4','--memory','8g','--pids-limit','256','--mount',f'type=volume,src={volume},dst=/validation',
              image,'--local-request-file',GUEST+'/'+request_path.name]
        try:
            result=run(*argv,timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            for name,data in [('stdout',exc.stdout),('stderr',exc.stderr)]:
                (output_dir/(name+'.log')).write_bytes(data if isinstance(data,bytes) else (data or '').encode())
            raise TimeoutError(f'container invocation exceeded {timeout_seconds}s; logs: {output_dir}') from None
        (output_dir/'stdout.log').write_text(result.stdout);(output_dir/'stderr.log').write_text(result.stderr)
        run('cp',anchor+':'+GUEST,str(output_dir/'workspace'),check=True)
        return {'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr,
                'workspace':str(output_dir/'workspace')}
    finally:
        run('rm','-f',container)
        run('rm','-f',anchor)
        if created:run('volume','rm',volume)
