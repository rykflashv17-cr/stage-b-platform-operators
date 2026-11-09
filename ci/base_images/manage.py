"""Build, accept and publish independently versioned linux/amd64 foundations."""
from ci import ROOT
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote

from ci.base_images.config import BOOTSTRAP_REVISION, DIGEST, VERSION, bootstrap_reference, recipes

PLATFORM = "linux/amd64"
REVISION = "org.opencontainers.image.revision"
TREE = "internal.striding.foundation-recipe-tree"
PARENT = "internal.striding.foundation-parent"
PROJECT = "stridingai/data/data-platform-operators"
RECEIPT_TYPE = "application/vnd.striding.foundation.receipt.v1+json"
ANNOTATION = "internal.striding.foundation.receipt"


def docker(*args, stdin=None):
    subprocess.run(["docker", *map(str, args)], check=True, stdin=stdin, timeout=1800)


def inspect(ref):
    return json.loads(subprocess.check_output(["docker", "image", "inspect", ref]))[0]


def remote(tag):
    """Also used by the existing final operator image publisher."""
    result = subprocess.run(["docker", "pull", "--platform", PLATFORM, tag],
                            capture_output=True, text=True, timeout=900)
    if result.returncode:
        error = result.stderr.strip()
        repository = tag.rsplit(":", 1)[0].split("/", 1)[1]
        if (error.endswith(f"{tag}: not found") or error.endswith(f"repository {repository} not found")
                or error.endswith(f"artifact {tag.split('/', 1)[1]} not found")
                or re.search(r"manifest unknown|manifest .*not found", error, re.I)):
            return None
        raise RuntimeError(f"cannot pull {tag}: {error}")
    return inspect(tag)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def validate_ci(mode, revision, env):
    branch = {"publish": "develop", "verify": "main"}[mode]
    if (env.get("CI_PROJECT_PATH") != PROJECT or env.get("CI_COMMIT_BRANCH") != branch
            or env.get("CI_COMMIT_REF_PROTECTED") != "true"
            or env.get("CI_PIPELINE_SOURCE") in (None, "merge_request_event")
            or (mode == "publish" and env.get("CI_PIPELINE_SOURCE") != "push")
            or env.get("CI_COMMIT_SHA") != revision or not re.fullmatch("[0-9a-f]{40}", revision)
            or not re.fullmatch("[1-9][0-9]*", env.get("CI_PIPELINE_ID", ""))):
        raise ValueError(f"{mode} requires a protected {PROJECT} {branch} pipeline")
    return {"project": PROJECT, "branch": branch, "sourceCommit": revision, "pipelineId": env["CI_PIPELINE_ID"]}


