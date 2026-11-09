"""Generic context fixtures do not consume any operator's production wheel."""
import shutil
import tomllib
from zipfile import ZipFile
import pytest
from ci import ROOT


@pytest.fixture
def context_project(tmp_path):
    from ci.plan.projects import read_project,source_files
    from tests.ci.helpers import sdk_fixture
    root=tmp_path/'repository';target=root/'operators/record_quality_gate'
    shutil.copytree(ROOT/'templates/python-operator',target,ignore=shutil.ignore_patterns('__pycache__','.venv'))
    for name in ('pyproject.toml','uv.lock'):
        path=target/name;path.write_text(path.read_text())
    project=read_project(target);metadata=tomllib.loads((target/'pyproject.toml').read_text())['project']
    wheels=root/'build/wheels';wheels.mkdir(parents=True)
    sdk, fixture=sdk_fixture(root,target)
    shutil.copyfile(fixture,wheels/sdk.filename)
    # This synthetic wheel is a test input for archive/source comparison, not
    # a production packaging artifact or a replacement for installed-wheel tests.
    fields=['Metadata-Version: 2.4','Name: '+project.distribution,'Version: '+project.version,
            'Requires-Python: '+metadata['requires-python']]
    fields += ['Requires-Dist: '+value for value in metadata.get('dependencies',[])]
    with ZipFile(wheels/project.wheel,'w') as archive:
        archive.writestr(project.distribution.replace('-','_')+'-'+project.version+'.dist-info/METADATA','\n'.join(fields)+'\n')
        for path in source_files(project):
            if path.is_relative_to(target/'src'):archive.writestr(path.relative_to(target/'src').as_posix(),path.read_bytes())
    return root,project
