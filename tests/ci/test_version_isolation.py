"""Two versions with the same import name must execute in separate processes."""
import json
from pathlib import Path
import subprocess
import sys

from ci.acceptance.versions import run_probe


def test_each_source_version_runs_its_own_tests_and_reports_failure(tmp_path):
    results = []
    for version, value in [("1.0.0", 1), ("2.0.0", 999)]:
        directory = tmp_path / version
        source = directory / "src"
        source.mkdir(parents=True)
        (source / "example.py").write_text(f"VALUE = {value}\n")
        tests = directory / "tests"
        tests.mkdir()
        (tests / "test_example.py").write_text("from example import VALUE\ndef test_value(): assert VALUE == 1\n")
        result = run_probe(python=Path(sys.executable), test_path=tests, sources=[source],
                           modules={"example": source}, report=directory / "report.json")
        results.append((result["passed"], result["exit_code"], result["collected"]))
    assert results == [(1, 0, 1), (0, 1, 1)]


def test_empty_or_all_skipped_version_cannot_pass(tmp_path):
    for body in ("", 'import pytest\n@pytest.mark.skip(reason="not tested")\ndef test_skipped(): pass\n'):
        (tmp_path / "test_example.py").write_text(body)
        result = run_probe(python=Path(sys.executable), test_path=tmp_path, sources=[],
                           modules={}, report=tmp_path / "report.json")
        assert result["exit_code"] != 0


def test_wheel_mode_rejects_source_path_leakage(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "example.py").write_text("VALUE = 1\n")
    (tmp_path / "test_example.py").write_text("def test_ok(): pass\n")
    result = run_probe(python=Path(sys.executable), test_path=tmp_path, sources=[source],
                       modules={"example": tmp_path / "site-packages"}, report=tmp_path / "report.json")
    assert result["exit_code"] != 0
    assert "origin" in result["error"]


def test_dependency_tests_execute_in_the_same_exact_version_environment(tmp_path):
    paths=[]
    for name,body in [('operator','assert True'),('shared','assert False')]:
        path=tmp_path/name;path.mkdir()
        (path/'test_value.py').write_text('def test_value(): '+body+'\n')
        paths.append(path)
    result=run_probe(python=Path(sys.executable),test_path=paths,sources=[],modules={},report=tmp_path/'report.json')
    assert result['collected']==2
    assert result['passed']==1 and result['failed']==1 and result['exit_code']!=0


def test_version_source_probe_rejects_invalid_declaration_even_with_passing_tests(tmp_path):
    from ci import ROOT
    (tmp_path/'operator.yaml').write_text('code: bad\n')
    tests=tmp_path/'tests';tests.mkdir();(tests/'test_ok.py').write_text('def test_ok(): pass\n')
    result=run_probe(python=Path(sys.executable),test_path=tests,sources=[],modules={},report=tmp_path/'report.json',
        project_directory=tmp_path,helpers=[ROOT],check_declaration=True)
    assert result['exit_code']!=0
    assert 'error' in result
