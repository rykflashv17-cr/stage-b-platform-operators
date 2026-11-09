"""Negative file-contract tests independent of transport and algorithm fitting."""
import io
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import numpy as np
import pytest

from pretrain_cleaning_common.artifact import pack_artifact, unpack_artifact
from pretrain_cleaning_common.contracts import McapEpisodeRef, NormalizedRecord
from pretrain_cleaning_common.handoff import _validate_normalized_episode, validate_source_members


def test_raw_members_cannot_be_included_or_smuggled(tmp_path):
    files = tmp_path / "business"
    files.mkdir()
    for name in ("manifest.json", "selection.json"):
        (files / name).write_bytes(b"{}")
    raw = files / "episode.mcap"
    raw.touch()
    clean = tmp_path / "normalized.zip"
    with pytest.raises(ValueError, match="unexpected"):
        pack_artifact(clean, "normalized_batch", files)
    raw.unlink()
    pack_artifact(clean, "normalized_batch", files)
    with pytest.raises(ValueError, match="kind"):
        unpack_artifact(clean, "statistics_context", tmp_path / "wrong")
    for i, name in enumerate(("../escape", "payload/episode.mcap", "elsewhere/manifest.json")):
        altered = tmp_path / f"bad-{i}.zip"
        with ZipFile(clean) as original, ZipFile(altered, "x") as bad:
            for member in original.namelist():
                bad.writestr(member, original.read(member))
            bad.writestr(name, b"raw")
        with pytest.raises(ValueError):
            unpack_artifact(altered, "normalized_batch", tmp_path / f"restored-{i}")
    altered = tmp_path / "tamper.zip"
    with ZipFile(clean) as original, ZipFile(altered, "x", ZIP_STORED) as bad:
        for member in original.namelist():
            data = original.read(member)
            bad.writestr(member, b'{"rewritten":true}' if member == "payload/selection.json" else data)
    with pytest.raises(ValueError, match="digest"):
        unpack_artifact(altered, "normalized_batch", tmp_path / "tampered")


@pytest.mark.parametrize("problem", ["extra", "object", "huge_shape", "timestamps", "repeated_topic", "empty_frames"])
def test_npz_members_are_validated_as_actual_arrays(tmp_path, problem):
    file = tmp_path / "episode_00000000.npz"
    record = NormalizedRecord(McapEpisodeRef(0, Path("/raw"), Path("/raw/one.mcap"), "one.mcap"), file)
    rows = 0 if problem == "empty_frames" else 1
    arrays = {"state": np.zeros((rows, 16)), "action": np.zeros((rows, 16)),
              "frame_times_ns": np.arange(rows, dtype=np.int64), "measurement_topics": np.array([], dtype=np.str_)}
    if problem == "extra":
        arrays["secret"] = np.arange(100)
    if problem == "object":
        arrays["state"] = np.zeros((1, 16), dtype=object)
    if problem == "timestamps":
        arrays["frame_times_ns"] = np.array([0.0])
    if problem == "repeated_topic":
        arrays.update(measurement_topics=np.array(["topic", "topic"]), measurement_0=np.array([0]),
                      measurement_1=np.array([1]))
    if problem == "huge_shape":
        stream = io.BytesIO()
        np.lib.format.write_array_header_1_0(stream, {"descr": "<f8", "shape": (2**32, 16), "fortran_order": False})
        with ZipFile(file, "x") as archive:
            archive.writestr("state.npy", stream.getvalue())
    else:
        np.savez(file, **arrays)
    with pytest.raises(ValueError):
        _validate_normalized_episode(record, "jindian_marvin")


@pytest.mark.parametrize("mutation", [
    {"input_index": True}, {"input_index": 2}, {"input_name": "other"}, {"logical_key": "../one.mcap"},
    {"sha256": "F"*64}, {"attempt_id": "tmp/run"},
])
def test_frozen_source_reference_rejects_forged_fields(mutation):
    ref = {"attempt_id": "00000000-0000-0000-0000-000000000001", "input_name": "raw_files", "input_index": 0,
           "logical_key": "one.mcap", "sha256": "0"*64}
    with pytest.raises(ValueError):
        validate_source_members("jindian_marvin", [{"episode_index": 0, "source_ref": ref | mutation}])
