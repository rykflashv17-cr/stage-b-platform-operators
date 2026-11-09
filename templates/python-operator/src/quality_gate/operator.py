from operator_sdk import BaseOperator, OperatorContext

from .core import quality_gate
from .io import read_record, write_decision, report_quality
from .validation import validate_decision


class QualityGateOperator(BaseOperator):
    def run(self, ctx: OperatorContext) -> None:
        record = read_record(ctx)
        decision = quality_gate(record, ctx.parameters["threshold"])
        validate_decision(record, decision, ctx.parameters["threshold"])
        write_decision(ctx, decision)
        report_quality(ctx, record, decision)
        ctx.log.info("quality decision staged", extra={"keep": decision["keep"]})
