"""B03 durable judgments and the engine-independent B03 to B04 handoff."""
from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
import os
import time
from pathlib import Path

import numpy as np

from .contracts import BatchQualityResult
from .deletion import fsync_directory, write_json_durable


def json_safe(value):
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def error_record(record, error):
    return {
        "episode_index": record.ref.episode_index, "source": str(record.ref.mcap_path),
        "status": "error", "reject_file": False, "error": str(error),
        "retained_due_to_error": True, "source_identity": json_safe(record.source_identity),
        "deletion_eligible": False,
        "deletion_blocked_reason": record.deletion_blocked_reason or "quality could not be determined",
    }


def quality_counts(records):
    """Quality categories are disjoint; execution failures may overlap rejects."""
    return {
        "episodes": len(records),
        "kept_files": sum(r["status"] == "kept" for r in records),
        "rejected_files": sum(r["status"] == "rejected" for r in records),
        "error_files": sum(r["status"] == "error" for r in records),
        "diagnostic_error_files": sum(bool(r.get("diagnostic_errors")) for r in records),
        "failed_episodes": sum(r["status"] == "error" or bool(r.get("diagnostic_errors")) for r in records),
    }


def finish_quality(ordered, context, spec, started):
    """Persist all judgments before B04 may copy or delete any source."""
    if context.status == "failed":
        for record in ordered:
            record["deletion_eligible"] = False
            record["deletion_blocked_reason"] = "batch global statistics failed; all source deletion blocked"
    output = spec.output_root
    report = output / "decisions.jsonl"
    with report.open("x", encoding="utf-8") as handle:
        for record in ordered:
            handle.write(json.dumps(json_safe(record), ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(output)
    summary = {
        "version": "v4-four-operators", "stage": "quality", **quality_counts(ordered),
        "statistics_status": context.status, "statistics_error": context.error,
        "normalized_manifest": str(context.batch.manifest_path),
        "statistics_report": str(output / "statistics_context.json"),
        "report": str(report), "elapsed_seconds": time.perf_counter() - started,
    }
    summary_path = output / "quality_summary.json"
    write_json_durable(summary_path, json_safe(summary))
    manifest_path = output / "quality_batch.json"
    write_json_durable(manifest_path, {
        "format": "quality_batch_v2", "input_root": str(spec.input_root),
        "decisions_file": report.name, "decisions_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "summary_file": summary_path.name, "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
    })
    failed = summary["failed_episodes"] or context.status == "failed"
    return BatchQualityResult(summary, 1 if failed else 0, manifest_path)


def read_quality_batch(manifest_path):
    """Read immutable handoff evidence once; no engine or raw MCAP decoding."""
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") not in {"quality_batch_v1", "quality_batch_v2"}:
        raise ValueError("unsupported quality batch format")
    def verified(file_key, hash_key):
        name = manifest[file_key]
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("quality handoff files must be local filenames")
        content = (manifest_path.parent / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest[hash_key]:
            raise ValueError("quality handoff checksum mismatch")
        return content
    summary = json.loads(verified("summary_file", "summary_sha256"))
    records = [json.loads(line) for line in verified("decisions_file", "decisions_sha256").splitlines()]
    if summary.get("statistics_status") not in {"ready", "empty", "failed"}:
        raise ValueError("invalid statistics status in quality handoff")
    seen_ids, seen_sources = set(), set()
    for record in records:
        status, source, index = record["status"], Path(record["source"]), record["episode_index"]
        if status not in {"kept", "rejected", "error"} or record.get("reject_file") is not (status == "rejected"):
            raise ValueError("inconsistent quality verdict")
        diagnostics = record.get("diagnostic_errors", {})
        if not isinstance(diagnostics, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in diagnostics.items()):
            raise ValueError("diagnostic_errors must map stage names to error strings")
        if diagnostics and (status == "kept" or manifest["format"] == "quality_batch_v1"):
            raise ValueError("diagnostic errors cannot be marked kept or use the old handoff format")
        if not source.is_absolute() or source.suffix != ".mcap":
            raise ValueError("quality source must be an absolute MCAP path")
        key = os.path.normpath(source)
        if index in seen_ids or key in seen_sources:
            raise ValueError("duplicate source or episode in quality handoff")
        seen_ids.add(index)
        seen_sources.add(key)
    expected = quality_counts(records)
    required = set(expected)
    if manifest["format"] == "quality_batch_v1":
        required -= {"error_files", "diagnostic_error_files"}
    if any(summary.get(key) != expected[key] for key in required):
        raise ValueError("quality summary does not match complete decisions")
    # Expose the same counters for an older, successfully verified v1 handoff.
    summary.update(expected)
    root = Path(manifest["input_root"])
    if not root.is_absolute():
        raise ValueError("quality input root must be absolute")
    return root, summary, records


def load_quality_batch(manifest_path):
    """Reconstruct B04's input from B03 files without loading B01/B02 objects."""
    _, summary, _ = read_quality_batch(manifest_path)
    failed = summary["failed_episodes"] or summary["statistics_status"] == "failed"
    return BatchQualityResult(summary, 1 if failed else 0, Path(manifest_path).resolve())
