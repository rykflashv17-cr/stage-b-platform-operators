"""Select original B01 input references covered by successful quality judgments."""
from pretrain_cleaning_common.handoff import require, validate_source_members, validate_source_ref
from pretrain_cleaning_common.reporting import quality_counts


def delivery_manifest(selection, records, summary, check_cancelled):
    require(isinstance(selection, dict) and set(selection) == {"source", "members"}, "invalid quality selection")
    require(isinstance(summary, dict) and summary.get("statistics_status") in ("ready", "empty"),
            "unsuccessful quality statistics")
    expected = validate_source_members(selection["source"], selection["members"], check_cancelled=check_cancelled)

    accepted = []
    for record in records:
        check_cancelled()
        require(isinstance(record, dict), "invalid quality decision")
        index, ref = record.get("episode_index"), record.get("source_ref")
        require(type(index) is int and index in expected, "duplicate or unknown quality episode")
        validate_source_ref(ref)
        require(expected.pop(index) == ref and record.get("source") == ref["logical_key"],
                "quality decision source differs from selection")
        require(record.get("diagnostic_errors", {}) == {} and isinstance(record.get("diagnostic_errors", {}), dict),
                "invalid diagnostic_errors")
        status = record.get("status")
        require(status in ("kept", "rejected") and record.get("reject_file") is (status == "rejected")
                and record.get("diagnostic_errors", {}) == {} and "error" not in record,
                "failed or inconsistent quality decision")
        if status == "kept":
            require(summary["statistics_status"] == "ready", "passing numerical judgment requires fitted statistics")
            accepted.append({"episode_index": index, "source_ref": ref})
    require(not expected, "quality decisions do not cover selection")
    counts = quality_counts(records)
    require(all(type(summary.get(key)) is int and summary[key] == count for key, count in counts.items()),
            "quality summary does not match decisions")
    accepted.sort(key=lambda row: row["episode_index"])
    return {"format": "mcap_delivery_manifest_v1", "accepted": accepted}
