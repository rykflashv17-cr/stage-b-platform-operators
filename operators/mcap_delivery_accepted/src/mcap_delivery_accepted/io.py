"""B04's only input is B03's portable quality file."""
from operator_sdk.contracts import json_load
from pretraining_data_cleaning_common.artifact import unpack_artifact


def read_batch_quality_result(ctx):
    folder = unpack_artifact(ctx.inputs.file("batch_quality_result"), "quality_batch",
                             ctx.workspace.path("quality-input"))
    selection = json_load((folder / "selection.json").read_bytes())
    summary = json_load((folder / "quality_summary.json").read_bytes())
    records = []
    with (folder / "decisions.jsonl").open("rb") as stream:
        for line in stream:
            ctx.check_cancelled()
            records.append(json_load(line))
    return selection, records, summary
