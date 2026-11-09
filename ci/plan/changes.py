"""Pure Git-backed version selection; no registry access or workspace mutation."""
from ci import ROOT
import argparse
from contextlib import contextmanager
import io
import posixpath
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import tomllib

from ci.plan.projects import build_inputs, dependencies, operator_projects, readme_file
from ci.publish.tags import PUBLICATION_VERSION


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


@contextmanager
def snapshot(root, commit):
    # Only repository objects participate: untracked files and dirty worktrees cannot
    # silently alter publication planning.
    with tempfile.TemporaryDirectory(prefix='operator-plan-') as directory:
        with tarfile.open(fileobj=io.BytesIO(git(root, 'archive', commit))) as archive:
            archive.extractall(directory, filter='data')
        yield Path(directory)


def changed_paths(root, base):
    # Disable rename collapsing so both the old and new owner are considered.
    raw = git(root, 'diff', '--no-renames', '--name-only', '-z', base, 'HEAD')
    return [p.decode() for p in raw.split(b'\0') if p]


def documentation(path):
    p = Path(path)
    return (p.parts[0] in {'docs', '.superpowers'} or
            p.name in {'AGENTS.md', 'CONTRACT.md', 'LICENSE', 'LICENSE.md'} or
            p.suffix.lower() in {'.md', '.rst'})


RECIPE_KINDS = {"runtimes": "runtime", "builders": "builder"}
LEGACY_CONFIG = {"images/profiles.toml", "images/pins.json"}
SHARED_FOUNDATION_INPUTS = {
    'ci/base_images/config.py', 'ci/base_images/manage.py', 'ci/base_images/planning.py', 'ci/base_images/jobs.py',
    'ci/gitlab/common.yml', 'ci/gitlab/build.yml', 'ci/gitlab/test.yml', 'ci/gitlab/publish.yml', '.gitlab-ci.yml', 'ci/scripts/oras.sh',
    'ci/plan/changes.py', 'ci/scripts/plan.sh', 'ci/runtime.py', 'ci/publish/tags.py',
}


def recipe_path(path):
    parts = path.split("/")
    return len(parts) >= 3 and parts[0] == "images" and parts[1] in RECIPE_KINDS


def docs_only(root, diff_base, paths=None):
    """Cheap pipeline preflight. Build-input Markdown is never skipped."""
    paths = changed_paths(root, diff_base) if paths is None else paths
    if not paths:
        return True
    if not all(not recipe_path(p) and documentation(p) and 'src' not in Path(p).parts for p in paths):
        return False
    # Read only tiny tracked manifests, without importing/building projects or
    # unpacking the repository. Removed/renamed declared readmes matter too.
    for revision in {'HEAD', diff_base}:
        tracked = git(root, 'ls-tree', '-r', '--name-only', '-z', revision).split(b'\0')
        for item in tracked:
            path = item.decode()
            if not path.endswith('/pyproject.toml') or not path.startswith(('operators/', 'packages/', 'templates/')):
                continue
            metadata = tomllib.loads(git(root, 'show', f'{revision}:{path}').decode())
            name = readme_file(metadata)
            if name and posixpath.normpath(Path(path).parent / name) in paths:
                return False
    return True


def producer_input(path):
    return (path in {'ci/base_images/jobs.py', 'ci/base_images/planning.py'} or
            path.startswith('images/') and path not in LEGACY_CONFIG | SHARED_FOUNDATION_INPUTS and not documentation(path))


def foundation_scope(root, diff_base, check_tooling=False):
    """Use this push/MR's Git delta, independently of the operator release baseline."""
    paths = changed_paths(root, diff_base)
    tracked = git(root, 'ls-tree', '-r', '--name-only', '-z', 'HEAD', '--', 'images').decode().split('\0')
    recipes = {tuple(p.split('/')[:3]) for p in tracked if recipe_path(p)}
    changed = {tuple(p.split('/')[:3]) for p in paths if recipe_path(p)}
    selected = recipes & changed  # Removing a recipe retires it without touching Harbor.
    for parts in sorted(selected):
        filename = '/'.join((*parts, 'VERSION'))
        if filename not in tracked:
            raise ValueError(f'missing {filename}')
        version = git(root, 'show', 'HEAD:' + filename).decode().removesuffix('\n')
        if not PUBLICATION_VERSION.fullmatch(version):
            raise ValueError(f'invalid {filename}: {version!r}')
        if git(root, 'ls-tree', diff_base, '--', filename):
            previous = git(root, 'show', diff_base + ':' + filename).decode().strip()
            if tuple(map(int, version.split('.'))) <= tuple(map(int, previous.split('.'))):
                raise ValueError(f'{filename} must increase from {previous} after a recipe change')
    if check_tooling and any(p in SHARED_FOUNDATION_INPUTS or (producer_input(p) and not recipe_path(p)) for p in paths):
        selected = recipes
    other = [p for p in paths if not recipe_path(p) and not producer_input(p)]
    from ci.plan.impact import classify
    for path in other:
        classify(root, path, resolve_owners=False)
    return {'foundations': sorted(RECIPE_KINDS[kind] + '/' + name for _, kind, name in selected),
            'operatorPipeline': not docs_only(root, diff_base, other)}


