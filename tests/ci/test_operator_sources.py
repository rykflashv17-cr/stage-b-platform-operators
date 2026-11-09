import subprocess
from pathlib import Path
import pytest
from tests.ci.test_publication import git_commit_all, make_operator, CODE


def test_original_definition_survives_directory_removal_and_new_version(tmp_path):
    from ci.publish.operator_sources import definition
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    p=tmp_path/'operators'/CODE/'v1.0.0';p.mkdir(parents=True)
    raw=f'code: {CODE}\noperatorVersion: 1.0.0\n'.encode();(p/'operator.yaml').write_bytes(raw)
    commit=git_commit_all(tmp_path,'old')
    (p/'operator.yaml').unlink();p.rmdir()
    (p.parent/'operator.yaml').write_text(f'code: {CODE}\noperatorVersion: 2.0.0\n')
    git_commit_all(tmp_path,'current')
    result=definition(tmp_path,CODE+'/v1.0.0',commit)
    assert result['raw']==raw and result['path']=='operators/'+CODE+'/v1.0.0'
    with pytest.raises(ValueError,match='identity|missing'):
        definition(tmp_path,CODE+'/v1.0.0')


def test_missing_original_commit_does_not_fall_back_to_current_file(tmp_path):
    from ci.publish.operator_sources import definition
    p=tmp_path/'operators'/CODE;p.mkdir(parents=True)
    (p/'operator.yaml').write_text(f'code: {CODE}\noperatorVersion: 1.0.0\n')
    with pytest.raises(ValueError,match='missing'):
        definition(tmp_path,CODE+'/v1.0.0','a'*40)
