from pathlib import Path
import subprocess

import pytest

from ci.build import images as image_builder

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "harbor.striding.internal/data/data-operators/base"
RUNTIME = PREFIX + "/runtime/python312-cpu:1.0.0"
BUILDER = PREFIX + "/builder/test:2.3.4"
DIGEST = "sha256:" + "1" * 64


def sample_project(root, version, runtime):
    key = "sample/v" + version
    project = root / "operators" / "sample"
    project.mkdir(parents=True,exist_ok=True)
    (project / "pyproject.toml").write_text(
        f'[project]\nname="sample"\nversion="{version}"\n'
        '[tool.hatch.build.targets.wheel]\npackages=["src/sample"]\n')
    (project / "operator.yaml").write_text(f"code: sample\noperatorVersion: {version}\n")
    (project / "Dockerfile").write_text(f"FROM {runtime} AS runtime\nFROM runtime AS final\n")
    return key


def test_dockerfiles_build_offline_with_a_versioned_runtime():
    from ci.build.bases import parse_dockerfile
    paths = sorted((ROOT / "operators").glob("*/Dockerfile")) + [ROOT / "templates/python-operator/Dockerfile"]
    for path in paths:
        source = path.read_text()
        refs, final = parse_dockerfile(source)
        assert final in refs
        assert all(flag in source for flag in ["--no-index", "--only-binary=:all:", "--require-hashes",
                                               "COPY wheelhouse/dependencies/", "COPY wheelhouse/projects/",
                                               "COPY requirements.dependencies.txt", "COPY requirements.projects.txt"])
        assert "USER 10001:10001" in source
        assert "OPERATOR_BASE_IMAGE" not in source


def test_dockerfile_is_the_only_version_selector(tmp_path):
    old = sample_project(tmp_path, "4.0.0", RUNTIME)
    new_runtime = RUNTIME.rsplit(":", 1)[0] + ":1.0.1"
    new = sample_project(tmp_path, "4.1.0", new_runtime)
    plan = image_builder.build_plan(tmp_path)
    assert [(key, refs, runtime) for key, _, refs, runtime in plan] == [
        (new, [new_runtime], new_runtime)]
    assert image_builder.build_plan(tmp_path, [new]) == [plan[0]]


def test_external_stages_are_pinned_and_final_alias_is_resolved():
    from ci.build.bases import parse_dockerfile, pin_dockerfile
    source = (f"FROM {RUNTIME} AS first\nFROM {BUILDER} AS native\n"
              "FROM first AS runtime\nfrom RUNTIME as final\nCOPY --from=native /artifact /artifact\n")
    assert parse_dockerfile(source) == ([RUNTIME, BUILDER], RUNTIME)
    refs = {ref: ref.rsplit(":", 1)[0] + "@" + DIGEST for ref in [RUNTIME, BUILDER]}
    pinned = pin_dockerfile(source, refs)
    assert "from RUNTIME as final" in pinned
    assert all(value in pinned for value in refs.values())
    assert BUILDER not in pinned and RUNTIME not in pinned


@pytest.mark.parametrize("bad", [
    "python", "python:latest", PREFIX + "/runtime/python312-cpu", PREFIX + "/runtime/python312-cpu:dev",
    "${OPERATOR_BASE_IMAGE}", "runtime:1.0.0", "--platform=linux/arm64 " + RUNTIME,
])
def test_arbitrary_floating_and_wrong_architecture_stages_fail(bad):
    from ci.build.bases import parse_dockerfile
    with pytest.raises(ValueError):
        parse_dockerfile(f"FROM {RUNTIME} AS runtime\nFROM {bad} AS build\nFROM runtime\n")


def test_final_must_derive_from_a_runtime():
    from ci.build.bases import parse_dockerfile
    with pytest.raises(ValueError, match="runtime"):
        parse_dockerfile(f"FROM {BUILDER} AS native\nFROM native")


@pytest.mark.parametrize("matching", [True, False])
def test_published_base_checks_receipt_digest_without_registry_policy(monkeypatch, tmp_path, matching):
    from ci.build import bases
    from ci.base_images.manage import Harbor

    repo, version = RUNTIME.rsplit(":", 1)
    monkeypatch.setattr(bases, "pull", lambda ref: DIGEST)
    monkeypatch.setattr(bases, "inspect_image", lambda ref: dict(
        Id=DIGEST, Os="linux", Architecture="amd64", RepoDigests=[repo + "@" + DIGEST]))
    monkeypatch.setattr(Harbor, "artifact", lambda self, tag: {"digest": DIGEST})
    monkeypatch.setattr(Harbor, "read_receipt", lambda self, tag: {
        "repository": repo, "version": version, "digest": DIGEST if matching else "sha256:" + "9" * 64})
    if matching:
        assert bases.resolve(tmp_path, RUNTIME)["digest"] == DIGEST
    else:
        with pytest.raises(ValueError, match="receipt"):
            bases.resolve(tmp_path, RUNTIME)


def test_resolution_checks_registry_manifest_arch_and_local_content(monkeypatch, tmp_path):
    from ci.build import bases
    repo = RUNTIME.rsplit(":", 1)[0]
    row = dict(Id="sha256:" + "2" * 64, Os="linux", Architecture="amd64", RepoDigests=[repo + "@" + DIGEST])
    monkeypatch.setattr(bases, "inspect_image", lambda ref: row)
    monkeypatch.setattr(bases, "pull", lambda ref: DIGEST)
    monkeypatch.setattr(bases.Harbor, "artifact", lambda self, tag: {
        "digest": DIGEST, "tags": [{"name": tag, "immutable": True}]})
    monkeypatch.setattr(bases.Harbor, "read_receipt", lambda self, tag: {"repository": repo, "version": tag, "digest": DIGEST})
    result = bases.resolve(tmp_path, RUNTIME)
    assert result["digest"] == DIGEST and result["image_id"] == row["Id"]
    assert result["reference"] == repo + "@" + DIGEST
    row["Architecture"] = "arm64"
    with pytest.raises(ValueError, match="linux/amd64"):
        bases.resolve(tmp_path, RUNTIME)
    row.update(Architecture="amd64", RepoDigests=[])
    with pytest.raises(ValueError, match="digest"):
        bases.resolve(tmp_path, RUNTIME)


