import hashlib
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile

import pytest


def wheel(tmp_path, name='example-core', version='1.0.0'):
    path = tmp_path / f'{name.replace("-", "_")}-{version}-py3-none-any.whl'
    with ZipFile(path, 'w') as archive:
        archive.writestr(f'{name.replace("-", "_")}-{version}.dist-info/METADATA',
                         f'Name: {name}\nVersion: {version}\n')
    return path


def locked_project(root, version='1.0.0'):
    path = root / 'operators/example_read_data'
    path.mkdir(parents=True)
    filename = f'example_core-{version}-py3-none-any.whl'
    source = '../../build/wheels/' + filename
    (path / 'pyproject.toml').write_text(
        '[project]\nname="example-operator"\nversion="1.0.0"\n'
        f'dependencies=["example-core=={version}"]\n'
        '[tool.hatch.build.targets.wheel]\npackages=["src/example"]\n'
        f'[tool.uv.sources]\nexample-core={{path="{source}"}}\n')
    (path / 'uv.lock').write_text(
        f'[[package]]\nname="example-core"\nversion="{version}"\nsource={{path="{source}"}}\n'
        f'wheels=[{{filename="{filename}",hash="sha256:{"a"*64}"}}]\n')
    (path/'operator.yaml').write_text('code: example_read_data\noperatorVersion: 1.0.0\n')
    return path


def test_locked_wheels_retain_historical_identity_and_digest(tmp_path):
    from ci.build.dependency_wheels import locked_wheels
    path = locked_project(tmp_path)
    pin, = locked_wheels(tmp_path, path)
    assert (pin.name, pin.version, pin.digest) == ('example-core', '1.0.0', 'a'*64)


def test_staging_path_cannot_escape_wheel_directory(tmp_path):
    from ci.build.dependency_wheels import locked_wheels
    path = locked_project(tmp_path)
    meta = path / 'pyproject.toml'
    meta.write_text(meta.read_text().replace('build/wheels/', 'outside/'))
    with pytest.raises(ValueError, match='path'):
        locked_wheels(tmp_path, path)


