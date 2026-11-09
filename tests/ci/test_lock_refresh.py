import base64
import hashlib
import subprocess
import tomllib
from zipfile import ZipFile

from ci.build.packages import refresh_local_lock
from ci.build.wheel_metadata import verify_wheel_lock


def write_wheel(directory, name, version, payload):
    module = name.replace("-", "_")
    stem = f"{module}-{version}"
    path = directory / f"{stem}-py3-none-any.whl"
    with ZipFile(path, "w") as archive:
        archive.writestr(f"{module}/__init__.py", payload)
        archive.writestr(
            f"{stem}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        archive.writestr(
            f"{stem}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        rows = []
        for entry in archive.namelist():
            data = archive.read(entry)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            rows.append(f"{entry},sha256={digest},{len(data)}")
        record = f"{stem}.dist-info/RECORD"
        archive.writestr(record, "\n".join([*rows, f"{record},,"]) + "\n")
    return path


def test_rebuilt_local_wheels_refresh_lock_hashes_without_dependency_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    registry = tmp_path / "registry"
    registry.mkdir()
    write_wheel(registry, "registry-dep", "1.0.0", "")
    local = write_wheel(tmp_path, "local-dep", "1.0.0", "before = True")
    (tmp_path / "pyproject.toml").write_text(f"""\
[project]
name = "lock-consumer"
version = "1.0.0"
requires-python = ">=3.12,<3.13"
dependencies = ["local-dep==1.0.0", "registry-dep>=1"]

[tool.uv]
no-index = true
find-links = ["registry"]

[tool.uv.sources]
local-dep = {{ path = "{local.name}" }}
""")
    lock = tmp_path / "uv.lock"
    subprocess.run(["uv", "lock", "--offline"], cwd=tmp_path, check=True)
    before = tomllib.loads(lock.read_text())

    # Only the local wheel should change even if an unrelated upgrade is available.
    write_wheel(registry, "registry-dep", "2.0.0", "")
    write_wheel(tmp_path, "local-dep", "1.0.0", "after = True")
    refresh_local_lock(tmp_path)
    after = tomllib.loads(lock.read_text())
    assert {p["name"]: p["version"] for p in before["package"]} == {
        p["name"]: p["version"] for p in after["package"]
    }
    verify_wheel_lock(local, after, "local-dep", "1.0.0")
