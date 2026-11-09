"""B04 5.0.0 acceptance through the real SDK image-fixed entrypoint."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

PROJECT=Path(os.environ["OPERATOR_TEST_PROJECT"])


def invocation(tmp_path, statuses=('kept','rejected'), *, change_raw=False, diagnostic=False):
    from pretraining_data_cleaning_common.artifact import pack_artifact
    from pretraining_data_cleaning_common.reporting import quality_counts
    raw=tmp_path/'raw'
    raw.mkdir()
    payload=tmp_path/'quality'
    payload.mkdir()
    records=[]
    members=[]
    mapping={}
    for index,status in enumerate(statuses):
        key=f'capture/e{index}/episode.mcap'
        path=raw/f'{index}.mcap'
        path.write_bytes(f'original-{index}'.encode())
        source={'attempt_id':'00000000-0000-4000-8000-000000000001','input_name':'raw_files','input_index':index,'logical_key':key,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        member={'episode_index':index,'source_ref':source}
        members.append(member)
        records.append({**member,'source':key,'status':status,'reject_file':status=='rejected','diagnostic_errors':{'stage':'broken'} if diagnostic else {}})
        mapping[key]=str(path)
    (payload/'selection.json').write_text(json.dumps({'source':'jindian_marvin','members':members}))
    (payload/'decisions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    (payload/'quality_summary.json').write_text(json.dumps({'statistics_status':'ready',**quality_counts(records)}))
    quality=tmp_path/'quality.zip'
    pack_artifact(quality,'quality_batch',payload)
    if change_raw:(raw/'0.mcap').write_bytes(b'changed')
    fixture=tmp_path/'quality-input.json'
    fixture.write_text(json.dumps(str(quality)))
    request=tmp_path/'request.json'
    request.write_text(json.dumps({'input_files':{'batch_quality_result':[{'fixture_path':str(fixture)}]},'workspace':str(tmp_path/'execution'),'read_roots':[str(tmp_path)],'storage_files':mapping}))
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}
    python=Path(sys.executable)
    result=subprocess.run([str(python),str(PROJECT/'main.py'),'--local-request-file',str(request)],env=env,capture_output=True,text=True,timeout=30)
    return result,tmp_path/'execution/work/accepted_originals.zip'


@pytest.mark.parametrize('statuses',[('kept','kept'),('kept','rejected'),('rejected','rejected')])
def test_v5_real_entrypoint_publishes_exact_kept_originals(tmp_path,statuses):
    result,path=invocation(tmp_path,statuses)
    assert result.returncode==0,(result.stdout,result.stderr)
    report=json.loads(result.stdout)
    assert set(report['outputs'])=={'archive'}
    with ZipFile(path) as archive:
        manifest=json.loads(archive.read('delivery_manifest.json'))
        files=manifest['files']
        assert manifest['format']=='delivery_manifest_v1'
        assert len(files)==statuses.count('kept')
        assert set(archive.namelist())=={'delivery_manifest.json',*(f['delivery_path'] for f in files)}
        for item in files:
            index=item['business']['episode_index']
            body=archive.read(item['delivery_path'])
            assert body==f'original-{index}'.encode()
            assert item['size_bytes']==len(body)
            assert item['sha256']==hashlib.sha256(body).hexdigest()==item['business']['source_ref']['sha256']


@pytest.mark.parametrize('kwargs',[{'change_raw':True},{'diagnostic':True}])
def test_v5_failure_never_publishes_success(tmp_path,kwargs):
    result,_=invocation(tmp_path,**kwargs)
    assert result.returncode!=0
    report=json.loads(result.stdout)
    assert report['validation']=='failed'
    assert 'digest' in report['error'] if kwargs.get('change_raw') else 'quality' in report['error'] or 'diagnostic' in report['error']
    assert not (tmp_path/'execution/published/result.json').exists()








from tests.support.storage_service import remote,location


def test_v5_fresh_pipeline_process_downloads_only_kept_and_publishes_zip(remote,tmp_path):
    from uuid import UUID
    endpoint,objects,calls,_=remote
    prepared=tmp_path/'prepared';prepared.mkdir()
    result,_=invocation(prepared)
    assert result.returncode==0
    request=json.loads((prepared/'request.json').read_text())
    objects['quality.zip']=(prepared/'quality.zip').read_bytes()
    for key,path in request['storage_files'].items():objects[key]=Path(path).read_bytes()
    ids={k:str(UUID(int=i+1)) for i,k in enumerate(['tenantId','projectId','taskId','batchId','stageExecutionId','attemptId'])}
    code='mcap_delivery_accepted'
    prefix='/'.join([ids['tenantId'],ids['projectId'],'pipeline',ids['taskId'],ids['batchId'],'batch',ids['stageExecutionId'],code,ids['attemptId']])
    context={**ids,**location('out'),'executionScope':'BATCH','nodeCode':code,'outputPrefix':prefix,
             'storageEndpoint':endpoint,'serviceCode':'data-platform-operator-runtime','serviceCredential':'fixture-only',
             'inputArtifactLocations':{'batch_quality_result':location('quality')}}
    env={**os.environ,'PIPELINE_EXECUTION_CONTEXT':json.dumps(context),'PIPELINE_NODE_PARAMS':json.dumps({'batch_quality_result':'quality.zip'})}
    command=[sys.executable,str(PROJECT/'main.py')]
    if image:=os.environ.get('B04_TEST_IMAGE'):
        command=['docker','run','--rm','--platform','linux/amd64','--read-only','--user','10001:10001',
                 '--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,nosuid,size=256m',
                 '-e','PIPELINE_EXECUTION_CONTEXT','-e','PIPELINE_NODE_PARAMS',image]
    proc=subprocess.run(command,env=env,capture_output=True,text=True,timeout=60)
    assert proc.returncode==0,(proc.stdout,proc.stderr)
    result=json.loads(objects[prefix+'/result.json'])
    assert set(result)=={'archive'}
    from io import BytesIO
    with ZipFile(BytesIO(objects[result['archive']])) as archive:
        manifest=json.loads(archive.read('delivery_manifest.json'))
        assert len(manifest['files'])==1
        assert archive.read(manifest['files'][0]['delivery_path'])==b'original-0'
    downloads=[key for operation,key in calls if operation=='DOWNLOAD']
    assert set(downloads)=={'quality.zip','capture/e0/episode.mcap'}
    assert prefix+'/operator.log' in objects





def test_archive_byte_budget_fails_before_publication(tmp_path):
    import importlib.util
    from types import SimpleNamespace
    from mcap_delivery_accepted import archive as module
    source=tmp_path/'raw';source.write_bytes(b'original')
    ref={'logical_key':'capture/one.mcap','sha256':hashlib.sha256(b'original').hexdigest()}
    context=SimpleNamespace(workspace=SimpleNamespace(path=lambda name:tmp_path/name),
                            storage=SimpleNamespace(limits=SimpleNamespace(max_upload_bytes=4)),check_cancelled=lambda:None)
    with pytest.raises(ValueError,match='budget|limit'):
        module.build_archive(context,[{'episode_index':0,'source_ref':ref}],[source])
    assert not (tmp_path/'accepted_originals.zip').exists()
