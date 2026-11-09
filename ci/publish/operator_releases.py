"""Operator Dev/RC/Stable Releases backed by verified image delivery."""
from pathlib import Path
import re
import subprocess
import urllib.parse
from ci.publish.gitlab_releases import GitLabReleases, WEB
from ci.publish.tags import release_title, PUBLICATION_VERSION
from ci.registry.handoff import DIGEST, repository_for
from ci.registry.gitlab import identifier
from ci.publish.mirror import ACR

API='https://gitlab.striding.internal/api/v4'
BEGIN='<!-- operator-release:start -->'
END='<!-- operator-release:end -->'


def release_branch(version, image_tag):
    """Keep the declared business version separate from its delivery identity."""
    if not PUBLICATION_VERSION.fullmatch(version):
        raise ValueError('invalid operator version')
    if image_tag == version:
        return 'main'
    match = re.fullmatch(re.escape(version) + r'-(dev|rc)\.([1-9][0-9]*)', image_tag)
    if match is None:
        raise ValueError('invalid operator release image tag')
    return {'dev': 'develop', 'rc': 'release'}[match[1]]


def release_notes(root, code, version, commit):
    """Read the chosen delivery revision, never a later checkout's notes."""
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('invalid release notes commit')
    subprocess.run(['git', 'cat-file', '-e', commit + '^{commit}'], cwd=root,
                   check=True, capture_output=True)
    path = f'docs/releases/operators/{code}/{version}.md'
    result = subprocess.run(['git', 'show', f'{commit}:{path}'], cwd=root, capture_output=True)
    if result.returncode:
        return '## 变更说明\n\n本次交付提交未提供独立版本说明。\n\n## 兼容性\n\n请核对本版本声明、源码及验收记录；未据此推定兼容性。'
    if not 0 < len(result.stdout) <= 1024 * 1024:
        raise ValueError('invalid release notes size')
    return result.stdout.decode('utf-8')


def release_content(record, project):
    code,version=record['code'],record['version']
    repository,_=repository_for(code+'/v'+version)
    image_tag=record.get('image_tag',version)
    release_branch(version,image_tag)
    commit,path=record['source_commit'],record['source_path']
    if (not PUBLICATION_VERSION.fullmatch(version) or not re.fullmatch('[a-f0-9]{40}',commit) or
        path not in {'operators/'+code,'operators/'+code+'/v'+version} or
        not DIGEST.fullmatch(record['image_digest']) or not re.fullmatch('[a-f0-9]{64}',record['definition_sha256'])):
        raise ValueError('invalid operator release identity')
    for name in ('build_pipeline_id','parent_pipeline_id'):identifier(record[name])
    title=release_title('operator',code,image_tag);tag=f'operators/{code}/v{image_tag}'
    target=f'{ACR}/data-operators/{code}'
    links=[{'name':'operator.yaml','url':f'{WEB}/-/raw/{commit}/{path}/operator.yaml','link_type':'other'},
        {'name':'本算子源码（tar.gz）','url':f'{API}/projects/{project}/repository/archive.tar.gz?'+
            urllib.parse.urlencode({'sha':commit,'path':path}),'link_type':'other'}]
    block='\n'.join([BEGIN,'## 使用与来源','',
        f'```sh\ndocker pull {repository}:{image_tag}\ndocker pull {target}:{image_tag}\n```','',
        f'- 两端镜像摘要：`{record["image_digest"]}`',
        f'- 原始构建提交：[{commit}]({WEB}/-/commit/{commit})',
        f'- 算子目录：[{path}]({WEB}/-/tree/{commit}/{path})',
        f'- operator.yaml SHA-256：`{record["definition_sha256"]}`',
        f'- [原构建与验收子流水线]({WEB}/-/pipelines/{record["build_pipeline_id"]})',
        f'- [原构建父流水线]({WEB}/-/pipelines/{record["parent_pipeline_id"]})','',
        '资源及调用约束以本版本 operator.yaml 为准；源码归档仅含本算子，依赖按锁文件从 Nexus 安装。',
        'GitLab 登记时间不代表镜像原始构建时间。正式交付以发布清单和镜像摘要为准。',END])
    return tag,title,block,links


