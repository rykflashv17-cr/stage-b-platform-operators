"""Declared combinations use the original five real numerical container cases."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from ci import ROOT


def test_declared_container_combination(tmp_path):
    filename=os.environ.get('OPERATOR_TEST_IMAGES')
    if not filename:pytest.skip('container image mapping is supplied by the acceptance runner')
    images=json.loads(Path(filename).read_text())
    # Runtime mapping is exact-version keyed, translated only in this business
    # workflow's adapter to the original four node names.
    mapping={key.split('/')[0]:{**row,'operator':key,'image':row['reference']} for key,row in images.items()}
    assert len(mapping)==len(images)==4
    request=tmp_path/'images.json';request.write_text(json.dumps(mapping))
    output=tmp_path/'acceptance.json'
    result=subprocess.run([sys.executable,'-m','tests.pipelines.pretraining_data_cleaning.container_acceptance',
        '--images',str(request),'--output',str(output)],cwd=ROOT,capture_output=True,text=True,timeout=600)
    assert result.returncode==0,(result.stdout,result.stderr)
    report=json.loads(output.read_text())
    assert set(report['cases'])=={'marvin','gaps','abc','empty','input_error'}
    assert {row['operator'] for row in report['images'].values()}==set(images)