def signatures(root, *, historical_snapshot=False):
    # Published parents may still use the retired indirection. Project just the
    # selected digest pair for each historical version, never today's recipes.
    historical = {}
    profile_file = root / 'images/profiles.toml'
    if profile_file.exists():
        rows = tomllib.loads(profile_file.read_text())['profiles']
        pins = json.loads((root / 'images/pins.json').read_text())
        for row in rows:
            references = (pins['runtime'][row['runtime']], pins['builder'][row['builder']])
            for key in row['operators']:
                historical[key] = references
    if historical_snapshot and list((root/'operators').glob('*/v*/pyproject.toml')):
        # Read-only Git snapshots predating the source-layout migration. Current
        # trees still go through strict discovery and may not reintroduce these.
        from dataclasses import replace
        from ci.plan.projects import read_project
        catalog=[replace(read_project(path.parent),name=path.parent.parent.name)
                 for path in sorted((root/'operators').glob('*/v*/pyproject.toml'))]
    else:
        catalog=operator_projects(root)
    return {f'{p.name}/v{p.version}': (build_inputs(root, p, operator_catalog=catalog), historical.get(f'{p.name}/v{p.version}'))
            for p in catalog}


def extra_affected(root, paths):
    """Shared ownership; MR and delivery choose policy separately."""
    from ci.plan.impact import affected_versions
    return affected_versions(root, paths)


def component_keys(components):
    if components is None:
        return set()
    if isinstance(components, dict) and 'components' in components:
        components = components['components']
    if isinstance(components,list) and all(isinstance(row,dict) for row in components):
        return {row['code']+'/v'+row['buildVersion'] for row in components}
    keys = set(components)
    if isinstance(components, dict):
        for code, value in components.items():
            if isinstance(value, dict) and value.get('operator'):
                keys.add(value['operator'])
            if isinstance(value, dict) and value.get('buildVersion'):
                version = value['buildVersion']
                keys.add(code if '/v' in code else f'{code}/v{version.removeprefix("v")}')
    return keys


def make_plan(root, base_commit, baseline_components, diff_base=None):
    """Compare selected HEAD versions to the last verified published parent.

    diff_base is a separate MR base used only to validate changed inactive versions.
    Removed versions are never built; removing a still-selected version is an error.
    """
    root = Path(root)
    with snapshot(root, 'HEAD') as head:
        current = signatures(head)
        from ci.plan.release_scope import load_versions
        selected = set(load_versions(head, current))
        available = component_keys(baseline_components)
        if base_commit:
            with snapshot(root, base_commit) as old:
                previous = signatures(old, historical_snapshot=True)
                old_selection = old / 'ci/release-versions.yaml'
                if old_selection.exists():
                    previous_active = set(load_versions(old, previous))
                else:
                    legacy = old / 'ci/active-versions.json'
                    previous_active = set(json.loads(legacy.read_text()).values()) if legacy.exists() else set()
            impacted = extra_affected(head, changed_paths(root, base_commit))
            changed = {key for key in current if current[key] != previous.get(key)} | impacted
            changed.update(selected - previous_active)
        else:
            changed = set(current)
        publish = {key for key in selected & current.keys() if key in changed or key not in available}
        # Until per-version delivery receipts are introduced, conservatively
        # retain inactive changes since the last delivered publication. The
        # latest push's diff alone can hide a failed candidate after a docs push.
        build = set(publish) | (changed - selected)
        if diff_base:
            with snapshot(root, diff_base) as old:
                previous = signatures(old, historical_snapshot=True)
            impacted = extra_affected(head, changed_paths(root, diff_base))
            build.update(key for key in current if key not in selected and
                         (current[key] != previous.get(key) or key in impacted))
        for key in selected | build:
            if not PUBLICATION_VERSION.fullmatch(key.split("/v", 1)[1]):
                raise ValueError(f"publication requires a numeric X.Y.Z operator version: {key}")
        return {'publication_versions': sorted(selected), 'build': sorted(build),
                'publish': sorted(publish)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--base-commit')
    parser.add_argument('--baseline-components', type=Path)
    parser.add_argument('--diff-base')
    parser.add_argument('--docs-only', action='store_true')
    parser.add_argument('--preflight', action='store_true', help='plan recipe work before any external access')
    parser.add_argument('--check-tooling', action='store_true', help='MR: test recipes after producer tooling changes')
    parser.add_argument('--scope-file', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.docs_only:
        if not args.diff_base:
            parser.error('--docs-only requires --diff-base')
        raise SystemExit(0 if docs_only(args.root, args.diff_base) else 1)
    if args.preflight:
        if not args.diff_base:
            parser.error('--preflight requires --diff-base')
        result = {'build': [], 'publish': [], **foundation_scope(args.root, args.diff_base, args.check_tooling)}
    else:
        components = json.loads(args.baseline_components.read_text()) if args.baseline_components else None
        result = make_plan(args.root, args.base_commit, components, args.diff_base)
        if isinstance(components, dict) and 'components' in components:
            result = {**components, **result}
        if args.scope_file:
            scope = json.loads(args.scope_file.read_text())
            result.update(foundations=scope['foundations'],
                          operatorPipeline=bool(scope['operatorPipeline'] or result['build'] or result['publish']))
    result['commit'] = git(args.root, 'rev-parse', 'HEAD').decode().strip()
    if not args.preflight and args.diff_base:
        from ci.plan.checks import select_checks
        with snapshot(args.root, 'HEAD') as head:
            result['checks'] = select_checks(head, changed_paths(args.root, args.diff_base), result)
    content = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    else:
        print(content, end='')


if __name__ == '__main__':
    main()
