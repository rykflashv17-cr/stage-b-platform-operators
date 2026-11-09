"""Restore the verified normalized batch and write B02's fitted statistics file."""
from pretraining_data_cleaning_common.artifact import pack_artifact, unpack_artifact
from pretraining_data_cleaning_common.handoff import load_normalized, save_context, validate_source_refs


def read_normalized_batch_ref(ctx):
    folder = unpack_artifact(ctx.inputs.file("normalized_batch_ref"), "normalized_batch",
                             ctx.workspace.path("restored-batch"))
    batch, metadata = load_normalized(folder, ctx.parameters["source"])
    return batch, validate_source_refs(metadata)


def write_statistics_context(ctx, context, metadata, folder):
    save_context(context, folder, metadata)
    artifact = ctx.workspace.path("statistics_context.zip")
    pack_artifact(artifact, "statistics_context", folder)
    ctx.outputs.file("statistics_context", artifact)
