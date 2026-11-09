"""Exercise generated jobs and the dependency-free documentation shell path."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml
from ci import ROOT
from ci.plan.pipeline import assign_jobs,render,STAGES
from tests.ci.test_child_pipeline import plan


def load_local(path):
    value=yaml.safe_load((ROOT/path).read_text());result={}
    for include in value.get('include',[]):result.update(load_local(include['local'].lstrip('/')))
    result.update(value)
    return result


def test_docs_only_pipeline_stops_before_dependency_or_registry_preparation(tmp_path):
    shutil.copytree(ROOT/'ci',tmp_path/'ci',ignore=shutil.ignore_patterns('__pycache__'))
    (tmp_path/'README.md').write_text('before\n')
    def git(*args):return subprocess.check_output(['git',*args],cwd=tmp_path,text=True).strip()
    git('init','-q');git('config','user.name','fixture');git('config','user.email','test@example.invalid')
    git('add','.');git('commit','-qm','baseline');parent=git('rev-parse','HEAD')
    (tmp_path/'README.md').write_text('after\n');git('commit','-qam','docs');head=git('rev-parse','HEAD')
    tools=tmp_path/'bin';tools.mkdir()
    wrapper=tools/'python3.12'
    wrapper.write_text('#!'+sys.executable+'\n'+"""import os,runpy,sys
sys.path.insert(0,os.getcwd())
from ci.registry.gitlab import GitLab
class Client:
    project='395'
    def pipeline(self,identifier):return {'id':10,'sha':os.environ['CI_COMMIT_SHA'],'ref':'feature/docs','source':'merge_request_event'}
    def current_job(self):return {'id':11,'name':'plan-changes','pipeline':{'id':10,'project_id':395}}
