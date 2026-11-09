"""Join one-episode B01 files and publish cohort-bound batch statistics."""
import json

from pretrain_cleaning_common.artifact import pack_artifact, unpack_artifact
from pretrain_cleaning_common.filesystem import fsync_directory, write_json_durable
from pretrain_cleaning_common.handoff import load_normalized, require, save_context, sha256
from pretrain_cleaning_common.single_file import merge_normalized_episodes, write_cohort


def read_normalized_episode_refs(ctx):
    paths = ctx.inputs.files("normalized_episode_refs")
    require(paths, "at least one normalized episode handoff is required")
    items = []
    for index, path in enumerate(paths):
        ctx.check_cancelled()
        folder = unpack_artifact(path, "normalized_batch", ctx.workspace.path(f"restored-episode-{index}"))
        items.append(load_normalized(folder, ctx.parameters["source"]))
    return merge_normalized_episodes(
        items, ctx.workspace.path("normalized-cohort"), ctx.parameters["source"],
        check_cancelled=ctx.check_cancelled,
    )


def write_statistics_context(ctx, context, metadata, folder, cohort):
    save_context(context, folder, metadata)
    write_cohort(folder, cohort)
    # save_context writes its verified file index before the cohort exists.
    # Replace that private index before packaging so cohort.json receives the
    # same checksum coverage as the normalized data and statistics cache.
    target = folder / "context.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["files"]["cohort.json"] = sha256(folder / "cohort.json")
    complete = folder / ".context-with-cohort.json"
    write_json_durable(complete, payload)
    complete.replace(target)
    fsync_directory(folder)
    ctx.check_cancelled()
    artifact = ctx.workspace.path("statistics_context.zip")
    pack_artifact(artifact, "statistics_context", folder)
    ctx.outputs.file("statistics_context", artifact)
