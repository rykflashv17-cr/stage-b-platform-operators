"""Regression checks for SDK acquisition and immutable Harbor release refs."""
import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from ci.build.sdk_wheel import locked_sdk, prepare_sdk
from ci.publish.images import publish, release_target, remote_artifact

ROOT = Path(__file__).resolve().parents[2]


def checkout(tmp):
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(ROOT / name, tmp / name)
    from tests.ci.helpers import sdk_fixture
    return sdk_fixture(tmp, tmp)[0]


def test_fetch_once_then_read_cache_without_network(tmp_path):
    pin = checkout(tmp_path)
    cached = tmp_path / "fixture-download" / pin.filename
    calls = []
    def download(pin, directory):
        calls.append(pin.filename)
        shutil.copyfile(cached, directory / pin.filename)
    wheel = prepare_sdk(tmp_path, pin, download=download)
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == pin.digest
    assert prepare_sdk(tmp_path, pin, download=download) == wheel
    assert calls == [pin.filename]


def test_bad_download_never_populates_cache(tmp_path):
    pin = checkout(tmp_path)
    def download(pin, directory):
        (directory / pin.filename).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="digest"):
        prepare_sdk(tmp_path, pin, download=download)
    assert not (tmp_path / "build/wheels" / pin.filename).exists()


def test_tampered_cache_fails_instead_of_refreshing_lock(tmp_path):
    pin = checkout(tmp_path)
    wheel = tmp_path / "build/wheels" / pin.filename
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"tampered")
    before = (tmp_path / "uv.lock").read_bytes()
    with pytest.raises(ValueError, match="digest"):
        prepare_sdk(tmp_path, pin, download=lambda *_: pytest.fail("unexpected download"))
    assert (tmp_path / "uv.lock").read_bytes() == before


def test_explicit_wheel_must_have_locked_identity(tmp_path):
    pin = checkout(tmp_path)
    wrong = tmp_path / "other.whl"
    shutil.copyfile(tmp_path / "fixture-download" / pin.filename, wrong)
    with pytest.raises(ValueError, match="filename"):
        prepare_sdk(tmp_path, pin, wheel=wrong)


def test_release_uses_declared_harbor_repository_and_version():
    assert release_target("test_operator", "2.0.0", "harbor.striding.internal/data/data-operators/test_operator:2.0.0") == "harbor.striding.internal/data/data-operators/test_operator:2.0.0"
    for ref in ["evil.invalid/data/test_operator:2.0.0", "harbor.striding.internal/data/data-operators/test_operator:latest"]:
        with pytest.raises(ValueError):
            release_target("test_operator", "2.0.0", ref)


def test_conflicting_existing_tag_blocks_all_pushes():
    plan = {"first:1": "image-a", "second:1": "image-b"}
    def lookup(tag):
        return {"image_id": "different"} if tag == "second:1" else None
    with pytest.raises(ValueError, match="overwrite"):
        publish(plan, lookup=lookup, run=lambda *_a, **_kw: pytest.fail("unexpected push"))


def test_retry_reuses_already_pushed_image_and_reads_remote_digest():
    plan = {"first:1": "image-a", "second:1": "image-b"}
    registry = {"first:1": {"image_id": "image-a", "reference": "first@digest"}}
    pushed = []
    def run(args, **kwargs):
        if args[1] == "push":
            tag = args[2]
            pushed.append(tag)
            registry[tag] = {"image_id": plan[tag], "reference": tag.split(":")[0] + "@digest"}
    assert publish(plan, lookup=registry.get, run=run)["second:1"]["reference"] == "second@digest"
    assert publish(plan, lookup=registry.get, run=run) == registry
    assert pushed == ["second:1"]

@pytest.mark.parametrize("missing", [True, False])
def test_first_harbor_candidate_vs_permission_error(monkeypatch, missing):
    tag = "harbor.striding.internal/data/data-operators/unit:candidate-test"
    error = f"failed to resolve reference: {tag}: not found" if missing else "unauthorized: access denied"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1, "", error))
    if missing:
        assert remote_artifact(tag) is None
    else:
        with pytest.raises(RuntimeError, match="denied"):
            remote_artifact(tag)
