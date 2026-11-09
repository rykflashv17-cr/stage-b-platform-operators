"""Planner checks against real Git histories."""
import io
import json
from pathlib import Path
import subprocess
import tarfile
import shutil
import pytest
import yaml
from ci.plan.changes import component_keys, docs_only, make_plan

REPO = Path(__file__).resolve().parents[2]

def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()

def commit(root):
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'fixture')
    return git(root, 'rev-parse', 'HEAD')

@pytest.fixture
def history(tmp_path):
    # Exercise the working implementation, including uncommitted file moves.
    # Git still defines the baseline/history after this source snapshot.
    paths = subprocess.check_output(['git', '-C', str(REPO), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'])
    for name in set(paths.decode().split('\0')) - {''}:
        source, target = REPO / name, tmp_path / name
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.email', 'fixture@example.invalid')
    git(tmp_path, 'config', 'user.name', 'Fixture')
    selected = yaml.safe_load((tmp_path / 'ci/release-versions.yaml').read_text())
    # This fixture approves the original combination; v5 remains a candidate.
    active = {key.split('/')[0]:key for key in selected if key.endswith('/v4.0.1') and not key.startswith('mcap_delivery_accepted/')}
    (tmp_path / 'ci/release-versions.yaml').write_text(yaml.safe_dump(list(active.values())))
    base = commit(tmp_path)
    return tmp_path, base, active

def test_empty_parent_and_unchanged(history):
    from ci.plan.projects import operator_projects
    root, base, active = history
    assert make_plan(root, None, None)['build'] == sorted(f'{p.name}/v{p.version}' for p in operator_projects(root))
    assert make_plan(root, base, active.values())['build'] == []
    records = {code: {'buildVersion': key.split('/v')[1]} for code, key in active.items()}
    assert make_plan(root, base, {'components': records})['build'] == []
    assert component_keys({'components': {'example': {'buildVersion': '4.0.0'}}}) >= {'example/v4.0.0'}

def test_docs_and_packaged_docs(history):
    root, base, active = history
    key = next(iter(active.values()))
    (root / 'operators' / key.split('/v')[0] / 'README.md').write_text('standalone docs')
    commit(root)
    assert docs_only(root, base)
    assert make_plan(root, base, active.values())['build'] == []
    (root / 'operators' / key.split('/v')[0] / 'src' / 'packaged.md').write_text('packaged input')
    commit(root)
    assert not docs_only(root, base)
    assert make_plan(root, base, active.values())['build'] == [key]

def test_root_lock_rename_and_removal(history):
    root, base, active = history
    (root / 'uv.lock').write_text('owner hash changed')
    commit(root)
    assert make_plan(root, base, active.values())['build'] == []
    key = next(iter(active.values()))
    source = next((root / 'operators' / key.split('/v')[0] / 'src').rglob('*.py'))
    renamed = source.with_name('renamed.py')
    source.rename(renamed)
    commit(root)
    assert make_plan(root, base, active.values())['publish'] == [key]
    renamed.unlink()
    commit(root)
    assert make_plan(root, base, active.values())['build'] == [key]

def test_current_version_upgrade_requires_explicit_release_scope(history):
    root,base,active=history
    key=active['mcap_read_normalize'];old=key.split('/v')[1];new='4.0.2'
    folder=root/'operators/mcap_read_normalize'
    for name in ('pyproject.toml','operator.yaml'):
        p=folder/name;p.write_text(p.read_text().replace(old,new))
    commit(root)
    with pytest.raises(ValueError,match='release version'):
        make_plan(root,base,active.values())
    active['mcap_read_normalize']='mcap_read_normalize/v'+new
    (root/'ci/release-versions.yaml').write_text(yaml.safe_dump(list(active.values())))
    commit(root)
    result=make_plan(root,base,active.values())
    assert result['build']==result['publish']==['mcap_read_normalize/v'+new]

def test_removed_selected_version_rejected(history):
    root, base, active = history
    key = next(iter(active.values()))
    shutil.rmtree(root / 'operators' / key.split('/v')[0])
    commit(root)
    with pytest.raises(ValueError, match='invalid release version'):
        make_plan(root, base, active.values())

def legacy_selection(root, active):
    keys = list(active.values())
    (root / 'images/profiles.toml').write_text(
        '[[profiles]]\nruntime = "legacy"\nbuilder = "legacy"\noperators = ' + json.dumps(keys, indent=4))
    (root / 'images/pins.json').write_text(json.dumps({
        kind: {'legacy': f'harbor.striding.internal/data/data-operators/base/{kind}/legacy@sha256:' + '1' * 64}
        for kind in ('runtime', 'builder')}))
    return commit(root)


def test_pin_projection_and_shared_dependency(history):
    root, _, active = history
    base = legacy_selection(root, active)
    path = root / 'images/pins.json'
    pins = json.loads(path.read_text())
    pins['runtime']['unused'] = 'unused'
    path.write_text(json.dumps(pins))
    commit(root)
    assert make_plan(root, base, active.values())['build'] == []
    source = next((root / 'packages/pretrain-cleaning-common/src').rglob('*.py'))
    source.write_text(source.read_text() + '\n# shared change\n')
    commit(root)
    assert make_plan(root, base, active.values())['build'] == []  # old consumers use Nexus wheels

def test_workspace_metadata_and_foundation_recipe_do_not_fan_out(history):
    root, base, active = history
    metadata = root / 'pyproject.toml'
    text = metadata.read_text()
    dependency='striding-operator-sdk==0.3.0.dev9'
    assert dependency in text
    metadata.write_text(text.replace(dependency,'striding-operator-sdk==99.0.0'))
    recipe = root / 'images/runtimes/python312-cpu/Dockerfile'
    recipe.write_text(recipe.read_text() + '\n# unpublished recipe change\n')
    commit(root)
    assert make_plan(root, base, active.values())['build'] == []
    key = active['mcap_quality_detect']
    source = next((root / 'operators' / key.split('/v')[0] / 'src').rglob('*.py'))
    source.write_text(source.read_text() + '\n# only B03 source changed\n')
    commit(root)
    assert make_plan(root, base, active.values())['build'] == [key]

@pytest.mark.parametrize('shared', [False, True])
def test_declared_project_readme_is_a_build_input(history, shared):
    root, _, active = history
    key = next(iter(active.values()))
    owner = root / ('packages/pretrain-cleaning-common' if shared else 'operators/' + key.split('/v')[0])
    metadata = owner / 'pyproject.toml'
    metadata.write_text(metadata.read_text().replace('[project]', '[project]\nreadme = "PROJECT.md"', 1))
    readme = owner / 'PROJECT.md'
    readme.write_text('initial metadata description')
    base = commit(root)
    readme.write_text('changed wheel metadata description')
    commit(root)
    assert not docs_only(root, base)
    assert make_plan(root, base, active.values())['build'] == ([] if shared else [key])

def test_only_selected_foundation_pin_consumers_change(history):
    root, _, active = history
    legacy_selection(root, active)
    key = next(iter(active.values()))
    profile = root / 'images/profiles.toml'
    profile.write_text(profile.read_text().replace(f'    "{key}",\n', '') +
                       f'\n[[profiles]]\nruntime = "runtime-alt"\nbuilder = "builder-alt"\noperators = ["{key}"]\n')
    path = root / 'images/pins.json'
    pins = json.loads(path.read_text())
    for kind, name in [('runtime', 'runtime-alt'), ('builder', 'builder-alt')]:
        pins[kind][name] = f'harbor.striding.internal/data/data-operators/base/{kind}/{name}@sha256:' + '1' * 64
    path.write_text(json.dumps(pins))
    base = commit(root)
    pins['runtime']['runtime-alt'] = pins['runtime']['runtime-alt'].replace('1' * 64, '2' * 64)
    path.write_text(json.dumps(pins))
    commit(root)
    assert make_plan(root, base, active.values())['build'] == [key]
    (root / 'new-build-tool.sh').write_text('echo changed')
    commit(root)
    with pytest.raises(ValueError, match='unclassified'):
        make_plan(root, base, active.values())


def test_operator_plan_compares_pre_migration_snapshot(history):
    root, _, active = history
    profiles = root / 'images/profiles.toml'
    pins = root / 'images/pins.json'
    profiles.write_text('[[profiles]]\nruntime="old"\nbuilder="old"\noperators=' + json.dumps(list(active.values())))
    pins.write_text(json.dumps({kind: {'old': f'example/{kind}@sha256:' + '1' * 64} for kind in ('runtime', 'builder')}))
    base = commit(root)
    profiles.unlink()
    pins.unlink()
    for key in active.values():
        (root / 'operators' / key.split('/v')[0] / 'Dockerfile').write_text('FROM example/runtime:1.0.0\n')
    commit(root)
    assert make_plan(root, base, active.values())['build'] == sorted(active.values())
    base = git(root, 'rev-parse', 'HEAD')
    key = next(iter(active.values()))
    (root / 'operators' / key.split('/v')[0] / 'Dockerfile').write_text('FROM example/runtime:1.0.1\n')
    commit(root)
    assert make_plan(root, base, active.values())['build'] == [key]


def test_foundation_version_scope_is_independent_of_operator_publication(history):
    from ci.plan.changes import foundation_scope
    root, _, active = history
    recipe = root / 'images/runtimes/python312-cpu'
    (recipe / 'VERSION').write_text('1.2.3\n')
    base = commit(root)
    source = recipe / 'Dockerfile'
    source.write_text(source.read_text() + '\nENV RELEASE=next\n')
    commit(root)
    with pytest.raises(ValueError, match='VERSION'):
        foundation_scope(root, base)
    (recipe / 'VERSION').write_text('1.10.0\n')
    commit(root)
    assert foundation_scope(root, base) == {'foundations': ['runtime/python312-cpu'], 'operatorPipeline': False}
    key = next(iter(active.values()))
    (root / 'operators' / key.split('/v')[0] / 'main.py').write_text('# consumer change\n')
    commit(root)
    assert foundation_scope(root, base) == {'foundations': ['runtime/python312-cpu'], 'operatorPipeline': True}


def test_unclassified_build_input_cannot_silently_skip_gitlab_jobs(history):
    from ci.plan.changes import foundation_scope
    root, base, _ = history
    path = root / 'vendor' / 'image-tool'
    path.parent.mkdir()
    path.write_text('new build tool')
    commit(root)
    with pytest.raises(ValueError, match='vendor/image-tool'):
        foundation_scope(root, base)


def test_foundation_tooling_checks_all_recipes_only_on_merge_requests(history):
    from ci.plan.changes import foundation_scope
    root, _, _ = history
    recipe = root / 'images/runtimes/unconsumed'
    recipe.mkdir()
    (recipe / 'VERSION').write_text('1.0.0\n')
    base = commit(root)
    tool = root / 'ci/base_images/jobs.py'
    tool.write_text(tool.read_text() + '\n# change producer tooling\n')
    commit(root)
    assert 'runtime/unconsumed' in foundation_scope(root, base, check_tooling=True)['foundations']
    assert foundation_scope(root, base) == {'foundations': [], 'operatorPipeline': False}
    shutil.rmtree(recipe)
    commit(root)
    assert foundation_scope(root, base)['foundations'] == []


@pytest.mark.parametrize('path', ['ci/base_images/config.py', 'ci/base_images/manage.py', 'ci/scripts/oras.sh'])
def test_tooling_shared_between_producer_and_operator_checks_both_on_mr(history, path):
    from ci.plan.changes import foundation_scope
    root, base, _ = history
    tool = root / path
    tool.write_text(tool.read_text() + '\n# shared tooling change\n')
    commit(root)
    mr = foundation_scope(root, base, check_tooling=True)
    assert mr['operatorPipeline'] and 'runtime/python312-cpu' in mr['foundations']
    assert foundation_scope(root, base) == {'foundations': [], 'operatorPipeline': True}
