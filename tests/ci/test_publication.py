"""Publications must stay deterministic, complete and bound to this pipeline."""
import base64
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from ci.publish import manifest as pp
from ci.publish.tags import publication_tag
from ci.plan.projects import operator_projects
from ci.publish.manifest import (
    HARBOR_PREFIX, package_publication,
    route_for, validate_document, validate_image_manifest,
    write_registry_config,
)

CODE = "mcap_read_normalize"
COMMIT = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
IMAGE_ID = "sha256:" + "e" * 64
REPO_DEVELOP = pp.PUBLICATION_PREFIX + "operators-develop"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-11-02T08:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-11-02T08:00:00+00:00",
}


def make_operator(root, code=CODE, version="2.0.0"):
    path = Path(root) / "operators" / code
    (path / "src").mkdir(parents=True,exist_ok=True)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{code.replace("_", "-")}"\nversion = "{version}"\n\n'
        f'[tool.hatch.build.targets.wheel]\npackages = ["src/{code}"]\n'
    )
    (path / "operator.yaml").write_text(
        f"code: {code}\noperatorVersion: {version}\n"
        f"image: {HARBOR_PREFIX}{code}:{version}\n"
    )
    return path


def ci_env(branch="develop", pipeline="4711", commit=COMMIT):
    return {
        "CI_PIPELINE_SOURCE": "push",
        "CI_COMMIT_REF_PROTECTED": "true",
        "CI_PROJECT_PATH": "stridingai/data/data-platform-operators",
        "CI_PROJECT_ID": "4242",
        "CI_PIPELINE_ID": pipeline,
        "CI_COMMIT_SHA": commit,
        "CI_COMMIT_BRANCH": branch,
        "CI_PIPELINE_URL": pp.PIPELINE_URL_PREFIX + pipeline,
    }


def image_manifest():
    raw = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                   "digest": "sha256:" + "c" * 64, "size": 7028},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": "sha256:" + "d" * 64, "size": 123}],
    }).encode()
    return raw, pp.digest(raw)




class FakeRegistry:
    """In-memory oras: values maps refs to bytes, copies records layout pushes."""

    def __init__(self):
        self.values = {}
        self.tags = {}
        self.copies = []

    def _ok(self, stdout=b""):
        return subprocess.CompletedProcess([], 0, stdout, b"")

    def run(self, argv, timeout=60):
        name = argv[1]
        if name == "version":
            return self._ok(b"Version:        1.3.0\nGitCommit: fixture\n")
        if name == "resolve":
            ref = argv[-1]
            raw = self.values.get(ref)
            if raw is None:
                stderr = (
                    f"Error response from registry: failed to resolve digest:"
                    f" {ref}: not found\n"
                ).encode()
                return subprocess.CompletedProcess([], 1, b"", stderr)
            return self._ok((pp.digest(raw) + "\n").encode())
        if name == "repo":
            repository = argv[-1]
            if repository not in self.tags:
                # Real Harbor echoes the registry-relative repository name.
                stderr = (
                    "Error response from registry: not found: repository "
                    + repository.split("/", 1)[1] + " not found\n"
                ).encode()
                return subprocess.CompletedProcess([], 1, b"", stderr)
            return self._ok(b"\n".join(
                tag.encode() for tag in self.tags[repository]))
        if name in ("manifest", "blob"):
            ref, output = argv[-1], Path(argv[4])
            raw = self.values.get(ref)
            if raw is None:
                return subprocess.CompletedProcess([], 1, b"", b"missing\n")
            output.write_bytes(raw)
            return self._ok()
        if name == "cp":
            layout_ref, target = argv[3], argv[-1]
            layout, tag = layout_ref.rsplit(":", 1)
            index = json.loads((Path(layout) / "index.json").read_bytes())
            manifest_digest = index["manifests"][0]["digest"]
            manifest_raw = (Path(layout) / "blobs" / "sha256"
                            / manifest_digest[7:]).read_bytes()
            layer_digest = json.loads(manifest_raw)["layers"][0]["digest"]
            repository = target.rsplit(":", 1)[0]
            self.values[target] = manifest_raw
            self.values[repository + "@" + manifest_digest] = manifest_raw
            self.values[repository + "@" + layer_digest] = (
                (Path(layout) / "blobs" / "sha256" / layer_digest[7:]).read_bytes()
            )
            self.tags.setdefault(repository, []).append(tag)
            self.copies.append((layout_ref, target))
            return self._ok()
        return subprocess.CompletedProcess([], 1, b"", b"unknown subcommand\n")


