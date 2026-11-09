"""Parent entry: discover data, freeze registry evidence and generate child YAML."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from ci.plan.checks import select_checks
from ci.plan.pipeline import assign_jobs,render


def generate(root,*,commit,execution,paths,base_scope,evidence,baseline,package_records=None):
    from ci.plan.compose import compose
    checks=select_checks(root,paths,base_scope)
    if package_records is not None:
        from ci.plan.packages import plan_packages
        checks['packages']=plan_packages(root,checks['packages'],execution,package_records)
    checks['template']=any(p.startswith(('templates/','tests/templates/')) and not p.endswith('.md') for p in paths) or any(
        row['category']=='build-tool' for row in checks['reasons'].values())
    checks['extra']=any(row['category'] in {'build-tool','check-tool','release-tool'} or
        path.startswith(('tests/ci/','tests/templates/')) for path,row in checks['reasons'].items()) or checks['template']
    bases={}
    if base_scope['foundations']:
        from ci.base_images.planning import plan_recipes
        bases=plan_recipes(root,base_scope['foundations'],execution,evidence)
    delivery=evidence.delivery() if execution['source']!='merge_request_event' else None
    result=compose(root,commit=commit,execution=execution,checks=checks,evidence=evidence,
        baseline=baseline or {'parent':None,'components':{}},delivery=delivery,base_images=bases)
    return assign_jobs(result)


def write_plan(plan,output,*,plain=False):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    raw=(json.dumps(plan,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    digest=hashlib.sha256(raw).hexdigest()
    (output/'plan.json').write_bytes(raw)
    child=render(plan,digest)
    if plain:serialized=json.dumps(child,indent=2)+'\n'
    else:
        import yaml
        serialized=yaml.safe_dump(child,sort_keys=False)
    (output/'child.yml').write_text(serialized)
    checks=output/'checks';checks.mkdir(exist_ok=True)
    (checks/'planning.json').write_text(json.dumps({'plan_sha256':digest,'commit':plan['commit'],
        'execution':plan['execution'],'required_tests':{name:{'action':row['action'],'job':row.get('job'),
            'record_digest':row.get('record',{}).get('record_digest')} for name,row in plan.get('tests',{}).items()},
        'jobs':list(plan['jobs'])},sort_keys=True,indent=2)+'\n')
    (output/'base-images.json').write_text(json.dumps({'foundations':{key:row['receipt']
        for key,row in plan.get('base_images',{}).items() if row['action']=='verify'}},sort_keys=True,indent=2)+'\n')
    return digest


def parent_identity(client,env):
    current=client.current_job();pipeline=client.pipeline(env['CI_PIPELINE_ID'])
    if (str(current.get('id'))!=env['CI_JOB_ID'] or current.get('name')!='plan-changes' or
            str(current.get('pipeline',{}).get('id'))!=env['CI_PIPELINE_ID'] or
            str(current.get('pipeline',{}).get('project_id'))!=client.project or
            pipeline.get('sha')!=env['CI_COMMIT_SHA'] or pipeline.get('source')!=env['CI_PIPELINE_SOURCE']):
        raise ValueError('parent job identity mismatch')
    protected=env.get('CI_COMMIT_REF_PROTECTED')=='true';source=pipeline['source'];ref=pipeline['ref']
    if source!='merge_request_event' and (not protected or
            ref not in {'develop','release','main'}):
        raise ValueError('only a protected delivery branch may create delivery work')
    return {'project_id':client.project,'parent_pipeline_id':env['CI_PIPELINE_ID'],'plan_job_id':env['CI_JOB_ID'],
            'source':source,'ref':ref,'protected':protected}


def needs_registry(checks,scope,source):
    return bool(checks['containers'] or scope['foundations'] or source!='merge_request_event')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diff-base',required=True)
    parser.add_argument('--scope-only',action='store_true')
    parser.add_argument('--documents-only',action='store_true')
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--registry-config',type=Path)
    parser.add_argument('--oras',default='build/toolchain/oras')
    args=parser.parse_args();root=args.root.resolve();env=os.environ
    from ci.plan.changes import changed_paths,foundation_scope,git,docs_only
    paths=changed_paths(root,args.diff_base)
    if args.documents_only:
        if env['CI_PIPELINE_SOURCE']!='merge_request_event' or not docs_only(root,args.diff_base,paths):raise SystemExit(1)
        from ci.registry.gitlab import GitLab
        execution=parent_identity(GitLab.environment(env),env)
        commit=git(root,'rev-parse','HEAD').decode().strip()
        if commit!=env['CI_COMMIT_SHA']:raise ValueError('parent checkout identity mismatch')
        subprocess.run(['git','diff','--check',args.diff_base,'HEAD'],cwd=root,check=True)
        plan=assign_jobs({'format_version':2,'commit':commit,'execution':execution,'operators':{},'tests':{},
            'checks':{'wheels':[],'extra':False},'base_images':{},
            'publication':{'write_manifest':False,'write_delivery_receipt':False}})
        print(json.dumps({'plan_sha256':write_plan(plan,root/'build',plain=True),'documents_only':True}))
        return
    scope=foundation_scope(root,args.diff_base,check_tooling=env['CI_PIPELINE_SOURCE']=='merge_request_event')
    checks=select_checks(root,paths,scope)
    registry_needed=needs_registry(checks,scope,env['CI_PIPELINE_SOURCE'])
    if args.scope_only:
        (root/'build').mkdir(exist_ok=True)
        (root/'build/scope.json').write_text(json.dumps({'registry_needed':registry_needed}))
        return
    from ci.registry.gitlab import GitLab
    client=GitLab.environment(env);execution=parent_identity(client,env)
    commit=git(root,'rev-parse','HEAD').decode().strip()
    if commit!=env['CI_COMMIT_SHA']:raise ValueError('parent checkout identity mismatch')
    subprocess.run(['git','diff','--check',args.diff_base,'HEAD'],cwd=root,check=True)
    evidence=None;baseline=None
    if registry_needed:
        if args.registry_config is None:raise ValueError('registry reader configuration required')
        from ci.registry.harbor import Registry
        from ci.plan.evidence import Evidence
        from ci.plan.baseline import registry_baseline
        from ci.publish.manifest import Oras
        branch=env['CI_MERGE_REQUEST_TARGET_BRANCH_NAME'] if execution['source']=='merge_request_event' else execution['ref']
        registry=Registry(args.oras,args.registry_config,root/'build/registry')
        evidence=Evidence(registry,client,root,branch,commit,execution['parent_pipeline_id'])
        if execution['source']!='merge_request_event':
            baseline=registry_baseline(Oras(args.oras,str(args.registry_config),root/'build/registry'),
                branch,commit,execution['parent_pipeline_id'],client.project,root)
    from ci.publish.package_records import PackageRecords
    result=generate(root,commit=commit,execution=execution,paths=paths,base_scope=scope,evidence=evidence,baseline=baseline,
        package_records=PackageRecords())
    digest=write_plan(result,root/'build')
    print(json.dumps({'plan_sha256':digest,'jobs':list(result['jobs']),
        'builds':[key for key,row in result['operators'].items() if row['source']['kind']=='build']}))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        import sys
        print(json.dumps({'error':type(exc).__name__,'message':str(exc)}),file=sys.stderr)
        raise SystemExit(2)
