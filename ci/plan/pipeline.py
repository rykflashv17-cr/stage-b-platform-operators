"""Assign required work once and render native GitLab dynamic child jobs.

The frozen plan owns actions, test assignment and report paths. YAML contains
only fixed entrypoints, logical dependencies and non-secret plan identities.
"""
import copy
import re

from ci.registry.handoff import repository_for
from ci.serialization import hash_json

STAGES=['plan','build','test','publish-packages','publish-images','mirror-images','publish-manifest']
WRITE_ACTIONS={'build-operator-images','build-base-images','publish-operator-images','publish-base-images',
               'mirror-operator-images','publish-operator-manifest','publish-packages'}


def version_report(key, action, suffix):
    code,version=key.split('/v')
    return f'build/operators/{code}/{version}/{action}/{{job_id}}/{suffix}'


def assign_jobs(plan):
    p=copy.deepcopy(plan);jobs={};tests=p['tests'];operators=p['operators']
    mr=p['execution']['source']=='merge_request_event'
    publication=p['publication'];branch=p['execution']['ref']
    if mr and (any(publication.values()) or any(row['publish_tags'] or row['mirror_tags'] or row['write_records'] for row in operators.values())):
        raise ValueError('MR cannot contain delivery actions')
    for key in operators:repository_for(key)
    def add(name,action,stage,*,needs=(),**params):
        if name in jobs:raise ValueError('duplicate logical job')
        jobs[name]={'action':action,'stage':stage,'needs':sorted(set(needs)),'reports':[],**params}
        return name
    def assign(test_id,name):
        row=tests[test_id]
        if row.get('job'):raise ValueError('duplicate test assignment')
        row['job']=name
        if row['kind']=='pipeline':
            row['report']=f"build/pipelines/{row['subject']}/test/{{job_id}}/acceptance.json"
        else:
            row['report']=version_report(row['subject'],'test',row['kind']+'/acceptance.json')
        jobs[name]['reports'].append({'kind':row['kind'],'subject':row['subject'],'path':row['report'],'test_id':test_id})
    builds={key for key,row in operators.items() if row['source']['kind']=='build'}
    wheels=sorted(set(p['checks']['wheels'])|builds)
    environments=sorted({row['requirements']['environment'] for row in tests.values()
        if row['action']=='run' and row['requirements']['environment']!='ci/tooling/uv.lock'})
    packages=p['checks'].get('packages',[])
    if wheels or p['checks'].get('template') or environments or packages:
        name=add('build-packages','build-packages','build',versions=wheels,template=p['checks'].get('template',False),environments=environments,packages=packages)
        jobs[name]['reports']=[{'kind':'packages','subject':'selected','path':'build/packages/{job_id}/packages.json'}]
    for package in packages:
        name=add('test-packages:'+package,'test-packages','test',needs=['build-packages'],packages=[package])
        jobs[name]['reports']=[{'kind':'package-test','subject':package,'path':f'build/packages/{package}/test/{{job_id}}/acceptance.json'}]
    if packages and not mr:
        name=add('publish-packages','publish-packages','publish-packages',
            needs=['build-packages',*('test-packages:'+name for name in packages)],packages=packages)
        jobs[name]['reports']=[{'kind':'package-release','subject':'selected','path':'build/packages/{job_id}/release.json'}]
    if p['checks'].get('extra'):
        add('check-code','check-code','test',needs=['build-packages'] if p['checks'].get('template') else [])
        jobs['check-code']['reports']=[{'kind':'checks','subject':'repository','path':'build/checks/{job_id}/checks.json'}]
        if 'publish-packages' in jobs:jobs['publish-packages']['needs'].append('check-code')
    for test_id,row in tests.items():
        if row['action']!='run':continue
        if row['kind'] in {'source','wheel'}:
            key=row['subject'];name='test-operators:'+key
            if key not in wheels:raise ValueError('version test has no package preparation')
            if name not in jobs:add(name,'test-operators','test',needs=['build-packages'],versions=[key])
            assign(test_id,name)
        elif row['kind']=='container':
            if row['subject'] not in operators:raise ValueError('version test missing image source')
        elif row['kind']=='pipeline':
            if set(row['versions'].values())-operators.keys():raise ValueError('pipeline test missing image source')
        else:raise ValueError('unsupported test kind')
    flows={name:row for name,row in tests.items() if row['kind']=='pipeline' and row['action']=='run'}
    containers={name:row for name,row in tests.items() if row['kind']=='container' and row['action']=='run'}
    if mr and (flows or containers or builds):
        from ci.plan.test_sets import group_mr
        groups=group_mr(flows,list(flows),builds|{row['subject'] for row in containers.values()})
        for group in groups:
            label=group['versions'][0] if not group['combinations'] else 'group-'+hash_json(group)[:12]
            name=add('check-images:'+label,'check-images','test',
                     needs=['build-packages'] if builds & set(group['versions']) or any(
                         flows[name]['requirements']['environment']!='ci/tooling/uv.lock' for name in group['combinations']) else [],**group)
            for key in builds & set(group['versions']):operators[key]['source']['job']=name
            for test_id,row in containers.items():
                if row['subject'] in group['versions'] and not row.get('job'):assign(test_id,name)
            for test_id in group['combinations']:assign(test_id,name)
    elif not mr:
        for key in sorted(builds):
            name=add('build-operator-images:'+key,'build-operator-images','build',needs=['build-packages'],versions=[key])
            operators[key]['source']['job']=name
            path=version_report(key,'build','build.json');operators[key]['source']['report']=path
            jobs[name]['reports']=[{'kind':'image','subject':key,'path':path}]
        def image_needs(keys):
            return [operators[key]['source']['job'] for key in keys if operators[key]['source']['kind']=='build']
        for test_id,row in flows.items():
            name=add('test-pipelines:'+row['subject'],'test-pipelines','test',
                     needs=image_needs(row['versions'].values())+(['build-packages'] if row['requirements']['environment']!='ci/tooling/uv.lock' else []),
                     versions=sorted(row['versions'].values()),combinations=[test_id])
            assign(test_id,name)
        for test_id,row in containers.items():
            key=row['subject'];matching=[flow for flow in flows.values() if key in flow['versions'].values()]
            if (len(matching)==1 and matching[0].get('merge_with_pipeline',{}).get(key,False) and
                    matching[0]['requirements']==row['requirements']):
                assign(test_id,matching[0]['job'])
            else:
                name=add('test-operator-images:'+key,'test-operator-images','test',needs=image_needs([key]),versions=[key])
                assign(test_id,name)
    for key,row in p.get('base_images',{}).items():
        if row['action']=='verify':continue  # Parent verifies develop's receipt read-only.
        suffix=key+'/'+row['version']
        if mr:
            name=add('check-base-images:'+suffix,'check-base-images','test',recipes=[key])
        else:
            build=add('build-base-images:'+suffix,'build-base-images','build',recipes=[key])
            name=add('test-base-images:'+suffix,'test-base-images','test',needs=[build],recipes=[key])
            jobs[build]['reports']=[{'kind':'base-build','subject':key,'path':f'build/base-images/{suffix}/build/{{job_id}}/build.json'}]
            publish=add('publish-base-images:'+suffix,'publish-base-images','publish-images',needs=[name]+(['check-code'] if 'check-code' in jobs else []),recipes=[key])
            jobs[publish]['reports']=[{'kind':'base-release','subject':key,'path':f'build/base-images/{suffix}/publish/{{job_id}}/receipt.json'}]
        jobs[name]['reports']=[{'kind':'base-test','subject':key,'path':f'build/base-images/{suffix}/test/{{job_id}}/acceptance.json'}]
    writes=any(row['publish_tags'] or row['mirror_tags'] or row['write_records'] for row in operators.values()) or any(publication.values())
    if writes:
        needs={row['job'] for row in tests.values() if row['action']=='run'}
        needs.update(row['source']['job'] for row in operators.values() if row['source']['kind']=='build')
        if 'check-code' in jobs:needs.add('check-code')
        if 'publish-packages' in jobs:needs.add('publish-packages')
        add('publish-operator-images','publish-operator-images','publish-images',needs=needs,manual=branch=='main')
        jobs['publish-operator-images']['reports']=[{'kind':'harbor','subject':'delivery','path':'build/release/{job_id}/harbor.json'}]
        if any(row['mirror_tags'] for row in operators.values()):
            add('mirror-operator-images','mirror-operator-images','mirror-images',needs=['publish-operator-images'])
            jobs['mirror-operator-images']['reports']=[{'kind':'acr','subject':'delivery','path':'build/release/{job_id}/acr.json'}]
        if any(publication.values()):
            needs=['publish-operator-images']+(['mirror-operator-images'] if 'mirror-operator-images' in jobs else [])
            add('publish-operator-manifest','publish-operator-manifest','publish-manifest',needs=needs)
            jobs['publish-operator-manifest']['reports']=[{'kind':'release','subject':'delivery','path':'build/release/{job_id}/release.json'}]
    if not jobs:add('check-plan','check-plan','plan')
    for key in builds:
        if not operators[key]['source'].get('job'):raise ValueError('unassigned image build')
    p['jobs']=jobs
    return p