def test_prepare_verified_wheel_rejects_wrong_metadata_and_corruption(tmp_path):
    from ci.build.dependency_wheels import WheelPin, prepare_wheel
    source = wheel(tmp_path)
    pin = WheelPin('other-core','1.0.0',source.name,hashlib.sha256(source.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match='metadata'):
        prepare_wheel(tmp_path/'repo',pin,wheel=source)
    pin = WheelPin('example-core','1.0.0',source.name,'a'*64)
    with pytest.raises(ValueError, match='digest'):
        prepare_wheel(tmp_path/'repo',pin,wheel=source)


def test_published_wheel_retry_and_conflict():
    from ci.publish.packages import publish_once
    state = []; calls = []
    def upload(): calls.append(1); state.append('a')
    publish_once('a', lambda: state[-1] if state else None, upload)
    publish_once('a', lambda: state[-1], upload)
    assert calls == [1]
    with pytest.raises(ValueError, match='different'):
        publish_once('b', lambda: state[-1], upload)
    assert calls == [1]


def test_retry_accepts_upload_response_loss_only_after_digest_verification():
    from ci.publish.packages import publish_once
    state = []
    def upload():
        state.append('a')
        raise subprocess.CalledProcessError(1,['uv','publish'])
    publish_once('a', lambda: state[-1] if state else None, upload)


def test_old_version_can_resolve_locked_wheel_when_current_source_is_newer(tmp_path):
    from ci.plan.projects import dependencies, read_project
    from tests.ci.test_project_discovery import project
    project(tmp_path,'packages/example-core','example-core','2.0.0')
    old = read_project(locked_project(tmp_path))
    assert dependencies(tmp_path,old) == []


def test_only_one_source_version_per_shared_distribution(tmp_path):
    from ci.plan.projects import shared_projects
    from tests.ci.test_project_discovery import project
    project(tmp_path,'packages/example-core','example-core','2.0.0')
    project(tmp_path,'packages/example-core-old','example-core','1.0.0')
    with pytest.raises(ValueError,match='one current'):
        shared_projects(tmp_path)


def test_old_operator_keeps_all_locked_artifacts_without_local_shared_sources():
    from ci import ROOT
    from ci.plan.projects import operator_projects, wheel_paths, dependencies
    for p in operator_projects(ROOT):
        filenames = {path.name for path in wheel_paths(ROOT,p)}
        common = '0.4.3' if p.version == '5.0.0' else '0.4.2'
        assert f'striding_pretraining_data_cleaning_common-{common}-py3-none-any.whl' in filenames
        assert 'striding_pretraining_data_cleaning_engine-0.1.0-py3-none-any.whl' in filenames
        assert dependencies(ROOT,p) == []


def test_shared_change_is_tested_without_rebuilding_archived_consumers():
    from ci import ROOT
    from ci.plan.checks import select_checks
    selected=select_checks(ROOT,['packages/pretrain-cleaning-common/src/pretrain_cleaning_common/archive.py'],{})
    assert selected['wheels'] == selected['containers'] == []
    assert selected['packages'] == ['pretrain-cleaning-common']
    assert select_checks(ROOT,['packages/pretrain-cleaning-common/README.md'],{})['packages'] == []


@pytest.mark.parametrize('source,ref,publishes',[('merge_request_event','feature/packages',False),('push','develop',True)])
def test_shared_package_jobs_bind_build_test_and_publication(source,ref,publishes):
    from ci.plan.pipeline import assign_jobs,render
    from tests.ci.test_child_pipeline import plan
    p=plan(source,ref)
    p.update(operators={},tests={},checks={'wheels':[],'extra':False,'packages':['example-core']},
             publication={'write_manifest':False,'write_delivery_receipt':False})
    result=assign_jobs(p);child=render(result,'a'*64)
    assert result['jobs']['build-packages']['packages']==['example-core']
    assert 'build-packages' in result['jobs']['test-packages:example-core']['needs']
    assert ('publish-packages' in result['jobs']) is publishes
    if publishes:
        assert 'test-packages:example-core' in result['jobs']['publish-packages']['needs']
        assert child['publish-packages']['environment']=='nexus-publish'


def test_develop_recovers_missing_package_publication_but_release_requires_it(tmp_path):
    from ci.plan.packages import plan_packages,package_identity
    from tests.ci.test_project_discovery import project
    p=project(tmp_path,'packages/example-core','example-core')
    class Records:
        def __init__(self,row):self.row=row
        def read(self,name,version):return self.row
    assert plan_packages(tmp_path,[],{'source':'push','ref':'develop'},Records(None))==['example-core']
    with pytest.raises(ValueError,match='develop'):
        plan_packages(tmp_path,[],{'source':'push','ref':'release'},Records(None))
    from tests.ci.test_package_records import record as fixture_record
    record={**fixture_record(),**package_identity(tmp_path,p)}
    assert plan_packages(tmp_path,['example-core'],{'source':'push','ref':'release'},Records(record))==[]
    assert plan_packages(tmp_path,[],{'source':'push','ref':'develop'},Records(record))==[]


def test_current_source_dependency_is_not_required_to_have_installed_metadata(tmp_path,monkeypatch):
    from ci.acceptance.versions import verify_versions
    from ci.build.dependency_wheels import WheelPin
    from ci.plan.projects import read_project
    from tests.ci.test_project_discovery import project
    owner=project(tmp_path,'packages/example-core','example-core')
    op=read_project(locked_project(tmp_path))
    sdk=WheelPin('striding-operator-sdk','0.3.0.dev9','striding_operator_sdk-0.3.0.dev9-py3-none-any.whl','b'*64)
    core=WheelPin('example-core','1.0.0',owner.wheel,'a'*64)
    monkeypatch.setattr('ci.build.dependency_wheels.locked_wheels',lambda *a:[sdk,core])
    monkeypatch.setattr('ci.build.dependency_wheels.verify_wheel',lambda *a:None)
    monkeypatch.setattr('ci.acceptance.versions.wheel_paths',lambda *a:[tmp_path/'build/wheels'/s for s in (sdk.filename,owner.wheel,op.wheel)])
    monkeypatch.setattr('ci.plan.test_sets.version_test',lambda *a:{'path':'tests','identity':'a'*64})
    monkeypatch.setattr('ci.acceptance.versions.subprocess.run',lambda *a,**kw:None)
    monkeypatch.setattr('ci.acceptance.versions.subprocess.check_output',lambda *a,**kw:str(tmp_path/'site'))
    observed=[]
    def probe(**kwargs):
        observed.append(kwargs)
        return {'exit_code':0,'passed':1,'failed':0}
    monkeypatch.setattr('ci.acceptance.versions.run_probe',probe)
    verify_versions(tmp_path,['example_read_data/v1.0.0'],modes=('source',))
    assert observed[0]['distributions']=={'striding-operator-sdk':'0.3.0.dev9'}
    assert owner.path/'src' in observed[0]['sources']


def test_package_upgrade_preserves_historical_module_and_vendor_ownership(monkeypatch):
    from dataclasses import replace
    from ci import ROOT
    from ci.plan.projects import shared_projects,boundary_errors,dependencies
    current=shared_projects(ROOT)
    upgraded=[replace(p,version='0.2.0') if p.distribution=='pretrain-cleaning-engine' else p for p in current]
    monkeypatch.setattr('ci.plan.projects.shared_projects',lambda root:upgraded)
    common=next(p for p in current if p.distribution=='pretrain-cleaning-common')
    assert dependencies(ROOT,common)==[]
    assert boundary_errors(ROOT)==[]