class Harbor:
    """Read release receipts and preserve OCI index digests when tagging."""
    def __init__(self, repository, *, publish=False):
        self.repository = repository
        host, project, name = repository.split("/", 2)
        if host != "harbor.striding.internal" or project != "data":
            raise ValueError("unexpected foundation repository")
        self.path = "/api/v2.0/projects/data/repositories/" + quote(quote(name, safe=""), safe="")
        self.auth_header = ""
        if publish:
            user = base64.b64decode(os.environ["DP_HARBOR_USERNAME_B64"], validate=True)
            auth = base64.b64encode(user + b":" + os.environ["DP_HARBOR_PASSWORD"].encode()).decode()
            self.auth_header = "Authorization: Basic " + auth + "\n"

    def get(self, endpoint):
        # Use the runner's TLS trust store. Pass Basic auth via stdin only to
        # this exact HTTPS origin. Never follow redirects or read ~/.curlrc.
        url = "https://harbor.striding.internal" + self.path + endpoint
        raw = subprocess.run(["curl", "--disable", "--silent", "--show-error", "--proto", "=https",
                              "--max-time", "45", "--max-filesize", "1048576", "--header", "@-",
                              "--write-out", "\n%{http_code}", url], input=self.auth_header.encode(),
                             stdout=subprocess.PIPE, timeout=50, check=True).stdout
        body, status = raw.rsplit(b"\n", 1)
        if status == b"404" and any(e.get("code") == "NOT_FOUND" for e in json.loads(body).get("errors", [])):
            return None
        if status != b"200":
            raise RuntimeError(f"Harbor lookup {endpoint} failed: HTTP {status.decode()}")
        return json.loads(body)

    def oras(self, *args):
        command = [str(ROOT / "build/toolchain/oras"), *args]
        if self.auth_header:
            command.extend(["--registry-config", str(Path(os.environ["DOCKER_CONFIG"]) / "config.json")])
        return subprocess.check_output(command, timeout=180)

    def tags(self):
        if self.get("") is None:
            return []
        return self.oras("repo", "tags", self.repository).decode().splitlines()

    def artifact(self, tag):
        return self.get("/artifacts/" + quote(tag, safe="") + "?with_tag=false")

    def resolve(self, tag):
        doc = self.artifact(tag)
        if doc is None:
            return None
        if not re.fullmatch(DIGEST, doc["digest"]):
            raise ValueError(f"invalid manifest digest: {tag}")
        return doc["digest"]

    def check_tag(self, tag, digest):
        if self.resolve(tag) != digest:
            raise ValueError(f"Harbor tag digest conflict: {self.repository}:{tag}")

    def check_version(self, version, digest):
        row = self.read_receipt(version)
        if row["digest"] != digest:
            raise ValueError(f"release receipt digest conflict: {self.repository}:{version}")
        self.check_tag(version, digest)

    def read_receipt(self, version):
        tag = "receipt-" + version
        digest = self.resolve(tag)
        if not digest:
            raise ValueError(f"missing develop receipt: {self.repository}:{tag}")
        raw = subprocess.check_output(
            ["docker", "buildx", "imagetools", "inspect", "--raw", self.repository + "@" + digest], timeout=180)
        manifest = json.loads(raw)
        if ("sha256:" + hashlib.sha256(raw).hexdigest() != digest
                or manifest.get("artifactType") != RECEIPT_TYPE):
            raise ValueError("invalid Harbor foundation receipt")
        row = json.loads(manifest["annotations"][ANNOTATION])
        if (row.get("repository") != self.repository or row.get("version") != version
                or row.get("project") != PROJECT or row.get("branch") != "develop"
                or not re.fullmatch(DIGEST, row.get("digest", ""))):
            raise ValueError(f"release receipt identity mismatch: {self.repository}:{version}")
        return row

    def save_receipt(self, row):
        tag = "receipt-" + row["version"]
        self.oras("push", "--artifact-type", RECEIPT_TYPE, "--annotation",
                  ANNOTATION + "=" + json.dumps(row, separators=(",", ":"), sort_keys=True),
                  self.repository + ":" + tag)

    def tag(self, digest, tag):
        self.oras("tag", self.repository + "@" + digest, tag)


def pull(ref):
    docker("pull", "--platform", PLATFORM, ref)
    return inspect(ref)


def smoke(recipe, ref, parent):
    metadata = inspect(ref)
    if (metadata["Os"], metadata["Architecture"]) != ("linux", "amd64"):
        raise ValueError(f"incorrect foundation platform: {ref}")
    run = ["run", "-i", "--rm", "--platform", PLATFORM, "--network", "none", "--read-only",
           "--user", "10001:10001", "--tmpfs", "/tmp", "--entrypoint", "/bin/sh"]
    with tempfile.TemporaryDirectory(prefix="foundation-sample-") as sample:
        if parent:
            Path(sample).chmod(0o777)
            with (recipe.path / "check-build.sh").open("rb") as script:
                docker(*run, "-v", sample + ":/sample", ref, "-es", stdin=script)
            if not any(p.is_file() for p in Path(sample).rglob("*")):
                raise ValueError("builder acceptance must create an artifact in /sample")
            run.extend(["-v", sample + ":/sample:ro"])
        with (recipe.path / "check.sh").open("rb") as script:
            docker(*run, parent or ref, "-es", stdin=script)
    return metadata