def render(plan, plan_sha256):
    if not re.fullmatch(r'[a-f0-9]{64}',plan_sha256):raise ValueError('invalid frozen plan digest')
    execution=plan['execution'];mr=execution['source']=='merge_request_event'
    result={'include':[{'local':'/ci/gitlab/child.yml'}],
            'variables':{'PLAN_SHA256':plan_sha256,'PLAN_JOB_ID':str(execution['plan_job_id']),
                         'PARENT_PIPELINE_ID':str(execution['parent_pipeline_id'])},'stages':STAGES}
    for name,spec in plan['jobs'].items():
        if len(name)>128 or not re.fullmatch(r'[a-zA-Z0-9_./:-]+',name):raise ValueError('invalid GitLab job name')
        if set(spec['needs'])-plan['jobs'].keys():raise ValueError('missing dependency in child pipeline')
        if len(spec['needs'])>=50:raise ValueError('GitLab needs limit exceeded; reduce the planned batch')
        paths=sorted({row['path'].replace('{job_id}','${CI_JOB_ID}').rsplit('/',1)[0]+'/' for row in spec['reports']})
        if spec['action']=='build-packages':paths.append('build/wheels/')
        if spec['action']=='publish-base-images':paths.append('build/base-images.json')
        if spec['action']=='publish-operator-manifest':paths.append('build/release/')
        job={'extends':'.'+spec['action'],'stage':spec['stage'],'interruptible':mr,
             'needs':[{'pipeline':str(execution['parent_pipeline_id']),'job':'plan-changes','artifacts':True},
                      *({'job':dep,'artifacts':True} for dep in spec['needs'])]}
        if spec['action']=='build-packages':job['variables']={'UV_CACHE_POLICY':'pull-push'}
        if spec['action']=='test-operators':
            key,=spec['versions']
            job['cache']={'key':{'prefix':'runtime-'+key.replace('/','-')+'-${CI_COMMIT_REF_PROTECTED}',
                'files':['operators/'+key.split('/v')[0]+'/uv.lock','ci/tooling/uv.lock']},
                'paths':['.ci-cache/uv/'],'policy':'pull-push'}
        elif spec['action'] in {'build-operator-images','check-images'}:
            job['cache']={'key':'wheels-linux-amd64-${CI_JOB_NAME_SLUG}-${CI_COMMIT_REF_PROTECTED}',
                'paths':['.ci-cache/wheels/'],'policy':'pull-push'}
        if paths:job['artifacts']={'when':'always','paths':paths,'expire_in':'30 days'}
        if spec.get('manual'):job.update(when='manual',allow_failure=False)
        if spec['action'] in WRITE_ACTIONS:
            if mr:raise ValueError('MR cannot generate a registry writer')
            job['environment']='acr-mirror' if spec['action']=='mirror-operator-images' else 'harbor-publish'
            job['interruptible']=False
            if spec['action'].startswith('publish-') or spec['action']=='mirror-operator-images':
                job['resource_group']='acr-data-operators' if spec['action']=='mirror-operator-images' else 'harbor-data-operators'
            if spec['action']=='publish-packages':
                job.update(environment='nexus-publish',resource_group='nexus-operator-packages')
        result[name]=job
    return result
