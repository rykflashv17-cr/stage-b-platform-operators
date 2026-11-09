"""Plan exact inputs, checks, reuse and delivery as separate actions."""
import copy
import json
from pathlib import Path

from ci.build.bases import parse_dockerfile
from ci.build.identity import build_identity
from ci.plan.projects import operator_projects
from ci.plan.release_scope import load_versions
from ci.plan.test_sets import discover,select,version_test
from ci.publish.tags import publication_tag
from ci.registry.handoff import repository_for


def test_request(row, images):
    return {'format_version':1,'kind':'test','subject':row['subject'],'mode':row['kind'],
            'definition_digest':row['identity'],'images':images}


def compose(root,*,commit,execution,checks,evidence,baseline,delivery=None,base_images=None):
    root=Path(root);mr=execution['source']=='merge_request_event';branch=execution['ref']
    projects={f'{p.name}/v{p.version}':p for p in operator_projects(root)}
    from ci.plan.history import load_history, accepted as archived_accepted
    history=load_history(root,projects);available=set(projects)|set(history)
    declared=load_versions(root,projects)
    publication_versions=set(declared) if branch in {'main','release'} else set(projects)|(set(declared)&set(history))
    checks=copy.deepcopy(checks)
    checks.setdefault('extra',bool(checks.get('tests')))
    checks.setdefault('wheels',[]);checks.setdefault('containers',[])
    if set(checks['wheels'])-projects.keys() or set(checks['containers'])-available:
        raise ValueError('check references an unknown exact version')
    plan={'format_version':2,'commit':commit,'execution':execution,'publication_versions':sorted(publication_versions),
          'parent':baseline.get('parent'),'components':baseline.get('components',{}),'delivery_baseline':delivery,
          'checks':checks,'operators':{},'tests':{},'base_images':base_images or {},
          'publication':{'write_manifest':False,'write_delivery_receipt':False}}
    definitions=discover(root,available)
    # No image lookup or dependency preparation is required for an ordinary MR.
    if mr:
        scope=set(checks['containers'])
        combinations=select(definitions,scope) if scope else []
        for name in combinations:scope.update(definitions[name]['versions'].values())
    else:
        scope=set(publication_versions)
        combinations=select(definitions,scope)
        for name in combinations:scope.update(definitions[name]['versions'].values())
        if branch in {'main','release'}:
            # A release diff may include unselected versions validated on develop.
            # Keep main's package and test checks within its delivery scope.
            for mode in ('wheels','containers'):
                if mode in checks:checks[mode]=sorted(set(checks[mode]) & scope)
    plan['retained_versions']=sorted(publication_versions | set((delivery or {}).get('versions',{})))
    sources={};bases={};archived_tests={}
    for key in sorted(scope):
        if key in history:
            record, prior_tests=archived_accepted(root,key,history[key],evidence)
            archived_tests.update(prior_tests)
            sources[key]={'source':{'kind':'registry','record':record},
                'build_identity':{'digest':record['input_digest']},'archived':history[key],
                'bases':{},'publish_tags':[],'mirror_tags':[],'write_records':False}
            continue
        project=projects[key]
        refs,_=parse_dockerfile((project.path/'Dockerfile').read_text())
        for ref in refs:
            if ref not in bases:bases[ref]=evidence.freeze_base(ref)
        identity=build_identity(root,key,bases)
        accepted=evidence.find_build(key,identity['digest'])
        source={'kind':'registry','record':accepted} if accepted is not None else {'kind':'build'}
        sources[key]={'source':source,'build_identity':identity,'bases':{ref:bases[ref] for ref in refs},
                      'publish_tags':[],'mirror_tags':[],'write_records':False}
    tests=dict(archived_tests)
    def add_test(name,row):
        members=set(row['versions'].values())
        images={key:sources[key]['source']['record']['image']['digest'] for key in members
                if key in sources and sources[key]['source']['kind']=='registry'}
        accepted=evidence.find_test(test_request(row,images)) if len(images)==len(members) and not mr else None
        row['action']='reuse' if accepted else 'run'
        if accepted:row['record']=accepted
        tests[name]=row
    version_modes={key:{'source','wheel'} for key in checks['wheels']}
    for key in scope & set(projects):
        version_modes.setdefault(key,set()).add('container')
        if not mr or sources[key]['source']['kind']=='build':version_modes[key].update(['source','wheel'])
    for key,modes in sorted(version_modes.items()):
        for mode in sorted(modes):
            row=version_test(root,key,mode);row['versions']={key.split('/')[0]:key}
            add_test(key+'/'+mode,row)
    for name in combinations:add_test('pipeline:'+name,copy.deepcopy(definitions[name]))
    if mr:
        plan.update(operators=sources,tests=tests)
        plan['checks']['wheels']=sorted(set(checks['wheels'])|{key for key,row in sources.items() if row['source']['kind']=='build'})
        return plan
    delivered=(delivery or {}).get('versions',{})
    changed=set()
    for key,row in sources.items():
        source=row['source'];record=source.get('record');image=record['image']['digest'] if record else None
        related=[t for t in tests.values() if key in t['versions'].values()]
        row['write_records']=source['kind']=='build' or any(t['action']=='run' for t in related)
        if key not in publication_versions:
            row['delivery_tag']=None
            if row['write_records']:changed.add(key)
            continue
        previous=delivered.get(key)
        complete=(previous is not None and previous.get('input_digest')==row['build_identity']['digest'] and
                  previous.get('image_digest')==image and previous.get('harbor') is True and previous.get('acr') is True)
        repository,version=repository_for(key)
        if complete:
            if evidence.resolve_tag(repository,previous['tag'])!=image:
                raise ValueError(f'previously delivered Harbor tag missing or changed: {key}')
            row['delivery_tag']=previous['tag']
        else:
            tag=publication_tag(version,branch,str(execution['parent_pipeline_id']))
            # A saved accepted build can prove the original branch candidate,
            # even when ACR failed before a delivery receipt existed.
            if record and branch!='main' and record['provenance']['ref']==branch and record.get('harbor_tag'):
                old=record['harbor_tag'];old_digest=evidence.resolve_tag(repository,old)
                if old_digest is not None and old_digest!=image:raise ValueError('accepted candidate tag digest conflict')
                if old_digest is not None:tag=old
            existing=evidence.resolve_tag(repository,tag)
            if existing is not None and existing!=image:raise ValueError('planned Harbor tag digest conflict')
            row['publish_tags']=[] if existing is not None else [tag]
            row['mirror_tags']=[tag];row['delivery_tag']=tag
            changed.add(key)
        if row['write_records']:changed.add(key)
    # Keep all participants of required combinations, including reused helpers.
    needed=set(changed)
    for test in tests.values():
        if test['action']=='run' or (test['kind']=='pipeline' and changed & set(test['versions'].values())):
            needed.update(test['versions'].values())
    components=baseline.get('components',{})
    manifest=baseline.get('schema_version',3)!=4
    for key in publication_versions:
        row=sources[key];old=components.get(key,{})
        digest=row['source'].get('record',{}).get('image',{}).get('digest')
        if branch=='main' and old and digest is not None and old.get('digest')!=digest:
            raise ValueError('published operator version digest conflict')
        if row['source']['kind']=='build' or old.get('buildVersion')!=key.rsplit('/v',1)[-1] or old.get('digest')!=digest:
            manifest=True
    if manifest:needed.update(publication_versions)
    plan['operators']={key:sources[key] for key in sorted(needed)}
    plan['tests']={name:t for name,t in tests.items()
                   if set(t['versions'].values()) & needed and (t['kind']!='pipeline' or set(t['versions'].values())<=needed)}
    plan['checks']['wheels']=sorted(set(checks['wheels'])|{t['subject'] for t in plan['tests'].values()
        if t['action']=='run' and t['kind'] in {'source','wheel'}})
    receipt=bool(changed) or manifest or set(delivered)!=set(plan['retained_versions'])
    plan['publication']={'write_manifest':manifest,'write_delivery_receipt':receipt}
    return plan
