import hashlib
from pathlib import Path
from packaging.markers import default_environment
import pytest


def target():
    return {"markers": {**default_environment(), "python_version": "3.12", "python_full_version": "3.12.14",
                        "implementation_name": "cpython", "platform_machine": "x86_64", "sys_platform": "linux"},
            "tags": ["cp312-cp312-manylinux_2_17_x86_64", "py3-none-any"]}


def wheel(tag, digest="a" * 64):
    return dict(url=f"https://nexus.striding.internal/repository/pypi/packages/abc/pkg-1.0-{tag}.whl", hashes={"sha256": digest})


def test_selects_only_runtime_marker_and_first_compatible_locked_wheel():
    from ci.build.wheelhouse import select_wheels
    picked = wheel(target()["tags"][0])
    lock = {"requires-python": ">=3.12", "packages": [
        {"name": "win", "version": "1", "marker": "sys_platform == 'win32'", "wheels": []},
        {"name": "pkg", "version": "1.0", "marker": "python_full_version >= '3.12.12'",
         "wheels": [wheel("cp312-cp312-win_amd64"), wheel("py3-none-any"), picked]}]}
    assert list(select_wheels(lock, target())) == [("pkg", "1.0", picked)]


def test_missing_binary_reports_name_version_and_platform():
    from ci.build.wheelhouse import select_wheels
    lock = {"packages": [{"name": "pkg", "version": "1.0", "sdist": {},
                          "wheels": [wheel("cp312-cp312-win_amd64")]}]}
    with pytest.raises(ValueError, match=r"pkg==1.0.*linux/amd64"):
        list(select_wheels(lock, target()))


def test_lock_export_uses_runtime_dependency_closure():
    from ci.build.wheelhouse import export_lock
    directory = Path(__file__).resolve().parents[2] / "operators/mcap_read_normalize"
    lock = export_lock(directory)
    names = {row["name"] for row in lock["packages"]}
    assert {"grpcio", "jsonschema-specifications", "pyarrow", "striding-operator-sdk"} <= names
    assert "pytest" not in names and "striding-operator-mcap-read-normalize" not in names


def test_corrupt_cached_or_fetched_bytes_fail_hash_check(tmp_path, monkeypatch):
    from ci.build import wheelhouse
    from io import BytesIO
    artifact = wheel("py3-none-any", hashlib.sha256(b"correct").hexdigest())
    monkeypatch.setattr(wheelhouse, "urlopen", lambda url, timeout: BytesIO(b"incorrect"))
    with pytest.raises(ValueError, match="sha256"):
        wheelhouse.obtain(artifact, tmp_path)
    assert list(tmp_path.rglob("*.whl")) == []


def test_lock_hash_is_required_and_must_be_sha256():
    from ci.build.wheelhouse import select_wheels
    lock = {"packages": [{"name": "pkg", "version": "1.0", "wheels": [wheel("py3-none-any", "")]}]}
    with pytest.raises(ValueError, match="sha256"):
        list(select_wheels(lock, target()))


@pytest.mark.parametrize("record, message", [
    ({"path": "/tmp/pkg-1.0-py3-none-any.whl", "hashes": {"sha256": "../escape"}}, "sha256"),
    ({"url": "ftp://bad/pkg-1.0-py3-none-any.whl", "hashes": {"sha256": "a"*64}}, "HTTPS"),
    ({"url": "https://bad/pkg-1.0.tar.gz", "hashes": {"sha256": "a"*64}}, "wheel"),
    ({"url": "https://bad/pkg-1.0-cp310-cp310-win_amd64.whl", "hashes": {"sha256": "a"*64}}, "linux/amd64"),
    ({"path": "../pkg-1.0-cp310-cp310-win_amd64.whl", "hashes": {"sha256": "a"*64}}, "linux/amd64"),
    ({"path": "../other-1.0-py3-none-any.whl", "hashes": {"sha256": "a"*64}}, "identity"),
    ({"hashes": {"sha256": "a"*64}}, "location"),
])
def test_direct_archive_lock_is_validated_before_access(record, message):
    from ci.build.wheelhouse import select_wheels
    lock = {"packages": [{"name": "pkg", "version": "1.0", "archive": record}]}
    with pytest.raises(ValueError, match=message):
        list(select_wheels(lock, target()))


def test_obtain_cannot_use_untrusted_digest_as_a_cache_path(tmp_path):
    from ci.build.wheelhouse import obtain
    with pytest.raises(ValueError, match="sha256"):
        obtain(wheel("py3-none-any", "../escape"), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_redirect_cannot_downgrade_https():
    from ci.build.wheelhouse import HTTPSRedirect
    from urllib.request import Request
    with pytest.raises(ValueError, match="HTTPS"):
        HTTPSRedirect().redirect_request(Request("https://example.org/old"), None, 302, "Found", {},
                                        "http://example.org/wheel")