def git_commit_all(repo, message):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                   env={**os.environ, **GIT_ENV})
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True,
                   env={**os.environ, **GIT_ENV})
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "operators-repo"
    root.mkdir()
    make_operator(root)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git_commit_all(root, "ancestor")
    return root


def test_routes_and_tags_for_the_three_protected_lines():
    assert route_for("develop") == (REPO_DEVELOP, "v4-develop", "develop")
    assert route_for("main") == (
        pp.PUBLICATION_PREFIX + "operators", "v4-main", "main",
    )
    assert route_for("release") == (
        pp.PUBLICATION_PREFIX + "operators", "v4-release", "release",
    )
    assert route_for("develop", 3) == (REPO_DEVELOP, "v3-develop", "develop")
    with pytest.raises(pp.PublicationError):
        route_for("feature/other")
    repository, tag, _, _, info = pp.validate_ci(ci_env())
    assert repository == REPO_DEVELOP
    assert tag == "v4-develop-p00000000000000004711-" + COMMIT[:12]
    assert info == {
        "projectId": "4242", "pipelineId": "4711",
        "pipelineUrl": pp.PIPELINE_URL_PREFIX + "4711",
    }


def test_ci_boundary_rejects_foreign_pipeline_identity():
    for override in (
        {"CI_PIPELINE_SOURCE": "schedule"},
        {"CI_COMMIT_REF_PROTECTED": "false"},
        {"CI_PROJECT_PATH": "stridingai/data/other"},
        {"CI_PIPELINE_URL": "https://elsewhere.invalid/4711"},
        {"CI_COMMIT_SHA": "short"},
        {"CI_PROJECT_ID": "0"},
    ):
        with pytest.raises(pp.PublicationError):
            pp.validate_ci({**ci_env(), **override})




def component(repository=None):
    return {CODE: {
        "buildVersion": "2.0.0", "sourceCommit": COMMIT,
        "digest": "sha256:" + "a" * 64,
        "repositories": {"local-harbor": {
            "role": "canonical",
            "repository": repository or HARBOR_PREFIX + CODE,
            "verifiedAt": "2026-11-02T08:00:00Z",
        }},
    }}


def document(components, parent=None, **extra):
    return validate_document({
        "schemaVersion": 2, "producer": "operators", "releaseLine": "develop",
        "sourceRepository": "data-platform-operators", "sourceBranch": "develop",
        "sourceCommit": COMMIT,
        "pipeline": {"projectId": "4242", "pipelineId": "4711",
                     "pipelineUrl": pp.PIPELINE_URL_PREFIX + "4711"},
        "parentPublication": parent
        or {"artifactDigest": None, "sourceCommit": None},
        "components": components, **extra,
    })


def test_document_validation_enforces_the_name_repository_tie():
    assert document(component())["components"][CODE]["buildVersion"] == "2.0.0"
    for bad in (
        lambda: document(component(HARBOR_PREFIX + "other_code")),
        lambda: document(component(), {"artifactDigest": "sha256:" + "a" * 64,
                                       "sourceCommit": None}),
        lambda: document(component(), extra={"unexpected": True}),
        lambda: document({CODE: {**component()[CODE], "buildVersion": "2.0"}}),
        lambda: document(component(), {"artifactDigest": "sha256:" + "a" * 64,
                                       "sourceCommit": "x" * 40}),
    ):
        with pytest.raises(pp.PublicationError):
            bad()


