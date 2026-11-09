def validate_decision(record, decision, threshold):
    if decision["record_id"] != record["record_id"]:
        raise ValueError("decision record_id does not match input")
    if decision["keep"] is not (record["quality_score"] >= threshold):
        raise ValueError("decision does not match effective threshold")
