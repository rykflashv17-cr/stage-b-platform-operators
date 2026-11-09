"""Repository project ownership and deterministic, per-operator build inputs."""
import ast
from dataclasses import dataclass
import hashlib
from importlib.util import resolve_name
from pathlib import Path
import re
import tomllib


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    distribution: str
    version: str
    modules: tuple[str, ...]

    @property
    def wheel(self):
        return f"{self.distribution.replace('-', '_')}-{self.version}-py3-none-any.whl"


def read_project(path):
    raw = tomllib.loads((path / "pyproject.toml").read_text())
    p = raw["project"]
    modules = tuple(Path(m).name for m in raw["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])
    identity = path.name if path.parent.name == "operators" else p["name"].replace("-", "_")
    return Project(identity, path, p["name"], p["version"], modules)


def operator_projects(root):
    import yaml
    folder = Path(root) / "operators"
    if list(folder.glob("*/v*/pyproject.toml")):
        raise ValueError("operators must keep one current source directory without a version subdirectory")
    projects = [read_project(p.parent) for p in sorted(folder.glob("*/pyproject.toml"))]
    names = set()
    for project in projects:
        declaration = project.path / "operator.yaml"
        if not declaration.is_file():
            raise ValueError(f"missing operator identity: {declaration}")
        body = yaml.safe_load(declaration.read_text())
        if (not isinstance(body, dict) or body.get("code") != project.name or
                body.get("operatorVersion") != project.version or
                not re.fullmatch(r"[a-z][a-z0-9_]*", project.name) or
                not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", project.version)):
            raise ValueError(f"operator identity mismatch: {project.path}")
        name = normalized_name(project.distribution)
        if name in names:
            raise ValueError(f"duplicate operator distribution: {name}")
        names.add(name)
    return projects


def operator_project(root, key):
    matches = [p for p in operator_projects(root) if key == f"{p.name}/v{p.version}"]
    if len(matches) != 1:
        raise ValueError(f"no current source for exact operator version: {key}")
    return matches[0]


def normalized_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def shared_projects(root):
    projects = [read_project(p.parent) for p in sorted((Path(root) / "packages").glob("*/pyproject.toml"))]
    identities = set()
    for project in projects:
        identity = normalized_name(project.distribution)
        if identity in identities:
            raise ValueError(f"duplicate shared package {identity}: keep one current source version")
        identities.add(identity)
    return projects


def dependencies(root, project, *, environment=None, operator_catalog=None):
    """Resolve one target's local closure; never merge different version environments.

    Unpinned ambiguous local requirements fail instead of choosing a directory by
    name/sort order. The default is the supported Linux/Python 3.12 image target;
    source checks can supply their own marker environment.
    """
    from packaging.markers import default_environment
    from packaging.requirements import Requirement

    markers = {**default_environment(), "sys_platform": "linux", "os_name": "posix",
               "platform_system": "Linux", "platform_machine": "x86_64",
               "python_version": "3.12", "python_full_version": "3.12.14", **(environment or {})}
    known = {}
    for p in shared_projects(root):
        known.setdefault(normalized_name(p.distribution), []).append(p)
    operator_names = {normalized_name(p.distribution) for p in (operator_projects(root) if operator_catalog is None else operator_catalog)}
    found, visiting, ordered = {}, [], []

    def walk(p):
        name = normalized_name(p.distribution)
        if name in visiting:
            raise ValueError("local dependency cycle: " + " -> ".join([*visiting, name]))
        visiting.append(name)
        for raw in tomllib.loads((p.path / "pyproject.toml").read_text())["project"].get("dependencies", []):
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate(markers):
                continue
            dependency_name = normalized_name(requirement.name)
            if dependency_name in operator_names:
                raise ValueError(f"local dependency on operator is forbidden: {p.distribution}: {raw}")
            if dependency_name not in known:
                continue
            if requirement.url or requirement.extras:
                raise ValueError(f"local dependency must use a version without extras or URL: {raw}")
            candidates = [candidate for candidate in known[dependency_name]
                          if requirement.specifier.contains(candidate.version, prereleases=True)]
            if not candidates:
                from ci.build.dependency_wheels import locked_wheels
                if (p.path / 'uv.lock').exists() and any(
                        pin.name == dependency_name and str(requirement.specifier) == '==' + pin.version
                        for pin in locked_wheels(root, p.path)):
                    continue
                raise ValueError(f"unsatisfied local dependency: {p.distribution}: {raw}")
            if len(candidates) != 1:
                raise ValueError(f"ambiguous local dependency; pin exact version: {p.distribution}: {raw}")
            child = candidates[0]
            if dependency_name in visiting:
                raise ValueError("local dependency cycle: " + " -> ".join([*visiting, dependency_name]))
            if dependency_name in found:
                if found[dependency_name] != child:
                    raise ValueError(f"local dependency version conflict: {dependency_name}")
                continue
            walk(child)
            found[dependency_name] = child
            ordered.append(child)
        visiting.pop()

    walk(project)
    return ordered


def readme_file(metadata):
    readme = metadata["project"].get("readme")
    return readme.get("file") if isinstance(readme, dict) else readme


def source_files(project):
    paths = [project.path / n for n in ("pyproject.toml", "uv.lock", "Dockerfile", "main.py", "operator.yaml")]
    readme = readme_file(tomllib.loads((project.path / "pyproject.toml").read_text()))
    if readme:
        paths.append(project.path / readme)
    paths += sorted((project.path / "src").rglob("*"))
    return [p for p in paths if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]


def build_inputs(root, project, *, operator_catalog=None):
    from ci.plan.impact import build_tool_files
    root = Path(root)
    paths = [p for owner in [*dependencies(root, project, operator_catalog=operator_catalog), project] for p in source_files(owner)]
    paths.extend(root / path for path in build_tool_files(root))
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def wheel_paths(root, project):
    from ci.build.dependency_wheels import locked_wheels
    root = Path(root)
    requirements = tomllib.loads((project.path / "pyproject.toml").read_text())["project"]["dependencies"]
    sdk_pins = [r.split("==", 1)[1] for r in requirements if re.fullmatch(r"striding-operator-sdk==[0-9][A-Za-z0-9.]*", r)]
    if len(sdk_pins) != 1:
        raise ValueError("operator requires one exact SDK version")
    sdk = sdk_pins[0]
    names = [f"striding_operator_sdk-{sdk}-py3-none-any.whl"]
    if (project.path / 'uv.lock').exists():
        names.extend(pin.filename for pin in locked_wheels(root, project.path))
    names.extend(p.wheel for p in [*dependencies(root, project), project])
    return [root / 'build/wheels' / name for name in dict.fromkeys(names)]


def import_modules(project):
    """Top-level imports owned by a package, including declared vendored roots."""
    raw=tomllib.loads((project.path/'pyproject.toml').read_text())
    roots=raw.get('tool',{}).get('operator-ci',{}).get('import-roots',[])
    if not isinstance(roots,list) or any(not isinstance(path,str) for path in roots):
        raise ValueError('import roots must be a list of packaged directories')
    packages=[(project.path/path).resolve() for path in raw['tool']['hatch']['build']['targets']['wheel']['packages']]
    modules=set(project.modules)
    for value in roots:
        relative=Path(value);path=project.path/relative;resolved=path.resolve()
        if (relative.is_absolute() or '..' in relative.parts or not path.is_dir() or path.is_symlink()
                or not resolved.is_relative_to(project.path.resolve())
                or not any(resolved.is_relative_to(package) for package in packages)):
            raise ValueError(f'invalid packaged import root: {project.path}: {value}')
        for member in path.iterdir():
            if member.is_symlink():
                raise ValueError(f'symlink in packaged import root: {member}')
            if member.is_dir() and (member/'__init__.py').is_file() and member.name.isidentifier():
                modules.add(member.name)
            elif member.is_file() and member.suffix=='.py' and member.stem.isidentifier():
                modules.add(member.stem)
    return modules


def boundary_errors(root):
    """Reject reverse and sibling imports, including literal dynamic import targets."""
    root = Path(root)
    shared, operators = shared_projects(root), operator_projects(root)
    metadata = tomllib.loads((root/'pyproject.toml').read_text()) if (root/'pyproject.toml').exists() else {}
    external = metadata.get('tool',{}).get('operator-ci',{}).get('external-imports',{})
    if not isinstance(external,dict) or any(not k.isidentifier() or not isinstance(v,str) for k,v in external.items()):
        raise ValueError('external import ownership must map module to distribution')
    errors = []
    if (root / "src").exists():
        errors.append("root src is not an implementation owner")
    owned={p:import_modules(p) for p in [*shared,*operators]}
    private = set().union(*(owned[p] for p in operators))
    local = set().union(*owned.values())
    for project in [*shared, *operators]:
        allowed = set().union(*(owned[p] for p in [project,*dependencies(root,project)]))
        if (project.path/'uv.lock').exists():
            from ci.build.dependency_wheels import locked_wheels
            pinned={pin.name for pin in locked_wheels(root,project.path)}
            for owner in shared:
                if normalized_name(owner.distribution) in pinned:
                    allowed.update(owned[owner])
            allowed.update(module for module,distribution in external.items() if normalized_name(distribution) in pinned)
        for path in (project.path / "src").rglob("*.py"):
            module = ".".join(path.relative_to(project.path / "src").with_suffix("").parts)
            package = module.rsplit(".", 1)[0]
            tree = ast.parse(path.read_text())
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports += [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imports.append(resolve_name("." * node.level + (node.module or ""), package)
                                   if node.level else (node.module or ""))
                elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    func = ast.unparse(node.func)
                    if func.endswith("import_module") or func == "__import__":
                        imports.append(node.args[0].value)
            for name in imports:
                top = name.split(".")[0]
                if top in {"ci", "tests", "tools"} or (top in private and top not in owned[project]):
                    errors.append(f"{path.relative_to(root)}: forbidden dependency {name}")
                if top in local and top not in allowed:
                    errors.append(f"{path.relative_to(root)}: undeclared local dependency {name}")
    return errors
