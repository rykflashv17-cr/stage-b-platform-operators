"""Nexus-only initial requests; never log credentials or forward them cross-origin."""
import base64
import netrc
import os
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

NEXUS_HOST = "nexus.striding.internal"
INDEX = f"https://{NEXUS_HOST}/repository/pypi/simple/"


def https_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("expected credential-free HTTPS URL")
    return parts


class NexusRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = https_url(req.full_url), https_url(newurl)
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (old.hostname, old.port or 443) != (new.hostname, new.port or 443):
            result.remove_header("Authorization")
            result.remove_header("Cookie")
        return result


def nexus_request(url, *, netrc_file=None):
    parts = https_url(url)
    if parts.hostname != NEXUS_HOST or parts.port not in {None, 443}:
        raise ValueError("initial dependency downloads must use Nexus")
    request = Request(url)
    path = netrc_file or os.environ.get("NETRC")
    try:
        auth = netrc.netrc(path).authenticators(parts.hostname)
    except FileNotFoundError:
        auth = None
    if auth:
        login, _, password = auth
        token = base64.b64encode(f"{login}:{password}".encode()).decode()
        request.add_header("Authorization", "Basic " + token)
    return request


urlopen = build_opener(NexusRedirect()).open
