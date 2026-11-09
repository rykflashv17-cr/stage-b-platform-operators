"""The Harbor publisher and ACR mirror must agree on each operator's tag."""
import json
import sys
from types import SimpleNamespace

import pytest
import yaml

from ci.publish import mirror as mirror_images

from ci.publish import images as publish_images


COMMIT = "a" * 40
PIPELINE = "11617"
VERSIONS = {"alpha": "2.0.0", "beta": "3.1.4"}


@pytest.mark.parametrize(("branch", "expected"), [
    ("develop", {"alpha": "2.0.0-dev.11617", "beta": "3.1.4-dev.11617"}),
    ("release", {"alpha": "2.0.0-rc.11617", "beta": "3.1.4-rc.11617"}),
    ("main", VERSIONS),
])
def test_publish_and_mirror_use_same_per_operator_tags(tmp_path, monkeypatch, branch, expected):
    projects = []
    images = {}
    acceptance = {
        "images": {}, "cases": dict.fromkeys(["marvin", "gaps", "abc", "empty", "input_error"])
    }
    for name, version in VERSIONS.items():
        directory = tmp_path / name
        directory.mkdir()
        projects.append(SimpleNamespace(name=name, version=version, path=directory))
        declaration = {"code": name, "operatorVersion": version,
                       "image": f"{publish_images.PREFIX}{name}:{version}"}
        (directory / "operator.yaml").write_text(yaml.safe_dump(declaration))
        image_id = "sha256:" + "b" * 64
        images[name] = {"image": image_id}
        acceptance["images"][name] = {"id": image_id}

    image_file = tmp_path / "images.json"
    acceptance_file = tmp_path / "acceptance.json"
    receipt = tmp_path / "harbor.json"
    result = tmp_path / "acr.json"
    image_file.write_text(json.dumps(images))
    acceptance_file.write_text(json.dumps(acceptance))
    for module in (publish_images, mirror_images):
        monkeypatch.setattr(module, "operator_projects", lambda root: projects)
    monkeypatch.setattr(publish_images.subprocess, "check_output", lambda *a, **kw: COMMIT)
    monkeypatch.setenv("CI_COMMIT_REF_PROTECTED", "true")
    monkeypatch.setenv("CI_COMMIT_BRANCH", branch)
    monkeypatch.setenv("CI_COMMIT_SHA", COMMIT)
    monkeypatch.setenv("CI_PIPELINE_ID", PIPELINE)

    def publish(plan):
        return {ref: {"image_id": image_id, "reference": ref.rsplit(":", 1)[0] + "@sha256:" + "c" * 64}
                for ref, image_id in plan.items()}
    monkeypatch.setattr(publish_images, "publish", publish)
    monkeypatch.setattr(sys, "argv", ["publish", "--gitlab", "--images", str(image_file),
                                     "--acceptance", str(acceptance_file), "--output", str(receipt)])
    publish_images.main()
    harbor = json.loads(receipt.read_text())
    assert harbor["commit"] == COMMIT
    assert set(harbor["images"]) == {publish_images.PREFIX + n + ":" + t for n, t in expected.items()}

    def mirror(items, registry, **kwargs):
        return {item.code: item.tag for item in items}
    monkeypatch.setattr(mirror_images, "mirror", mirror)
    monkeypatch.setattr(sys, "argv", ["mirror", "--registry-config", "unused", "--receipt", str(receipt),
                                     "--output", str(result)])
    mirror_images.main()
    assert json.loads(result.read_text()) == {"commit": COMMIT, "images": expected}
    if branch != "main":
        # A different pipeline must not consume this receipt.
        monkeypatch.setenv("CI_PIPELINE_ID", "11618")
        with pytest.raises(ValueError, match="Harbor tags"):
            mirror_images.main()


@pytest.mark.parametrize(("version", "pipeline"), [
    ("2.0.0", ""), ("2.0.0", "２"), ("2.0.0+build", PIPELINE), ("2.0.0-" + "a" * 120, PIPELINE),
])
def test_invalid_develop_tag_is_rejected(version, pipeline):
    from ci.publish.tags import publication_tag
    with pytest.raises(ValueError):
        publication_tag(version, "develop", pipeline)