GitLab.environment=lambda env:Client()
sys.argv=sys.argv[2:]
try:runpy.run_module(sys.argv[0],run_name='__main__')
finally:assert not {'yaml','pytest','operator_sdk','packaging','numpy'}.intersection(sys.modules)
""")
    wrapper.chmod(0o755)
    env={**os.environ,'PATH':str(tools)+os.pathsep+os.environ['PATH'],'CI_COMMIT_BEFORE_SHA':parent,'CI_MERGE_REQUEST_DIFF_BASE_SHA':parent,
         'CI_COMMIT_SHA':head,'CI_PIPELINE_SOURCE':'merge_request_event','CI_PIPELINE_ID':'10','CI_JOB_ID':'11'}
    subprocess.run(['sh','ci/scripts/plan.sh'],cwd=tmp_path,env=env,check=True)
    frozen=json.loads((tmp_path/'build/plan.json').read_text());child=yaml.safe_load((tmp_path/'build/child.yml').read_text())
    assert list(frozen['jobs'])==['check-plan'] and child['check-plan']['extends']=='.check-plan'
    assert not (tmp_path/'.ci-tools').exists() and not (tmp_path/'build/toolchain').exists()


@pytest.mark.parametrize('source,ref',[('merge_request_event','feature/demo'),('push','develop'),('push','release'),('push','main')])
def test_every_generated_job_has_a_real_template_and_frozen_parent(source,ref):
    frozen=assign_jobs(plan(source,ref));config=load_local('ci/gitlab/child.yml');child=render(frozen,'b'*64)
    assert config['stages']==STAGES
    for name,spec in frozen['jobs'].items():
        template=child[name]['extends']
        assert template in config and config[template]['extends']=='.operator-job'
        assert child[name]['needs'][0]=={'pipeline':'10','job':'plan-changes','artifacts':True}
        assert all(spec['stage']==frozen['jobs'][dep]['stage'] or STAGES.index(frozen['jobs'][dep]['stage'])<STAGES.index(spec['stage']) for dep in spec['needs'])
    assert config['.operator-job']['script']==['sh ci/scripts/job.sh']
    if source=='merge_request_event':assert not any('environment' in child[name] for name in frozen['jobs'])
    if ref=='main':assert child['publish-operator-images']['when']=='manual' and child['publish-operator-images']['allow_failure'] is False


def test_parent_waits_for_child_and_only_transfers_small_plan_artifacts():
    config=load_local('.gitlab-ci.yml')
    assert config['stages']==STAGES
    assert config['run-jobs']['trigger']['strategy']=='mirror'
    assert config['run-jobs']['needs']==['plan-changes']
    assert config['run-jobs']['trigger']['include']==[{'artifact':'build/child.yml','job':'plan-changes'}]
    assert config['run-jobs']['trigger']['forward']['pipeline_variables'] is False
    assert config['plan-changes']['artifacts']['paths']==['build/plan.json','build/child.yml','build/checks/','build/base-images.json']
    assert not any('changes' in rule for row in config.values() if isinstance(row,dict) for rule in row.get('rules',[]))


def test_ordinary_source_mr_has_no_image_job_to_prepare():
    p=plan('merge_request_event','feature/source');p['operators']={};p['tests'].pop('example/v1.0.0/container')
    frozen=assign_jobs(p)
    assert set(frozen['jobs'])=={'build-packages','test-operators:example/v1.0.0'}


def test_registry_setup_uses_the_shared_config_function():
    for relative in ('ci/scripts/plan.sh', 'ci/scripts/job.sh'):
        source = (ROOT / relative).read_text()
        assert '-m ci.publish.manifest' not in source
        assert 'from ci.publish.manifest import write_registry_config' in source


def test_uv_paths_are_resolved_in_actual_child_checkout(tmp_path):
    tools=tmp_path/'bin';tools.mkdir();uv=tools/'uv';uv.write_text('#!/bin/sh\necho uv 0.12.6\n');uv.chmod(0o755)
    env={**os.environ,'PATH':str(tools)+os.pathsep+os.environ['PATH'],'CI_PROJECT_DIR':str(tmp_path),
         'UV_CACHE_DIR':'/.ci-cache/uv','UV_TOOL_DIR':'/wrong-parent/.ci-cache/tools'}
    for name in ('NEXUS_READ_USERNAME','NEXUS_READ_PASSWORD'):env.pop(name,None)
    script='. ci/scripts/uv.sh; printf "%s\\n%s\\n" "$UV_CACHE_DIR" "$UV_TOOL_DIR"'
    actual=subprocess.check_output(['sh','-c',script],cwd=ROOT,env=env,text=True).splitlines()
    assert actual==[str(tmp_path/'.ci-cache/uv'),str(tmp_path/'.ci-cache/tools')]


def test_package_publish_shell_runs_without_oras_or_harbor_credentials(tmp_path):
    scripts=tmp_path/'ci/scripts';scripts.mkdir(parents=True)
    shutil.copy(ROOT/'ci/scripts/job.sh',scripts/'job.sh')
    (scripts/'uv.sh').write_text(':\n')
    (scripts/'oras.sh').write_text('exit 97\n')
    (tmp_path/'build').mkdir()
    tools=tmp_path/'bin';tools.mkdir()
    metadata=tmp_path/'.ci-tools/metadata-venv/bin';metadata.mkdir(parents=True)
    body='#!'+sys.executable+'\n'+'''import json,sys
from pathlib import Path
if sys.argv[1:]==['-m','ci.run','--validate']:
    print(json.dumps({'source':'push','action':'publish-packages'}))
elif sys.argv[1:]==['-m','ci.build.metadata_environment']:pass
elif sys.argv[1:]==['-m','ci.run']:
    Path('build/ran-package-publisher').write_text('ok')
elif sys.argv[1]=='-c':exec(sys.argv[2])
else:raise SystemExit(98)
'''
    for path in (tools/'python3.12',metadata/'python'):
        path.write_text(body);path.chmod(0o755)
    (tools/'git').write_text('#!/bin/sh\nexit 0\n');(tools/'git').chmod(0o755)
    env={**os.environ,'PATH':str(tools)+os.pathsep+os.environ['PATH'],
         'CI_PROJECT_DIR':str(tmp_path),'CI_COMMIT_REF_PROTECTED':'true'}
    for name in ('DP_HARBOR_USERNAME_B64','DP_HARBOR_PASSWORD','OPERATOR_REGISTRY_CONFIG'):env.pop(name,None)
    subprocess.run(['sh','ci/scripts/job.sh'],cwd=tmp_path,env=env,check=True)
    assert (tmp_path/'build/ran-package-publisher').read_text()=='ok'
    assert not (tmp_path/'build/toolchain').exists()