class OperatorReleases(GitLabReleases):
    BEGIN,END=BEGIN,END
    content=staticmethod(release_content)
    legacy_title=staticmethod(lambda record:None)

    @staticmethod
    def tag_message(record,title):
        return f'{title}\nSource: {record["source_commit"]}\nPath: {record["source_path"]}\nImage: {record["image_digest"]}\nDefinition: {record["definition_sha256"]}'


def _sync_version(client, root, key, record, *, image_tag, branch, notes_commit, layer_digest=None):
    from ci.publish.operator_sources import definition
    source=definition(root,key,record['provenance']['commit'])
    code,version=key.rsplit('/v',1)
    if release_branch(version,image_tag)!=branch:raise ValueError('release image tag does not match delivery branch')
    saved={'code':code,'version':version,'image_tag':image_tag,'source_commit':source['commit'],'source_path':source['path'],
        'definition_sha256':source['sha256'],'image_digest':record['image']['digest'],
        'build_pipeline_id':record['provenance']['pipeline_id'],
        'parent_pipeline_id':record['provenance']['parent_pipeline_id']}
    notes=release_notes(root,code,version,notes_commit)
    assets=[]
    if layer_digest is not None:
        from ci.publish.manifest import route_for
        if not DIGEST.fullmatch(layer_digest):raise ValueError('invalid manifest asset digest')
        host,path=route_for(branch)[0].split('/',1)
        assets=[{'name':'版本发布清单','url':f'http://{host}/v2/{path}/blobs/{layer_digest}','link_type':'other'}]
    return client.sync(saved,notes=notes,first_assets=assets)


def sync_releases(rt,evidence,images,receipt=None):
    branch=rt.plan['execution']['ref']
    if (branch not in {'develop','release','main'} or rt.plan['execution']['source']=='merge_request_event' or
            not rt.plan['execution'].get('protected')):
        raise ValueError('operator Releases require protected develop/release/main delivery')
    client=OperatorReleases.environment(rt.env);results=[]
    for key in rt.plan['publication_versions']:
        if key not in images:continue
        image=images[key];frozen=image.get('producer',{}).get('record_digest')
        record=(evidence.read_build(key,image['input_digest'],frozen) if frozen else
                evidence.find_build(key,image['input_digest']))
        if (record is None or record['image']['digest']!=image['image']['digest'] or
            record['input_digest']!=image['input_digest'] or
            record['provenance']['commit']!=image['source_commit']):raise ValueError('release build evidence mismatch')
        results.append(_sync_version(client,rt.root,key,record,
            image_tag=rt.plan['operators'][key]['delivery_tag'],branch=branch,notes_commit=rt.plan['commit'],
            layer_digest=receipt['layerDigest'] if receipt else None))
    return results


