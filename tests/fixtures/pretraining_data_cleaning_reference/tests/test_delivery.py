"""Real file delivery/audits, with explicit controlled upstream numerics."""
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from stage_b_mcap_v4 import delivery, operators, load_quality_batch
from test_p0_input_contract import input_runtime
from test_serial_pipeline import numerical_engine, gap_source, refs
from test_mcap_integration import write_marvin_mcap


@pytest.fixture
def prepared(numerical_engine, tmp_path):
    spec, cfg, adapter, _, _, _ = numerical_engine
    paths = [tmp_path / name / "episode.mcap" for name in ("任务甲", "任务乙")]
    for path in paths:
        write_marvin_mcap(path)
    paths += [gap_source(tmp_path / "gap"), tmp_path / "broken.mcap"]
    paths[-1].write_bytes(b"invalid MCAP")
    originals = {p: p.read_bytes() for p in paths}
    batch = operators.McapReadOperator().run_batch(refs(paths), spec, cfg, adapter)
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    assert context.status == "ready"
    return spec, judged, paths, originals


def test_b03_is_read_only_and_b04_uses_reloadable_verdicts_only(prepared, monkeypatch):
    spec, judged, paths, originals = prepared
    assert all(p.read_bytes() == value for p, value in originals.items())
    for name in ("accepted", "deletion_plan.json", "deletion_events.jsonl", "run_summary.json"):
        assert not (spec.output_root / name).exists()
    assert not hasattr(operators.QualityDetectionOperator, "delete_rejected")
    shutil.rmtree(spec.output_root / "normalized")
    for cls, method in ((operators.McapReadOperator, "run_batch"),
                        (operators.GlobalStatsOperator, "run"),
                        (operators.QualityDetectionOperator, "run_batch")):
        monkeypatch.setattr(cls, method, lambda *a, **kw: pytest.fail("B04 must not re-run upstream operators"))
    reloaded = load_quality_batch(judged.manifest_path)
    reloaded.summary["kept_files"] = 999  # B04 reads verified saved evidence.
    result = delivery.McapDeliveryOperator().run(reloaded)
    assert result.exit_code == 1  # the broken input is reported, not silently passed
    assert result.summary["exported_files"] == 2 and result.summary["kept_files"] == 2
    exports = json.loads((spec.output_root / "delivery_manifest.json").read_text())["records"]
    for record, source in zip(exports, paths[:2]):
        exported = spec.output_root / "accepted" / source.relative_to(spec.input_root)
        assert exported.read_bytes() == originals[source]
        assert exported.stat().st_ino != source.stat().st_ino
        assert record["sha256"] == hashlib.sha256(originals[source]).hexdigest()
    assert len(list((spec.output_root / "accepted").rglob("*.mcap"))) == 2
    assert all(p.suffix == ".mcap" for p in (spec.output_root / "accepted").rglob("*") if p.is_file())
    assert all(p.read_bytes() == value for p, value in originals.items())
    assert not (spec.output_root / "deletion_events.jsonl").exists()


def test_run_publishes_passed_files_and_evidence_before_deletion(prepared, monkeypatch):
    spec, judged, paths, originals = prepared
    real_delete, observed = delivery.delete_rejected, []
    def delete(records, **kwargs):
        manifest = json.loads((spec.output_root / "delivery_manifest.json").read_text())
        assert manifest["status"] == "complete" and manifest["exported_files"] == 2
        assert (spec.output_root / "quality_batch.json").is_file()
        assert (spec.output_root / "deletion_plan.json").is_file()
        assert all(Path(r["destination"]).read_bytes() == originals[Path(r["source"])] for r in manifest["records"])
        observed.append(True)
        return real_delete(records, **kwargs)
    monkeypatch.setattr(delivery, "delete_rejected", delete)
    result = delivery.McapDeliveryOperator().run(judged, mode="run")
    assert observed == [True] and result.summary["deleted_files"] == 1
    assert not paths[2].exists()
    assert all(p.read_bytes() == originals[p] for p in (paths[0], paths[1], paths[3]))


@pytest.mark.parametrize("fail_on", [1, 2])
def test_copy_failure_publishes_no_partial_batch_and_blocks_deletion(prepared, monkeypatch, fail_on):
    spec, judged, paths, originals = prepared
    real_copy, calls = delivery._copy_checked, []
    def copy(record, target, root):
        calls.append(record)
        if len(calls) == fail_on:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"partial write")
            raise OSError("controlled output disk failure")
        return real_copy(record, target, root)
    monkeypatch.setattr(delivery, "_copy_checked", copy)
    result = delivery.McapDeliveryOperator().run(judged, mode="run")
    assert result.exit_code == 1 and result.summary["delivery_status"] == "failed"
    assert result.summary["exported_files"] == result.summary["deleted_files"] == 0
    assert not (spec.output_root / "accepted").exists()
    assert not list(spec.output_root.glob(".delivery-*"))
    assert all(p.read_bytes() == value for p, value in originals.items())