def parent_record(recipe):
    if not recipe.parent:
        return None
    parent_repo, version = recipe.parent.rsplit(":", 1)
    registry = Harbor(parent_repo)
    digest = registry.resolve(version)
    registry.check_version(version, digest)
    return {"reference": recipe.parent, "digest": digest}


def parent_image(record):
    return record["reference"].rsplit(":", 1)[0] + "@" + record["digest"] if record else None


def build(recipe, origin, tree, parent, *, accept=True):
    local = f"local/operator-foundations/{recipe.kind}/{recipe.name}:" + origin["sourceCommit"][:12] + f"-{os.getpid()}"
    command = ["build", "--platform", PLATFORM, "--pull", "--label", REVISION + "=" + origin["sourceCommit"],
               "--label", TREE + "=" + tree, "-t", local]
    # Pin the literal FROM read from this recipe to the manifest accepted above.
    if parent:
        command.extend(["--label", PARENT + "=" + parent_image(parent)])
        source = re.sub(r"^([ \t]*FROM[ \t]+)" + re.escape(recipe.parent) + r"(?=\s|$)",
                        lambda m: m[1] + parent_image(parent), (recipe.path / "Dockerfile").read_text(),
                        count=1, flags=re.I | re.M)
        with tempfile.TemporaryFile() as dockerfile:
            dockerfile.write(source.encode())
            dockerfile.seek(0)
            docker(*command, "-f", "-", recipe.path, stdin=dockerfile)
    else:
        docker(*command, recipe.path)
    return (smoke(recipe, local, parent_image(parent)) if accept else inspect(local))["Id"]


def prepare_image(recipe, registry, origin, tree, parent):
    seed = bootstrap_reference(ROOT, recipe.repository + ":" + recipe.version)
    if seed:
        metadata = pull(seed)
        if metadata["Config"]["Labels"].get(REVISION) != BOOTSTRAP_REVISION:
            raise ValueError("bootstrap source revision mismatch")
        smoke(recipe, seed, None)
        digest = seed.partition("@")[2]
    else:
        # A recovery tag holds the accepted bytes across a runner crash before
        # the durable receipt. Retag from its digest to preserve any OCI index.
        recovery = "tested-" + origin["sourceCommit"]
        digest = registry.resolve(recovery)
        if digest:
            ref = recipe.repository + "@" + digest
            metadata = pull(ref)
            labels = metadata["Config"].get("Labels") or {}
            if (labels.get(REVISION) != origin["sourceCommit"] or labels.get(TREE) != tree
                    or (parent and labels.get(PARENT) != parent_image(parent))):
                raise ValueError(f"recovery tag conflict: {recovery}")
            smoke(recipe, ref, parent_image(parent))
        else:
            accepted_id = build(recipe, origin, tree, parent)
            remote_ref = recipe.repository + ":" + recovery
            docker("tag", accepted_id, remote_ref)
            docker("push", remote_ref)
            digest = registry.resolve(recovery)
            if not digest or pull(recipe.repository + "@" + digest)["Id"] != accepted_id:
                raise ValueError("registry image differs from locally accepted image")
        registry.check_tag(recovery, digest)
    return {**origin, "repository": recipe.repository, "version": recipe.version, "platform": PLATFORM,
            "recipeTree": tree, "digest": digest, "parent": parent}


