"""Prepare binary-only dependencies for the exact Python environment in the target runtime."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from threading import Lock
from urllib.parse import urlsplit, unquote
from urllib.request import HTTPRedirectHandler, build_opener

from packaging.markers import Marker
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from ci.build.bases import PLATFORM
from ci.build.downloads import nexus_request, urlopen

_locks_guard = Lock()
_download_locks = {}


@lru_cache(maxsize=32)
def probe_target(image_id):
    script = """
import json
from pip._vendor.packaging.markers import default_environment
from pip._vendor.packaging.tags import sys_tags
print(json.dumps(dict(markers=default_environment(), tags=[str(t) for t in sys_tags()])))
"""
    target = json.loads(subprocess.check_output(
        ["docker", "run", "--rm", "--pull=never", "--network", "none", "--platform", PLATFORM,
         "--entrypoint", "python", image_id, "-I", "-c", script], text=True))
    env = target["markers"]
    if (env["sys_platform"], env["platform_machine"], env["implementation_name"], env["python_version"]) != (
            "linux", "x86_64", "cpython", "3.12"):
        raise ValueError(f"expected {PLATFORM} CPython 3.12 runtime, got {env}")
    return target


def export_lock(directory):
    directory = Path(directory)
    return _export_lock(str(directory.resolve()), digest(directory / "uv.lock"), digest(directory / "pyproject.toml"))


@lru_cache(maxsize=128)
def _export_lock(directory, lock_hash, project_hash):
    output = subprocess.check_output(
        ["uv", "export", "--format", "pylock.toml", "--frozen", "--offline", "--no-default-groups",
         "--no-emit-project", "--no-header", "--project", str(directory)], text=True)
    return tomllib.loads(output)


def https_url(value):
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None:
        raise ValueError("expected a credential-free HTTPS wheel URL")
    return url


class HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def filename(record):
    if ("url" in record) == ("path" in record):
        raise ValueError("expected one wheel location in the lock")
    if "url" in record:
        name = unquote(https_url(record["url"]).path.rsplit("/", 1)[-1])
    else:
        name = Path(record["path"]).name
    if name != Path(name).name or not name.endswith(".whl"):
        raise ValueError("lock must provide a wheel")
    return name


def locked_digest(record):
    value = record.get("hashes", {}).get("sha256", "")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("expected a locked wheel sha256")
    return value


def select_wheels(lock, target):
    env = target["markers"]
    ranks = {tag: i for i, tag in enumerate(target["tags"])}
    if not SpecifierSet(lock.get("requires-python", "")).contains(env["python_full_version"]):
        raise ValueError("operator lock does not support the target Python")
    selected = set()
    for row in lock.get("packages", []):
        if "marker" in row and not Marker(row["marker"]).evaluate(environment=env):
            continue
        name, version = row["name"], row["version"]
        if name in selected:
            raise ValueError(f"ambiguous target lock for {name}")
        selected.add(name)
        records = [row["archive"]] if "archive" in row else row.get("wheels", [])
        choices = []
        for record in records:
            path = filename(record)
            dist, ver, _, wheel_tags = parse_wheel_filename(path)
            if dist != canonicalize_name(name) or ver != Version(version):
                raise ValueError(f"{name}=={version}: wrong wheel identity {path}")
            matching = [ranks[str(t)] for t in wheel_tags if str(t) in ranks]
            if matching:
                choices.append((min(matching), path, record))
        if not choices:
            raise ValueError(f"{name}=={version}: no locked wheel for {PLATFORM} Python {env['python_full_version']}")
        _, _, artifact = min(choices, key=lambda choice: choice[:2])
        locked_digest(artifact)
        yield name, version, artifact


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def obtain(record, cache):
    expected = locked_digest(record)
    target = Path(cache) / expected / filename(record)
    with _locks_guard:
        lock = _download_locks.setdefault(str(target.resolve()), Lock())
    with lock:
        return _obtain(record, target, expected)


def _obtain(record, target, expected):
    if target.is_file():
        if digest(target) != expected:
            raise ValueError(f"{target.name}: cached sha256 differs from lock")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        staged = Path(temp) / target.name
        with urlopen(nexus_request(record["url"]), timeout=120) as src, staged.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        if digest(staged) != expected:
            raise ValueError(f"{target.name}: downloaded sha256 differs from lock")
        staged.replace(target)
    return target


def prepare(project, owned_wheels, target, destination, cache):
    local = {p.name: p for p in owned_wheels}
    lock = export_lock(project.path)
    selected = list(select_wheels(lock, target))
    records = {locked_digest(row): row for _, _, row in selected if "url" in row}
    with ThreadPoolExecutor(max_workers=4) as pool:
        downloaded = dict(zip(records, pool.map(lambda row: obtain(row, cache), records.values())))
    artifacts = [(project.distribution, project.version, local[project.wheel], None)]
    for name, version, record in selected:
        if "path" in record:
            wheel = local.get(Path(record["path"]).name)
            if (wheel is None or (project.path / record["path"]).resolve() != wheel.resolve()):
                raise ValueError(f"{name}=={version}: local wheel differs from lock")
        else:
            wheel = downloaded[locked_digest(record)]
        artifacts.append((name, version, wheel, locked_digest(record)))

    directory = destination / "wheelhouse"
    groups = {"dependencies": [], "projects": []}
    for group in groups:
        (directory / group).mkdir(parents=True)
    project_wheels = {p.name for p in owned_wheels[1:]}
    hashes, requirements = {}, []
    for name, version, path, expected in sorted(artifacts):
        wheel_name, wheel_version, _, _ = parse_wheel_filename(path.name)
        if wheel_name != canonicalize_name(name) or wheel_version != Version(version):
            raise ValueError(f"{name}=={version}: wrong wheel identity {path.name}")
        group = "projects" if path.name in project_wheels else "dependencies"
        target_path = directory / group / path.name
        shutil.copyfile(path, target_path)
        actual = digest(target_path)
        if expected is not None and actual != expected:
            raise ValueError(f"{name}=={version}: staged wheel sha256 differs from lock")
        hashes[f"wheelhouse/{group}/{path.name}"] = actual
        requirement = f"{name}=={version} --hash=sha256:{actual}"
        requirements.append(requirement)
        groups[group].append(requirement)
    for group, lines in groups.items():
        install = destination / f"requirements.{group}.txt"
        install.write_text("\n".join(lines) + "\n")
        hashes[install.name] = digest(install)
    install = destination / "requirements.install.txt"
    install.write_text("\n".join(requirements) + "\n")
    hashes[install.name] = digest(install)
    return hashes