def test_publication_yaml_is_canonical_and_repeatable():
    first = None
    for parent in (None, {"artifactDigest": "sha256:" + "a" * 64,
                          "sourceCommit": "b" * 40}):
        raw = pp.build_document(
            "develop", COMMIT,
            {"projectId": "4242", "pipelineId": "4711",
             "pipelineUrl": pp.PIPELINE_URL_PREFIX + "4711"},
            parent, pp.component_index(document(component())),
        )
        assert validate_document(yaml.safe_load(raw))["parentPublication"][
            "sourceCommit"
        ] == (parent["sourceCommit"] if parent else None)
        first = first or raw
    # Rebuilding from the parsed document reproduces identical canonical bytes.
    assert pp.build_document(
        "develop", COMMIT,
        {"projectId": "4242", "pipelineId": "4711",
         "pipelineUrl": pp.PIPELINE_URL_PREFIX + "4711"},
        None, yaml.safe_load(first)["components"],
    ) == first


def test_layout_is_deterministic_and_verified(tmp_path):
    raw, _ = image_manifest()
    validate_image_manifest(raw)
    raw = yaml.safe_dump(document(component())).encode()
    tag = "v2-develop-p00000000000000004711-" + COMMIT[:12]
    receipts = [package_publication(tmp_path / f"layout{i}", tag, raw) for i in (1, 2)]
    assert receipts[0] == receipts[1]
    manifest_raw = pp.verify_layout(tmp_path / "layout1", receipts[0], tag, raw)
    assert manifest_raw == (tmp_path / "layout1" / "blobs" / "sha256"
                            / receipts[0]["artifactDigest"][7:]).read_bytes()
    with pytest.raises(pp.PublicationError):
        pp.verify_layout(tmp_path / "layout2", receipts[1], tag, b"changed")
    with pytest.raises(pp.PublicationError):
        validate_image_manifest(b'{"schemaVersion":1}')
    with pytest.raises(pp.PublicationError):
        validate_image_manifest(b"not json")


def run_publish(repo, work, registry, *, env):
    from types import SimpleNamespace
    from ci.publish.delivery import publish_document
    from ci.plan.baseline import registry_baseline
    work.mkdir(parents=True, exist_ok=True)
    client = pp.Oras('oras', work / 'auth.json', work / 'scratch', registry.run)
    baseline = registry_baseline(client, env['CI_COMMIT_BRANCH'], env['CI_COMMIT_SHA'],
                                 env['CI_PIPELINE_ID'], env['CI_PROJECT_ID'], repo)
    versions = [p.name + '/v' + p.version for p in operator_projects(repo)]
    _, image_digest = image_manifest()
    images = {key: {'image': {'digest': image_digest}, 'source_commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()}
              for key in versions}
    rt = SimpleNamespace(root=repo, plan={'execution': {'ref': env['CI_COMMIT_BRANCH'],
        'parent_pipeline_id': env['CI_PIPELINE_ID'], 'project_id': env['CI_PROJECT_ID']},
        'commit': env['CI_COMMIT_SHA'], 'publication_versions': versions, **baseline})
    publish_document(rt, SimpleNamespace(client=lambda _: client), images)
    import shutil
    output=work/'build/release'
    if output.resolve()!=(repo/'build/release').resolve():
        shutil.copytree(repo/'build/release',output,dirs_exist_ok=True)
    return output


def test_first_publication_has_no_parent_and_retry_reuses_it(repo, tmp_path):
    registry = FakeRegistry()
    output = run_publish(repo, tmp_path, registry, env=ci_env())
    document = yaml.safe_load((output / "manifest.yaml").read_bytes())
    assert document["parentPublication"] == {
        "artifactDigest": None, "sourceCommit": None,
    }
    assert {(r["code"],r["buildVersion"]) for r in document["components"]} == {(CODE,"2.0.0")}
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["ok"] and receipt["releaseLine"] == "develop"
    assert receipt["componentVersions"] == [CODE+"/v2.0.0"]
    assert len(registry.copies) == 2
    # The retry path: the tag already resolves to the same artifact.
    output2 = run_publish(repo, tmp_path / "again", registry, env=ci_env())
    assert len(registry.copies) == 2
    assert (output2 / "manifest.yaml").read_bytes() == (
        output / "manifest.yaml").read_bytes()


def test_retry_with_different_bytes_is_an_immutable_conflict(repo, tmp_path):
    registry = FakeRegistry()
    output = run_publish(repo, tmp_path, registry, env=ci_env())
    tag = json.loads((output / "receipt.json").read_text())["tag"]
    registry.values[REPO_DEVELOP + ":" + tag] = b"conflicting artifact"
    with pytest.raises(pp.PublicationError):
        run_publish(repo, tmp_path / "again", registry, env=ci_env())


def test_parent_chain_selects_the_git_trusted_ancestor(repo, tmp_path):
    parent_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
    ).stdout.strip()
    (repo / "child-change").write_text("child")
    child_sha = git_commit_all(repo, "child")
    registry = FakeRegistry()
    parent_output = run_publish(
        repo, tmp_path / "parent", registry,
        env=ci_env(pipeline="4700", commit=parent_sha),
    )
    output = run_publish(
        repo, tmp_path / "child", registry,
        env=ci_env(pipeline="4711", commit=child_sha),
    )
    document = yaml.safe_load((output / "manifest.yaml").read_bytes())
    parent_receipt = json.loads((parent_output / "receipt.json").read_text())
    assert document["parentPublication"] == {
        "artifactDigest": parent_receipt["artifactDigest"],
        "sourceCommit": parent_sha,
    }


