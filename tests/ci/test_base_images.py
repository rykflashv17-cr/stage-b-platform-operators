from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ci.base_images.config import bootstrap_reference, recipes, repo
from ci.base_images.manage import (
    Harbor, PLATFORM, PARENT, REVISION, TREE, build, parent_image,
    prepare_image, publish, remote, validate_ci, validate_receipt, verify,
)


def recipe_at(root, name="new-cpu", version="1.0.0"):
    path = root / "images/runtimes" / name
    path.mkdir(parents=True)
    for filename, value in {"VERSION": version + "\n", "Dockerfile": "FROM scratch\n", "check.sh": "true\n"}.items():
        (path / filename).write_text(value)
    return recipes(root)["runtime/" + name]


class FoundationTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.recipe = recipe_at(self.root)
        self.digest = "sha256:" + "a" * 64
        self.commit = "1" * 40
        self.origin = {"project": "stridingai/data/data-platform-operators", "branch": "develop",
                       "sourceCommit": self.commit, "pipelineId": "7"}
        self.row = {**self.origin, "repository": self.recipe.repository, "version": "1.0.0",
                    "platform": PLATFORM, "recipeTree": "2" * 40, "digest": self.digest, "parent": None}

    def test_unconsumed_recipes_are_independent(self):
        second = recipe_at(self.root, "second")
        self.assertEqual(set(recipes(self.root)), {"runtime/new-cpu", "runtime/second"})
        for version in ("01.0.0", "1.0.0\n1.0.1", "0.2", "1.0.0-dev", "1.0.0 "):
            (second.path / "VERSION").write_text(version + "\n")
            with self.assertRaisesRegex(ValueError, "VERSION"):
                recipes(self.root)

    def test_builder_requires_an_exact_runtime_and_sample(self):
        directory = self.root / "images/builders/compiler"
        directory.mkdir(parents=True)
        (directory / "VERSION").write_text("1.0.0")
        (directory / "check.sh").touch()
        parent = self.recipe.repository + ":1.0.0"
        (directory / "Dockerfile").write_text(f"FROM {parent}\nRUN true\n")
        with self.assertRaisesRegex(ValueError, "check-build.sh"):
            recipes(self.root)
        (directory / "check-build.sh").touch()
        self.assertEqual(recipes(self.root)["builder/compiler"].parent, parent)
        (directory / "Dockerfile").write_text("FROM " + parent.replace("1.0.0", "dev"))
        with self.assertRaisesRegex(ValueError, "runtime"):
            recipes(self.root)

    def test_only_known_seed_recipe_can_use_bootstrap(self):
        import shutil
        source = Path(__file__).resolve().parents[2] / "images/runtimes/python312-cpu"
        target = self.root / "images/runtimes/python312-cpu"
        shutil.copytree(source, target)
        # Bootstrap is restricted to the original 1.0.0 recipe, not the current runtime.
        (target / "VERSION").write_text("1.0.0\n")
        (target / "Dockerfile").write_text(
            "FROM harbor.striding.internal/docker-hub/library/python@sha256:"
            "1148dbb014615300fa7e713b2f94f550c0d1c69780c0805f84f093ceee34dd0b\n"
            "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
            'RUN python -c "import sys; assert sys.version_info[:2] == (3, 12)"\n'
        )
        tag = repo("runtime", "python312-cpu") + ":1.0.0"
        self.assertIn("@sha256:b8b1fb", bootstrap_reference(self.root, tag))
        self.assertIsNone(bootstrap_reference(self.root, tag.replace("1.0.0", "1.0.1")))
        (target / "changed-input").write_text("surprise")
        with self.assertRaisesRegex(ValueError, "bootstrap"):
            bootstrap_reference(self.root, tag)

    def test_publication_requires_protected_project_develop_push(self):
        valid = dict(CI_COMMIT_BRANCH="develop", CI_PROJECT_PATH=self.origin["project"],
                     CI_PIPELINE_SOURCE="push", CI_COMMIT_REF_PROTECTED="true",
                     CI_COMMIT_SHA=self.commit, CI_PIPELINE_ID="7")
        self.assertEqual(validate_ci("publish", self.commit, valid), self.origin)
        for changed in [dict(CI_COMMIT_BRANCH="main"), dict(CI_PROJECT_PATH="other/operators"),
                        dict(CI_PIPELINE_SOURCE="merge_request_event"), dict(CI_COMMIT_REF_PROTECTED="false")]:
            with self.assertRaises(ValueError):
                validate_ci("publish", self.commit, {**valid, **changed})

    def test_publication_persists_receipt_then_advances_by_digest(self):
        registry = Mock()
        registry.tags.return_value = []
        registry.read_receipt.return_value = self.row
        registry.resolve.return_value = None
        prepare = Mock(return_value=self.row)
        row = publish(self.recipe, registry, prepare, lambda row: self.assertEqual(row, self.row))
        self.assertEqual(row, self.row)
        registry.save_receipt.assert_called_once_with(self.row)
        self.assertEqual([c.args for c in registry.tag.call_args_list],
                         [(self.digest, "1.0.0"), (self.digest, "dev")])

    def test_retry_does_not_rebuild_or_rewrite_the_exact_tag(self):
        registry = Mock()
        registry.tags.return_value = ["1.0.0", "receipt-1.0.0"]
        registry.read_receipt.return_value = self.row
        def exact_or_dev(tag):
            return self.digest if tag == "1.0.0" else None
        registry.resolve.side_effect = exact_or_dev
        prepare = Mock(side_effect=AssertionError)
        publish(self.recipe, registry, prepare, lambda row: None)
        registry.tag.assert_called_once_with(self.digest, "dev")
        registry.save_receipt.assert_not_called()

    def test_delayed_version_does_not_create_or_move_any_tag(self):
        registry = Mock()
        registry.tags.return_value = ["1.10.0"]
        with self.assertRaisesRegex(ValueError, "newer"):
            publish(self.recipe, registry, Mock(), Mock())
        registry.tag.assert_not_called()
        registry.save_receipt.assert_not_called()

    def test_existing_numeric_tag_requires_accepted_digest(self):
        registry = Mock()
        registry.tags.return_value = ["1.0.0", "receipt-1.0.0"]
        registry.read_receipt.return_value = self.row
        registry.resolve.return_value = "sha256:" + "b" * 64
        with self.assertRaisesRegex(ValueError, "conflict"):
            publish(self.recipe, registry, Mock(), Mock())
        registry.tag.assert_not_called()

    def test_conflicting_numeric_tag_readback_cannot_advance_dev(self):
        registry = Mock()
        registry.tags.return_value = ["1.0.0", "receipt-1.0.0"]
        registry.read_receipt.return_value = self.row
        registry.resolve.return_value = self.digest
        registry.check_tag.side_effect = ValueError("digest conflict")
        with self.assertRaisesRegex(ValueError, "conflict"):
            publish(self.recipe, registry, Mock(), Mock())
        registry.tag.assert_not_called()

    def test_main_checks_version_against_receipt_read_only(self):
        registry = Mock()
        registry.read_receipt.return_value = self.row
        registry.resolve.return_value = self.digest
        self.assertEqual(verify(self.recipe, registry, lambda row: None), self.row)
        registry.tag.assert_not_called()
        registry.save_receipt.assert_not_called()
        registry.resolve.return_value = None
        with self.assertRaisesRegex(ValueError, "conflict"):
            verify(self.recipe, registry, lambda row: None)

    def test_receipt_from_another_recipe_or_branch_fails_before_promotion(self):
        for change in ({"branch": "main"}, {"recipeTree": "3" * 40}, {"version": "1.2.0"}, {"sourceCommit": "fake"}):
            row = {**self.row, **change}
            with self.assertRaisesRegex(ValueError, "provenance"):
                validate_receipt(self.recipe, row, self.commit, self.row["recipeTree"], None)

    def test_builder_pins_the_from_instruction_and_runs_its_sample_in_parent(self):
        directory = self.root / "images/builders/tool"
        directory.mkdir(parents=True)
        base = self.recipe.repository + ":1.0.0"
        (directory / "VERSION").write_text("1.0.0")
        (directory / "check.sh").touch()
        (directory / "check-build.sh").touch()
        (directory / "Dockerfile").write_text(f"# Source {base}\nFROM {base} AS build\nRUN true\n")
        recipe = recipes(self.root)["builder/tool"]
        parent = {"reference": base, "digest": self.digest}
        contents = []
        def collect(*args, stdin):
            contents.append(stdin.read().decode())
            self.assertIn(PARENT + "=" + parent_image(parent), args)
        with patch("ci.base_images.manage.docker", side_effect=collect), patch("ci.base_images.manage.smoke", return_value={"Id": "id"}) as smoke:
            self.assertEqual(build(recipe, self.origin, self.row["recipeTree"], parent), "id")
        self.assertEqual(contents, [f"# Source {base}\nFROM {parent_image(parent)} AS build\nRUN true\n"])
        self.assertEqual(smoke.call_args.args[-1], parent_image(parent))

    def test_recovery_tag_requires_the_accepted_parent(self):
        parent = {"reference": self.recipe.repository + ":1.0.0", "digest": self.digest}
        incomplete_labels = {REVISION: self.commit, TREE: self.row["recipeTree"]}
        registry = Mock()
        registry.resolve.return_value = self.digest
        with patch("ci.base_images.manage.pull", return_value={"Config": {"Labels": incomplete_labels}}):
            with self.assertRaisesRegex(ValueError, "recovery tag conflict"):
                prepare_image(self.recipe, registry, self.origin, self.row["recipeTree"], parent)

    def test_trusted_bootstrap_refuses_conflicting_harbor_source_metadata(self):
        with patch("ci.base_images.manage.bootstrap_reference", return_value="seed@" + self.digest), \
             patch("ci.base_images.manage.pull", return_value={"Config": {"Labels": {REVISION: self.commit}}}), \
             patch("ci.base_images.manage.smoke") as smoke:
            with self.assertRaisesRegex(ValueError, "bootstrap source"):
                prepare_image(self.recipe, Mock(), self.origin, self.row["recipeTree"], None)
            smoke.assert_not_called()

    def test_read_receipt_by_content_and_verify_identity(self):
        import hashlib
        import json
        from ci.base_images.manage import ANNOTATION, RECEIPT_TYPE
        reg = Harbor(self.recipe.repository)
        for row, valid in [(self.row, True), ({**self.row, "repository": "other"}, False)]:
            raw = json.dumps({"artifactType": RECEIPT_TYPE,
                              "annotations": {ANNOTATION: json.dumps(row)}}).encode()
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            with patch.object(reg, "resolve", return_value=digest), patch("subprocess.check_output", return_value=raw):
                if valid:
                    self.assertEqual(reg.read_receipt("1.0.0"), self.row)
                else:
                    with self.assertRaisesRegex(ValueError, "identity"):
                        reg.read_receipt("1.0.0")

    def test_missing_is_distinct_from_registry_denial(self):
        tag = self.recipe.repository + ":1.0.0"
        for error, missing in [(f'failed to resolve reference "{tag}": {tag}: not found', True),
                               ("unauthorized: access to the requested resource is denied", False)]:
            with patch("ci.base_images.manage.subprocess.run", return_value=CompletedProcess([], 1, "", error)):
                if missing:
                    self.assertIsNone(remote(tag))
                else:
                    with self.assertRaises(RuntimeError):
                        remote(tag)
