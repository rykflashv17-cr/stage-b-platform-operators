from operator_sdk import BaseOperator, OperatorContext
from pretraining_data_cleaning_common.handoff import make_handoff_spec
from .io import read_normalized_batch_ref, write_statistics_context
from pretraining_data_cleaning_common.spec import quality_config
from .core import GlobalStatsOperator as _Business

class GlobalStatsOperator(BaseOperator):
    def run(self, ctx: OperatorContext) -> None:
        batch,metadata=read_normalized_batch_ref(ctx)
        output=ctx.workspace.path("statistics");output.mkdir()
        spec=make_handoff_spec(ctx,"normalized_batch_ref",metadata,output)
        ctx.log.info("statistics started",extra={"episodes":len(batch.records)})
        result=self.calculate(batch,spec,quality_config(spec),recompute=ctx.parameters["recompute"],check_cancelled=ctx.check_cancelled)
        ctx.check_cancelled()
        handoff=ctx.workspace.path("context")
        if result.status=="failed":
            raise RuntimeError("global statistics failed: "+str(result.error))
        write_statistics_context(ctx,result,metadata,handoff)

    def calculate(self,batch,spec,cfg,*,recompute,check_cancelled=None):
        return _Business().run(batch,spec,cfg,recompute=recompute,check_cancelled=check_cancelled)
