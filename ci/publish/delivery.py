"""Publish exact accepted content; never build images or run acceptance here."""
import copy
import json
import re
import tempfile
from pathlib import Path

from ci.publish import manifest as external
from ci.registry.handoff import repository_for,promote
from ci.registry import records


def accepted(rt,evidence):
    images={key:rt.image(key,evidence) for key in rt.plan['operators']};tests={}
    packages=None
    if any(row['source']['kind']=='build' for row in rt.plan['operators'].values()) or any(
            row['action']=='run' and (row['kind'] in {'source','wheel'} or
                row.get('requirements',{}).get('environment','ci/tooling/uv.lock')!='ci/tooling/uv.lock') for row in rt.plan['tests'].values()):
        packages=rt.packages(verify_files=False)
    for key,image in images.items():
        if rt.plan['operators'][key]['source']['kind']=='build' and image['inputs']['packages']!=packages:
            raise ValueError('image package input belongs to an older producer attempt')
        if packages is not None and any(packages['payload']['wheels'].get(name)!=digest for name,digest in image['wheels'].items()):
            # Reused helper images need not appear in this build-packages job.
            if rt.plan['operators'][key]['source']['kind']=='build':raise ValueError('image wheel content mismatch')
    for name,test in rt.plan['tests'].items():
        members=sorted(test['versions'].values())
        required={'format_version':1,'kind':'test','subject':test['subject'],'mode':test['kind'],
            'definition_digest':test['identity'],'images':{key:images[key]['image']['digest'] for key in members}}
        if test['action']=='reuse':
            record=evidence.read_test(required,test['record']['record_digest'])
            if record is None or record!=test['record']:raise ValueError('frozen test evidence missing or changed')
            tests[name]={'record':record};continue
        report=rt.consume(test['job'],test['kind'],test['subject']);payload=report['payload']
        result=payload.get('result',{})
        if (payload.get('definition_digest')!=test['identity'] or type(result.get('passed')) is not int or
                result['passed']<1 or result.get('failed')!=0 or result.get('exit_code')!=0):
            raise ValueError('required test definition or result mismatch')
        if test['kind'] in {'source','wheel'}:
            if payload.get('inputs',{}).get('packages')!=packages:raise ValueError('test package input belongs to an older attempt')
            for key in members:
                wheels=images[key]['wheels']
                if any(packages['payload']['wheels'].get(name)!=digest for name,digest in wheels.items()):
                    raise ValueError('tested wheels differ from the accepted image build')
                if test['kind']=='wheel' and result.get('wheels')!=wheels:
                    raise ValueError('installed wheel test did not consume exact image wheels')
        else:
            if test.get('requirements',{}).get('environment','ci/tooling/uv.lock')!='ci/tooling/uv.lock':
                if payload.get('inputs',{}).get('packages')!=packages:
                    raise ValueError('pipeline package input belongs to an older producer attempt')
            expected={key:{'image':images[key]['image'],'producer':images[key]['producer']} for key in members}
            if payload.get('inputs',{}).get('images')!=expected:raise ValueError('test image input belongs to an older producer attempt')
        tests[name]=report
    if 'check-code' in rt.plan['jobs']:rt.consume('check-code','checks','repository')
    return images,tests


def provenance(rt,producers):
    execution=rt.plan['execution']
    jobs={row['job_id']:row['job'] for row in producers if 'job_id' in row}
    if not jobs:raise ValueError('missing current evidence producers')
    return {'project_id':str(execution['project_id']),'parent_pipeline_id':str(execution['parent_pipeline_id']),
        'pipeline_id':rt.pipeline,'source':execution['source'],'ref':execution['ref'],'commit':rt.plan['commit'],
        'jobs':[{'id':key,'name':jobs[key]} for key in sorted(jobs,key=int)]}


def components(rt,images,*,now=external.utc_now):
    result=copy.deepcopy(rt.plan['components'])
    for key in rt.plan['publication_versions']:
        if key not in images:
            if key not in result:raise ValueError('missing release version in publication')
            continue
        image=images[key];repository,version=repository_for(key)
        old=result.get(key,{})
        if old.get('digest')==image['image']['digest'] and old.get('buildVersion')==version:continue
        if old and rt.plan['execution']['ref']=='main':raise ValueError('published operator version digest conflict')
        result[key]={'buildVersion':version,'sourceCommit':image['source_commit'],'digest':image['image']['digest'],
            'repositories':{'local-harbor':{'role':'canonical','repository':repository,'verifiedAt':now()}}}
    return result


