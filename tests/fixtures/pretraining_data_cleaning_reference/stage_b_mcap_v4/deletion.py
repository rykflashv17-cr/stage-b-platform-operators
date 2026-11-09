"""Audited whole-source deletion; no video or MCAP rewriting takes place here."""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class UnsafeSourceError(ValueError):
    """The selected pathname cannot safely be used for destructive processing."""


class AuditWriteError(OSError):
    """Deletion must stop because a durable audit event could not be written."""


class DeletionExecutionError(OSError):
    """A deletion attempt or its durability is uncertain; stop the batch."""


def _add_cleanup_error(primary: BaseException, secondary: BaseException, operation: str) -> None:
    errors = list(getattr(primary, "cleanup_errors", []))
    errors.append(f"{operation}: {type(secondary).__name__}: {secondary}")
    primary.cleanup_errors = errors


@contextmanager
def _close_preserving_failure(close, operation: str):
    """Never replace an earlier failure with a later close failure."""
    try:
        yield
    except BaseException as primary:
        try:
            close()
        except BaseException as secondary:
            _add_cleanup_error(primary, secondary, operation)
        raise
    else:
        close()


def _absolute(path: Path) -> Path:
    # Keep symlinks visible. Path.resolve() would conceal a symlink input.
    path = Path(path).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                raise UnsafeSourceError(f"directory is missing, not a directory, or a symlink: {path}") from exc
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_parent(path: Path, input_root: Path) -> tuple[int, str]:
    path, root = _absolute(path), _absolute(input_root)
    # Validate the original root spelling before normalizing benign '..'
    # components for containment. A symlink followed by '..' must stay visible.
    root_fd = _open_directory(root)
    os.close(root_fd)
    try:
        rel = Path(os.path.normpath(path)).relative_to(Path(os.path.normpath(root)))
    except ValueError as exc:
        raise UnsafeSourceError(f"source is outside input_root: {path}") from exc
    if not rel.parts or path.suffix != ".mcap":
        raise UnsafeSourceError(f"source must be an MCAP file below input_root: {path}")
    return _open_directory(path.parent), path.name


