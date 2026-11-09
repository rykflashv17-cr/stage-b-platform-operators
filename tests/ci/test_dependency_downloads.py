import base64
import hashlib
from io import BytesIO
from pathlib import Path
from urllib.request import Request
from urllib.error import HTTPError

import pytest

from ci.build.downloads import NexusRedirect, nexus_request
from ci.build import wheelhouse


def test_nexus_uses_netrc_without_embedding_credentials_in_urls(tmp_path):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine nexus.striding.internal login reader password private\n")
    request = nexus_request("https://nexus.striding.internal/repository/pypi/packages/pkg.whl", netrc_file=netrc)
    assert request.get_header("Authorization") == "Basic " + base64.b64encode(b"reader:private").decode()
    assert "private" not in request.full_url
    with pytest.raises(ValueError, match="Nexus"):
        nexus_request("https://files.pythonhosted.org/pkg.whl", netrc_file=netrc)


def test_redirect_does_not_forward_nexus_authentication_to_another_origin():
    handler = NexusRedirect()
    request = Request("https://nexus.striding.internal/repository/pypi/file.whl", headers={"Authorization": "secret"})
    redirected = handler.redirect_request(request, None, 302, "redirect", {}, "https://cdn.example/file.whl")
    assert not redirected.has_header("Authorization")
    with pytest.raises(ValueError, match="HTTPS"):
        handler.redirect_request(request, None, 302, "redirect", {}, "http://cdn.example/file.whl")


@pytest.mark.parametrize("code", [401, 403])
def test_denied_download_never_falls_back_or_leaves_a_cache_entry(tmp_path, monkeypatch, code):
    calls = []
    def denied(request, timeout):
        calls.append(request)
        raise HTTPError("https://nexus.striding.internal/", code, "denied", {}, None)
    monkeypatch.setattr(wheelhouse, "urlopen", denied)
    record = {"url": "https://nexus.striding.internal/pkg-1.0-py3-none-any.whl", "hashes": {"sha256": "a" * 64}}
    with pytest.raises(HTTPError):
        wheelhouse.obtain(record, tmp_path)
    assert len(calls) == 1
    assert not list(tmp_path.rglob("*.whl"))


def test_parallel_requests_for_one_digest_download_only_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import time
    calls = []
    data = b"locked-content"
    record = {"url": "https://nexus.striding.internal/pkg-1.0-py3-none-any.whl",
              "hashes": {"sha256": hashlib.sha256(data).hexdigest()}}
    def download(request, timeout):
        calls.append(request)
        time.sleep(.02)
        return BytesIO(data)
    monkeypatch.setattr(wheelhouse, "urlopen", download)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: wheelhouse.obtain(record, tmp_path), range(8)))
    assert len(calls) == 1
    assert all(path.read_bytes() == data for path in results)