def test_bootstrap_never_masks_auth_failure(monkeypatch):
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "merge_request_event")
    from ci.build import bases
    def fail(ref):
        raise subprocess.CalledProcessError(1, ["docker", "pull", ref], stderr="unauthorized")
    monkeypatch.setattr(bases, "pull", fail)
    with pytest.raises(RuntimeError, match="unauthorized"):
        bases.resolve(ROOT, RUNTIME, bootstrap=True)


def test_harbor_reports_artifact_not_found_for_first_publication(monkeypatch):
    from ci.build import bases
    from ci.base_images import config
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "merge_request_event")
    seed = RUNTIME.rsplit(":", 1)[0] + "@" + config.BOOTSTRAP_DIGEST
    monkeypatch.setattr(config, "bootstrap_reference", lambda root, ref: seed)
    def pull(ref):
        if ref == RUNTIME:
            artifact = ref.split("/", 1)[1]
            raise subprocess.CalledProcessError(1, ["docker", "pull", ref],
                stderr=f"Error response from daemon: unknown: artifact {artifact} not found")
        assert ref == seed
        return seed.split("@")[1]
    monkeypatch.setattr(bases, "pull", pull)
    monkeypatch.setattr(bases, "inspect_image", lambda ref: dict(Id=DIGEST, Os="linux",
                                                               Architecture="amd64", RepoDigests=[seed]))
    assert bases.resolve(ROOT, RUNTIME, bootstrap=True)["reference"] == seed


@pytest.mark.parametrize("instruction", [
    "COPY --from=other:latest /x /x",
    "COPY --from=2 /x /x",
    "RUN --mount=type=bind,from=other:dev,source=/x,target=/x ls /x",
    'RUN --mount="type=bind,from=other:dev,source=/x,target=/x" ls /x',
    "RUN --mount=type=bind,\\\nfrom=other,source=/x,target=/x ls /x",
    "RUN --network=default echo hello",
    "ADD https://example.org/wheel /tmp/wheel",
])
def test_external_image_or_network_flags_cannot_bypass_the_stage_manifest(instruction):
    from ci.build.bases import parse_dockerfile
    with pytest.raises(ValueError):
        parse_dockerfile(f"FROM {RUNTIME} AS build\nFROM build\n{instruction}\n")


def test_prior_stages_work_by_index_or_alias():
    from ci.build.bases import parse_dockerfile
    source = (f"FROM {BUILDER} AS native\nFROM {RUNTIME}\n"
              "COPY --from=0 /x /x\nRUN --mount=type=bind,from=native,target=/work ls /work")
    assert parse_dockerfile(source) == ([BUILDER, RUNTIME], RUNTIME)


def test_published_tag_ignores_newer_checked_out_recipe(tmp_path, monkeypatch):
    from ci.build import bases
    from ci.base_images import config
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "merge_request_event")
    monkeypatch.setattr(config, "bootstrap_reference", lambda *_: pytest.fail("consulted rotated recipe"))
    ref = RUNTIME.rsplit(":", 1)[0] + "@" + config.BOOTSTRAP_DIGEST
    monkeypatch.setattr(bases, "pull", lambda image: config.BOOTSTRAP_DIGEST)
    monkeypatch.setattr(bases, "inspect_image", lambda image: dict(Id=DIGEST, Os="linux",
                                                                 Architecture="amd64", RepoDigests=[ref]))
    monkeypatch.setattr(bases.Harbor, "artifact", lambda self, tag: {
        "digest": config.BOOTSTRAP_DIGEST, "tags": [{"name": tag, "immutable": True}]})
    monkeypatch.setattr(bases.Harbor, "read_receipt", lambda self, tag: {
        "repository": RUNTIME.rsplit(":", 1)[0], "version": tag, "digest": config.BOOTSTRAP_DIGEST})
    assert not bases.resolve(tmp_path, RUNTIME, bootstrap=True)["bootstrap"]


def test_offline_build_with_explicit_cache_control(tmp_path, monkeypatch):
    from ci.build import bases as operator_bases
    from ci.build import wheelhouse
    runtime = RUNTIME
    key = sample_project(tmp_path, "4.0.0", runtime)
    digest_ref = RUNTIME.rsplit(":", 1)[0] + "@" + DIGEST
    base = {"reference": digest_ref, "image_id": DIGEST, "digest": DIGEST, "bootstrap": False}
    monkeypatch.setattr(operator_bases, "resolve", lambda *_args, **_kw: base)
    monkeypatch.setattr(wheelhouse, "probe_target", lambda *_: {})
    monkeypatch.setattr(image_builder, "stage", lambda *_args, **_kw: {"bases": {runtime: base}})
    commands = []
    monkeypatch.setattr(image_builder.subprocess, "run", lambda args, check: commands.append(args))
    monkeypatch.setattr(image_builder, "identity", lambda ref: DIGEST)
    plan = image_builder.build_plan(tmp_path, [key])
    for no_cache in [False, True]:
        result = image_builder.build(tmp_path, plan, "test", no_cache=no_cache, wheel_cache=tmp_path)
        assert result["sample"]["bases"][runtime] == base
        command = commands.pop()
        assert command[command.index("--network") + 1] == "none"
        assert ("--no-cache" in command) == no_cache