def test_parent_from_a_foreign_history_is_ignored(repo, tmp_path):
    parent_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
    ).stdout.strip()
    (repo / "child-change").write_text("child")
    child_sha = git_commit_all(repo, "child")
    other = tmp_path / "unrelated"
    other.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    (other / "marker").write_text("unrelated history")
    foreign_sha = git_commit_all(other, "foreign")
    assert foreign_sha not in {parent_sha, child_sha}
    registry = FakeRegistry()
    run_publish(repo, tmp_path / "foreign-parent", registry,
                env=ci_env(pipeline="4700", commit=foreign_sha))
    output = run_publish(repo, tmp_path / "child", registry,
                         env=ci_env(pipeline="4711", commit=child_sha))
    document = yaml.safe_load((output / "manifest.yaml").read_bytes())
    assert document["parentPublication"] == {
        "artifactDigest": None, "sourceCommit": None,
    }






def v4_document():
    value = document(component())
    value["schemaVersion"] = 4
    value["components"] = [{
        "code": CODE, **value["components"][CODE],
        "operatorYaml": {
            "repository": pp.DEFINITIONS_PREFIX + CODE,
            "digest": "sha256:" + "a" * 64,
        },
    }]
    return value


def test_v4_rows_accept_an_operator_yaml_reference():
    value = v4_document()
    assert validate_document(value) == value
    reference = dict(value["components"][0]["operatorYaml"])
    for change in (
        {"repository": "harbor.striding.internal/data/data-operators/definitions/" + CODE},
        {"repository": pp.DEFINITIONS_PREFIX + "other_code"},
        {"digest": "sha256:" + "a" * 63},
        {"unexpected": True},
    ):
        row = value["components"][0]
        row["operatorYaml"] = {**reference, **change}
        with pytest.raises(pp.PublicationError):
            validate_document(value)
        row["operatorYaml"] = dict(reference)
    # Rows without the reference stay legal (legacy directories removed).
    del value["components"][0]["operatorYaml"]
    assert validate_document(value) == value
    # Unknown seventh fields are still rejected.
    value["components"][0]["operatorYaml"] = dict(reference)
    value["components"][0]["extra"] = True
    with pytest.raises(pp.PublicationError):
        validate_document(value)


