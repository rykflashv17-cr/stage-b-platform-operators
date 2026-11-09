from pathlib import Path
import sys


def test_pytest_runner_rejects_all_skipped_and_reports_failure(tmp_path):
    from ci.acceptance.pytest_runner import run_tests
    tests=tmp_path/'tests';tests.mkdir()
    (tests/'test_example.py').write_text('import pytest\n@pytest.mark.skip(reason="not executed")\ndef test_skip(): pass\n')
    row=run_tests(Path(sys.executable),[tests],tmp_path,tmp_path/'report.json',env={})
    assert row['passed']==0 and row['exit_code']!=0 and row['skipped']==1
    (tests/'test_example.py').write_text('def test_fail(): assert False\n')
    row=run_tests(Path(sys.executable),[tests],tmp_path,tmp_path/'failure.json',env={})
    assert row['failed']==1 and row['exit_code']!=0


def test_pytest_runner_honors_explicit_selection_and_environment(tmp_path):
    from ci.acceptance.pytest_runner import run_tests
    tests=tmp_path/'tests';tests.mkdir()
    (tests/'test_selected.py').write_text('import os\ndef test_ok(): assert os.environ["SELECTED"]=="exact"\n')
    (tests/'test_other.py').write_text('def test_not_selected(): assert False\n')
    row=run_tests(Path(sys.executable),[tests/'test_selected.py'],tmp_path,tmp_path/'report.json',env={'SELECTED':'exact'})
    assert row['passed']==row['collected']==1 and row['exit_code']==0
