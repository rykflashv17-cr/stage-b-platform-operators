from pathlib import Path
import pytest
import yaml


def fixture(root, combinations=None):
    for key in ('read/v1.0.0','write/v2.0.0','independent/v1.0.0'):
        from tests.ci.test_project_discovery import project
        project(root,'operators/'+key.split('/v')[0],key.split('/v')[0],key.split('/v')[1])
        for mode in ('source','wheel','container'):
            path=root/'operators'/key.split('/v')[0]/'tests'/mode/'test_call.py'
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text('raise AssertionError("must not import while planning")\n')
    folder=root/'tests/pipelines/cleaning';folder.mkdir(parents=True)
    (folder/'__init__.py').write_text('raise AssertionError("must not import while planning")\n')
    (folder/'test_pipeline.py').write_text('raise AssertionError("must not import while planning")\n')
    body={'requirements':{'platform':'linux/amd64','runner_tags':['software'],'environment':'ci/tooling/uv.lock'},
          'combinations': combinations or {'current':{'versions':{'read':'read/v1.0.0','write':'write/v1.0.0'},
            'tests':['test_pipeline.py'],'parameters':{}}}, 'container_checks':{}}
    path=folder/'definition.yaml';path.write_text(yaml.safe_dump(body))
    return path


def test_discovery_does_not_import_tests_and_independent_operator_needs_no_workflow(tmp_path):
    from ci.plan.test_sets import discover, select
    fixture(tmp_path)
    definitions=discover(tmp_path,{'read/v1.0.0','write/v1.0.0','write/v2.0.0','independent/v1.0.0'})
    assert select(definitions,{'independent/v1.0.0'})==[]
    assert select(definitions,{'read/v1.0.0'})==['cleaning/current']


def test_new_member_version_requires_explicit_compatible_combination(tmp_path):
    from ci.plan.test_sets import discover, select
    fixture(tmp_path)
    definitions=discover(tmp_path,{'read/v1.0.0','write/v1.0.0','write/v2.0.0'})
    with pytest.raises(ValueError,match='compatible combination.*write/v2.0.0'):
        select(definitions,{'write/v2.0.0'})


@pytest.mark.parametrize('mutation', ['duplicate','unknown','escape','missing','duplicate-version'])
def test_malformed_declarations_fail_before_test_execution(tmp_path,mutation):
    from ci.plan.test_sets import discover
    path=fixture(tmp_path);body=yaml.safe_load(path.read_text())
    if mutation=='duplicate':path.write_text(path.read_text()+'combinations: {}\n')
    else:
        if mutation=='unknown':body['surprise']=True
        if mutation=='escape':body['combinations']['current']['tests']=['../outside.py']
        if mutation=='missing':body['combinations']['current']['versions']['read']='read/v9.0.0'
        if mutation=='duplicate-version':body['combinations']['current']['versions']['another']='read/v1.0.0'
        path.write_text(yaml.safe_dump(body))
    with pytest.raises(ValueError):discover(tmp_path,{'read/v1.0.0','write/v1.0.0'})


def test_linked_mr_combinations_form_one_build_group(tmp_path):
    from ci.plan.test_sets import discover, group_mr
    fixture(tmp_path,{'old':{'versions':{'read':'read/v1.0.0','write':'write/v1.0.0'},'tests':['test_pipeline.py'],'parameters':{}},
        'new':{'versions':{'read':'read/v1.0.0','write':'write/v2.0.0'},'tests':['test_pipeline.py'],'parameters':{}}})
    definitions=discover(tmp_path,{'read/v1.0.0','write/v1.0.0','write/v2.0.0'})
    groups=group_mr(definitions,['cleaning/new','cleaning/old'],{'read/v1.0.0','write/v1.0.0','write/v2.0.0'})
    assert len(groups)==1 and groups[0]['versions']==['read/v1.0.0','write/v1.0.0','write/v2.0.0']


