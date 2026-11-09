"""Local dependency boundaries follow package metadata, independent of business names."""
import json

import pytest

from ci.plan.projects import boundary_errors


def owner(root, directory, name, module, requires=()):
    path=root/directory; source=path/'src'/module;source.mkdir(parents=True)
    (source/'__init__.py').write_text('')
    (path/'pyproject.toml').write_text(
        f'[project]\nname={json.dumps(name)}\nversion="1.0.0"\n'
        f'dependencies={json.dumps(list(requires))}\n'
        f'[tool.hatch.build.targets.wheel]\npackages=["src/{module}"]\n')
    if path.parent.name=='operators':(path/'operator.yaml').write_text(json.dumps({'code':path.name,'operatorVersion':'1.0.0'}))
    return path


def test_new_shared_package_is_not_an_undeclared_external_library(tmp_path):
    owner(tmp_path,'packages/text-metrics','text-metrics','text_metrics')
    op=owner(tmp_path,'operators/text_check_length','text-check-length','text_check_length')
    (op/'src/text_check_length/__init__.py').write_text('import text_metrics\n')
    assert any('undeclared local dependency text_metrics' in error for error in boundary_errors(tmp_path))
    metadata=op/'pyproject.toml'
    metadata.write_text(metadata.read_text().replace('dependencies=[]','dependencies=["text-metrics==1.0.0"]'))
    assert boundary_errors(tmp_path)==[]


def test_packaged_import_root_belongs_to_declared_transitive_dependency(tmp_path):
    engine=owner(tmp_path,'packages/engine','example-engine','example_engine')
    root=engine/'src/example_engine/_vendor/tool/src'
    vendor=root/'vendor_math';vendor.mkdir(parents=True);(vendor/'__init__.py').write_text('')
    metadata=engine/'pyproject.toml'
    metadata.write_text(metadata.read_text()+'[tool.operator-ci]\nimport-roots=["src/example_engine/_vendor/tool/src"]\n')
    owner(tmp_path,'packages/middle','example-middle','example_middle',['example-engine==1.0.0'])
    op=owner(tmp_path,'operators/text_check_length','text-check-length','text_check_length')
    (op/'src/text_check_length/__init__.py').write_text('from vendor_math import value\n')
    assert any('undeclared local dependency vendor_math' in error for error in boundary_errors(tmp_path))
    metadata=op/'pyproject.toml'
    metadata.write_text(metadata.read_text().replace('dependencies=[]','dependencies=["example-middle==1.0.0"]'))
    assert boundary_errors(tmp_path)==[]


@pytest.mark.parametrize('root',['../outside','/tmp','src/unpackaged','src/example_engine/missing'])
def test_import_root_must_be_a_real_packaged_directory(tmp_path,root):
    path=owner(tmp_path,'packages/engine','example-engine','example_engine')
    metadata=path/'pyproject.toml'
    metadata.write_text(metadata.read_text()+f'[tool.operator-ci]\nimport-roots=[{json.dumps(root)}]\n')
    with pytest.raises(ValueError,match='import root'):
        boundary_errors(tmp_path)


def test_operator_cannot_import_another_operator_even_if_packaged(tmp_path):
    owner(tmp_path,'operators/text_check_length','text-check-length','text_check_length')
    consumer=owner(tmp_path,'operators/text_export_lines','text-export-lines','text_export_lines')
    (consumer/'src/text_export_lines/__init__.py').write_text('import text_check_length\n')
    assert any('forbidden dependency text_check_length' in error for error in boundary_errors(tmp_path))
