"""Run a declared test set in its locked Python process and count real results."""
import json
import os
from pathlib import Path
import subprocess
import sys


def run_tests(python,paths,root,report,*,env,timeout=1200):
    report=Path(report).resolve();report.parent.mkdir(parents=True,exist_ok=True)
    request=report.with_suffix('.request.json')
    request.write_text(json.dumps({'root':str(Path(root).resolve()),'paths':[str(Path(p).resolve()) for p in paths],'report':str(report)}))
    report.unlink(missing_ok=True)
    environment={key:value for key,value in os.environ.items() if key not in {'PYTHONPATH','PYTHONHOME','CI_OPERATOR_PLAN'}}
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTHONDONTWRITEBYTECODE='1',**env)
    with report.with_suffix('.log').open('w') as log:
        process=subprocess.run([str(python),'-I',str(Path(__file__).resolve()),str(request)],
            cwd=root,env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
    if not report.exists():raise ValueError(f'test set did not write results: {report}')
    result=json.loads(report.read_text())
    if result['exit_code']!=process.returncode:raise ValueError('test result exit code mismatch')
    return result


def main(request):
    config=json.loads(Path(request).read_text());root=Path(config['root']);destination=Path(config['report'])
    result={'collected':0,'passed':0,'failed':0,'skipped':0,'exit_code':1}
    try:
        sys.path.insert(0,str(root))
        import pytest
        class Counts:
            def pytest_collection_finish(self,session):result['collected']=len(session.items)
            def pytest_runtest_logreport(self,report):
                if report.failed:result['failed']+=1
                elif report.skipped:result['skipped']+=1
                elif report.when=='call' and report.passed:result['passed']+=1
        ini=destination.with_suffix('.ini');ini.write_text('[pytest]\naddopts = --import-mode=importlib\n')
        code=int(pytest.main(['-q','-c',str(ini),'--rootdir',str(root),*config['paths']],plugins=[Counts()]))
        result['exit_code']=code if code else (0 if result['passed'] else 5)
    except Exception as exc:result.update(exit_code=1,error=f'{type(exc).__name__}: {exc}')
    destination.write_text(json.dumps(result,indent=2)+'\n')
    return result['exit_code']


if __name__=='__main__':raise SystemExit(main(sys.argv[1]))