def test_version_test_identity_includes_shared_conftest_and_rejects_empty_directory(tmp_path):
    from ci.plan.test_sets import version_test
    fixture(tmp_path)
    first=version_test(tmp_path,'read/v1.0.0','wheel')
    conftest=tmp_path/'operators/read/tests/conftest.py';conftest.write_text('# shared fixture\n')
    assert version_test(tmp_path,'read/v1.0.0','wheel')['identity']!=first['identity']
    (tmp_path/'operators/read/tests/wheel/test_call.py').unlink()
    with pytest.raises(ValueError,match='empty'):
        version_test(tmp_path,'read/v1.0.0','wheel')


def test_dependency_test_identity_tracks_only_the_selected_package_version(tmp_path):
    from ci.plan.test_sets import version_test
    from tests.ci.test_project_discovery import project
    fixture(tmp_path)
    operator=project(tmp_path,'operators/read','example-reader','1.0.0')
    path=operator.path/'pyproject.toml'
    path.write_text(path.read_text().replace('dependencies = []','dependencies = ["shared-config==2.0.0"]'))
    for version in ('1.0.0','2.0.0'):
        owner=project(tmp_path,'packages/shared-'+version,('unrelated-config' if version=='1.0.0' else 'shared-config'),version)
        tests=owner.path/'tests/source';tests.mkdir(parents=True)
        (tests/'test_contract.py').write_text('def test_ok(): pass\n')
    first=version_test(tmp_path,'read/v1.0.0','source')
    assert first['paths']==['operators/read/tests/source','packages/shared-2.0.0/tests/source']
    for version in ('1.0.0','2.0.0'):
        path=tmp_path/f'packages/shared-{version}/tests/source/test_contract.py'
        path.write_text('def test_failure(): assert False\n')
        assert (version_test(tmp_path,'read/v1.0.0','source')['identity']!=first['identity']) == (version=='2.0.0')


def test_combination_can_use_its_own_locked_environment_without_changing_peers(tmp_path):
    from ci.plan.test_sets import discover
    path=fixture(tmp_path)
    env=path.parent/'manifest/uv.lock';env.parent.mkdir();env.write_text('version = 1\n')
    body=yaml.safe_load(path.read_text())
    body['combinations']['current']['environment']='tests/pipelines/cleaning/manifest/uv.lock'
    path.write_text(yaml.safe_dump(body))
    row=discover(tmp_path,{'read/v1.0.0','write/v1.0.0'})['cleaning/current']
    assert row['requirements']['environment']=='tests/pipelines/cleaning/manifest/uv.lock'
    before=row['identity'];env.write_text('version = 2\n')
    assert discover(tmp_path,{'read/v1.0.0','write/v1.0.0'})['cleaning/current']['identity']!=before
    body['combinations']['current']['environment']='operators/read/uv.lock'
    path.write_text(yaml.safe_dump(body))
    with pytest.raises(ValueError,match='environment'):
        discover(tmp_path,{'read/v1.0.0','write/v1.0.0'})


@pytest.mark.parametrize('mode', ['source','wheel','container','pipeline'])
def test_execution_inputs_invalidate_each_acceptance_mode(tmp_path, mode):
    from ci.plan.test_sets import discover,version_test
    fixture(tmp_path)
    runner=tmp_path/'ci/acceptance/jobs.py';runner.parent.mkdir(parents=True);runner.write_text('# before\n')
    def fingerprint():
        if mode=='pipeline':return discover(tmp_path,{'read/v1.0.0','write/v1.0.0'})['cleaning/current']['identity']
        return version_test(tmp_path,'read/v1.0.0',mode)['identity']
    before=fingerprint();runner.write_text('# changed execution\n')
    assert fingerprint()!=before


def test_container_runner_change_selects_mr_acceptance_without_image_rebuild():
    from ci import ROOT
    from ci.plan.checks import select_checks
    from ci.plan.impact import affected_versions
    paths=['ci/acceptance/pytest_runner.py']
    selected=select_checks(ROOT,paths,{})
    assert len(selected['containers'])==4
    assert affected_versions(ROOT,paths)==set()
