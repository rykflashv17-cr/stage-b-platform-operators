"""Persist actual deletion outcomes across filesystem and audit failures."""
import os
import time
from types import SimpleNamespace as NS

import pytest

from stage_b_mcap_v4 import deletion
from stage_b_mcap_v4.delivery import McapDeliveryOperator
from stage_b_mcap_v4.reporting import finish_quality
from test_deletion import write_mcap, rejected, events


@pytest.fixture
def prepared(tmp_path):
    root, reports = tmp_path / "sources", tmp_path / "reports"
    reports.mkdir()
    first, second = root / "first.mcap", root / "second.mcap"
    originals = {path: write_mcap(path) for path in (first, second)}
    records = [rejected(path, root, episode_index=i, reject_file=True) for i, path in enumerate((first, second))]
    return root, reports, (first, second), originals, records


def source_sync_failure(monkeypatch, root):
    real_fsync = deletion.os.fsync
    calls = []
    identity = root.stat()
    def fsync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            calls.append(fd)
            raise OSError("controlled source-directory sync failure")
        return real_fsync(fd)
    monkeypatch.setattr(deletion.os, "fsync", fsync)
    return calls


def audit_close_failure(monkeypatch):
    real_close = deletion._EventLog.close
    def close(log):
        real_close(log)
        raise OSError("controlled audit-close failure")
    monkeypatch.setattr(deletion._EventLog, "close", close)


def test_normal_deletions_report_durable_known_outcomes(prepared):
    root, reports, paths, _, records = prepared
    result = deletion.delete_rejected(records, input_root=root, events_path=reports / "events.jsonl")
    assert [item["status"] for item in result] == ["deleted", "deleted"]
    assert all(item["source_directory_synced"] is True for item in result)
    assert not any(path.exists() for path in paths)


def test_directory_sync_failure_stops_after_first_unlink_and_keeps_result(prepared, monkeypatch):
    root, reports, (first, second), originals, records = prepared
    calls = source_sync_failure(monkeypatch, root)
    with pytest.raises(deletion.DeletionExecutionError, match="sync failed after unlink") as exc:
        deletion.delete_rejected(records, input_root=root, events_path=reports / "events.jsonl")
    assert len(calls) == 1 and not first.exists()
    assert second.read_bytes() == originals[second]
    result = exc.value.completed_results
    assert len(result) == 1 and result[0]["status"] == "deleted"
    assert result[0]["source_directory_synced"] is False
    assert "sync failure" in result[0]["source_directory_sync_error"]
    assert [event["event"] for event in events(reports / "events.jsonl")] == ["delete_intent", "delete_durability_failed"]


def test_directory_sync_failure_is_nonzero_in_b04_summary(prepared, monkeypatch):
    root, reports, (first, second), originals, records = prepared
    context = NS(status="ready", error=None, batch=NS(manifest_path=reports / "normalized/manifest.json"))
    judged = finish_quality(records, context, NS(input_root=root, output_root=reports), time.perf_counter())
    source_sync_failure(monkeypatch, root)
    result = McapDeliveryOperator().run(judged, mode="run")
    assert result.exit_code == 1
    assert result.summary["deleted_files"] == 1 and result.summary["audit_error"]
    assert result.summary["execution_status"] == "failed"
    assert result.summary["deletion_sync_failed_files"] == 1
    assert not first.exists() and second.read_bytes() == originals[second]
    assert result.summary["deletion_results"][0]["source_directory_synced"] is False


def test_audit_close_failure_preserves_all_completed_deletions(prepared, monkeypatch):
    root, reports, paths, _, records = prepared
    audit_close_failure(monkeypatch)
    with pytest.raises(OSError, match="audit-close failure") as exc:
        deletion.delete_rejected(records, input_root=root, events_path=reports / "events.jsonl")
    assert not any(path.exists() for path in paths)
    assert [item["status"] for item in exc.value.completed_results] == ["deleted", "deleted"]


def test_audit_close_failure_keeps_b04_deleted_count(prepared, monkeypatch):
    root, reports, paths, _, records = prepared
    context = NS(status="ready", error=None, batch=NS(manifest_path=reports / "normalized/manifest.json"))
    judged = finish_quality(records, context, NS(input_root=root, output_root=reports), time.perf_counter())
    audit_close_failure(monkeypatch)
    result = McapDeliveryOperator().run(judged, mode="run")
    assert result.exit_code == 1 and result.summary["deleted_files"] == 2
    assert result.summary["execution_status"] == "failed"
    assert "audit-close failure" in result.summary["audit_error"]
    assert len(result.summary["deletion_results"]) == 2
    assert not any(path.exists() for path in paths)


