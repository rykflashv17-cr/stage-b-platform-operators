"""Versioned publication format, deterministic OCI packaging and registry I/O.

Execution authorization and test coverage belong to ci.publish.delivery.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

from ci.publish.tags import PUBLICATION_VERSION, publication_tag


HARBOR_PREFIX = "harbor.striding.internal/data/data-operators/"
PUBLICATION_PREFIX = "192.168.121.144/data/data-platform/release-publications/"
DEFINITIONS_PREFIX = "192.168.121.144/data/data-operators/definitions/"
PROJECT_PATH = "stridingai/data/data-platform-operators"
PIPELINE_URL_PREFIX = (
    "https://gitlab.striding.internal/stridingai/data/data-platform-operators/-/pipelines/"
)
PUBLICATION_ARTIFACT_TYPE = "application/vnd.striding.data-platform.publication.v3"
PUBLICATION_LAYER_MEDIA_TYPE = (
    "application/vnd.striding.data-platform.publication.v3+yaml"
)
# v4 rows reference each version's operator.yaml as an immutable registry
# artifact; consumers download it by repository@digest, never by directory.
DEFINITION_ARTIFACT_TYPE = "application/vnd.striding.data-platform.operator-definition.v1"
DEFINITION_LAYER_MEDIA_TYPE = DEFINITION_ARTIFACT_TYPE + "+yaml"
EMPTY_CONFIG_DIGEST = (
    "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
)
MAX_PUBLICATION_BYTES = 4 * 1024 * 1024
MAX_HISTORY_SNAPSHOTS = 4096
ORAS_VERSION = "1.3.0"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[1-9][0-9]{0,31}\Z")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_TAG = re.compile(
    r"(?:v4-(?:develop|release|main)|v3-(?:develop|release|main)|v2-(?:develop|main|v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)))"
    r"-p[0-9]{20}-[0-9a-f]{12}\Z"
)
_TOP_FIELDS = {
    "schemaVersion", "producer", "releaseLine", "sourceRepository",
    "sourceBranch", "sourceCommit", "pipeline", "parentPublication",
    "components",
}
_PIPELINE_FIELDS = {"projectId", "pipelineId", "pipelineUrl"}
_PARENT_FIELDS = {"artifactDigest", "sourceCommit"}
_COMPONENT_FIELDS = {"buildVersion", "sourceCommit", "digest", "repositories"}
_CANONICAL_FIELDS = {"role", "repository", "verifiedAt"}
# v4 adds an optional immutable operator.yaml artifact reference per row.
_OPERATOR_YAML_FIELDS = {"repository", "digest"}


class PublicationError(Exception):
    def __init__(self, category, detail=None):
        super().__init__(category)
        self.category = category
        self.detail = detail


def require(ok, category, detail=None):
    if not ok:
        raise PublicationError(category, detail)


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_timestamp(value):
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ) == value
    except ValueError:
        return False


def command(argv, timeout=60):
    # Tokens and robot passwords are never inherited by child processes.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "TMPDIR", "LANG", "SSL_CERT_FILE"}}
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        raise PublicationError("command-failed") from None
    require(
        len(result.stdout) <= 1024 * 1024 and len(result.stderr) <= 1024 * 1024,
        "command-output-limit",
    )
    return result


def _git(repo, *arguments):
    try:
        result = subprocess.run(
            ["git", *arguments], cwd=repo, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _release_line(branch, schema_version=4):
    require(type(branch) is str, "ci-boundary-invalid")
    if branch in {"develop", "main"} or schema_version in {3, 4} and branch == "release":
        return branch
    match = re.fullmatch(
        r"release/v((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))",
        branch or "",
    )
    if schema_version == 2 and match is not None:
        return "v" + match.group(1)
    raise PublicationError("ci-boundary-invalid")


def route_for(branch, schema_version=4):
    require(type(schema_version) is int and schema_version in {2, 3, 4}, "publication-invalid")
    line = _release_line(branch, schema_version)
    version = "v" + str(schema_version)
    if line == "develop":
        repository, prefix = PUBLICATION_PREFIX + "operators-develop", version + "-develop"
    elif line == "main":
        repository, prefix = PUBLICATION_PREFIX + "operators", version + "-main"
    elif line == "release":
        repository, prefix = PUBLICATION_PREFIX + "operators", version + "-release"
    else:
        repository, prefix = PUBLICATION_PREFIX + "operators", "v2-v" + line[1:]
    return repository, prefix, line


def validate_ci(env):
    repository, prefix, line = route_for(env.get("CI_COMMIT_BRANCH"))
    require(
        env.get("CI_PIPELINE_SOURCE") == "push"
        and env.get("CI_COMMIT_REF_PROTECTED") == "true"
        and env.get("CI_PROJECT_PATH") == PROJECT_PATH,
        "ci-boundary-invalid",
    )
    commit = env.get("CI_COMMIT_SHA", "")
    pipeline = env.get("CI_PIPELINE_ID", "")
    project_id = env.get("CI_PROJECT_ID", "")
    require(
        _SHA.fullmatch(commit) is not None
        and _IDENTIFIER.fullmatch(pipeline) is not None
        and _IDENTIFIER.fullmatch(project_id) is not None,
        "ci-boundary-invalid",
    )
    require(
        env.get("CI_PIPELINE_URL") == PIPELINE_URL_PREFIX + pipeline,
        "ci-boundary-invalid",
    )
    tag = prefix + "-p" + pipeline.zfill(20) + "-" + commit[:12]
    require(_TAG.fullmatch(tag) is not None, "ci-boundary-invalid")
    pipeline_info = {
        "projectId": project_id,
        "pipelineId": pipeline,
        "pipelineUrl": PIPELINE_URL_PREFIX + pipeline,
    }
    return repository, tag, line, commit, pipeline_info




REGISTRY_HOSTS = (HARBOR_PREFIX.split("/")[0], PUBLICATION_PREFIX.split("/")[0])


def write_registry_config(path):
    """One Harbor robot credential serves both endpoints of the same registry."""

    user = base64.b64decode(os.environ["DP_HARBOR_USERNAME_B64"], validate=True)
    password = os.environ["DP_HARBOR_PASSWORD"].encode()
    require(
        user and b":" not in user and password
        and not any(c in user + password for c in (b"\0", b"\n", b"\r")),
        "invalid-harbor-credentials",
    )
    encoded = base64.b64encode(user + b":" + password).decode()
    auths = {host: {"auth": encoded} for host in REGISTRY_HOSTS}
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"auths": auths}, stream)




class Oras:
    def __init__(self, binary, config, work, run=command):
        self.binary = str(binary)
        self.config = str(config)
        self.work = Path(work)
        # oras --output requires the target directory to exist.
        self.work.mkdir(parents=True, exist_ok=True)
        self.run = run
        self.counter = 0

    def check_version(self):
        result = self.run([self.binary, "version"])
        versions = [
            line.split()
            for line in result.stdout.splitlines()
            if line.startswith(b"Version:")
        ]
        require(
            result.returncode == 0 and versions == [[b"Version:", ORAS_VERSION.encode()]],
            "tool-version-mismatch",
        )

    def _flags(self):
        return ["--plain-http", "--registry-config", self.config]

    def resolve(self, ref):
        result = self.run([self.binary, "resolve", *self._flags(), ref])
        if result.returncode == 0:
            value = result.stdout.decode("ascii").strip()
            require(_DIGEST.fullmatch(value) is not None, "registry-resolve-invalid")
            return value
        missing = (
            f"Error response from registry: failed to resolve digest: {ref}: not found\n"
        ).encode()
        require(
            result.returncode == 1
            and result.stdout == b""
            and result.stderr == missing,
            "registry-resolve-failed",
        )
        return None

    def fetch(self, ref, expected, *, blob=False):
        self.counter += 1
        path = self.work / f"registry-object-{self.counter}"
        result = self.run([
            self.binary, "blob" if blob else "manifest", "fetch",
            "--output", str(path), *self._flags(), ref,
        ])
        require(result.returncode == 0, "registry-fetch-failed")
        raw = path.read_bytes()
        require(len(raw) <= MAX_PUBLICATION_BYTES, "registry-response-too-large")
        require(digest(raw) == expected, "registry-digest-mismatch")
        return raw

    def repo_tags(self, repository):
        result = self.run([self.binary, "repo", "tags", *self._flags(), repository])
        if result.returncode != 0:
            # A first publication addresses a repository that does not exist
            # yet; the push below creates it, so absence reads as no tags.
            # Harbor echoes the registry-relative repository name, so accept
            # both the requested and the host-stripped spelling.
            names = [repository]
            if "/" in repository:
                names.append(repository.split("/", 1)[1])
            missing = {
                (
                    "Error response from registry: not found: repository "
                    + name + " not found\n"
                ).encode("ascii")
                for name in names
            }
            require(
                result.returncode == 1 and result.stdout == b""
                and result.stderr in missing,
                "registry-list-failed",
            )
            return []
        try:
            lines = result.stdout.decode("ascii").splitlines()
        except UnicodeDecodeError:
            raise PublicationError("registry-list-failed") from None
        require(
            len(lines) <= 4096 and all(0 < len(line) <= 255 for line in lines),
            "registry-list-failed",
        )
        return lines

    def publish(self, layout, tag, artifact_digest, manifest_raw, publication_raw):
        ref = self.target + ":" + tag
        existing = self.resolve(ref)
        require(
            existing is None or existing == artifact_digest,
            "immutable-tag-conflict",
        )
        if existing is None:
            result = self.run([
                self.binary, "cp", "--from-oci-layout", str(layout) + ":" + tag,
                "--to-plain-http", "--to-registry-config", self.config, ref,
            ])
            require(result.returncode == 0, "registry-push-failed")
        require(self.resolve(ref) == artifact_digest, "publication-digest-mismatch")
        actual = self.fetch(
            self.target + "@" + artifact_digest, artifact_digest
        )
        require(actual == manifest_raw, "publication-bytes-mismatch")
        layer_digest = digest(publication_raw)
        layer = self.fetch(self.target + "@" + layer_digest, layer_digest, blob=True)
        require(layer == publication_raw, "publication-bytes-mismatch")

    def bind(self, target):
        registry = Oras(self.binary, self.config, self.work, self.run)
        registry.target = str(target)
        return registry


def validate_image_manifest(raw):
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublicationError("image-manifest-invalid") from None
    require(
        type(value) is dict and value.get("schemaVersion") == 2,
        "image-manifest-invalid",
    )
    media_type = value.get("mediaType")
    if media_type in (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ):
        descriptors = value.get("manifests")
        require(
            type(descriptors) is list and len(descriptors) > 0,
            "image-manifest-invalid",
        )
    else:
        require(
            media_type in (
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            )
            and type(value.get("layers")) is list,
            "image-manifest-invalid",
        )
        descriptors = [value.get("config"), *value["layers"]]
    for descriptor in descriptors:
        require(
            type(descriptor) is dict
            and type(descriptor.get("digest")) is str
            and _DIGEST.fullmatch(descriptor["digest"]) is not None
            and type(descriptor.get("size")) is int
            and descriptor["size"] >= 0
            and type(descriptor.get("mediaType")) is str,
            "image-manifest-invalid",
        )


def _component_fields(version):
    """Mandatory and allowed component row fields per schema version."""

    mandatory = _COMPONENT_FIELDS if version == 2 else _COMPONENT_FIELDS | {"code"}
    allowed = mandatory if version != 4 else mandatory | {"operatorYaml"}
    return mandatory, allowed


def validate_document(document):
    """Validate v2/v3/v4 without altering historical bytes or field semantics."""

    require(
        type(document) is dict and set(document) == _TOP_FIELDS
        and type(document["schemaVersion"]) is int
        and document["schemaVersion"] in {2, 3, 4}
        and document["producer"] == "operators"
        and document["sourceRepository"] == "data-platform-operators",
        "publication-invalid",
    )
    version = document["schemaVersion"]
    line = _release_line(document["sourceBranch"], version)
    require(document["releaseLine"] == line, "publication-invalid")
    source_commit = document["sourceCommit"]
    pipeline = document["pipeline"]
    require(
        type(source_commit) is str and _SHA.fullmatch(source_commit) is not None
        and type(pipeline) is dict
        and set(pipeline) == _PIPELINE_FIELDS
        and type(pipeline.get("projectId")) is str
        and type(pipeline.get("pipelineId")) is str
        and _IDENTIFIER.fullmatch(str(pipeline.get("projectId", ""))) is not None
        and _IDENTIFIER.fullmatch(str(pipeline.get("pipelineId", ""))) is not None
        and pipeline.get("pipelineUrl")
        == PIPELINE_URL_PREFIX + pipeline.get("pipelineId"),
        "publication-invalid",
    )
    parent = document["parentPublication"]
    require(type(parent) is dict and set(parent) == _PARENT_FIELDS,
            "publication-invalid")
    artifact, parent_commit = parent["artifactDigest"], parent["sourceCommit"]
    require((artifact is None) == (parent_commit is None),
            "publication-invalid")
    if artifact is not None:
        require(
            type(artifact) is str and _DIGEST.fullmatch(artifact) is not None
            and type(parent_commit) is str and _SHA.fullmatch(parent_commit) is not None,
            "publication-invalid",
        )
    components = document["components"]
    require(type(components) is (dict if version == 2 else list) and len(components) >= 1,
            "publication-invalid")
    rows = components.items() if version == 2 else ((row.get("code"), row) if type(row) is dict else (None, row) for row in components)
    seen = set()
    mandatory, allowed = _component_fields(version)
    for name, row in rows:
        require(
            isinstance(name, str) and _CODE.fullmatch(name) is not None
            and type(row) is dict and mandatory <= set(row) <= allowed
            and all(type(row[field]) is str for field in ("buildVersion", "sourceCommit", "digest"))
            and PUBLICATION_VERSION.fullmatch(str(row["buildVersion"])) is not None
            and _SHA.fullmatch(str(row["sourceCommit"])) is not None
            and _DIGEST.fullmatch(str(row["digest"])) is not None
            and type(row["repositories"]) is dict
            and set(row["repositories"]) == {"local-harbor"},
            "publication-invalid",
        )
        identity = (name, row["buildVersion"])
        require(identity not in seen, "publication-duplicate-version")
        seen.add(identity)
        canonical = row["repositories"]["local-harbor"]
        require(
            type(canonical) is dict
            and set(canonical) == _CANONICAL_FIELDS
            and canonical["role"] == "canonical"
            and canonical["repository"] == HARBOR_PREFIX + name
            and _valid_timestamp(canonical["verifiedAt"]),
            "publication-invalid",
        )
        if "operatorYaml" in row:
            reference = row["operatorYaml"]
            require(
                type(reference) is dict
                and set(reference) == _OPERATOR_YAML_FIELDS
                and type(reference["repository"]) is str
                and reference["repository"] == DEFINITIONS_PREFIX + name
                and type(reference["digest"]) is str
                and _DIGEST.fullmatch(reference["digest"]) is not None,
                "publication-invalid",
            )
    return document


def component_index(document):
    """Internal exact-version map; public v3 uses explicit code/version fields."""
    validate_document(document)
    rows = document["components"]
    if document["schemaVersion"] == 2:
        return {code + "/v" + row["buildVersion"]: dict(row) for code, row in rows.items()}
    return {row["code"] + "/v" + row["buildVersion"]:
            {key: value for key, value in row.items() if key != "code"} for row in rows}


def media_types(schema_version):
    require(type(schema_version) is int and schema_version in {2, 3, 4}, "publication-invalid")
    artifact = "application/vnd.striding.data-platform.publication.v" + str(schema_version)
    return artifact, artifact + "+yaml"


def build_document(branch, commit, pipeline_info, parent, components):
    if isinstance(components, dict):
        for name, row in components.items():
            require(type(name) is str and type(row) is dict
                    and name == name.split("/")[0] + "/v" + str(row.get("buildVersion")),
                    "publication-version-key-invalid")
    parent_row = (
        {"artifactDigest": None, "sourceCommit": None} if parent is None
        else dict(parent)
    )
    document = validate_document({
        "schemaVersion": 4,
        "producer": "operators",
        "releaseLine": _release_line(branch),
        "sourceRepository": "data-platform-operators",
        "sourceBranch": branch,
        "sourceCommit": commit,
        "pipeline": dict(pipeline_info),
        "parentPublication": parent_row,
        "components": sorted(
            ([{"code": name.split("/")[0], **row} for name, row in components.items()]
             if isinstance(components, dict) else [dict(row) for row in components]),
            key=lambda row: (row["code"], row["buildVersion"])),
    })
    raw = yaml.safe_dump(
        document, allow_unicode=False, default_flow_style=False, sort_keys=False
    ).encode("utf-8")
    require(len(raw) <= MAX_PUBLICATION_BYTES, "input-too-large")
    return raw


def fetch_publication(oras, repository, artifact):
    """Read v2/v3/v4 OCI bytes and require envelope/document version agreement."""
    raw = oras.fetch(repository + "@" + artifact, artifact)
    try:
        manifest = json.loads(raw)
        require(type(manifest) is dict and manifest.get("schemaVersion") == 2
                and manifest.get("mediaType") == "application/vnd.oci.image.manifest.v1+json"
                and manifest.get("artifactType") in {media_types(v)[0] for v in (2, 3, 4)}
                and manifest.get("config") == {"mediaType": "application/vnd.oci.empty.v1+json", "digest": EMPTY_CONFIG_DIGEST, "size": 2}, "parent-invalid")
        layers = manifest.get("layers")
        require(type(layers) is list and len(layers) == 1, "parent-invalid")
        layer = layers[0]
        require(type(layer) is dict and layer.get("mediaType") == manifest["artifactType"] + "+yaml"
                and _DIGEST.fullmatch(str(layer.get("digest", ""))) is not None
                and type(layer.get("size")) is int, "parent-invalid")
        publication = oras.fetch(repository + "@" + layer["digest"], layer["digest"], blob=True)
        require(len(publication) == layer["size"], "parent-invalid")
        document = validate_document(yaml.safe_load(publication))
        require(manifest["artifactType"] == media_types(document["schemaVersion"])[0], "parent-invalid")
        return document, publication
    except (ValueError, TypeError, yaml.YAMLError):
        raise PublicationError("parent-invalid") from None


def trusted_parent(oras, repository, prefix, line, repo, commit, pipeline, parent, project_id=None):
    require(type(parent) is dict and set(parent) == _PARENT_FIELDS
            and _DIGEST.fullmatch(str(parent.get("artifactDigest", ""))) is not None, "parent-invalid")
    document, _ = fetch_publication(oras, repository, parent["artifactDigest"])
    parent_commit = document["sourceCommit"]
    parent_pipeline = document["pipeline"]["pipelineId"]
    require(document["releaseLine"] == line and parent_commit == parent["sourceCommit"]
            and int(parent_pipeline) < int(pipeline)
            and (project_id is None or document["pipeline"]["projectId"] == project_id), "parent-invalid")
    actual_repository, actual_prefix, actual_line = route_for(document["sourceBranch"], document["schemaVersion"])
    require((actual_repository, actual_prefix, actual_line) == (repository, prefix, line), "parent-route-mismatch")
    tag = f"{actual_prefix}-p{int(parent_pipeline):020d}-{parent_commit[:12]}"
    require(oras.resolve(repository + ":" + tag) == parent["artifactDigest"], "parent-invalid")
    for value in {parent_commit, *(row["sourceCommit"] for row in component_index(document).values())}:
        require(_git(repo, "cat-file", "-e", value + "^{commit}")
                and _git(repo, "merge-base", "--is-ancestor", value, commit), "parent-invalid")
    return document




def discover_parent(oras, repository, prefix, line, repo, source_commit,
                    current_pipeline, include_same_commit=False):
    """Return the highest-pipeline complete ancestor for the requested prefix.

    A retried pipeline must re-derive its first attempt's bytes exactly, so
    this pipeline's own tag (and any same-commit artifact) is never a parent.
    """

    tag_pattern = re.compile(
        re.escape(prefix) + r"-p([0-9]{20})-[0-9a-f]{12}\Z"
    )
    current_pipeline = int(current_pipeline)
    tags = sorted(
        {tag for tag in oras.repo_tags(repository)
         if tag_pattern.fullmatch(tag) and int(tag_pattern.fullmatch(tag).group(1)) < current_pipeline},
        key=lambda value: (int(tag_pattern.fullmatch(value).group(1)), value),
        reverse=True,
    )
    require(len(tags) <= MAX_HISTORY_SNAPSHOTS, "history-scan-limit")
    for tag in tags:
        artifact_digest = oras.resolve(repository + ":" + tag)
        require(artifact_digest is not None, "history-artifact-missing")
        document, _ = fetch_publication(oras, repository, artifact_digest)
        # An existing but malformed snapshot is not an empty registry. Failing
        # here prevents a partial older snapshot from erasing published versions.
        require(route_for(document["sourceBranch"], document["schemaVersion"]) == (repository, prefix, line),
                "parent-route-mismatch")
        pipeline_id = document["pipeline"]["pipelineId"]
        parent_commit = document["sourceCommit"]
        require(len(pipeline_id) <= 20
                and tag == f"{prefix}-p{int(pipeline_id):020d}-{parent_commit[:12]}", "parent-invalid")
        if not include_same_commit and parent_commit == source_commit:
            continue
        commits = {parent_commit}
        commits.update(
            row["sourceCommit"] for row in component_index(document).values()
        )
        if any(
            not _git(repo, "cat-file", "-e", value + "^{commit}")
            or not _git(repo, "merge-base", "--is-ancestor", value, source_commit)
            for value in commits
        ):
            continue
        return {"artifactDigest": artifact_digest, "sourceCommit": parent_commit}
    return None


def package_publication(layout, tag, raw):
    """Create deterministic OCI bytes, including historical v2 verification."""

    require(
        isinstance(layout, Path)
        and _TAG.fullmatch(tag) is not None
        and type(raw) is bytes
        and 1 <= len(raw) <= MAX_PUBLICATION_BYTES
        and not layout.exists()
        and not layout.is_symlink(),
        "packager-input-invalid",
    )
    document = validate_document(yaml.safe_load(raw))
    artifact_type, layer_type = media_types(document["schemaVersion"])
    _, prefix, _ = route_for(document["sourceBranch"], document["schemaVersion"])
    require(tag == f'{prefix}-p{int(document["pipeline"]["pipelineId"]):020d}-{document["sourceCommit"][:12]}', "packager-input-invalid")
    config_raw = b"{}"
    layer_digest = digest(raw)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": artifact_type,
        "config": {
            "mediaType": "application/vnd.oci.empty.v1+json",
            "digest": EMPTY_CONFIG_DIGEST,
            "size": len(config_raw),
        },
        "layers": [{
            "mediaType": layer_type,
            "digest": layer_digest,
            "size": len(raw),
        }],
        "annotations": {
            "ai.striding.data-platform.publication.digest": layer_digest,
        },
    }
    manifest_raw = json.dumps(
        manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    artifact_digest = digest(manifest_raw)
    index_raw = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [{
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": artifact_digest,
                "size": len(manifest_raw),
                "artifactType": artifact_type,
                "annotations": {"org.opencontainers.image.ref.name": tag},
            }],
        },
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    layout.mkdir(mode=0o700)
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True, mode=0o700)
    files = {
        layout / "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        layout / "index.json": index_raw,
        blobs / layer_digest[7:]: raw,
        blobs / EMPTY_CONFIG_DIGEST[7:]: config_raw,
        blobs / artifact_digest[7:]: manifest_raw,
    }
    for path, value in files.items():
        with path.open("xb") as stream:
            stream.write(value)
    return {
        "ok": True,
        "check": "publication-oci-layout",
        "tag": tag,
        "artifactDigest": artifact_digest,
        "publicationDigest": layer_digest,
        "layerMediaType": layer_type,
    }


def verify_layout(layout, receipt, tag, raw):
    document = validate_document(yaml.safe_load(raw))
    artifact_type, layer_type = media_types(document["schemaVersion"])
    require(
        type(receipt) is dict
        and receipt.get("ok") is True
        and receipt.get("check") == "publication-oci-layout"
        and receipt.get("tag") == tag
        and receipt.get("publicationDigest") == digest(raw)
        and receipt.get("layerMediaType") == layer_type
        and _DIGEST.fullmatch(str(receipt.get("artifactDigest", ""))) is not None,
        "packager-receipt-invalid",
    )
    artifact_digest = receipt["artifactDigest"]
    index = json.loads((layout / "index.json").read_bytes())
    require(
        type(index) is dict
        and type(index.get("manifests")) is list
        and len(index["manifests"]) == 1
        and index["manifests"][0].get("digest") == artifact_digest
        and index["manifests"][0].get("annotations", {}).get(
            "org.opencontainers.image.ref.name"
        ) == tag,
        "packager-index-invalid",
    )
    manifest_raw = (layout / "blobs" / "sha256" / artifact_digest[7:]).read_bytes()
    require(digest(manifest_raw) == artifact_digest, "packager-manifest-invalid")
    manifest = json.loads(manifest_raw)
    require(
        type(manifest) is dict
        and manifest.get("schemaVersion") == 2
        and manifest.get("mediaType") == "application/vnd.oci.image.manifest.v1+json"
        and manifest.get("artifactType") == artifact_type
        and manifest.get("config") == {
            "mediaType": "application/vnd.oci.empty.v1+json",
            "digest": EMPTY_CONFIG_DIGEST,
            "size": 2,
        }
        and type(manifest.get("layers")) is list
        and len(manifest["layers"]) == 1
        and manifest["layers"][0].get("mediaType") == layer_type
        and manifest["layers"][0].get("digest") == digest(raw)
        and manifest["layers"][0].get("size") == len(raw)
        and (layout / "blobs" / "sha256" / digest(raw)[7:]).read_bytes() == raw,
        "packager-layer-invalid",
    )
    return manifest_raw


def read_operator_definition(root, code, version):
    """Read one operator.yaml and pin its identity to the version directory."""

    from ci.publish.operator_sources import definition
    try:
        return definition(root, code + '/v' + version)['raw']
    except ValueError as exc:
        raise PublicationError(str(exc)) from None


def package_definition(layout, tag, raw):
    """Create deterministic OCI bytes for a standalone operator.yaml artifact."""

    require(
        isinstance(layout, Path)
        and (PUBLICATION_VERSION.fullmatch(tag) is not None or re.fullmatch(r"sha256-[a-f0-9]{64}", tag) is not None)
        and type(raw) is bytes
        and 1 <= len(raw) <= MAX_PUBLICATION_BYTES
        and not layout.exists()
        and not layout.is_symlink(),
        "packager-input-invalid",
    )
    config_raw = b"{}"
    layer_digest = digest(raw)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": DEFINITION_ARTIFACT_TYPE,
        "config": {
            "mediaType": "application/vnd.oci.empty.v1+json",
            "digest": EMPTY_CONFIG_DIGEST,
            "size": len(config_raw),
        },
        "layers": [{
            "mediaType": DEFINITION_LAYER_MEDIA_TYPE,
            "digest": layer_digest,
            "size": len(raw),
        }],
        "annotations": {
            "ai.striding.data-platform.operator-definition.digest": layer_digest,
        },
    }
    manifest_raw = json.dumps(
        manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    artifact_digest = digest(manifest_raw)
    index_raw = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [{
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": artifact_digest,
                "size": len(manifest_raw),
                "artifactType": DEFINITION_ARTIFACT_TYPE,
                "annotations": {"org.opencontainers.image.ref.name": tag},
            }],
        },
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    layout.mkdir(mode=0o700)
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True, mode=0o700)
    files = {
        layout / "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        layout / "index.json": index_raw,
        blobs / layer_digest[7:]: raw,
        blobs / EMPTY_CONFIG_DIGEST[7:]: config_raw,
        blobs / artifact_digest[7:]: manifest_raw,
    }
    for path, value in files.items():
        with path.open("xb") as stream:
            stream.write(value)
    return {
        "ok": True,
        "check": "operator-definition-oci-layout",
        "tag": tag,
        "artifactDigest": artifact_digest,
        "definitionDigest": layer_digest,
        "layerMediaType": DEFINITION_LAYER_MEDIA_TYPE,
    }


def verify_definition(layout, receipt, tag, raw):
    require(
        type(receipt) is dict
        and receipt.get("ok") is True
        and receipt.get("check") == "operator-definition-oci-layout"
        and receipt.get("tag") == tag
        and receipt.get("definitionDigest") == digest(raw)
        and receipt.get("layerMediaType") == DEFINITION_LAYER_MEDIA_TYPE
        and _DIGEST.fullmatch(str(receipt.get("artifactDigest", ""))) is not None,
        "packager-receipt-invalid",
    )
    artifact_digest = receipt["artifactDigest"]
    index = json.loads((layout / "index.json").read_bytes())
    require(
        type(index) is dict
        and type(index.get("manifests")) is list
        and len(index["manifests"]) == 1
        and index["manifests"][0].get("digest") == artifact_digest
        and index["manifests"][0].get("annotations", {}).get(
            "org.opencontainers.image.ref.name"
        ) == tag,
        "packager-index-invalid",
    )
    manifest_raw = (layout / "blobs" / "sha256" / artifact_digest[7:]).read_bytes()
    require(digest(manifest_raw) == artifact_digest, "packager-manifest-invalid")
    manifest = json.loads(manifest_raw)
    require(
        type(manifest) is dict
        and manifest.get("schemaVersion") == 2
        and manifest.get("mediaType") == "application/vnd.oci.image.manifest.v1+json"
        and manifest.get("artifactType") == DEFINITION_ARTIFACT_TYPE
        and manifest.get("config") == {
            "mediaType": "application/vnd.oci.empty.v1+json",
            "digest": EMPTY_CONFIG_DIGEST,
            "size": 2,
        }
        and type(manifest.get("layers")) is list
        and len(manifest["layers"]) == 1
        and manifest["layers"][0].get("mediaType") == DEFINITION_LAYER_MEDIA_TYPE
        and manifest["layers"][0].get("digest") == digest(raw)
        and manifest["layers"][0].get("size") == len(raw)
        and (layout / "blobs" / "sha256" / digest(raw)[7:]).read_bytes() == raw,
        "packager-layer-invalid",
    )
    return manifest_raw


def attach_definition_references(registry, root, values, work, *, required=()):
    """Bind declarations to original image commits, independent of current paths."""
    from ci.publish.operator_sources import definition
    pending=[]
    for key,row in values.items():
        if not isinstance(row,dict):raise PublicationError('definition-row-invalid')
        if 'operatorYaml' in row:continue
        code,_,version=key.rpartition('/v')
        if _CODE.fullmatch(code) is None or PUBLICATION_VERSION.fullmatch(version) is None:
            raise PublicationError('definition-key-invalid')
        try:source=definition(root,key,row['sourceCommit'])
        except ValueError:
            if key in required:raise
            continue  # Legacy cumulative rows without a recoverable definition stay unchanged.
        pending.append((code,row,source))
    if not pending:return
    output=Path(work);output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='definitions-',dir=output) as scratch:
        for index,(code,row,source) in enumerate(pending):
            raw=source['raw'];tag='sha256-'+source['sha256']
            target=registry.client(DEFINITIONS_PREFIX+code).bind(DEFINITIONS_PREFIX+code)
            layout=Path(scratch)/f'definition-{index}'
            receipt=package_definition(layout,tag,raw)
            manifest=verify_definition(layout,receipt,tag,raw)
            target.publish(layout,tag,receipt['artifactDigest'],manifest,raw)
            row['operatorYaml']={'repository':DEFINITIONS_PREFIX+code,'digest':receipt['artifactDigest']}
