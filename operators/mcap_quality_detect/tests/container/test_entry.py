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
    value='/validation/job/fixtures/statistics_context.zip'
    (job/'input.json').write_text(json.dumps(value))
    request={'input_files':{'statistics_context':[{'fixture_path':GUEST+'/input.json'}]} if valid else {},
        'parameter_overrides':{'source':'jindian_marvin','workers':1},'workspace':GUEST+'/run',
        'read_roots':[GUEST+'/fixtures'], }
    path=job/'request.json';path.write_text(json.dumps(request))
    result=run_container(image,path,tmp_path/'result',180)
    report=json.loads(result['stdout'])
    if valid:
        assert result['exit_code']==0,(report,result['stderr'])
        assert report['validation']=='passed' and set(report['outputs'])=={'batch_quality_result'}
        published=Path(result['workspace'])/'run/published'
        assert (published/'result.json').is_file()
        assert json.loads((published/'quality.json').read_text())['summary']['passed']==1
    else:
        assert result['exit_code']!=0 and report['validation']=='failed'
        assert not (Path(result['workspace'])/'run/published/result.json').exists()
