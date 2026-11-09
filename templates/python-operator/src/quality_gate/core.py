def quality_gate(record, threshold):
    return {"record_id": record["record_id"], "keep": record["quality_score"] >= threshold}
