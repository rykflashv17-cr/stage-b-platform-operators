"""B04 5.0.0 acceptance through the real SDK image-fixed entrypoint."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

PROJECT=Path(os.environ.get('OPERATOR_TEST_PROJECT',Path(__file__).resolve().parents[2]))


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
    python=sys.executable
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
