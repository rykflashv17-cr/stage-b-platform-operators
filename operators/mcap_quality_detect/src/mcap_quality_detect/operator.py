from operator_sdk import BaseOperator, OperatorContext
from pretrain_cleaning_common.handoff import make_handoff_spec
from pretrain_cleaning_common.reporting import read_quality_batch
from pretrain_cleaning_common.spec import quality_config

from .core import QualityDetectionOperator as _Business
from .io import portable_decisions, read_statistics_context, report_quality, write_episode_quality_result


class QualityDetectionOperator(BaseOperator):
    def run(self, ctx: OperatorContext) -> None:
        context, metadata = read_statistics_context(ctx)
        output = ctx.workspace.path("quality")
        output.mkdir()
        spec = make_handoff_spec(ctx, "statistics_context", metadata, output)
        ctx.log.info("quality detection started", extra={"episodes": len(context.batch.records)})
        result = self.evaluate_batch(context, spec, quality_config(spec), check_cancelled=ctx.check_cancelled)
        ctx.check_cancelled()
        _, summary, records = read_quality_batch(result.manifest_path)
        decisions = portable_decisions(records, metadata)
        if result.exit_code or summary["failed_episodes"] or summary["statistics_status"] == "failed":
            raise RuntimeError("quality execution failed")
        report_quality(ctx, decisions)
        write_episode_quality_result(ctx, decisions, metadata, summary["statistics_status"])

    def evaluate_batch(self, context, spec, cfg, *, check_cancelled=None):
        return _Business().run_batch(context, spec, cfg, check_cancelled=check_cancelled)