def backfill(client,evidence,key,manifest_digest,resolve_acr,*,record_digest,branch='main'):
    """Only write GitLab presentation; the specified frozen delivery is authoritative."""
    from ci.publish import manifest as pp
    from ci.plan.baseline import validate_baseline
    repository,version=repository_for(key)
    if not DIGEST.fullmatch(manifest_digest) or not DIGEST.fullmatch(record_digest):
        raise ValueError('exact manifest and build record digests required')
    manifest_repository=pp.route_for(branch)[0];registry=evidence.registry
    reader=registry.client(manifest_repository)
    document,raw=pp.fetch_publication(reader,manifest_repository,manifest_digest)
    if document['sourceBranch']!=branch or document['releaseLine']!=branch:
        raise ValueError('manifest does not match requested delivery branch')
    pipeline=document['pipeline']['pipelineId']
    tag=validate_baseline(document,branch,evidence.commit,str(int(pipeline)+1),client.project,evidence.root)
    if reader.resolve(manifest_repository+':'+tag)!=manifest_digest:raise ValueError('delivery manifest tag differs')
    component=pp.component_index(document).get(key)
    if component is None:raise ValueError('version absent from specified delivery manifest')
    delivery=registry.read_record(manifest_repository,'delivery-'+branch+'-p'+pipeline)
    if (not isinstance(delivery,dict) or delivery.get('kind')!='delivery' or delivery.get('format_version')!=1 or
        delivery.get('ref')!=branch):raise ValueError('completed delivery record missing')
    provenance=delivery.get('provenance',{})
    if (provenance.get('project_id')!=client.project or provenance.get('parent_pipeline_id')!=pipeline or
        provenance.get('commit')!=document['sourceCommit'] or provenance.get('ref')!=branch):
        raise ValueError('manifest and delivery provenance mismatch')
    evidence.verify(provenance)
    delivered=delivery.get('versions',{}).get(key,{})
    image_tag=delivered.get('tag','')
    if (release_branch(version,image_tag)!=branch or delivered.get('harbor') is not True or delivered.get('acr') is not True or
        delivered.get('image_digest')!=component['digest'] or not re.fullmatch('[a-f0-9]{64}',str(delivered.get('input_digest')))):
        raise ValueError('version does not have completed delivery')
    record=evidence.read_build(key,delivered['input_digest'],record_digest)
    if (record is None or record['input_digest']!=delivered['input_digest'] or
        record['image']['digest']!=component['digest'] or record['provenance']['commit']!=component['sourceCommit']):
        raise ValueError('release build evidence mismatch')
    code=key.rsplit('/v',1)[0]
    if (registry.resolve(repository,image_tag)!=component['digest'] or
        resolve_acr(f'{ACR}/data-operators/{code}:{image_tag}')!=component['digest']):
        raise ValueError('delivery Harbor/ACR image digest mismatch')
    return _sync_version(client,evidence.root,key,record,image_tag=image_tag,branch=branch,
        notes_commit=document['sourceCommit'],layer_digest=pp.digest(raw))


def main():
    import argparse,json,subprocess,tempfile
    from ci import ROOT
    from ci.plan.evidence import Evidence
    from ci.registry.harbor import Registry
    from ci.publish.manifest import Oras
    parser=argparse.ArgumentParser(description='从已完成的 develop/release/main 交付补登记算子 Release；只写 GitLab，不构建或发布镜像')
    parser.add_argument('--branch',choices=['develop','release','main'],default='main',help='交付分支；默认 main')
    parser.add_argument('--operator',required=True,help='<code>/v<version>')
    parser.add_argument('--manifest-digest',required=True,help='指定分支发布清单 OCI 摘要')
    parser.add_argument('--build-record-digest',required=True,help='原镜像构建记录 OCI 摘要')
    parser.add_argument('--registry-config',required=True,type=Path,help='Harbor/ACR 只读认证配置')
    parser.add_argument('--oras',default='build/toolchain/oras')
    parser.add_argument('--token-file',type=Path,help='本地 GitLab API Token 文件；CI 使用 CI_JOB_TOKEN')
    args=parser.parse_args()
    client=(OperatorReleases(API,args.token_file.read_text().strip(),'395',token_header='PRIVATE-TOKEN')
            if args.token_file else OperatorReleases.environment())
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    with tempfile.TemporaryDirectory(prefix='operator-release-backfill-') as scratch:
        registry=Registry(args.oras,args.registry_config,Path(scratch)/'harbor')
        evidence=Evidence(registry,client,ROOT,args.branch,commit,'1')
        acr=Oras(args.oras,args.registry_config,Path(scratch)/'acr')
        acr._flags=lambda:['--registry-config',str(args.registry_config)]
        result=backfill(client,evidence,args.operator,args.manifest_digest,acr.resolve,record_digest=args.build_record_digest,branch=args.branch)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
