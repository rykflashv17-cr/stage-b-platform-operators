from operator_sdk import BaseOperator, OperatorContext
from pretraining_data_cleaning_common.spec import quality_config

from .core import McapReadOperator as _Business
from .io import read_raw_files, write_normalized_batch_ref


class McapReadOperator(BaseOperator):
    def run(self, ctx: OperatorContext) -> None:
        output = ctx.workspace.path("normalization")
        spec, refs, metadata = read_raw_files(ctx, output)
        ctx.log.info("normalization started", extra={"episodes": len(refs)})
        batch = self.normalize_batch(refs, spec, quality_config(spec), check_cancelled=ctx.check_cancelled)
        ctx.check_cancelled()
        write_normalized_batch_ref(ctx, batch, metadata)

    def normalize_batch(self, refs, spec, cfg, *, check_cancelled=None):
        return _Business().run_batch(refs, spec, cfg, check_cancelled=check_cancelled)
