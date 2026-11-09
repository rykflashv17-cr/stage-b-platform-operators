"""Resolve the actual package closure without hard-coded package/algorithm names."""
import json

import pytest

from ci.plan.projects import dependencies, operator_projects, read_project, shared_projects


def project(root, directory, name, version="1.0.0", requires=()):
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
        f'dependencies = {json.dumps(list(requires))}\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/example"]\n'
    )
    if path.parent.name == 'operators':
        (path/'operator.yaml').write_text(json.dumps({'code':path.name,'operatorVersion':version}))
    return read_project(path)


def test_new_shared_package_transitive_closure_is_dependency_first(tmp_path):
    project(tmp_path, "packages/z-core", "Example_Core")
    project(tmp_path, "packages/a-data", "example-data", requires=["example.core>=1,<2"])
    operator = project(tmp_path, "operators/example_read_data", "example-operator",
                       requires=["example-data==1.0.0"])
    assert [p.distribution for p in dependencies(tmp_path, operator)] == ["Example_Core", "example-data"]


def test_independent_operator_with_three_shared_packages_gets_own_candidate_jobs(tmp_path):
    from ci.plan.compose import compose
    from ci.plan.pipeline import assign_jobs
    from tests.ci.test_delivery_plan import Evidence,BASE,execution
    project(tmp_path,'packages/core','example-core')
    project(tmp_path,'packages/data','example-data',requires=['example-core==1.0.0'])
    project(tmp_path,'packages/metrics','example-metrics',requires=['example-data==1.0.0'])
    op=project(tmp_path,'operators/text_check_length','text-check-length',
               requires=['striding-operator-sdk==0.3.0.dev7','example-metrics==1.0.0'])
    (op.path/'Dockerfile').write_text('FROM '+BASE+'\n')
    (op.path/'uv.lock').write_text('version=1\n')
    for mode in ('source','wheel','container'):
        path=op.path/'tests'/mode/'test_entry.py';path.parent.mkdir(parents=True)
        path.write_text('def test_entry(): assert True\n')
    (tmp_path/'ci').mkdir();(tmp_path/'ci/release-versions.yaml').write_text('- text_check_length/v1.0.0\n')
    plan=compose(tmp_path,commit='a'*40,execution=execution(),evidence=Evidence(),
        checks={'wheels':[],'containers':[],'extra':False},baseline={'parent':None,'components':{},'schema_version':4},delivery=None)
    plan=assign_jobs(plan)
    key='text_check_length/v1.0.0'
    assert [p.distribution for p in dependencies(tmp_path,op)]==['example-core','example-data','example-metrics']
    assert set(plan['operators'])=={key}
    assert {row['kind'] for row in plan['tests'].values()}=={'source','wheel','container'}
    assert plan['operators'][key]['publish_tags']==['1.0.0-dev.10']
    assert 'build-operator-images:'+key in plan['jobs']
    assert 'test-operator-images:'+key in plan['jobs']
    assert {'publish-operator-images','mirror-operator-images','publish-operator-manifest'}<=plan['jobs'].keys()
    assert not any(name.startswith('test-pipelines:') for name in plan['jobs'])


def test_operator_versions_share_a_distribution_but_keep_old_shared_wheels(tmp_path):
    from tests.ci.test_shared_packages import locked_project
    project(tmp_path, "packages/example-core", "example-core", "2.0.0")
    old=read_project(locked_project(tmp_path))
    new=project(tmp_path,"operators/example_read_new","example-operator-new","2.0.0",["example-core==2.0.0"])
    assert dependencies(tmp_path,old)==[]
    assert [p.version for p in dependencies(tmp_path,new)]==["2.0.0"]


def test_duplicate_normalized_shared_identity_is_rejected(tmp_path):
    project(tmp_path, "packages/one", "example-core")
    project(tmp_path, "packages/two", "Example_Core")
    with pytest.raises(ValueError, match="duplicate.*example-core"):
        shared_projects(tmp_path)


@pytest.mark.parametrize("requirement", ["example-core==3.0.0", "example-core[extra]==1.0.0"])
def test_unlocked_or_unsupported_shared_requirement_fails(tmp_path,requirement):
    project(tmp_path,"packages/core","example-core")
    operator=project(tmp_path,"operators/example_read_data","example-operator",requires=[requirement])
    with pytest.raises(ValueError):dependencies(tmp_path,operator)


def test_dependency_cycle_has_actionable_path(tmp_path):
    project(tmp_path, "packages/a", "example-a", requires=["example-b==1.0.0"])
    project(tmp_path, "packages/b", "example-b", requires=["example-a==1.0.0"])
    operator = project(tmp_path, "operators/example_read_data", "example-operator",
                       requires=["example-a==1.0.0"])
    with pytest.raises(ValueError, match="cycle.*example-a.*example-b.*example-a"):
        dependencies(tmp_path, operator)


def test_marker_evaluated_for_requested_target_environment(tmp_path):
    project(tmp_path, "packages/linux", "linux-helper")
    operator = project(tmp_path, "operators/example_read_data", "example-operator",
                       requires=['linux-helper==1.0.0; sys_platform == "linux"'])
    assert dependencies(tmp_path, operator, environment={"sys_platform": "darwin"}) == []
    assert [p.distribution for p in dependencies(tmp_path, operator, environment={"sys_platform": "linux"})] == ["linux-helper"]


def test_operator_declaration_and_metadata_version_cannot_disagree(tmp_path):
    p=project(tmp_path, "operators/example_read_data", "example-operator", "1.0.0")
    (p.path/'operator.yaml').write_text('code: example_read_data\noperatorVersion: "2.0.0"\n')
    with pytest.raises(ValueError, match="identity"):
        operator_projects(tmp_path)
