"""Installed wheel must load the real entrypoint without repository source."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_installed_entry_reports_invalid_input(tmp_path):
    project=Path(os.environ['OPERATOR_TEST_PROJECT'])
    request=tmp_path/'request.json'
    request.write_text(json.dumps({'input_files':{},'workspace':str(tmp_path/'run')}))
    result=subprocess.run([sys.executable,str(project/'main.py'),'--local-request-file',str(request)],
        capture_output=True,text=True,timeout=30)
    assert result.returncode!=0,(result.stdout,result.stderr)
    report=json.loads(result.stdout)
    assert report['validation']=='failed' and report['error']
    assert not (tmp_path/'run/published/result.json').exists()
