"""Resolve the exact SDK artifact before any environment or lock is changed."""
import hashlib
from importlib.util import find_spec
import os
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from zipfile import ZipFile

NAME = "striding-operator-sdk"
INDEX = "https://nexus.striding.internal/repository/pypi/simple/"


@dataclass(frozen=True)
class SDKPin:
    version: str
    filename: str
    digest: str


def locked_sdk(root, directory):
    project = tomllib.loads((directory / "pyproject.toml").read_text())
    pins = [m[1] for dep in project["project"]["dependencies"]
            if (m := re.fullmatch(r"striding-operator-sdk==([0-9][A-Za-z0-9.]*)", dep))]
    if len(pins) != 1:
        raise ValueError(f"{directory}: expected one exact SDK pin")
    version = pins[0]
    filename = f"striding_operator_sdk-{version}-py3-none-any.whl"
    expected = (root / "build/wheels" / filename).resolve()
    if (directory / project["tool"]["uv"]["sources"][NAME]["path"]).resolve() != expected:
        raise ValueError(f"{directory}: SDK source path mismatch")
    lock = tomllib.loads((directory / "uv.lock").read_text())
    packages = [p for p in lock["package"] if p["name"] == NAME and p["version"] == version]
    if len(packages) != 1 or (directory / packages[0]["source"]["path"]).resolve() != expected:
        raise ValueError(f"{directory}: missing exact SDK lock")
    records = [w["hash"] for w in packages[0]["wheels"] if w.get("filename") == filename]
    if len(records) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", records[0]):
        raise ValueError(f"{directory}: missing exact SDK wheel digest")
    return SDKPin(version, filename, records[0].removeprefix("sha256:"))


def verify(path, pin):
    if path.name != pin.filename:
        raise ValueError("SDK wheel filename differs from lock")
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != pin.digest:
        raise ValueError("SDK wheel digest differs from lock")
    with ZipFile(path) as archive:
        metadata = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("invalid SDK wheel metadata")
        package = BytesParser().parsebytes(archive.read(metadata[0]))
    if package["Name"] != NAME or package["Version"] != pin.version:
        raise ValueError("SDK wheel package name or version differs from lock")


def download_from_nexus(pin, directory):
    requirements = directory / "sdk-requirement.txt"
    requirements.write_text(f"{getattr(pin, 'name', NAME)}=={pin.version} --hash=sha256:{pin.digest}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    pip = ([sys.executable, "-m", "pip"] if find_spec("pip") is not None else
           ["uv", "tool", "run", "--python", sys.executable, "--from", "pip==26.2.1",
            "--default-index", INDEX, "pip"])
    subprocess.run([*pip, "--disable-pip-version-check", "--no-input", "download", "--index-url", INDEX,
                    "--no-deps", "--only-binary=:all:", "--require-hashes", "--dest", str(directory),
                    "-r", str(requirements)], check=True, env=env)


def prepare_sdk(root, pin, *, wheel=None, download=download_from_nexus):
    directory = root / "build/wheels"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / pin.filename
    if wheel is not None:
        verify(Path(wheel), pin)
    if target.exists() or target.is_symlink():
        verify(target, pin)
        return target
    with tempfile.TemporaryDirectory(dir=directory) as scratch:
        staged = Path(scratch) / pin.filename
        if wheel is None:
            download(pin, Path(scratch))
        else:
            shutil.copyfile(wheel, staged)
        verify(staged, pin)
        staged.replace(target)
    return target
