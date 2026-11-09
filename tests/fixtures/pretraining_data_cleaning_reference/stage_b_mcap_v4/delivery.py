"""B04: deliver passed MCAPs and optionally apply recorded whole-file rejections."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time

from .contracts import BatchDeliveryResult, BatchQualityResult
from .deletion import (
    AuditWriteError, UnsafeSourceError, _identity, _open_parent,
    capture_source_identity, delete_rejected, fsync_directory, write_json_durable,
)
from .reporting import json_safe, read_quality_batch


def _copy_checked(record, target: Path, input_root: Path) -> dict:
    """Copy bytes from an identity-checked regular source, without re-encoding."""
    source = Path(record["source"])
    expected = record.get("source_identity")
    if not expected or record.get("deletion_blocked_reason"):
        raise UnsafeSourceError(record.get("deletion_blocked_reason") or "source identity unavailable")
    parent_fd, name = _open_parent(source, input_root)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as reader:
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode) or _identity(before) != expected:
                raise UnsafeSourceError("source identity changed since quality read")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with target.open("xb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    writer.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if size != expected["st_size"] or _identity(os.fstat(reader.fileno())) != expected:
                raise UnsafeSourceError("source changed during delivery")
            if capture_source_identity(source, input_root) != expected:
                raise UnsafeSourceError("source pathname changed during delivery")
            return {"bytes": size, "sha256": digest.hexdigest()}
    finally:
        os.close(parent_fd)


class McapDeliveryOperator:
    """B04 consumes B03's durable verdicts; it never performs quality checks."""

    def run(self, batch: BatchQualityResult, *, mode="quality") -> BatchDeliveryResult:
        if mode not in {"quality", "run"}:
            raise ValueError("delivery mode must be quality or run")
        started = time.perf_counter()
        output = Path(batch.manifest_path).resolve().parent
        input_root, quality_summary, records = read_quality_batch(batch.manifest_path)
        accepted = output / "accepted"
        products = ("accepted", "delivery_plan.json", "delivery_manifest.json", "deletion_plan.json",
                    "deletion_events.jsonl", "run_summary.json")
        if any((output / name).exists() or (output / name).is_symlink() for name in products):
            raise FileExistsError("delivery outputs already exist; B04 does not overwrite or implicitly resume")
        kept = [record for record in records if record["status"] == "kept"]
        rejected = [dict(record) for record in records if record["status"] == "rejected"]
        for record in rejected:
            if record.get("diagnostic_errors"):
                record["deletion_eligible"] = False
                record["deletion_blocked_reason"] = "quality diagnostics failed; source deletion blocked"
        targets = []
        root_path = Path(os.path.normpath(input_root))
        for record in kept:
            source = Path(os.path.normpath(record["source"]))
            try:
                relative = source.relative_to(root_path)
            except ValueError as exc:
                raise UnsafeSourceError("accepted source is outside input_root") from exc
            targets.append((record, relative))
        write_json_durable(output / "delivery_plan.json", {
            "version": "v4-four-operators", "mode": mode,
            "quality_batch": str(batch.manifest_path), "input_root": str(input_root),
            "accepted_root": str(accepted), "policy": "copy_passed_mcaps_byte_for_byte",
            "records": [{"episode_index": r["episode_index"], "source": r["source"],
                         "destination": str(accepted / rel), "source_identity": r.get("source_identity")}
                        for r, rel in targets],
        })
        blocked = "batch global statistics failed; all source deletion blocked" if quality_summary["statistics_status"] == "failed" else None
        exports, export_error = [], None
        if blocked is None:
            try:
                # Publish the directory only after all expected copies succeed.
                with tempfile.TemporaryDirectory(prefix=".delivery-", dir=output) as temporary:
                    staged = Path(temporary) / "accepted"
                    staged.mkdir()
                    for record, relative in targets:
                        details = _copy_checked(record, staged / relative, input_root)
                        exports.append({"episode_index": record["episode_index"], "source": record["source"],
                                        "destination": str(accepted / relative), **details})
                    for directory in sorted((p for p in staged.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                        fsync_directory(directory)
                    fsync_directory(staged)
                    if accepted.exists() or accepted.is_symlink():
                        raise FileExistsError("accepted output appeared during delivery")
                    staged.rename(accepted)
                    fsync_directory(output)
            except (OSError, ValueError) as exc:
                export_error = f"{type(exc).__name__}: {exc}"
                blocked = "accepted-file delivery failed; all source deletion blocked"
        delivery_status = "failed" if export_error else "blocked" if blocked else "complete"
        # An exceptional directory-sync failure can leave published files; only
        # a complete manifest declares them a successful batch delivery.
        manifest = {
            "version": "v4-four-operators", "status": delivery_status,
            "quality_batch": str(batch.manifest_path), "accepted_root": str(accepted),
            "exported_files": len(exports) if delivery_status == "complete" else 0,
            "records": exports if delivery_status == "complete" else [],
            "error": export_error or blocked,
            "rejected_files": len(rejected), "input_or_quality_errors": quality_summary["failed_episodes"],
        }
        write_json_durable(output / "delivery_manifest.json", manifest)
        if blocked:
            for record in rejected:
                record["deletion_eligible"] = False
                record["deletion_blocked_reason"] = blocked
        write_json_durable(output / "deletion_plan.json", {
            "version": "v4-four-operators", "mode": mode, "input_root": str(input_root),
            "policy": "delete_entire_original_mcap_if_any_quality_failure",
            "records": rejected, "rejected_files": len(rejected),
            "eligible_files": sum(bool(r.get("deletion_eligible")) for r in rejected),
            "execution_enabled": mode == "run", "blocked_reason": blocked,
        })
        results, audit_error, cleanup_errors = [], None, []
        if mode == "run":
            try:
                results = delete_rejected(rejected, input_root=input_root, events_path=output / "deletion_events.jsonl")
            except (AuditWriteError, OSError) as exc:
                audit_error = f"{type(exc).__name__}: {exc}"
                results = getattr(exc, "completed_results", [])
                cleanup_errors = getattr(exc, "cleanup_errors", [])
        summary = {
            **quality_summary, "stage": "delivery", "mode": mode,
            "delivery_status": delivery_status, "exported_files": manifest["exported_files"],
            "accepted_root": str(accepted), "delivery_manifest": str(output / "delivery_manifest.json"),
            "export_error": export_error, "audit_error": audit_error,
            "deletion_cleanup_errors": cleanup_errors,
            "deleted_files": sum(r["status"] == "deleted" for r in results),
            "deletion_skipped_files": sum(r["status"] == "skipped" for r in results),
            "deletion_unknown_files": sum(r["status"] == "unknown" for r in results),
            "deletion_sync_failed_files": sum(bool(r.get("source_directory_sync_error")) for r in results),
            "deleted_bytes": sum(r.get("deleted_bytes", 0) for r in results),
            "deletion_results": results, "deletion_plan": str(output / "deletion_plan.json"),
            "delivery_elapsed_seconds": time.perf_counter() - started,
            "elapsed_seconds": quality_summary["elapsed_seconds"] + time.perf_counter() - started,
        }
        failed = (quality_summary["failed_episodes"] or blocked or audit_error or summary["deletion_skipped_files"]
                  or summary["deletion_unknown_files"] or summary["deletion_sync_failed_files"])
        summary["execution_status"] = "failed" if failed else "complete"
        write_json_durable(output / "run_summary.json", json_safe(summary))
        return BatchDeliveryResult(summary, 1 if failed else 0)
