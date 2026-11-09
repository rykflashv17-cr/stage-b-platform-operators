"""Join independent B01 episodes and bind single-item judgments to their cohort.

Only verified numeric intermediates are copied here. Frozen raw-file references
keep their originating B01 attempt and input slot throughout the four nodes.
"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re

from .contracts import NormalizedBatchRef
from .filesystem import write_json_durable
from .handoff import (
    PROFILES, SOURCE_REVISION, REVISION, load_normalized, profile_digest, require,
    save_selection, selection_metadata, sha256, validate_selection,
    validate_source_members, validate_source_ref, validate_source_refs,
)
from .storage import load_batch, save_batch


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate cohort JSON field")
        result[key] = value
    return result


def normalized_record_digest(record):
    """Digest facts that remain unchanged by batch-global renumbering."""
    require((record.data_path is None) == (record.input_error is not None),
            "normalized record requires data or an input error")
    return _sha({
        "data_sha256": sha256(record.data_path) if record.data_path is not None else None,
        "input_error": record.input_error,
        "deletion_blocked_reason": record.deletion_blocked_reason,
    })


def validate_single_episode(batch, metadata):
    """A data-mode invocation represents exactly one main MCAP episode."""
    validate_source_refs(metadata)
    validate_selection(metadata, metadata["source"], batch)
    require(len(batch.records) == len(metadata["members"]) == 1,
            "single-file handoff must contain exactly one MCAP episode")
    return batch.records[0], metadata["members"][0]


def _body(cohort):
    return {key: value for key, value in cohort.items() if key != "cohort_id"}


def validate_cohort(cohort):
    require(isinstance(cohort, dict) and set(cohort) == {
        "format", "source", "profile_digest", "source_revision", "engine_revision", "members", "cohort_id",
    }, "invalid cohort fields")
    require(cohort["format"] == "mcap_cohort_v1" and cohort["source"] in PROFILES,
            "invalid cohort format/source")
    require(cohort["source_revision"] == SOURCE_REVISION and cohort["engine_revision"] == REVISION
            and cohort["profile_digest"] == profile_digest(cohort["source"]),
            "cohort implementation/profile mismatch")
    members = cohort["members"]
    require(isinstance(members, list) and members, "empty or invalid statistics cohort")
    keys, refs = [], set()
    for index, member in enumerate(members):
        require(isinstance(member, dict) and set(member) == {"episode_index", "source_ref", "normalized_digest"},
                "invalid cohort member fields")
        require(type(member["episode_index"]) is int and member["episode_index"] == index,
                "cohort episode indices must be consecutive and ordered")
        ref = validate_source_ref(member["source_ref"])
        key = _canonical(ref)
        require(key not in refs, "duplicate cohort source reference")
        refs.add(key)
        keys.append(ref["logical_key"])
        require(isinstance(member["normalized_digest"], str)
                and re.fullmatch(r"[0-9a-f]{64}", member["normalized_digest"]), "invalid normalized digest")
    require(len(set(keys)) == len(keys) and keys == sorted(keys, key=lambda key: key.encode("utf-8")),
            "cohort logical keys must be unique and sorted")
    validate_source_members(cohort["source"], members)
    require(isinstance(cohort["cohort_id"], str) and re.fullmatch(r"[0-9a-f]{64}", cohort["cohort_id"])
            and cohort["cohort_id"] == _sha(_body(cohort)), "cohort identity checksum mismatch")
    return cohort


def make_cohort(batch, metadata):
    validate_source_refs(metadata)
    validate_selection(metadata, metadata["source"], batch)
    cohort = {key: metadata[key] for key in ("source", "profile_digest", "source_revision", "engine_revision")}
    cohort.update(format="mcap_cohort_v1", members=[{
        "episode_index": record.ref.episode_index,
        "source_ref": dict(member["source_ref"]),
        "normalized_digest": normalized_record_digest(record),
    } for record, member in zip(batch.records, metadata["members"], strict=True)])
    cohort["cohort_id"] = _sha(cohort)
    return validate_cohort(cohort)


def cohort_member(cohort, source_ref):
    validate_cohort(cohort)
    validate_source_ref(source_ref)
    matches = [member for member in cohort["members"] if member["source_ref"] == source_ref]
    require(len(matches) == 1, "target source is not a unique member of the statistics cohort")
    return matches[0]


def write_cohort(folder, cohort):
    validate_cohort(cohort)
    write_json_durable(Path(folder) / "cohort.json", cohort)


def read_cohort(folder, metadata=None):
    """Read either a full statistics payload or a single quality payload.

    When full statistics selection metadata is supplied, verify every member's
    frozen identity and numeric digest against the restored normalized batch.
    B04 can omit metadata and subsequently verify complete judgment coverage.
    """
    folder = Path(folder)
    cohort = validate_cohort(json.loads((folder / "cohort.json").read_text(encoding="utf-8"),
                                        object_pairs_hook=_pairs))
    if metadata is not None:
        validate_source_refs(metadata)
        require(all(cohort[key] == metadata[key] for key in
                    ("source", "profile_digest", "source_revision", "engine_revision")),
                "cohort differs from statistics selection")
        batch = load_batch(folder / "normalized" / "manifest.json")
        validate_selection(metadata, metadata["source"], batch)
        require(make_cohort(batch, metadata) == cohort,
                "cohort members or normalized content differ from statistics")
    return cohort


def merge_normalized_episodes(items, destination, source, *, check_cancelled=None):
    """Merge singleton artifacts in logical-key order without decoding raw data."""
    require(source in PROFILES and isinstance(items, (list, tuple)) and items,
            "at least one normalized episode handoff is required")
    rows, keys, refs = [], set(), set()
    for batch, metadata in items:
        if check_cancelled is not None:
            check_cancelled()
        require(metadata.get("source") == source, "mixed source profiles in statistics input")
        record, member = validate_single_episode(batch, metadata)
        ref = member["source_ref"]
        key, identity = ref["logical_key"], _canonical(ref)
        require(key not in keys, "duplicate logical_key across normalized episodes")
        require(identity not in refs, "duplicate frozen source_ref across normalized episodes")
        keys.add(key)
        refs.add(identity)
        rows.append((key, record, member, normalized_record_digest(record)))
    rows.sort(key=lambda row: row[0].encode("utf-8"))
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    for index, (key, record, _, digest) in enumerate(rows):
        if check_cancelled is not None:
            check_cancelled()
        virtual = Path("/raw") / key
        relative = Path(key).parent if source == "abc_130k" else Path(key)
        ref = replace(record.ref, episode_index=index, dataset_root=Path("/raw"),
                      mcap_path=virtual, rel_path=relative.as_posix())
        target = None
        if record.data_path is not None:
            target = destination / f"episode_{index:08d}.npz"
            with record.data_path.open("rb") as src, target.open("xb") as dst:
                while block := src.read(1024 * 1024):
                    if check_cancelled is not None:
                        check_cancelled()
                    dst.write(block)
                dst.flush()
                os.fsync(dst.fileno())
        merged_record = replace(record, ref=ref, data_path=target)
        require(normalized_record_digest(merged_record) == digest, "normalized content changed while merging")
        records.append(merged_record)
    batch = NormalizedBatchRef(destination / "manifest.json", tuple(records))
    metadata = selection_metadata(source, [Path("/raw")], batch.records)
    for merged_member, (_, _, member, _) in zip(metadata["members"], rows, strict=True):
        merged_member["source_ref"] = dict(member["source_ref"])
        merged_member["source_episode_index"] = member["source_episode_index"]
    metadata["business_refs"] = [{
        "episode_index": record.ref.episode_index, "dataset_root": "raw",
        "mcap_path": record.ref.mcap_path.relative_to("/").as_posix(),
        "rel_path": record.ref.rel_path, "part": record.ref.part, "task": record.ref.task,
    } for record in batch.records]
    validate_source_refs(metadata)
    save_batch(batch)
    save_selection(destination, metadata)
    # Recheck filenames, numeric layout, and rebuilt manifest before publication.
    batch, metadata = load_normalized(destination, source)
    return batch, metadata, make_cohort(batch, metadata)
