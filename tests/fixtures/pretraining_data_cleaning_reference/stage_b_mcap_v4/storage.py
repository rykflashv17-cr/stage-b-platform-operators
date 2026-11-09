"""Numeric intermediate storage between B01, B02 and B03; no raw decoding."""
from dataclasses import asdict
import json
import os
from pathlib import Path

import numpy as np

from .contracts import McapEpisodeRef, NormalizedBatchRef, NormalizedEpisode, NormalizedRecord, SourceIdentity
from .deletion import write_json_durable


def save_episode(path: Path, episode: NormalizedEpisode) -> None:
    topics = sorted(episode.valid_measurement_times_ns)
    arrays = {
        "state": episode.state, "action": episode.action,
        "frame_times_ns": episode.frame_times_ns,
        "measurement_topics": np.asarray(topics, dtype=np.str_),
    }
    arrays.update({f"measurement_{i}": episode.valid_measurement_times_ns[topic]
                   for i, topic in enumerate(topics)})
    with path.open("xb") as stream:
        np.savez_compressed(stream, **arrays)
        stream.flush()
        os.fsync(stream.fileno())


def load_episode(record: NormalizedRecord) -> NormalizedEpisode:
    if record.input_error or record.data_path is None:
        raise ValueError(record.input_error or "normalized data unavailable")
    with np.load(record.data_path, allow_pickle=False) as data:
        topics = data["measurement_topics"].tolist()
        return NormalizedEpisode(
            record.ref, data["state"], data["action"], data["frame_times_ns"],
            record.source_identity,
            {topic: data[f"measurement_{i}"] for i, topic in enumerate(topics)},
        )


def save_batch(batch: NormalizedBatchRef) -> None:
    records = []
    for record in batch.records:
        ref = {key: str(value) if isinstance(value, Path) else value
               for key, value in asdict(record.ref).items()}
        records.append({
            "ref": ref, "data_file": record.data_path.name if record.data_path else None,
            "source_identity": asdict(record.source_identity) if record.source_identity else None,
            "input_error": record.input_error,
            "deletion_blocked_reason": record.deletion_blocked_reason,
        })
    write_json_durable(batch.manifest_path, {"format": "normalized_batch_v1", "records": records})


def load_batch(manifest_path: Path) -> NormalizedBatchRef:
    manifest_path = Path(manifest_path).resolve()
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if raw.get("format") != "normalized_batch_v1":
        raise ValueError("unsupported normalized batch format")
    records = []
    seen = set()
    for item in raw["records"]:
        ref = dict(item["ref"])
        ref["dataset_root"], ref["mcap_path"] = Path(ref["dataset_root"]), Path(ref["mcap_path"])
        ref = McapEpisodeRef(**ref)
        if ref.episode_index in seen:
            raise ValueError("duplicate episode index in normalized batch")
        seen.add(ref.episode_index)
        name = item["data_file"]
        if name is not None and (not isinstance(name, str) or Path(name).name != name or not name.endswith(".npz")):
            raise ValueError("normalized data must be a local NPZ filename")
        identity = SourceIdentity(**item["source_identity"]) if item["source_identity"] else None
        records.append(NormalizedRecord(ref, manifest_path.parent / name if name else None,
                                        identity, item["input_error"], item["deletion_blocked_reason"]))
    return NormalizedBatchRef(manifest_path, tuple(records))
