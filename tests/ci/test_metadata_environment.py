import tomllib
from ci import ROOT


def test_metadata_requirements_are_locked_and_exclude_sdk_and_test_dependencies():
    from ci.build.metadata_environment import requirements
    value=requirements(ROOT/'ci/tooling/uv.lock')
    lines=value.splitlines()
    assert len(lines)==2
    assert any(line.startswith('pyyaml==6.0.3 ') for line in lines)
    assert any(line.startswith('packaging==26.3 ') for line in lines)
    assert all('--hash=sha256:' in line for line in lines)
    assert 'sdk' not in value and 'pytest' not in value and 'numpy' not in value
