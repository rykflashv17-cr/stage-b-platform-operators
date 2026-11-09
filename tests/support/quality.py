import json
from copy import deepcopy



ATTEMPT = "00000000-0000-4000-8000-000000000001"


def source_ref(input_index, key):
    return {
        "attempt_id": ATTEMPT, "input_name": "raw_files", "input_index": input_index,
        "logical_key": key, "sha256": "ab" * 32,
    }


def quality_data(statuses=("kept", "rejected", "kept")):
    # Episode IDs and input indexes deliberately differ and decisions arrive out of order.
    members = [{"episode_index": i, "source_ref": source_ref(len(statuses) - 1 - i, f"capture/{i}.mcap")}
               for i in range(len(statuses))]
    records = [{"episode_index": i, "source": m["source_ref"]["logical_key"],
                "source_ref": deepcopy(m["source_ref"]), "status": status,
                "reject_file": status == "rejected", "diagnostic_errors": {}}
               for i, (m, status) in enumerate(zip(members, statuses))]
    from pretraining_data_cleaning_common.reporting import quality_counts
    summary = {"statistics_status": "ready", **quality_counts(records)}
    return {"source": "jindian_marvin", "members": members}, records[::-1], summary


def quality_archive(folder, data):
    from pretraining_data_cleaning_common.artifact import pack_artifact
    selection, records, summary = data
    payload = folder / "payload"
    payload.mkdir()
    (payload / "selection.json").write_text(json.dumps(selection))
    (payload / "decisions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    (payload / "quality_summary.json").write_text(json.dumps(summary))
    archive = folder / "quality.zip"
    pack_artifact(archive, "quality_batch", payload)
    return archive
