"""Select one normalized MCAP against its complete statistics cohort."""
from dataclasses import replace
import json
import re

from pretrain_cleaning_common.artifact import pack_artifact, unpack_artifact
from pretrain_cleaning_common.filesystem import write_json_durable
from pretrain_cleaning_common.handoff import load_context, load_normalized, require, sha256, validate_source_refs
from pretrain_cleaning_common.reporting import quality_counts
from pretrain_cleaning_common.single_file import (
    cohort_member, normalized_record_digest, read_cohort, validate_single_episode, write_cohort,
)


def read_statistics_context(ctx):
    episode_folder = unpack_artifact(
        ctx.inputs.file("normalized_episode_ref"), "normalized_batch", ctx.workspace.path("episode-input"))
    ctx.check_cancelled()
    target_batch, target_metadata = load_normalized(episode_folder, ctx.parameters["source"])
    target_record, target_member = validate_single_episode(target_batch, target_metadata)

    statistics_folder = unpack_artifact(
        ctx.inputs.file("statistics_context"), "statistics_context", ctx.workspace.path("statistics-input"))
    ctx.check_cancelled()
    context, metadata = load_context(statistics_folder, ctx.parameters["source"])
    validate_source_refs(metadata)
    cohort = read_cohort(statistics_folder, metadata)
    member = cohort_member(cohort, target_member["source_ref"])
    require(normalized_record_digest(target_record) == member["normalized_digest"],
            "target normalized content differs from the statistics cohort")

    # B02 assigns stable cohort-wide indices. Use its restored record and fitted
    # statistics; the independent B01 artifact only selects and authenticates it.
    index = member["episode_index"]
    records = tuple(record for record in context.batch.records if record.ref.episode_index == index)
    members = [item for item in metadata["members"] if item["episode_index"] == index]
    business_refs = [item for item in metadata["business_refs"] if item["episode_index"] == index]
    require(len(records) == len(members) == len(business_refs) == 1,
            "target is missing from the restored statistics context")
    require(members[0]["source_ref"] == target_member["source_ref"],
            "target source reference differs from the restored statistics context")
    selected_context = replace(
        context,
        batch=replace(context.batch, records=records),
        fit_records=tuple(item for item in context.fit_records if item["episode_index"] == index),
    )
    selected_metadata = {
        **metadata, "members": members, "business_refs": business_refs, "_cohort": cohort,
        # context.json's verified file index covers the numeric inputs, fitted
        # caches and cohort. Bind each verdict to that exact B02 result, too.
        "_statistics_context_digest": sha256(statistics_folder / "context.json"),
    }
    validate_single_episode(selected_context.batch, selected_metadata)
    ctx.check_cancelled()
    return selected_context, selected_metadata


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


def write_episode_quality_result(ctx, decisions, metadata, statistics_status):
    require(len(decisions) == len(metadata["members"]) == 1,
            "single-file quality output must contain exactly one MCAP episode")
    statistics_digest = metadata.get("_statistics_context_digest")
    require(isinstance(statistics_digest, str) and re.fullmatch(r"[0-9a-f]{64}", statistics_digest),
            "missing or invalid statistics context digest")
    folder = ctx.workspace.path("quality-payload")
    folder.mkdir()
    members = [{"episode_index": m["episode_index"], "source_ref": m["source_ref"]}
               for m in metadata["members"]]
    write_json_durable(folder / "selection.json", {"source": metadata["source"], "members": members})
    write_json_durable(folder / "quality_summary.json", {
        "statistics_status": statistics_status, "statistics_context_digest": statistics_digest,
        **quality_counts(decisions)})
    write_cohort(folder, metadata["_cohort"])
    with (folder / "decisions.jsonl").open("x", encoding="utf-8") as stream:
        for decision in decisions:
            ctx.check_cancelled()
            stream.write(json.dumps(decision, ensure_ascii=False, allow_nan=False) + "\n")
    path = ctx.workspace.path("episode_quality_result.zip")
    pack_artifact(path, "quality_batch", folder)
    ctx.check_cancelled()
    ctx.outputs.file("episode_quality_result", path)
