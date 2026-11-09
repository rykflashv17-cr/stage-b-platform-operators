import json
import yaml
from types import SimpleNamespace
import pytest
from ci.plan.images import load_plan, selection
from ci.publish.mirror import plan_from_receipt


def fixture(tmp_path, monkeypatch):
    import ci.plan.projects
    monkeypatch.setattr(ci.plan.projects, "operator_projects", lambda root: [SimpleNamespace(name="a", version="4.1.0", path=tmp_path / "operators/a")])
    plan = {"commit": "a" * 40, "publication_versions": ["a/v4.0.0"], "build": ["a/v4.1.0"], "publish": [], "components": {"a/v4.0.0": {"buildVersion": "4.0.0", "digest": "sha256:" + "b" * 64, "repositories": {"local-harbor": {"repository": "harbor.striding.internal/data/data-operators/a"}}}}, "parent": None}
    (tmp_path / "ci").mkdir()
    (tmp_path/'ci/archived-versions.json').write_text(json.dumps({'a/v4.0.0':{
        'build':{'input_digest':'a'*64,'record_digest':'sha256:'+'b'*64},
        'source':{'commit':'c'*40,'path':'operators/a/v4.0.0','definition_sha256':'d'*64},
        'tests':{mode:{'definition_digest':'e'*64,'record_digest':'sha256:'+'f'*64}
                 for mode in ('source','wheel','container')}}}))
    (tmp_path / "ci/release-versions.yaml").write_text(yaml.safe_dump(plan["publication_versions"]))
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return plan, path


def test_candidate_build_and_inherited_release_version(tmp_path, monkeypatch):
    plan, path = fixture(tmp_path, monkeypatch)
    assert load_plan(path, tmp_path, commit=plan["commit"]) == plan
    inherited = plan["components"]["a/v4.0.0"]
    selection(plan, {"a/v4.0.0": {"operator": "a/v4.0.0", "image": "harbor.striding.internal/data/data-operators/a@" + inherited["digest"], "inherited": inherited}, "a/v4.1.0": {"operator": "a/v4.1.0", "image": "sha256:" + "c" * 64}})
    with pytest.raises(ValueError, match="coverage"):
        selection(plan, {"a/v4.0.0": {"operator": "a/v4.0.0"}})


def test_inactive_cannot_publish(tmp_path, monkeypatch):
    plan, path = fixture(tmp_path, monkeypatch)
    plan["publish"] = plan["build"]
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="built release"):
        load_plan(path, tmp_path, commit=plan["commit"])


def test_wrong_inherited_version(tmp_path, monkeypatch):
    plan, path = fixture(tmp_path, monkeypatch)
    plan["components"]["a/v4.0.0"]["buildVersion"] = "3.0.0"
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="version mismatch"):
        load_plan(path, tmp_path, commit=plan["commit"])


def test_empty_receipt_requires_explicit_allowlist():
    receipt = {"commit": "a" * 40, "images": {}}
    assert plan_from_receipt(receipt, receipt["commit"], {}, allow_empty=True) == []
    with pytest.raises(ValueError):
        plan_from_receipt(receipt, receipt["commit"], {})


def test_reject_changed_release_scope(tmp_path, monkeypatch):
    plan, path = fixture(tmp_path, monkeypatch)
    plan["publication_versions"] = ["a/v4.1.0"]
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="authoritative"):
        load_plan(path, tmp_path, commit=plan["commit"])


def test_candidate_ships_inactive_but_main_does_not():
    from ci.plan.images import shipping_keys
    plan = {"build": ["a/v4.0.0", "a/v4.1.0"], "publish": ["a/v4.0.0"]}
    assert shipping_keys(plan, "develop") == plan["build"]
    assert shipping_keys(plan, "release") == plan["publish"]
    assert shipping_keys(plan, "main") == plan["publish"]


def test_two_versions_receipt_and_mirror_do_not_overwrite(tmp_path):
    from ci.publish.mirror import mirror, HARBOR
    import hashlib
    raw = b'{"schemaVersion":2}'
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    repo = HARBOR + "/data/data-operators/a"
    tags = {"a/v4.0.0": "4.0.0-dev.1", "a/v4.1.0": "4.1.0-dev.1"}
    receipt = {"commit": "a" * 40, "images": {repo + ":" + tag: {"image_id": "sha256:" + "b" * 64, "reference": repo + "@" + digest} for tag in tags.values()}}
    items = plan_from_receipt(receipt, receipt["commit"], tags)
    class Registry:
        def fetch(self, ref, **kwargs):
            return raw
        def copy(self, *args):
            raise AssertionError("already present")
    result = mirror(items, Registry(), evidence_dir=tmp_path)
    assert set(result) == {"a/" + tag for tag in tags.values()}
    for key in result:
        assert (tmp_path / key / "acr-public.json").read_bytes() == raw


@pytest.mark.parametrize("branch,expected", [("develop", 2), ("main", 1)])
def test_publish_two_versions_uses_exact_version_acceptance(tmp_path, monkeypatch, branch, expected):
    import sys
    import yaml
    import ci.plan.images
    from ci.publish import images as publish_images
    plan = {"commit": "a" * 40, "publication_versions": ["a/v4.0.0"], "build": ["a/v4.0.0", "a/v4.1.0"], "publish": ["a/v4.0.0"]}
    monkeypatch.setattr(ci.plan.images, "load_plan", lambda *a, **kw: plan)
    image_id = "sha256:" + "b" * 64
    images = {label: {"operator": key, "image": image_id} for label, key in [("a/v4.0.0", "a/v4.0.0"), ("a/v4.1.0", "a/v4.1.0")]}
    cases = dict.fromkeys(["marvin", "gaps", "abc", "empty", "input_error"])
    def chain(version):
        return {"cases": cases, "images": {"a": {"id": image_id, "operator": "a/v" + version, "operator_version": version}}}
    acceptance = {"cases": cases, "images": {"a/v"+v:chain(v)["images"]["a"] for v in ("4.0.0","4.1.0")}, "commit": plan["commit"], "publication_versions": plan["publication_versions"]}
    projects = []
    for version in ("4.0.0", "4.1.0"):
        directory = tmp_path / version
        directory.mkdir()
        (directory / "operator.yaml").write_text(yaml.safe_dump({"code": "a", "operatorVersion": version, "image": publish_images.PREFIX + "a:" + version}))
        projects.append(SimpleNamespace(name="a", version=version, path=directory))
    monkeypatch.setattr(publish_images, "operator_projects", lambda root: projects)
    monkeypatch.setattr(publish_images.subprocess, "check_output", lambda *a, **kw: plan["commit"])
    monkeypatch.setenv("CI_COMMIT_REF_PROTECTED", "true")
    monkeypatch.setenv("CI_COMMIT_SHA", plan["commit"])
    monkeypatch.setenv("CI_COMMIT_BRANCH", branch)
    monkeypatch.setenv("CI_PIPELINE_ID", "12")
    monkeypatch.setattr(publish_images, "publish", lambda selected: selected)
    for name, value in [("images", images), ("acceptance", acceptance)]:
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(sys, "argv", ["publish", "--gitlab", "--plan", "unused", "--images", str(tmp_path / "images.json"), "--acceptance", str(tmp_path / "acceptance.json"), "--output", str(output)])
    publish_images.main()
    assert len(json.loads(output.read_text())["images"]) == expected
    if branch == "develop":
        acceptance["images"]["a/v4.1.0"]["id"] = "sha256:" + "c" * 64
        (tmp_path / "acceptance.json").write_text(json.dumps(acceptance))
        with pytest.raises(ValueError, match="not accepted"):
            publish_images.main()
