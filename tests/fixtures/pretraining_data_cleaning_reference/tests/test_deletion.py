from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mcap.writer import Writer

from stage_b_mcap_v4 import deletion


def write_mcap(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        writer = Writer(stream)
        writer.start()
        channel = writer.register_channel("/test", "raw", 0)
        writer.add_message(channel, 1_000_000_000, b"quality-fixture", 1_000_000_000)
        writer.finish()
    return path.read_bytes()


def rejected(path: Path, root: Path, **updates) -> dict:
    record = {
        "status": "rejected", "episode_index": 0, "source": str(path),
        "source_identity": deletion.capture_source_identity(path, root),
        "deletion_eligible": True, "deletion_blocked_reason": None,
    }
    record.update(updates)
    return record


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_rejected_deleted_but_kept_and_error_sources_are_byte_identical(tmp_path: Path) -> None:
    root = tmp_path / "sources"
    bad, good, error = (root / name for name in ("bad.mcap", "good.mcap", "error.mcap"))
    write_mcap(bad)
    good_bytes, error_bytes = write_mcap(good), write_mcap(error)
    records = [
        rejected(bad, root), rejected(good, root, status="kept"),
        rejected(error, root, status="error"),
    ]
    audit = tmp_path / "events.jsonl"
    result = deletion.delete_rejected(records, input_root=root, events_path=audit)
    assert [record["status"] for record in result] == ["deleted"]
    assert not bad.exists()
    assert good.read_bytes() == good_bytes
    assert error.read_bytes() == error_bytes
    assert [event["event"] for event in events(audit)] == [
        "delete_intent", "delete_complete", "deletion_batch_complete",
    ]


def test_changed_source_is_retained(tmp_path: Path) -> None:
    source = tmp_path / "source.mcap"
    write_mcap(source)
    record = rejected(source, tmp_path)
    with source.open("ab") as stream:
        stream.write(b"changed after checking")
    changed_bytes = source.read_bytes()
    result = deletion.delete_rejected([record], input_root=tmp_path, events_path=tmp_path / "events.jsonl")
    assert result[0]["status"] == "skipped"
    assert "identity changed" in result[0]["reason"]
    assert source.read_bytes() == changed_bytes


@pytest.mark.parametrize("kind", [
    "source_symlink", "ancestor_symlink", "ancestor_symlink_dotdot", "outside_root",
])
def test_unsafe_paths_never_delete_targets(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    target = tmp_path / "outside" / "target.mcap"
    original = write_mcap(target)
    identity = {key: int(getattr(target.stat(), key)) for key in (
        "st_dev", "st_ino", "st_size", "st_mtime_ns",
    )}
    if kind == "source_symlink":
        source = root / "link.mcap"
        source.symlink_to(target)
    elif kind == "ancestor_symlink":
        parent = root / "link"
        parent.symlink_to(target.parent, target_is_directory=True)
        source = parent / target.name
    elif kind == "ancestor_symlink_dotdot":
        nested = target.parent / "nested"
        nested.mkdir()
        parent = root / "link"
        parent.symlink_to(nested, target_is_directory=True)
        source = parent / ".." / target.name
    else:
        source = target
    record = {
        "status": "rejected", "source": str(source), "episode_index": 0,
        "source_identity": identity, "deletion_eligible": True,
    }
    result = deletion.delete_rejected([record], input_root=root, events_path=tmp_path / "events.jsonl")
    assert result[0]["status"] == "skipped"
    assert target.read_bytes() == original
    assert source.exists()
    with pytest.raises((OSError, deletion.UnsafeSourceError)):
        deletion.capture_source_identity(source, root)


def test_missing_identity_is_retained(tmp_path: Path) -> None:
    source = tmp_path / "source.mcap"
    original = write_mcap(source)
    record = rejected(source, tmp_path, source_identity=None)
    result = deletion.delete_rejected([record], input_root=tmp_path, events_path=tmp_path / "events.jsonl")
    assert result[0]["status"] == "skipped"
    assert source.read_bytes() == original


def test_delete_intent_is_written_and_fsynced_before_unlink(tmp_path: Path, monkeypatch) -> None:
    source, audit = tmp_path / "source.mcap", tmp_path / "events.jsonl"
    write_mcap(source)
    record = rejected(source, tmp_path)
    real_write, real_fsync, real_unlink = os.write, os.fsync, os.unlink
    written: dict[int, int] = {}
    synced: dict[int, int] = {}
    observed = []

    def write(fd, data):
        result = real_write(fd, data)
        written[fd] = written.get(fd, 0) + result
        return result

    def fsync(fd):
        result = real_fsync(fd)
        synced[fd] = written.get(fd, 0)
        return result

    def unlink(path, *, dir_fd=None):
        assert events(audit)[-1]["event"] == "delete_intent"
        assert written and all(synced.get(fd) == count for fd, count in written.items())
        observed.append(path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(deletion.os, "write", write)
    monkeypatch.setattr(deletion.os, "fsync", fsync)
    monkeypatch.setattr(deletion.os, "unlink", unlink)
    deletion.delete_rejected([record], input_root=tmp_path, events_path=audit)
    assert observed == [source.name]


def test_audit_failure_before_unlink_retains_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.mcap"
    original = write_mcap(source)
    record = rejected(source, tmp_path)
    real_append = deletion._EventLog.append

    def append(self, payload):
        if payload["event"] == "delete_intent":
            raise deletion.AuditWriteError("simulated full audit disk")
        return real_append(self, payload)

    monkeypatch.setattr(deletion._EventLog, "append", append)
    with pytest.raises(deletion.AuditWriteError):
        deletion.delete_rejected([record], input_root=tmp_path, events_path=tmp_path / "events.jsonl")
    assert source.read_bytes() == original


def test_source_changed_during_intent_write_is_not_deleted(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.mcap"
    write_mcap(source)
    record = rejected(source, tmp_path)
    real_append = deletion._EventLog.append

    def append(self, payload):
        result = real_append(self, payload)
        if payload["event"] == "delete_intent":
            with source.open("ab") as stream:
                stream.write(b"concurrent update")
        return result

    monkeypatch.setattr(deletion._EventLog, "append", append)
    results = deletion.delete_rejected([record], input_root=tmp_path, events_path=tmp_path / "events.jsonl")
    assert source.exists()
    assert results[0]["status"] == "skipped"
    assert "immediately before deletion" in results[0]["reason"]


def test_audit_failure_after_unlink_stops_further_deletions(tmp_path: Path, monkeypatch) -> None:
    first, second = tmp_path / "first.mcap", tmp_path / "second.mcap"
    write_mcap(first)
    second_bytes = write_mcap(second)
    records = [rejected(first, tmp_path), rejected(second, tmp_path)]
    real_append = deletion._EventLog.append

    def append(self, payload):
        if payload["event"] == "delete_complete":
            raise deletion.AuditWriteError("simulated result-event failure")
        return real_append(self, payload)

    monkeypatch.setattr(deletion._EventLog, "append", append)
    with pytest.raises(deletion.AuditWriteError) as exc:
        deletion.delete_rejected(records, input_root=tmp_path, events_path=tmp_path / "events.jsonl")
    assert not first.exists()
    assert second.read_bytes() == second_bytes
    assert exc.value.completed_results[0]["source"] == str(first)
    assert exc.value.completed_results[0]["status"] == "deleted"


def test_existing_audit_is_not_overwritten_or_replayed(tmp_path: Path) -> None:
    source, audit = tmp_path / "source.mcap", tmp_path / "events.jsonl"
    original = write_mcap(source)
    record = rejected(source, tmp_path)
    audit.write_text("existing audit\n")
    with pytest.raises(FileExistsError):
        deletion.delete_rejected([record], input_root=tmp_path, events_path=audit)
    assert audit.read_text() == "existing audit\n"
    assert source.read_bytes() == original
