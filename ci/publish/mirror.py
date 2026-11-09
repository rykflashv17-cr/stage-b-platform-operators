"""Copy accepted Harbor manifests to Beijing ACR without rebuilding images."""
from ci import ROOT
import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from ci.publish.tags import publication_tag
from ci.plan.projects import operator_projects

HARBOR = "harbor.striding.internal"
ACR = "registry-dp-registry.cn-beijing.cr.aliyuncs.com"
DIGEST = r"sha256:[a-f0-9]{64}"


@dataclass(frozen=True)
class ImageMirror:
    code: str
    tag: str
    digest: str

    @property
    def source(self):
        return f"{HARBOR}/data/data-operators/{self.code}@{self.digest}"

    @property
    def source_tag(self):
        return self.source.rsplit("@", 1)[0] + ":" + self.tag

    @property
    def target(self):
        return f"{ACR}/data-operators/{self.code}:{self.tag}"

    def reference(self, host):
        return f"{host}/data-operators/{self.code}@{self.digest}"


def plan_from_receipt(receipt, commit, tags, *, allow_empty=False):
    if not re.fullmatch(r"[a-f0-9]{40,64}", commit) or receipt["commit"] != commit:
        raise ValueError("Harbor receipt does not match pipeline commit")
    expected = {f"{HARBOR}/data/data-operators/{key.split('/v', 1)[0]}:{tag}": key for key, tag in tags.items()}
    if (not expected and not allow_empty) or set(receipt["images"]) != set(expected):
        raise ValueError("incomplete or unexpected Harbor tags")
    items = []
    for tag, key in expected.items():
        code = key.split("/v", 1)[0]
        row = receipt["images"][tag]
        repository, _, digest = row["reference"].rpartition("@")
        if repository != tag.rsplit(":", 1)[0] or not re.fullmatch(DIGEST, digest) or not re.fullmatch(DIGEST, row["image_id"]):
            raise ValueError(f"{code}: invalid accepted Harbor reference")
        items.append(ImageMirror(code, tags[key], digest))
    return items


def checked(raw, digest, ref):
    if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"{ref}: manifest digest mismatch")
    return raw


def registry_error(stderr):
    """Return only recognizable registry status codes, never free-form authentication data."""
    error = stderr.decode(errors="replace").lower()
    for pattern in (r"\b(401|403|unauthorized|forbidden|denied)\b",
                    r"\b(400|408|409|429|5[0-9]{2}|timeout|no such host|x509|connection refused)\b",
                    r"\b(404|name[_ ]unknown|manifest[_ ]unknown)\b"):
        match = re.search(pattern, error)
        if match:
            return match[0]
    return "not found" if error.rstrip().endswith(": not found") else "unclassified"


class Oras:
    def __init__(self, config):
        self.config = str(config)

    def fetch(self, ref, *, missing_ok=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest"
            res = subprocess.run(["oras", "manifest", "fetch", "--registry-config", self.config,
                                  "--output", str(path), ref], capture_output=True, timeout=90)
            if res.returncode:
                status = registry_error(res.stderr)
                if missing_ok and status in {"404", "not found", "name_unknown", "name unknown",
                                             "manifest_unknown", "manifest unknown"}:
                    return None
                raise RuntimeError(f"unable to fetch manifest {ref} ({status}, ORAS exit {res.returncode})")
            return path.read_bytes()

    def copy(self, source, target):
        res = subprocess.run(["oras", "cp", "--from-registry-config", self.config,
                              "--to-registry-config", self.config, source, target],
                             capture_output=True, timeout=900)
        if res.returncode:
            raise RuntimeError(f"unable to copy {source} to {target} ({registry_error(res.stderr)}, ORAS exit {res.returncode})")


def mirror(items, registry, *, evidence_dir=None):
    from collections import Counter
    counts = Counter(item.code for item in items)
    def evidence_key(item):
        return item.code if counts[item.code] == 1 else item.code + "/" + item.tag
    pending = []
    for item in items:
        checked(registry.fetch(item.source), item.digest, item.source)
        checked(registry.fetch(item.source_tag), item.digest, item.source_tag)
        existing = registry.fetch(item.target, missing_ok=True)
        if existing is None:
            pending.append(item)
        else:
            checked(existing, item.digest, item.target)
    for item in pending:
        try:
            registry.copy(item.source, item.target)
        except RuntimeError:
            # A concurrent/idempotent create may have completed before the error.
            existing = registry.fetch(item.target, missing_ok=True)
            if existing is None:
                raise
            checked(existing, item.digest, item.target)
    results = {}
    for item in items:
        for endpoint, ref in [("harbor", item.source_tag), ("acr-public", item.target)]:
            manifest = checked(registry.fetch(ref), item.digest, ref)
            if evidence_dir is not None:
                directory = evidence_dir / evidence_key(item)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"{endpoint}.json").write_bytes(manifest)
        results[evidence_key(item)] = {"tag": item.tag, "source_reference": item.source,
                              "public_reference": item.reference(ACR)}
    return results


def write_registry_config(path):
    auths = {}
    for prefix, hosts in [("DP_HARBOR", [HARBOR]), ("DP_ALIYUN_REGISTRY", [ACR])]:
        user = base64.b64decode(os.environ[prefix + "_USERNAME_B64"], validate=True)
        password = os.environ[prefix + "_PASSWORD"].encode()
        if not user or b":" in user or not password or any(c in user + password for c in (b"\0", b"\n", b"\r")):
            raise ValueError(f"invalid {prefix} CI credentials")
        encoded = base64.b64encode(user + b":" + password).decode()
        for host in hosts:
            auths[host] = {"auth": encoded}
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"auths": auths}, f)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--registry-config", required=True, type=Path)
    parser.add_argument("--prepare-config", action="store_true")
    parser.add_argument("--receipt", type=Path, default=Path("build/harbor-release.json"))
    parser.add_argument("--output", type=Path, default=Path("build/acr-mirror.json"))
    args = parser.parse_args()
    if args.prepare_config:
        os.umask(0o077)
        write_registry_config(args.registry_config)
        return
    if os.environ.get("CI_COMMIT_REF_PROTECTED") != "true":
        parser.error("protected publication required")
    commit = os.environ["CI_COMMIT_SHA"]
    branch = os.environ["CI_COMMIT_BRANCH"]
    pipeline = os.environ["CI_PIPELINE_ID"]
    root = ROOT
    projects = operator_projects(root)
    if args.plan:
        from ci.plan.images import load_plan, shipping_keys
        plan = load_plan(args.plan, root, commit=commit)
        projects = [p for p in projects if f"{p.name}/v{p.version}" in shipping_keys(plan, branch)]
    tags = {(f"{p.name}/v{p.version}" if args.plan else p.name): publication_tag(p.version, branch, pipeline) for p in projects}
    items = plan_from_receipt(json.loads(args.receipt.read_text()), commit, tags, allow_empty=bool(args.plan))
    images = mirror(items, Oras(args.registry_config), evidence_dir=args.output.parent / "manifests")
    report = {"commit": commit, "images": images}
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
