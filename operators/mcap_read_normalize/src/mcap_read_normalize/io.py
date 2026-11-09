"""Bind B01's selected inputs to portable normalized records."""
from dataclasses import replace
from pathlib import Path

from pretrain_cleaning_common.artifact import pack_artifact
from pretrain_cleaning_common.contracts import NormalizedRecord, SourceIdentity
from pretrain_cleaning_common.handoff import make_spec, require, save_selection, selection_metadata, validate_source_refs
from pretrain_cleaning_common.storage import save_batch
from .raw_files import select_episodes


def read_raw_files(ctx, output):
    refs, source_refs, identities = select_episodes(ctx)
    staged = refs[0].dataset_root
    spec = make_spec(ctx.parameters["source"], staged, output, ctx.parameters["workers"])
    virtual = Path("/raw")
    portable = [replace(ref, dataset_root=virtual, mcap_path=virtual / ref.mcap_path.relative_to(staged)) for ref in refs]
    metadata = selection_metadata(ctx.parameters["source"], [virtual], [NormalizedRecord(r, None) for r in portable])
    for member, source_ref in zip(metadata["members"], source_refs, strict=True):
        member["source_ref"] = source_ref
    metadata["business_refs"] = [
        dict(episode_index=r.episode_index, dataset_root="raw", mcap_path=str(r.mcap_path.relative_to("/")),
             rel_path=r.rel_path, part=r.part, task=r.task) for r in portable
    ]
    validate_source_refs(metadata)
    metadata["_identities"] = identities  # transient B01 state for rebuilding the portable manifest
    return spec, refs, metadata


def write_normalized_episode_ref(ctx, batch, metadata):
    metadata = dict(metadata)
    identities = metadata.pop("_identities")
    require(len(batch.records) == len(metadata["members"]) == len(identities) == 1,
            "normalized_episode_ref must contain exactly one episode record")
    portable = []
    for record, member, (original, staged) in zip(batch.records, metadata["members"], identities, strict=True):
        ctx.check_cancelled()
        require(SourceIdentity.capture(record.ref.mcap_path) == record.source_identity == staged,
                "staged MCAP changed during normalization")
        ref = replace(record.ref, dataset_root=Path(member["dataset_root"]), mcap_path=Path(member["source"]))
        # An adapter may include a source pathname in an input diagnostic.
        def portable_message(message):
            return message.replace(str(record.ref.dataset_root), str(ref.dataset_root)) if message else message
        portable.append(replace(record, ref=ref, source_identity=original,
                                input_error=portable_message(record.input_error),
                                deletion_blocked_reason=portable_message(record.deletion_blocked_reason)))
    batch = replace(batch, records=tuple(portable))
    batch.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = batch.manifest_path.with_suffix(".portable.json")
    save_batch(replace(batch, manifest_path=temporary))
    temporary.replace(batch.manifest_path)
    save_selection(batch.manifest_path.parent, metadata)
    artifact = ctx.workspace.path("normalized_episode_ref.zip")
    pack_artifact(artifact, "normalized_batch", batch.manifest_path.parent)
    ctx.outputs.file("normalized_episode_ref", artifact)


# Keep the Python helper import stable for callers that predate the single-
# episode contract. The submitted output is always normalized_episode_ref.
write_normalized_batch_ref = write_normalized_episode_ref