def delivery_versions(rt,harbor,acr):
    previous=(rt.plan.get('delivery_baseline') or {}).get('versions',{})
    result={key:copy.deepcopy(row) for key,row in previous.items() if key in rt.plan['retained_versions']}
    for key,planned in rt.plan['operators'].items():
        if key not in rt.plan['publication_versions']:continue
        image=harbor['payload']['images'][key]['image']['digest'];tag=planned['delivery_tag']
        if planned['mirror_tags']:
            verified=(acr or {}).get('payload',{}).get('versions',{}).get(key)
            if verified!={'tag':tag,'digest':image}:raise ValueError('ACR delivery evidence missing or mismatched')
        else:
            old=previous.get(key,{})
            if old.get('tag')!=tag or old.get('image_digest')!=image or old.get('acr') is not True:
                raise ValueError('ACR delivery evidence missing for retained image')
        result[key]={'input_digest':planned['build_identity']['digest'],'image_digest':image,'tag':tag,'harbor':True,'acr':True}
    if set(result)!=set(rt.plan['retained_versions']):raise ValueError('delivery receipt version coverage mismatch')
    return result


def require_delivery(rt):
    execution=rt.plan['execution']
    if execution['source']=='merge_request_event' or not execution.get('protected'):
        raise ValueError('publication requires protected execution')


def assert_manifest_current(rt,evidence):
    from ci.plan.baseline import registry_baseline
    execution=rt.plan['execution'];repository,prefix,line=external.route_for(execution['ref'])
    registry=evidence.registry.client(repository);parent_id=execution['parent_pipeline_id']
    pattern=re.compile(re.escape(prefix)+r'-p([0-9]{20})-([a-f0-9]{12})\Z')
    for tag in registry.repo_tags(repository):
        match=pattern.fullmatch(tag)
        if match and int(match[1])>int(parent_id):
            digest=registry.resolve(repository+':'+tag)
            document,_=external.fetch_publication(registry,repository,digest)
            if document['pipeline']['projectId']==execution['project_id']:
                raise ValueError('newer publication exists; start a new parent pipeline')
    current=registry_baseline(registry,execution['ref'],rt.plan['commit'],parent_id,execution['project_id'],rt.root)
    if current['parent']!=rt.plan['parent']:raise ValueError('publication baseline changed; start a new parent pipeline')
    if current['components']!=rt.plan['components']:raise ValueError('publication parent components changed')


def proof_references(tests):
    return {name:({'record_digest':row['record']['record_digest']} if 'record' in row else row['producer']) for name,row in tests.items()}


def publish_images(rt):
    require_delivery(rt);evidence=rt.evidence(writable=True)
    evidence.assert_delivery_current(rt.plan['delivery_baseline']);assert_manifest_current(rt,evidence)
    images,tests=accepted(rt,evidence)
    rt.assert_producers_current()
    for key,image in images.items():
        planned=rt.plan['operators'][key];repository,_=repository_for(key)
        for tag in planned['publish_tags']:promote(evidence.registry,repository,image['image']['digest'],tag)
        if planned.get('delivery_tag') is not None and evidence.registry.resolve(repository,planned['delivery_tag'])!=image['image']['digest']:
            raise ValueError('accepted Harbor delivery tag missing or changed')
        if planned['source']['kind']=='build':
            producers=[image['producer'],image['inputs']['packages']['producer']]+[
                row['producer'] for name,row in tests.items() if 'producer' in row and key in rt.plan['tests'][name]['versions'].values()]
            records.save_build(evidence.registry,{'format_version':1,'kind':'build','operator':key,
                'input_digest':planned['build_identity']['digest'],'image':image['image'],'wheels':image['wheels'],
                'harbor_tag':planned['delivery_tag'],'provenance':provenance(rt,producers)})
    for name,test in rt.plan['tests'].items():
        if test['action']!='run':continue
        report=tests[name];members=test['versions'].values()
        body={'format_version':1,'kind':'test','subject':test['subject'],'mode':test['kind'],
            'definition_digest':test['identity'],'images':{key:images[key]['image']['digest'] for key in members},
            'result':report['payload']['result'],'inputs':report['payload']['inputs'],
            'provenance':provenance(rt,[report['producer']])}
        records.save_test(evidence.registry,evidence.test_repository(body),body)
    rt.emit('harbor','delivery',{'images':images,'tests':proof_references(tests)})


def publish_document(rt,registry,images):
    execution=rt.plan['execution'];branch=execution['ref'];commit=rt.plan['commit']
    repository,prefix,line=external.route_for(branch);pipeline_id=str(execution['parent_pipeline_id'])
    tag=f'{prefix}-p{int(pipeline_id):020d}-{commit[:12]}'
    client=registry.client(repository);target=client.bind(repository)
    values=components(rt,images)
    existing=client.resolve(repository+':'+tag)
    if existing is not None:
        prior,_=external.fetch_publication(client,repository,existing)
        previous=external.component_index(prior)
        for key,row in values.items():
            if key in previous:row['repositories']=previous[key]['repositories']
    external.attach_definition_references(registry,rt.root,values,rt.root/'build/release',required=rt.plan['publication_versions'])
    info={'projectId':str(execution['project_id']),'pipelineId':pipeline_id,'pipelineUrl':external.PIPELINE_URL_PREFIX+pipeline_id}
    raw=external.build_document(branch,commit,info,rt.plan['parent'],values)
    output=rt.root/'build/release';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='layout-',dir=output) as scratch:
        layout=Path(scratch)/'layout'
        receipt=external.package_publication(layout,tag,raw)
        manifest=external.verify_layout(layout,receipt,tag,raw)
        target.publish(layout,tag,receipt['artifactDigest'],manifest,raw)
    receipt={'ok':True,'check':'operator-publication','tag':tag,'artifactDigest':receipt['artifactDigest'],
        'layerDigest':external.digest(raw),'sourceCommit':commit,'sourceBranch':branch,'releaseLine':line,
        'producer':'operators','pipeline':info,'componentVersions':sorted(values)}
    (output/'manifest.yaml').write_bytes(raw)
    (output/'receipt.json').write_text(json.dumps(receipt,sort_keys=True)+'\n')
    return receipt


