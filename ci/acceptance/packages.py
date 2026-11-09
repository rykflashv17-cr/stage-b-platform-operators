"""Test a shared package from source and an installed wheel in isolated environments."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from ci.build.context import verify_project_wheel
from ci.build.dependency_wheels import locked_wheels, verify_wheel
from ci.build.sdk_wheel import INDEX
from ci.plan.projects import dependencies, shared_projects
from ci.acceptance.versions import run_probe


def verify_package(root, name):
    root = Path(root).resolve()
    project = next(p for p in shared_projects(root) if p.distribution == name)
    owners = [*dependencies(root, project), project]
    pins = locked_wheels(root, project.path)
    for pin in pins:
        verify_wheel(root/'build/wheels'/pin.filename,pin)
    wheels = list(dict.fromkeys([*(root/'build/wheels'/pin.filename for pin in pins),
                                *(verify_project_wheel(root,p) for p in owners)]))
    tests = project.path/'tests/source'
    if not any(tests.rglob('test_*.py')):
        raise ValueError('shared package requires tests/source/test_*.py')
    results = []
    for mode in ('source','wheel'):
        directory = root/'build/package-tests'/name/mode
        target = root/'.ci-envs/packages'/name/mode
        python = target/'bin/python'
        subprocess.run(['uv','sync','--project',str(project.path),'--frozen','--no-install-local',
                        '--no-default-groups','--default-index',INDEX],
                       env={**os.environ,'UV_PROJECT_ENVIRONMENT':str(target)},check=True)
        subprocess.run(['uv','pip','install','--python',str(python),'--default-index',INDEX,'pytest==8.4.2'],check=True)
        local = {p.wheel for p in owners}
        installed = wheels if mode=='wheel' else [p for p in wheels if p.name not in local]
        if installed:
            subprocess.run(['uv','pip','install','--python',str(python),'--no-deps','--reinstall',*map(str,installed)],check=True)
        site = Path(subprocess.check_output([str(python),'-I','-c',
                     "import sysconfig; print(sysconfig.get_path('purelib'))"],text=True).strip())
        with tempfile.TemporaryDirectory(prefix='shared-package-tests-') as scratch:
            harness = tests
            if mode=='wheel':
                harness = Path(scratch)/'tests'
                shutil.copytree(tests,harness,ignore=shutil.ignore_patterns('__pycache__'))
            result = run_probe(python=python,test_path=harness,
                sources=[p.path/'src' for p in owners] if mode=='source' else [],
                modules={m:(p.path/'src' if mode=='source' else site) for p in owners for m in p.modules},
                distributions={**{p.name:p.version for p in pins if mode=='wheel' or p.filename not in local},
                               **({p.distribution:p.version for p in owners} if mode=='wheel' else {})},
                report=directory/'result.json')
        results.append({'mode':mode,**result})
    return {'name':name,'version':project.version,'results':results,
            'wheels':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in wheels}}


def test_packages(rt):
    packages=rt.packages()
    for name in rt.spec['packages']:
        result=verify_package(rt.root,name)
        destination=rt.destination('package-test',name).parent
        destination.mkdir(parents=True,exist_ok=True)
        for mode in ('source','wheel'):
            log=rt.root/'build/package-tests'/name/mode/'result.log'
            if log.is_file():shutil.copy2(log,destination/(mode+'.log'))
        passed=all(row['exit_code']==0 and row['passed']>0 and row['failed']==0 for row in result['results'])
        rt.emit('package-test',name,{'result':result,'inputs':{'packages':packages}},
                status='passed' if passed else 'failed')
        if not passed:
            raise ValueError('shared package tests failed: '+name)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('names',nargs='*');args=parser.parse_args()
    root=Path.cwd()
    results=[verify_package(root,name) for name in args.names or [p.distribution for p in shared_projects(root)]]
    print(json.dumps(results,indent=2))
    if any(row['exit_code'] for result in results for row in result['results']):raise SystemExit(1)