def test_definition_source_identity_and_packaging(repo, tmp_path):
    raw = pp.read_operator_definition(repo, CODE, "2.0.0")
    assert raw == (Path(repo) / "operators" / CODE / "operator.yaml").read_bytes()
    stale = make_operator(repo, CODE, "3.0.0")
    (stale / "operator.yaml").write_text(
        f"code: {CODE}\noperatorVersion: 2.0.0\nimage: {HARBOR_PREFIX}{CODE}:3.0.0\n")
    with pytest.raises(pp.PublicationError, match="identity"):
        pp.read_operator_definition(repo, CODE, "3.0.0")
    with pytest.raises(pp.PublicationError, match="missing"):
        pp.read_operator_definition(tmp_path, CODE, "2.0.0")
    receipts = [pp.package_definition(tmp_path / f"layout{i}", "2.0.0", raw)
                for i in (1, 2)]
    assert receipts[0] == receipts[1]
    assert receipts[0]["layerMediaType"] == pp.DEFINITION_LAYER_MEDIA_TYPE
    manifest = pp.verify_definition(tmp_path / "layout1", receipts[0], "2.0.0", raw)
    assert json.loads(manifest)["artifactType"] == pp.DEFINITION_ARTIFACT_TYPE
    with pytest.raises(pp.PublicationError):
        pp.verify_definition(tmp_path / "layout1", receipts[0], "2.0.0", b"changed")
    with pytest.raises(pp.PublicationError):
        pp.package_definition(tmp_path / "layout3", "2.0", raw)


def test_attach_definition_references_publishes_once_per_version(repo, tmp_path):
    from types import SimpleNamespace
    fake = FakeRegistry()
    scratch = tmp_path / "scratch"

    def client(repository):
        return pp.Oras("oras", "config", scratch, fake.run).bind(repository)

    row = {"buildVersion": "2.0.0", "sourceCommit": subprocess.check_output(["git","rev-parse","HEAD"],cwd=repo,text=True).strip(),
           "digest": "sha256:" + "b" * 64,
           "repositories": {"local-harbor": {"role": "canonical",
                                             "repository": HARBOR_PREFIX + CODE,
                                             "verifiedAt": "2026-11-02T08:00:00Z"}}}
    legacy = {"buildVersion": "1.0.0", "sourceCommit": COMMIT,
              "digest": "sha256:" + "c" * 64,
              "repositories": {"local-harbor": {"role": "canonical",
                                                "repository": HARBOR_PREFIX + "gone_code",
                                                "verifiedAt": "2026-11-02T08:00:00Z"}}}
    values = {CODE + "/v2.0.0": row, "gone_code/v1.0.0": legacy}
    registry = SimpleNamespace(client=client)
    work = tmp_path / "release"
    pp.attach_definition_references(registry, repo, values, work)
    assert "operatorYaml" not in legacy
    reference = row["operatorYaml"]
    assert reference == {"repository": pp.DEFINITIONS_PREFIX + CODE,
                         "digest": reference["digest"]}
    assert len(fake.copies) == 1
    expected = (Path(repo) / "operators" / CODE / "operator.yaml").read_bytes()
    layer = pp.digest(expected)
    assert fake.values[reference["repository"] + "@" + layer] == expected
    # Replay: same yaml bytes resolve the existing tag and push nothing.
    row2 = dict(row)
    del row2["operatorYaml"]
    pp.attach_definition_references(registry, repo,
                                    {CODE + "/v2.0.0": row2}, work)
    assert len(fake.copies) == 1
    assert row2["operatorYaml"] == reference
    # A dirty/current declaration must never replace the original build declaration.
    (Path(repo) / "operators" / CODE / "operator.yaml").write_text(
        "code: %s\noperatorVersion: 2.0.0\nimage: changed\n" % CODE)
    row3=dict(row);del row3['operatorYaml']
    pp.attach_definition_references(registry,repo,{CODE+'/v2.0.0':row3},work)
    assert row3['operatorYaml']==reference and len(fake.copies)==1


def test_registry_config_covers_both_endpoints_of_the_same_harbor(tmp_path, monkeypatch):
    monkeypatch.setenv("DP_HARBOR_USERNAME_B64",
                       base64.b64encode(b"robot$fixture").decode())
    monkeypatch.setenv("DP_HARBOR_PASSWORD", "test-only")
    config = tmp_path / "auth.json"
    write_registry_config(config)
    assert config.stat().st_mode & 0o077 == 0
    auth = json.loads(config.read_text())["auths"]
    assert set(auth) == {"harbor.striding.internal", "192.168.121.144"}
    assert base64.b64decode(auth["192.168.121.144"]["auth"]) == b"robot$fixture:test-only"






