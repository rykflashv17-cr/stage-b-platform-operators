from pathlib import Path

from ci import ROOT


def test_package_selection_does_not_build_template_or_other_operator_versions():
    from ci.build.environment import selected_projects
    owners=selected_projects(ROOT,['mcap_delivery_accepted/v5.0.0'],template=False,environments=[])
    actual={p.path.relative_to(ROOT).as_posix() for p in owners}
    assert 'operators/mcap_delivery_accepted' in actual
    assert not any(p.startswith(('templates/','operators/mcap_read_normalize/','operators/mcap_delivery_accepted/v5')) for p in actual)


def test_flow_only_tests_prepare_declared_shared_packages_without_operator_wheels():
    from ci.build.environment import selected_projects
    owners=selected_projects(ROOT,[],template=False,environments=['tests/pipelines/pretraining_data_cleaning/uv.lock'])
    assert owners == []  # Historical shared wheels are downloaded from Nexus.
    from ci.build.dependency_wheels import locked_wheels
    assert len(locked_wheels(ROOT,ROOT/'tests/pipelines/pretraining_data_cleaning')) == 3
    assert len({(p.distribution,p.version) for p in owners})==len(owners)
