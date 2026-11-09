"""Gather single-episode verdicts and require complete cohort coverage."""
import re
from pretrain_cleaning_common.archive import read_index as json_load
from pretrain_cleaning_common.artifact import unpack_artifact
from pretrain_cleaning_common.handoff import require
from pretrain_cleaning_common.reporting import quality_counts
from pretrain_cleaning_common.single_file import read_cohort

from .core import delivery_manifest


def read_batch_quality_result(ctx):
    paths = ctx.inputs.files("episode_quality_results")
    require(paths, "at least one episode quality result is required")
    cohort, status, statistics_digest = None, None, None
    records, members, seen = [], [], set()
    for position, path in enumerate(paths):
        ctx.check_cancelled()
        folder = unpack_artifact(path, "quality_batch", ctx.workspace.path(f"quality-input-{position}"))
        current = read_cohort(folder)
        if cohort is None:
            cohort = current
        require(current == cohort, "quality results belong to different statistics cohorts")
        selection = json_load((folder / "selection.json").read_bytes())
        summary = json_load((folder / "quality_summary.json").read_bytes())
        require(isinstance(summary, dict)
                and isinstance(summary.get("statistics_context_digest"), str)
                and re.fullmatch(r"[0-9a-f]{64}", summary["statistics_context_digest"]),
                "missing or invalid statistics context digest")
        if statistics_digest is None:
            statistics_digest = summary["statistics_context_digest"]
        require(summary["statistics_context_digest"] == statistics_digest,
                "quality results use different fitted statistics contexts")
        rows = [json_load(line) for line in (folder / "decisions.jsonl").read_bytes().splitlines()]
        require(isinstance(selection, dict) and selection.get("source") == cohort["source"]
                and isinstance(selection.get("members"), list)
                and len(selection["members"]) == len(rows) == 1,
                "quality result must contain exactly one episode")
        delivery_manifest(selection, rows, summary, ctx.check_cancelled)
        member = selection["members"][0]
        index = member["episode_index"]
        require(type(index) is int and 0 <= index < len(cohort["members"]) and index not in seen,
                "duplicate or unknown episode quality result")
        expected = cohort["members"][index]
        require(member == {"episode_index": index, "source_ref": expected["source_ref"]},
                "quality source differs from the statistics cohort")
        if status is None:
            status = summary["statistics_status"]
        require(summary["statistics_status"] == status, "inconsistent statistics status across quality results")
        seen.add(index)
        records.extend(rows)
        members.append(member)
    require(seen == set(range(len(cohort["members"]))), "missing episode quality results from statistics cohort")
    records.sort(key=lambda row: row["episode_index"])
    members.sort(key=lambda row: row["episode_index"])
    return ({"source": cohort["source"], "members": members}, records,
            {"statistics_status": status, "statistics_context_digest": statistics_digest, **quality_counts(records)})
