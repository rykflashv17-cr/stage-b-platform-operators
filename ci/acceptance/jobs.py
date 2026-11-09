"""Version, template and declared workflow checks."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from ci.build.jobs import build_local

def emit_test(rt,test,result,inputs):
    passed=result.get('exit_code')==0 and result.get('passed',0)>0 and result.get('failed')==0
    rt.emit(test['kind'],test['subject'],{'definition_digest':test['identity'],'result':result,'inputs':inputs},
        status='passed' if passed else 'failed')
    if not passed:raise ValueError(f"required test failed: {test['kind']}:{test['subject']}")


def test_operators(rt):
    from ci.acceptance.versions import verify_versions
    packages=rt.packages()
    for name,test in rt.assigned_tests().items():
        key=test['subject'];mode=test['kind']
        results=verify_versions(rt.root,[key],modes=(mode,),raise_on_failure=False)
        if len(results)!=1 or results[0]['definition_digest']!=test['identity']:
            raise ValueError('executed test definition differs from frozen plan')
        destination=rt.destination(mode,key);destination.parent.mkdir(parents=True,exist_ok=True)
        log=rt.root/'build/version-tests'/key/mode/'result.log'
        if log.is_file():shutil.copy2(log,destination.with_suffix('.log'))
        emit_test(rt,test,results[0],{'packages':packages})


def locked_environment(rt,lock):
    if lock=='ci/tooling/uv.lock':return Path(sys.executable)
    from ci.plan.test_sets import inside
    from ci.build.packages import verify_local_locks
    project=inside(rt.root,lock).parent
    # No business dependency is installed by a publisher or by the planner.
    rt.packages();verify_local_locks(project)
    target=rt.root/'.ci-envs/pipelines'/project.relative_to(rt.root/'tests/pipelines')
    subprocess.run(['uv','sync','--frozen','--no-editable','--project',str(project)],
        env={**os.environ,'UV_PROJECT_ENVIRONMENT':str(target)},check=True)
    return target/'bin/python'


def execute_image_tests(rt,images):
    from ci.acceptance.pytest_runner import run_tests
    from ci.plan.test_sets import discover,version_test
    environments={};failures=[]
    for name,test in rt.assigned_tests().items():
        kind=test['kind'];key=test['subject'];lock=test['requirements']['environment']
        if lock not in environments:environments[lock]=locked_environment(rt,lock)
        if kind=='container':
            actual=version_test(rt.root,key,kind)
            paths=[rt.root/test['path']]
            env={'OPERATOR_TEST_IMAGE':images[key]['image']['reference']}
            members=[key]
        elif kind=='pipeline':
            from ci.plan.projects import operator_projects
            from ci.plan.history import load_history
            current={f'{p.name}/v{p.version}' for p in operator_projects(rt.root)}
            actual=discover(rt.root,current|set(load_history(rt.root,current)))[key]
            paths=[rt.root/path for path in test['paths']]
            members=sorted(test['versions'].values())
            mapping=rt.destination(kind,key).with_name('images.json');mapping.parent.mkdir(parents=True,exist_ok=True)
            mapping.write_text(json.dumps({key:images[key]['image'] for key in members}))
            env={'OPERATOR_TEST_IMAGES':str(mapping),'OPERATOR_TEST_PARAMETERS':json.dumps(test['parameters'])}
        else:raise ValueError('non-container test assigned to image job')
        if actual['identity']!=test['identity']:raise ValueError('test definition changed after planning')
        destination=rt.destination(kind,key)
        result=run_tests(environments[lock],paths,rt.root,destination.with_name('pytest.json'),env=env)
        inputs={'images':{key:{'image':images[key]['image'],'producer':images[key]['producer']} for key in members}}
        if lock!='ci/tooling/uv.lock':inputs['packages']=rt.packages()
        try:emit_test(rt,test,result,inputs)
        except ValueError as error:failures.append(str(error))
    if failures:raise ValueError('; '.join(failures))


def test_images(rt):
    from ci.build.image_handoff import pull_image
    evidence=rt.evidence();images={}
    for key in rt.spec['versions']:
        images[key]=rt.image(key,evidence)
        pull_image(images[key]['image'])
    execute_image_tests(rt,images)


def check_images(rt):
    from ci.build.image_handoff import pull_image
    from ci.build.bases import inspect_image
    if rt.plan['execution']['source']!='merge_request_event':raise ValueError('combined image check is MR only')
    keys=[key for key in rt.spec['versions'] if rt.plan['operators'][key]['source']['kind']=='build']
    packages=rt.packages() if keys else None
    built=build_local(rt,keys,packages) if keys else {}
    evidence=rt.evidence();images={}
    for key in rt.spec['versions']:
        if key in built:
            row=built[key];metadata=inspect_image(row['tag'])
            if metadata['Id']!=row['image']:raise ValueError('local MR image changed after build')
            images[key]={'image':{'reference':row['image'],'digest':row['image'],'config_digest':row['image'],'platform':'linux/amd64'},
                'producer':{'job':rt.name,'job_id':rt.job_id,'pipeline_id':rt.pipeline}}
        else:
            images[key]=rt.image(key,evidence);pull_image(images[key]['image'])
    execute_image_tests(rt,images)


def check_code(rt):
    from ci.acceptance.pytest_runner import run_tests
    paths=[rt.root/'tests/ci']
    destination=rt.destination('checks','repository').parent
    result=run_tests(Path(sys.executable),paths,rt.root,destination/'pytest.json',env={})
    parts={'repository':result}
    if rt.plan['checks'].get('template'):
        rt.packages()
        from ci.plan.projects import read_project
        from ci.build.context import verify_project_wheel
        project=read_project(rt.root/'templates/python-operator')
        wheel=verify_project_wheel(rt.root,project)
        target=rt.root/'.ci-envs/templates/python-operator'
        subprocess.run(['uv','sync','--project',str(project.path),'--frozen','--no-install-project','--no-default-groups'],
            env={**os.environ,'UV_PROJECT_ENVIRONMENT':str(target)},check=True)
        python=target/'bin/python'
        from ci.build.sdk_wheel import INDEX
        subprocess.run(['uv','pip','install','--python',str(python),'--default-index',INDEX,'pytest==8.4.2'],check=True)
        subprocess.run(['uv','pip','install','--python',str(python),'--no-deps','--reinstall',str(wheel)],check=True)
        parts['template']=run_tests(python,[rt.root/'tests/templates'],rt.root,destination/'template.json',env={'OPERATOR_TEMPLATE_MODE':'wheel'})
    okay=all(row['exit_code']==0 and row['passed']>0 and row['failed']==0 for row in parts.values())
    result={key:sum(row.get(key,0) for row in parts.values()) for key in ('collected','passed','failed','skipped')}
    result['exit_code']=0 if okay else 1
    rt.emit('checks','repository',{'result':result,'components':parts},status='passed' if okay else 'failed')
    if not okay:raise ValueError('repository checks failed')