def validate_receipt(recipe, row, head, tree, parent):
    source = row["sourceCommit"]
    if (row["project"] != PROJECT or row["branch"] != "develop"
            or row["repository"] != recipe.repository or row["version"] != recipe.version
            or row["platform"] != PLATFORM or row["recipeTree"] != tree or row["parent"] != parent
            or not re.fullmatch(DIGEST, row["digest"]) or not re.fullmatch("[0-9a-f]{40}", source)
            or not re.fullmatch("[1-9][0-9]*", row["pipelineId"])
            or git("rev-parse", f"{source}:{recipe.path.relative_to(ROOT)}") != tree):
        raise ValueError(f"develop receipt does not match recipe or provenance: {recipe.repository}")
    git("merge-base", "--is-ancestor", source, head)


def verify(recipe, registry, validate):
    row = registry.read_receipt(recipe.version)
    validate(row)
    if registry.resolve(recipe.version) != row["digest"]:
        raise ValueError("numeric tag conflict with develop receipt")
    return row


def version_key(version):
    return tuple(map(int, version.split(".")))


def publish(recipe, registry, prepare, validate):
    tags = set(registry.tags())
    versions = [version_key(tag) for tag in tags if re.fullmatch(VERSION, tag)]
    version = recipe.version
    if versions and max(versions) > version_key(version):
        raise ValueError(f"newer numeric version already exists; refusing stale publication of {version}")
    if not versions and version != "1.0.0":
        raise ValueError("a new foundation series must begin at 1.0.0")
    receipt_tag = "receipt-" + version
    if receipt_tag not in tags and registry.resolve(receipt_tag) is None:
        fresh = prepare()
        existing = registry.resolve(version)
        if existing is not None and existing != fresh["digest"]:
            raise ValueError("numeric tag conflict with accepted image")
        registry.save_receipt(fresh)
        row = registry.read_receipt(version)
        if row != fresh:
            raise ValueError("stored receipt differs from accepted image")
    else:
        row = registry.read_receipt(version)
    validate(row)
    digest = row["digest"]
    existing = registry.resolve(version)
    if existing is None:
        registry.tag(digest, version)
    elif existing != digest:
        raise ValueError("numeric tag conflict with develop receipt")
    registry.check_tag(version, digest)
    current = registry.resolve("dev")
    if current != digest:
        registry.tag(digest, "dev")
    registry.check_tag("dev", digest)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "publish", "verify"])
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--recipe", action="append")
    selection.add_argument("--plan", type=Path)
    args = parser.parse_args()
    revision = git("rev-parse", "HEAD")
    origin = {"sourceCommit": revision}
    if args.mode != "check":
        origin = validate_ci(args.mode, revision, os.environ)
        if not args.plan:
            parser.error("protected publication/verification requires --plan")
    all_recipes = recipes(ROOT)
    keys = args.recipe if args.recipe is not None else list(all_recipes)
    if args.plan:
        plan = json.loads(args.plan.read_bytes())
        keys = plan["foundations"]
        if (plan["commit"] != revision or type(keys) is not list or len(keys) != len(set(keys))
                or not set(keys) <= all_recipes.keys()):
            raise ValueError("invalid foundation plan")
    results = {}
    for key in keys:
        recipe = all_recipes[key]
        parent = parent_record(recipe)
        if args.mode == "check":
            build(recipe, origin, "check", parent)
            continue
        # CI builds only checked-in recipe bytes, including the acceptance scripts.
        path = str(recipe.path.relative_to(ROOT))
        if git("status", "--porcelain", "--untracked-files=all", "--ignored", "--", path):
            raise ValueError(f"dirty foundation recipe: {path}")
        tree = git("rev-parse", "HEAD:" + path)
        registry = Harbor(recipe.repository, publish=args.mode == "publish")
        def validate(row):
            validate_receipt(recipe, row, revision, tree, parent)
        if args.mode == "verify":
            results[key] = verify(recipe, registry, validate)
        else:
            def prepare():
                return prepare_image(recipe, registry, origin, tree, parent)
            results[key] = publish(recipe, registry, prepare, validate)
    if args.mode != "check":
        output = ROOT / "build/base-images.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps({"foundations": results}, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
