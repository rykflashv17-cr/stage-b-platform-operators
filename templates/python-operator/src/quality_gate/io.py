"""Named input/output and standard quality reporting for this operator."""
from operator_sdk import OperatorContext


def read_record(ctx: OperatorContext):
    return ctx.inputs.value("record")


def write_decision(ctx: OperatorContext, decision) -> None:
    ctx.outputs.write_json("decision", decision)


def report_quality(ctx: OperatorContext, record, decision) -> None:
    report = {
        "details": {"record_id": record["record_id"]},
        "decision": "PASS" if decision["keep"] else "REJECT",
    }
    if not decision["keep"]:
        report["reasons"] = [{
            "code": "BELOW_THRESHOLD",
            "message": "quality score is below threshold",
        }]
    ctx.quality.report_items([report])
