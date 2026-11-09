"""This version consumes a fixed input; no upstream operator runs here."""
import json
import os
from pathlib import Path
import shutil
import pytest
from ci.acceptance.containers import GUEST,run_container


@pytest.mark.parametrize('valid',[True,False],ids=['success','invalid-input'])
def test_real_container_entry(tmp_path,valid):
    image=os.environ['OPERATOR_TEST_IMAGE']
    job=tmp_path/'job';job.mkdir()
    shutil.copytree(Path(__file__).parent/'fixtures',job/'fixtures')
    value='/validation/job/fixtures/quality_batch.zip'
    (job/'input.json').write_text(json.dumps(value))
    request={'input_files':{'batch_quality_result':[{'fixture_path':GUEST+'/input.json'}]} if valid else {},
        'parameter_overrides':{},'workspace':GUEST+'/run',
        'read_roots':[GUEST+'/fixtures'], 'storage_files':{'clean.mcap':GUEST+'/fixtures/clean.mcap'}}
    path=job/'request.json';path.write_text(json.dumps(request))
    result=run_container(image,path,tmp_path/'result',180)
    report=json.loads(result['stdout'])
    if valid:
        assert result['exit_code']==0,(report,result['stderr'])
        assert report['validation']=='passed' and set(report['outputs'])=={'archive'}
        published=Path(result['workspace'])/'run/published'
        assert (published/'result.json').is_file()
        assert report['outputs']['archive']
    else:
        assert result['exit_code']!=0 and report['validation']=='failed'
        assert not (Path(result['workspace'])/'run/published/result.json').exists()