def test_protected_baseline_auth_failure_is_not_bootstrap(tmp_path, monkeypatch):
    from ci.plan import baseline as base
    def fail(*args, **kwargs):
        raise pp.PublicationError("registry-list-failed")
    monkeypatch.setattr(base, "registry_baseline", fail)
    monkeypatch.setattr(pp.Oras, "check_version", lambda _: None)
    args = ["--branch", "develop", "--commit", COMMIT, "--pipeline-id", "4711",
            "--registry-config", str(tmp_path / "auth"), "--output", str(tmp_path / "baseline.json")]
    with pytest.raises(pp.PublicationError, match="registry-list-failed"):
        base.main(args)
    assert not (tmp_path / "baseline.json").exists()
    assert base.main([*args, "--merge-request"]) == 0
    assert json.loads((tmp_path / "baseline.json").read_text()) == {"parent": None, "components": {}}


def test_mr_job_artifact_baseline_verifies_digest_and_target_job(repo, tmp_path, monkeypatch):
    import io
    import zipfile
    from ci.plan import baseline as base
    parent_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    registry = FakeRegistry()
    output = run_publish(repo, tmp_path / "artifact-parent", registry,
                         env=ci_env(pipeline="4700", commit=parent_sha))
    (repo / "change").write_text("child")
    child_sha = git_commit_all(repo, "child")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for filename in ("publication.yaml", "receipt.json"):
            archive.writestr("build/publication/" + filename, (output / ("manifest.yaml" if filename == "publication.yaml" else filename)).read_bytes())
    class Opener:
        def open(self, request, timeout):
            assert request.full_url.endswith('/develop/download?job=publication-candidates')
            assert request.headers == {"Job-token": "secret-test-token"}
            return io.BytesIO(buffer.getvalue())
    monkeypatch.setattr(base.urllib.request, "build_opener", lambda *_: Opener())
    env = {**ci_env(commit=child_sha), "CI_API_V4_URL": "https://gitlab.striding.internal/api/v4",
           "CI_JOB_TOKEN": "secret-test-token"}
    result = base.artifact_baseline(env, "develop", child_sha, "4711", repo)
    assert result["parent"]["artifactDigest"] == json.loads((output / "receipt.json").read_text())["artifactDigest"]
    assert result["components"] == pp.component_index(yaml.safe_load((output / "manifest.yaml").read_text()))
    with pytest.raises(pp.PublicationError, match="baseline-untrusted"):
        base.artifact_baseline(env, "develop", "f" * 40, "4711", repo)






def test_denied_parent_layer_read_cannot_become_empty_baseline(repo, tmp_path):
    from ci.plan.baseline import registry_baseline
    parent_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    registry = FakeRegistry()
    output = run_publish(repo, tmp_path / "denied-parent", registry,
                         env=ci_env(pipeline="4700", commit=parent_sha))
    receipt = json.loads((output / "receipt.json").read_text())
    del registry.values[REPO_DEVELOP + "@" + receipt["layerDigest"]]
    (repo / "child").write_text("child")
    child_sha = git_commit_all(repo, "child")
    oras = pp.Oras("oras", tmp_path / "auth", tmp_path / "scratch", registry.run)
    with pytest.raises(pp.PublicationError, match="registry-fetch-failed"):
        registry_baseline(oras, "develop", child_sha, "4711", "4242", repo)

def test_mr_target_without_publication_line_uses_full_plan(tmp_path, monkeypatch):
    from ci.plan import baseline as base

    monkeypatch.delenv("CI_JOB_TOKEN", raising=False)
    monkeypatch.setattr(pp.Oras, "check_version", lambda _: None)
    output = tmp_path / "baseline.json"
    assert base.main(["--merge-request", "--branch", "hotfix/v4.0.1", "--commit", COMMIT,
                      "--pipeline-id", "5000", "--registry-config", str(tmp_path / "unused"),
                      "--output", str(output)]) == 0
    assert json.loads(output.read_text()) == {"parent": None, "components": {}}


def test_publication_module_is_protocol_only():
    assert not hasattr(pp, 'ACCEPTANCE_CASES')
    assert not hasattr(pp, 'publish')
    assert not hasattr(pp, 'load_batch')