def test_changed_passed_source_cannot_be_exported_as_the_judged_file(prepared):
    spec, judged, paths, originals = prepared
    paths[0].write_bytes(originals[paths[0]] + b"changed")
    result = delivery.McapDeliveryOperator().run(judged, mode="run")
    assert result.exit_code == 1 and "identity changed" in result.summary["export_error"]
    assert not (spec.output_root / "accepted").exists() and paths[2].exists()
    assert result.summary["deleted_files"] == 0


def test_source_change_during_copy_prevents_publication(prepared, monkeypatch):
    spec, judged, paths, originals = prepared
    real_capture = delivery.capture_source_identity
    def changed(source, root):
        if source == paths[0]:
            source.write_bytes(originals[source] + b"changed while copying")
        return real_capture(source, root)
    monkeypatch.setattr(delivery, "capture_source_identity", changed)
    result = delivery.McapDeliveryOperator().run(judged, mode="run")
    assert result.exit_code == 1 and "pathname changed" in result.summary["export_error"]
    assert not (spec.output_root / "accepted").exists() and paths[2].exists()


def test_changed_rejected_source_is_retained_after_successful_passed_export(prepared):
    spec, judged, paths, originals = prepared
    paths[2].write_bytes(originals[paths[2]] + b"new content")
    result = delivery.McapDeliveryOperator().run(judged, mode="run")
    assert result.summary["exported_files"] == 2 and result.summary["deleted_files"] == 0
    assert result.summary["deletion_skipped_files"] == 1 and result.exit_code == 1
    assert paths[2].exists()


def test_replaced_symlink_is_not_copied_or_deleted(prepared, tmp_path):
    spec, judged, paths, originals = prepared
    target = tmp_path / "external.mcap"
    target.write_bytes(b"unrelated file")
    paths[0].unlink()
    paths[0].symlink_to(target)
    result = delivery.McapDeliveryOperator().run(judged, mode="run")
    assert result.exit_code == 1 and result.summary["deleted_files"] == 0
    assert target.read_bytes() == b"unrelated file" and paths[0].is_symlink() and paths[2].exists()
    assert not (spec.output_root / "accepted").exists()


@pytest.mark.parametrize("filename", ["decisions.jsonl", "quality_summary.json"])
def test_changed_handoff_is_rejected_before_any_delivery(prepared, filename):
    spec, judged, paths, originals = prepared
    report = spec.output_root / filename
    report.write_bytes(report.read_bytes() + b" ")
    with pytest.raises(ValueError, match="checksum"):
        delivery.McapDeliveryOperator().run(judged, mode="run")
    assert not (spec.output_root / "delivery_plan.json").exists()
    assert all(p.read_bytes() == value for p, value in originals.items())


def test_inconsistent_verdict_is_not_executed_even_with_updated_checksum(prepared):
    spec, judged, paths, originals = prepared
    report = spec.output_root / "decisions.jsonl"
    records = [json.loads(line) for line in report.read_text().splitlines()]
    records[0]["reject_file"] = True
    report.write_text("".join(json.dumps(r) + "\n" for r in records))
    manifest = json.loads(judged.manifest_path.read_text())
    manifest["decisions_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    judged.manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="inconsistent quality verdict"):
        delivery.McapDeliveryOperator().run(judged, mode="run")
    assert all(p.read_bytes() == value for p, value in originals.items())


def test_existing_output_is_not_overwritten(prepared):
    spec, judged, paths, originals = prepared
    output = spec.output_root / "accepted"
    output.mkdir()
    sentinel = output / "existing.mcap"
    sentinel.write_bytes(b"existing delivery")
    with pytest.raises(FileExistsError, match="already exist"):
        delivery.McapDeliveryOperator().run(judged, mode="run")
    assert sentinel.read_bytes() == b"existing delivery"
    assert all(p.read_bytes() == value for p, value in originals.items())


def test_second_delivery_does_not_turn_quality_into_implicit_source_deletion(prepared):
    spec, judged, paths, originals = prepared
    delivery.McapDeliveryOperator().run(judged, mode="quality")
    with pytest.raises(FileExistsError, match="already exist"):
        delivery.McapDeliveryOperator().run(judged, mode="run")
    assert all(p.read_bytes() == value for p, value in originals.items())


def test_delivery_manifest_write_failure_prevents_any_source_deletion(prepared, monkeypatch):
    spec, judged, paths, originals = prepared
    real_write = delivery.write_json_durable
    def write(path, payload):
        if path.name == "delivery_manifest.json":
            raise OSError("controlled audit failure")
        return real_write(path, payload)
    monkeypatch.setattr(delivery, "write_json_durable", write)
    with pytest.raises(OSError, match="audit failure"):
        delivery.McapDeliveryOperator().run(judged, mode="run")
    assert all(p.read_bytes() == value for p, value in originals.items())
    assert not (spec.output_root / "deletion_events.jsonl").exists()
