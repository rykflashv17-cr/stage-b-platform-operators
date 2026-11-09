"""Restore the statistics file and publish portable quality decisions."""
import json

from pretraining_data_cleaning_common.artifact import pack_artifact, unpack_artifact
from pretraining_data_cleaning_common.filesystem import write_json_durable
from pretraining_data_cleaning_common.handoff import load_context, require, validate_source_refs
from pretraining_data_cleaning_common.reporting import quality_counts


def read_statistics_context(ctx):
    folder = unpack_artifact(ctx.inputs.file("statistics_context"), "statistics_context",
                             ctx.workspace.path("statistics-input"))
    context, metadata = load_context(folder, ctx.parameters["source"])
    validate_source_refs(metadata)
    return context, metadata


def portable_decisions(records, metadata):
    """Match engine verdicts to the validated selection before replacing virtual paths."""
    expected = {m["episode_index"]: m for m in metadata["members"]}
    decisions = []
    for record in records:
        index = record["episode_index"]
        require(type(index) is int and index in expected, "duplicate or unknown quality episode")
        member = expected.pop(index)
        require(record["source"] == member["source"], "quality episode source differs from selection")
        source_ref = member["source_ref"]
        # Only verdict fields belong in the handoff. Engine-local identities and cleanup
        # instructions are meaningful only inside the originating workspace.
        decision = {key: record[key] for key in (
            "episode_index", "status", "reject_file", "reasons", "num_frames",
            "detected_bad_frames", "detected_ranges", "effective_removed_ranges",
            "stage_results", "diagnostic_errors", "error",
        ) if key in record}
        decision.update(source=source_ref["logical_key"], source_ref=source_ref)
        decisions.append(decision)
    require(not expected, "quality report missing input episodes")
    return decisions


def _standard_record(record):
    status = record["status"]
    errors = [{"code": "DIAGNOSTIC_ERROR", "message": f"{stage}: {message}"}
              for stage, message in record.get("diagnostic_errors", {}).items()]
    if status == "error" and not errors:
        errors.append({"code": "INPUT_OR_QUALITY_ERROR",
                       "message": record.get("error") or "input or quality execution failed"})
    reasons = [{"code": "QUALITY_REJECT", "message": reason} for reason in record.get("reasons", [])]
    if status == "rejected" and not reasons:
        reasons.append({"code": "QUALITY_REJECT", "message": "quality rules rejected this MCAP"})
    return {"decision": {"kept": "PASS", "rejected": "REJECT", "error": "UNKNOWN"}[status],
            "reasons": reasons, "errors": errors, "details": record}


def report_quality(ctx, decisions):
    ctx.quality.report_items(_standard_record(row) for row in decisions)


def write_batch_quality_result(ctx, decisions, metadata, statistics_status):
    folder = ctx.workspace.path("quality-payload")
    folder.mkdir()
    members = [{"episode_index": m["episode_index"], "source_ref": m["source_ref"]}
               for m in metadata["members"]]
    write_json_durable(folder / "selection.json", {"source": metadata["source"], "members": members})
    write_json_durable(folder / "quality_summary.json", {
        "statistics_status": statistics_status, **quality_counts(decisions)})
    with (folder / "decisions.jsonl").open("x", encoding="utf-8") as stream:
        for decision in decisions:
            ctx.check_cancelled()
            stream.write(json.dumps(decision, ensure_ascii=False, allow_nan=False) + "\n")
    path = ctx.workspace.path("quality_batch.zip")
    pack_artifact(path, "quality_batch", folder)
    ctx.check_cancelled()
    ctx.outputs.file("batch_quality_result", path)
