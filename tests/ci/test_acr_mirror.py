"""ACR must serve the same manifest that Harbor accepted, including after retries."""
import base64
import hashlib
import json
import subprocess

import pytest

from ci.publish import mirror as mirror_images
from ci.publish.mirror import ImageMirror, mirror, plan_from_receipt, write_registry_config

COMMIT = "a" * 40
CODE = "mcap_read_normalize"
TAG = "2.0.0"
RAW = b'{"schemaVersion":2}'
DIGEST = "sha256:" + hashlib.sha256(RAW).hexdigest()
HARBOR = "harbor.striding.internal/data/data-operators/" + CODE
ACR = "registry-dp-registry.cn-beijing.cr.aliyuncs.com/data-operators/" + CODE


class Registry:
    def __init__(self):
        self.values = {HARBOR + "@" + DIGEST: RAW, HARBOR + ":" + TAG: RAW}
        self.copies = []

    def fetch(self, ref, *, missing_ok=False):
        assert ref.startswith((HARBOR, ACR)), "unexpected registry endpoint"
        if ref not in self.values and not missing_ok:
            raise RuntimeError("missing manifest")
        return self.values.get(ref)

    def copy(self, source, target):
        self.copies.append((source, target))
        self.values[target] = self.values[source]


def item():
    return ImageMirror(CODE, TAG, DIGEST)


def test_public_only_mirror_and_retry_does_not_copy():
    reg = Registry()
    report = mirror([item()], reg)
    assert mirror([item()], reg) == report
    assert reg.copies == [(HARBOR + "@" + DIGEST, ACR + ":" + TAG)]
    assert report[CODE] == {"tag": TAG, "source_reference": HARBOR + "@" + DIGEST,
                            "public_reference": ACR + "@" + DIGEST}


@pytest.mark.parametrize("endpoint", ["harbor", "public"])
def test_wrong_bytes_fail_closed(endpoint):
    reg = Registry()
    refs = {"harbor": HARBOR + "@" + DIGEST, "public": ACR + ":" + TAG}
    reg.values[refs[endpoint]] = b"different"
    with pytest.raises(ValueError, match="digest"):
        mirror([item()], reg)
    assert reg.copies == []


@pytest.mark.parametrize("fault", ["changed", "missing"])
def test_public_readback_after_copy_is_required(fault):
    class BadDestination(Registry):
        def copy(self, source, target):
            super().copy(source, target)
            if fault == "changed":
                self.values[target] = b"different after copy"
            else:
                del self.values[target]
    reg = BadDestination()
    with pytest.raises((ValueError, RuntimeError), match="digest|missing"):
        mirror([item()], reg)
    assert reg.copies == [(HARBOR + "@" + DIGEST, ACR + ":" + TAG)]


def test_receipt_is_bound_to_commit_all_registered_codes_and_tags():
    row = {"image_id": "sha256:"+"b"*64, "reference": HARBOR+"@"+DIGEST}
    receipt = {"commit": COMMIT, "images": {HARBOR+":"+TAG: row}}
    assert plan_from_receipt(receipt, COMMIT, {CODE: TAG}) == [item()]
    for bad in [{"commit": "c"*40, "images": receipt["images"]},
                {"commit": COMMIT, "images": {}},
                {"commit": COMMIT, "images": {HARBOR+":latest": row}},
                {"commit": COMMIT, "images": {HARBOR+":"+TAG: {**row,"reference":"other.invalid/repo@"+DIGEST}}}]:
        with pytest.raises(ValueError):
            plan_from_receipt(bad, COMMIT, {CODE: TAG})


def test_copy_error_only_succeeds_if_matching_destination_is_readable():
    class LostResponse(Registry):
        def copy(self, source, target):
            super().copy(source, target)
            raise RuntimeError("copy response lost")
    reg = LostResponse()
    assert mirror([item()], reg)[CODE]["source_reference"] == HARBOR + "@" + DIGEST


def test_existing_ci_credentials_are_scoped_to_the_expected_hosts(tmp_path, monkeypatch):
    for prefix, user in [("DP_HARBOR", b"robot$fixture"), ("DP_ALIYUN_REGISTRY", b"acr-fixture")]:
        monkeypatch.setenv(prefix + "_USERNAME_B64", base64.b64encode(user).decode())
        monkeypatch.setenv(prefix + "_PASSWORD", "test-only")
    config = tmp_path / "auth.json"
    write_registry_config(config)
    assert config.stat().st_mode & 0o077 == 0
    auth = json.loads(config.read_text())["auths"]
    assert {host: base64.b64decode(value["auth"]) for host, value in auth.items()} == {
        HARBOR.split("/")[0]: b"robot$fixture:test-only",
        ACR.split("/")[0]: b"acr-fixture:test-only",
    }


def test_archive_contains_bytes_read_from_each_registry(tmp_path):
    reg = Registry()
    mirror([item()], reg, evidence_dir=tmp_path)
    for endpoint in ("harbor", "acr-public"):
        assert (tmp_path / CODE / (endpoint + ".json")).read_bytes() == RAW
    assert {p.name for p in (tmp_path / CODE).iterdir()} == {"harbor.json", "acr-public.json"}


def test_changed_harbor_tag_is_rejected_before_copy():
    reg = Registry()
    reg.values[HARBOR + ":" + TAG] = b"changed after publication"
    with pytest.raises(ValueError, match="digest"):
        mirror([item()], reg)
    assert not reg.copies

@pytest.mark.parametrize("stderr,missing", [
    (b"NAME_UNKNOWN: repository name not known to registry", True),
    (b"401 Unauthorized: redaction-fixture: not found", False),
])
def test_acr_preflight_separates_missing_repo_from_denied(monkeypatch, stderr, missing):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, b"", stderr)
    monkeypatch.setattr(subprocess, "run", run)
    client = mirror_images.Oras("test-config")
    if missing:
        assert client.fetch(ACR + ":" + TAG, missing_ok=True) is None
    else:
        with pytest.raises(RuntimeError, match="401|unauthorized") as failure:
            client.fetch(ACR + ":" + TAG, missing_ok=True)
        assert "redaction-fixture" not in str(failure.value)
