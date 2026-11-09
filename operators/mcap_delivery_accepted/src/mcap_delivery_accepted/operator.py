from operator_sdk import BaseOperator, OperatorContext

from .core import delivery_manifest
from .io import read_batch_quality_result
from .archive import build_archive


class McapDeliveryOperator(BaseOperator):
    def run(self, ctx: OperatorContext) -> None:
        selection, records, summary = read_batch_quality_result(ctx)
        ctx.log.info("checking quality decisions for archive", extra={"episodes": len(records)})
        selected = delivery_manifest(selection, records, summary, ctx.check_cancelled)["accepted"]
        paths = ctx.storage.download([item["source_ref"]["logical_key"] for item in selected], max_concurrency=4)
        archive = build_archive(ctx, selected, paths)
        ctx.check_cancelled()
        ctx.outputs.file("archive", archive)