@pytest.mark.parametrize("failed_event, deleted", [("delete_intent", 0), ("delete_complete", 1)])
def test_secondary_audit_close_does_not_mask_primary_audit_failure(prepared, monkeypatch, failed_event, deleted):
    root, reports, paths, originals, records = prepared
    real_append = deletion._EventLog.append
    def append(log, payload):
        if payload["event"] == failed_event:
            raise deletion.AuditWriteError("primary audit-write failure")
        return real_append(log, payload)
    monkeypatch.setattr(deletion._EventLog, "append", append)
    audit_close_failure(monkeypatch)
    with pytest.raises(deletion.AuditWriteError, match="primary audit-write failure") as exc:
        deletion.delete_rejected(records, input_root=root, events_path=reports / "events.jsonl")
    assert len(exc.value.completed_results) == deleted
    assert all(r["status"] == "deleted" for r in exc.value.completed_results)
    assert "audit-close failure" in exc.value.cleanup_errors[0]
    for path in paths[deleted:]:
        assert path.read_bytes() == originals[path]


def test_sync_failure_survives_failed_failure_audit_and_close(prepared, monkeypatch):
    root, reports, (first, second), originals, records = prepared
    source_sync_failure(monkeypatch, root)
    real_append = deletion._EventLog.append
    def append(log, payload):
        if payload["event"] == "delete_durability_failed":
            raise deletion.AuditWriteError("secondary failure-event write error")
        return real_append(log, payload)
    monkeypatch.setattr(deletion._EventLog, "append", append)
    audit_close_failure(monkeypatch)
    with pytest.raises(deletion.DeletionExecutionError, match="sync failed after unlink") as exc:
        deletion.delete_rejected(records, input_root=root, events_path=reports / "events.jsonl")
    assert not first.exists() and second.read_bytes() == originals[second]
    assert len(exc.value.completed_results) == 1
    assert len(exc.value.cleanup_errors) == 2
    assert "failure-event" in exc.value.cleanup_errors[0]
    assert "audit-close" in exc.value.cleanup_errors[1]


def test_source_directory_close_failure_stops_and_keeps_unlink_result(prepared, monkeypatch):
    root, reports, (first, second), originals, records = prepared
    real_close = deletion.os.close
    identity = root.stat()
    failures = []
    def close(fd):
        info = os.fstat(fd)
        fail = not first.exists() and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)
        real_close(fd)
        if fail:
            failures.append(fd)
            raise OSError("controlled source-directory close failure")
    monkeypatch.setattr(deletion.os, "close", close)
    with pytest.raises(OSError, match="source-directory close failure") as exc:
        deletion.delete_rejected(records, input_root=root, events_path=reports / "events.jsonl")
    assert len(failures) == 1
    assert not first.exists() and second.read_bytes() == originals[second]
    assert len(exc.value.completed_results) == 1 and exc.value.completed_results[0]["status"] == "deleted"


@pytest.mark.parametrize("syscall_applied", [False, True])
def test_failed_unlink_is_unknown_never_mislabeled_skipped(prepared, monkeypatch, syscall_applied):
    root, reports, (first, second), originals, records = prepared
    real_unlink = deletion.os.unlink
    def unlink(name, *, dir_fd=None):
        if syscall_applied:
            real_unlink(name, dir_fd=dir_fd)
        raise OSError("controlled ambiguous unlink failure")
    monkeypatch.setattr(deletion.os, "unlink", unlink)
    with pytest.raises(deletion.DeletionExecutionError, match="outcome unknown") as exc:
        deletion.delete_rejected(records, input_root=root, events_path=reports / "events.jsonl")
    assert first.exists() == (not syscall_applied)
    assert second.read_bytes() == originals[second]
    assert len(exc.value.completed_results) == 1
    assert exc.value.completed_results[0]["status"] == "unknown"
    assert exc.value.completed_results[0]["deletion_attempted"] is True
    assert [e["event"] for e in events(reports / "events.jsonl")] == ["delete_intent", "deletion_outcome_unknown"]