def confirmed_harbor(rt,evidence):
    current=rt.consume('publish-operator-images','harbor','delivery')
    images,tests=accepted(rt,evidence)
    if current['payload']!={'images':images,'tests':proof_references(tests)}:
        raise ValueError('Harbor evidence belongs to an older build or test attempt')
    return current


def mirror_images(rt):
    from ci.publish.mirror import ImageMirror,Oras,mirror
    require_delivery(rt);evidence=rt.evidence()
    evidence.assert_delivery_current(rt.plan['delivery_baseline']);assert_manifest_current(rt,evidence)
    harbor=confirmed_harbor(rt,evidence)
    items=[];versions={}
    for key,planned in rt.plan['operators'].items():
        image=harbor['payload']['images'][key]['image']
        for tag in planned['mirror_tags']:
            items.append(ImageMirror(key.split('/')[0],tag,image['digest']))
            versions[key]={'tag':tag,'digest':image['digest']}
    if not items:raise ValueError('mirror job has no planned work')
    output=rt.destination('acr','delivery').parent/'manifests'
    rt.assert_producers_current()
    results=mirror(items,Oras(rt.env['OPERATOR_MIRROR_CONFIG']),evidence_dir=output)
    rt.emit('acr','delivery',{'harbor_producer':harbor['producer'],'versions':versions,'results':results})


def save_delivery(rt,registry,body):
    from ci.plan.evidence import delivery_pointer
    repository=external.route_for(rt.plan['execution']['ref'])[0]
    pointer=delivery_pointer(rt.plan['execution']['ref'])
    expected=(rt.plan.get('delivery_baseline') or {}).get('record_digest')
    current=registry.resolve(repository,pointer)
    if current!=expected:
        if current is not None and registry.read_record(repository,current)==body:return current
        raise ValueError('delivery baseline changed; start a new parent pipeline')
    tag='delivery-'+rt.plan['execution']['ref'].replace('/','-')+'-p'+str(rt.plan['execution']['parent_pipeline_id'])
    digest=registry.write_record(repository,tag,body)
    if registry.resolve(repository,pointer)!=current:raise ValueError('delivery baseline changed during publication')
    registry.alias_record(repository,tag,pointer)
    if registry.resolve(repository,pointer)!=digest:raise ValueError('delivery pointer verification failed')
    return digest


def publish_manifest(rt):
    require_delivery(rt);evidence=rt.evidence(writable=True)
    assert_manifest_current(rt,evidence)
    harbor=confirmed_harbor(rt,evidence);acr=None
    producers=[harbor['producer']]
    if 'mirror-operator-images' in rt.plan['jobs']:
        acr=rt.consume('mirror-operator-images','acr','delivery')
        if acr['payload'].get('harbor_producer')!=harbor['producer']:
            raise ValueError('ACR evidence belongs to an older Harbor job attempt')
        producers.append(acr['producer'])
    versions=delivery_versions(rt,harbor,acr)
    body={'format_version':1,'kind':'delivery','ref':rt.plan['execution']['ref'],'plan_sha256':rt.digest,
        'versions':versions,'provenance':provenance(rt,producers)}
    # Check the pointer before publishing the external document. An exact
    # completed receipt is allowed only for this final job's idempotent retry.
    from ci.plan.evidence import delivery_pointer
    repository=external.route_for(rt.plan['execution']['ref'])[0]
    current=evidence.registry.resolve(repository,delivery_pointer(rt.plan['execution']['ref']))
    if current!=(rt.plan.get('delivery_baseline') or {}).get('record_digest'):
        if current is None or evidence.registry.read_record(repository,current)!=body:
            raise ValueError('delivery baseline changed before manifest publication')
    rt.assert_producers_current()
    receipt=publish_document(rt,evidence.registry,harbor['payload']['images']) if rt.plan['publication']['write_manifest'] else None
    digest=save_delivery(rt,evidence.registry,body)
    output=rt.root/'build/release';output.mkdir(parents=True,exist_ok=True)
    (output/'delivery.json').write_text(json.dumps({**body,'record_digest':digest},sort_keys=True,indent=2)+'\n')
    from ci.publish.operator_releases import sync_releases
    releases=sync_releases(rt,evidence,harbor['payload']['images'],receipt)
    rt.emit('release','delivery',{'manifest':receipt,'delivery_digest':digest,'versions':versions,'gitlab_releases':releases})