def _identity(st: os.stat_result) -> dict[str, int]:
    return {key: int(getattr(st, key)) for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")}


def capture_source_identity(path: Path, input_root: Path) -> dict[str, int]:
    """lstat a regular MCAP without following its path or parent symlinks."""
    fd, name = _open_parent(path, input_root)
    try:
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeSourceError(f"source is not a regular MCAP (symlinks are never deleted): {path}")
        return _identity(st)
    finally:
        os.close(fd)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_durable(path: Path, payload: Any) -> None:
    """Create a report exactly once and durably finish it before deletion."""
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


class _EventLog:
    def __init__(self, path: Path):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        self.fd = os.open(path, flags, 0o600)
        try:
            os.fsync(self.fd)
            fsync_directory(path.parent)
        except BaseException as primary:
            try:
                os.close(self.fd)
            except BaseException as secondary:
                _add_cleanup_error(primary, secondary, "audit close during initialization")
            raise

    def append(self, payload: dict[str, Any]) -> None:
        event = {"event_time_utc": datetime.now(timezone.utc).isoformat(), **payload}
        try:
            data = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            view = memoryview(data)
            while view:
                size = os.write(self.fd, view)
                if size <= 0:
                    raise OSError("short audit write")
                view = view[size:]
            os.fsync(self.fd)
        except Exception as exc:
            raise AuditWriteError("Cannot durably write deletion audit; deletion stopped") from exc

    def close(self) -> None:
        os.close(self.fd)


def delete_rejected(
    plan_records: list[dict[str, Any]], *, input_root: Path, events_path: Path,
) -> list[dict[str, Any]]:
    """Apply only rejected, identity-checked original paths in a persisted plan.

    Callers must finish decisions.jsonl and deletion_plan.json before calling.
    This helper writes a durable intent before each unlink and a result after it.
    Existing audit logs are refused rather than overwritten or resumed implicitly.
    """
    results: list[dict[str, Any]] = []
    try:
        log = _EventLog(events_path)
        with _close_preserving_failure(log.close, "audit log close"):
            for record in plan_records:
                if record.get("status") != "rejected":
                    continue
                source = Path(record["source"])
                base = {"episode_index": record.get("episode_index"), "source": str(source)}
                expected = record.get("source_identity")
                if not record.get("deletion_eligible", False) or not expected:
                    result = {**base, "status": "skipped", "reason": record.get("deletion_blocked_reason") or "source identity unavailable"}
                    results.append(result)
                    log.append({"event": "deletion_skipped", **result})
                    continue
                try:
                    parent_fd, name = _open_parent(source, input_root)
                except (OSError, UnsafeSourceError) as exc:
                    result = {**base, "status": "skipped", "reason": f"{type(exc).__name__}: {exc}"}
                    results.append(result)
                    log.append({"event": "deletion_skipped", **result})
                    continue
                with _close_preserving_failure(lambda: os.close(parent_fd), "source directory close"):
                    try:
                        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                        if not stat.S_ISREG(st.st_mode):
                            raise UnsafeSourceError("source changed to a symlink or non-regular file")
                        actual = _identity(st)
                        if actual != expected:
                            raise UnsafeSourceError("source identity changed since quality read")
                    except (OSError, UnsafeSourceError) as exc:
                        result = {**base, "status": "skipped", "reason": f"{type(exc).__name__}: {exc}"}
                        results.append(result)
                        log.append({"event": "deletion_skipped", **result})
                        continue
                    log.append({"event": "delete_intent", **base, "source_identity": actual})
                    try:
                        # Recheck after durable intent, immediately before unlink.
                        latest = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                        if not stat.S_ISREG(latest.st_mode) or _identity(latest) != expected:
                            raise UnsafeSourceError("source changed immediately before deletion")
                    except (OSError, UnsafeSourceError) as exc:
                        result = {**base, "status": "skipped", "reason": f"{type(exc).__name__}: {exc}"}
                        results.append(result)
                        log.append({"event": "deletion_skipped", **result})
                        continue
                    try:
                        os.unlink(name, dir_fd=parent_fd)
                    except OSError as exc:
                        # A filesystem may report failure after applying an
                        # operation. Do not claim an attempted unlink was skipped.
                        result = {**base, "status": "unknown", "deletion_attempted": True,
                                  "error": f"{type(exc).__name__}: {exc}"}
                        results.append(result)
                        failure = DeletionExecutionError("source unlink failed; outcome unknown; deletion stopped")
                        try:
                            log.append({"event": "deletion_outcome_unknown", **result})
                        except BaseException as secondary:
                            _add_cleanup_error(failure, secondary, "record uncertain unlink")
                        raise failure from exc
                    # Record the successful syscall before any operation that
                    # can fail. Later errors must not erase this deletion.
                    result = {**base, "status": "deleted", "deleted_bytes": actual["st_size"],
                              "source_directory_synced": False}
                    results.append(result)
                    try:
                        os.fsync(parent_fd)
                    except OSError as exc:
                        result["source_directory_sync_error"] = f"{type(exc).__name__}: {exc}"
                        failure = DeletionExecutionError("source directory sync failed after unlink; deletion stopped")
                        try:
                            log.append({"event": "delete_durability_failed", **result})
                        except BaseException as secondary:
                            _add_cleanup_error(failure, secondary, "record directory sync failure")
                        raise failure from exc
                    result["source_directory_synced"] = True
                    log.append({"event": "delete_complete", **result})
            log.append({"event": "deletion_batch_complete", "deleted": sum(x["status"] == "deleted" for x in results), "skipped": sum(x["status"] == "skipped" for x in results)})
    except BaseException as exc:
        # Includes audit writes, source-directory cleanup, audit-log close and
        # interruptions. Retain known outcomes on every exceptional exit.
        exc.completed_results = [dict(result) for result in results]
        raise
    return results
