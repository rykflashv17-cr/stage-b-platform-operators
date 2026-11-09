"""B01 selects only bound raw files, independently of download paths and order."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcap_read_normalize.io import read_raw_files, write_normalized_batch_ref
from pretraining_data_cleaning_common.contracts import NormalizedBatchRef, NormalizedRecord, SourceIdentity


ATTEMPT = "d7d0c6d1-9890-4783-be57-21d08a6b3e78"


class Inputs:
    def __init__(self, entries, roots):
        self.entries, self.roots = entries, roots

    def value(self, name):
        assert name == "raw_files"
        return [key for key, _, _ in self.entries]

    def files(self, name):
        assert name == "raw_files"
        return [file for _, file, _ in self.entries]

    def file_origins(self, name):
        assert name == "raw_files"
        return [origin for _, _, origin in self.entries]

    def authorized_path(self, value, *, directory=False):
        assert directory
        if not any(value.is_relative_to(r) for r in self.roots):
            raise ValueError("outside read roots")
        return value


def context(root, entries, source="abc_130k", mode="pipeline", roots=()):
    def work(name):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return SimpleNamespace(
        inputs=Inputs(entries, roots), parameters={"source": source, "workers": 1},
        workspace=SimpleNamespace(path=work), execution={"mode": mode, "attemptId" if mode == "pipeline" else "attempt_id": ATTEMPT}, check_cancelled=lambda: None,
    )


def downloaded(root, keys):
    entries = []
    for i, key in enumerate(keys):
        path = root / str(i) / "blob"
        path.parent.mkdir(parents=True)
        path.write_bytes(str(key).encode())
        entries.append((key, path, "origin"))
    return entries


@pytest.mark.parametrize("mode", ["pipeline", "local"])
def test_abc_pairs_by_original_directory_and_keeps_stable_selection(tmp_path, mode):
    dirs = ["data/train/pick/episode_009", "data/test/put/episode_002"]
    keys = [dirs[1]+"/eef_pose.parquet", dirs[0]+"/episode.mcap",
            dirs[1]+"/episode.mcap", dirs[0]+"/eef_pose.parquet"]
    entries = downloaded(tmp_path / "downloads", keys)
    public = []
    for i, rows in enumerate([entries, entries[::-1]]):
        ctx = context(tmp_path / str(i), rows, mode=mode)
        _, refs, metadata = read_raw_files(ctx, ctx.workspace.path("normalization"))
        assert [(r.episode_index, r.rel_path, r.part, r.task) for r in refs] == [
            (0, dirs[1], "test", "put"), (1, dirs[0], "train", "pick")]
        for ref in refs:
            assert ref.mcap_path.read_text() == ref.rel_path + "/episode.mcap"
            assert ref.mcap_path.with_name("eef_pose.parquet").read_text() == ref.rel_path + "/eef_pose.parquet"
        public.append(metadata["business_refs"])
        for member, ref in zip(metadata["members"], refs, strict=True):
            slot = next(j for j, row in enumerate(rows) if row[0] == ref.rel_path+"/episode.mcap")
            assert member["source_ref"]["input_index"] == slot
        for ref, business in zip(refs, metadata["business_refs"], strict=True):
            assert business["dataset_root"] == "raw"
            assert business["mcap_path"] == "raw/" + ref.rel_path + "/episode.mcap"
    assert public[0] == public[1]


@pytest.mark.parametrize("keys", [
    [], ["input/episode.mcap"], ["input/eef_pose.parquet"],
    ["a/episode.mcap", "b/eef_pose.parquet"],
    ["a/episode.mcap", "a/eef_pose.parquet", "a/extra.txt"],
    ["a/episode.mcap", "a/episode.mcap", "a/eef_pose.parquet"],
    ["../a/episode.mcap", "../a/eef_pose.parquet"],
    ["a//episode.mcap", "a/eef_pose.parquet"],
    [["a/episode.mcap"]], [None], ["a/file.mcap", "a/eef_pose.parquet"],
])
def test_raw_input_structure_fails_before_normalization(tmp_path, keys):
    ctx = context(tmp_path / "work", downloaded(tmp_path / "inputs", keys))
    with pytest.raises(ValueError):
        read_raw_files(ctx, ctx.workspace.path("normalization"))


def test_abc_companions_must_share_one_storage_origin(tmp_path):
    mcap, parquet = downloaded(tmp_path, ["folder/episode.mcap", "folder/eef_pose.parquet"])
    ctx = context(tmp_path / "work", [mcap, (*parquet[:2], "other")])
    with pytest.raises(ValueError, match="origin"):
        read_raw_files(ctx, ctx.workspace.path("normalization"))


def test_marvin_rejects_colliding_keys_across_origins_and_non_mcap_files(tmp_path):
    a, b = downloaded(tmp_path, ["episode.mcap", "episode.mcap"])
    for invalid in [(*b[:2], "other"), ("eef_pose.parquet", b[1], "origin")]:
        ctx = context(tmp_path / "work", [a, invalid], source="jindian_marvin")
        with pytest.raises(ValueError):
            read_raw_files(ctx, ctx.workspace.path("normalization"))


def test_local_selection_preserves_authorized_sources_and_inferred_metadata(tmp_path):
    root = tmp_path / "sources"
    task = root / "pick"
    task.mkdir(parents=True)
    first, second = root / "one.mcap", task / "two.mcap"
    first.touch()
    second.touch()
    entries = [("pick/two.mcap", second, "root"), (str(first), first, "root")]
    ctx = context(tmp_path / "work", entries, source="jindian_marvin", mode="local", roots=[root])
    _, refs, metadata = read_raw_files(ctx, ctx.workspace.path("normalization"))
    assert [(r.mcap_path.relative_to(ctx.workspace.path("raw-input")), r.task, r.rel_path) for r in refs] == [
        (Path("one.mcap"), "", "one.mcap"), (Path("pick/two.mcap"), "pick", "pick/two.mcap")]
    assert [m["source_ref"]["logical_key"] for m in metadata["members"]] == ["one.mcap", "pick/two.mcap"]
    # Selecting the very same file twice by absolute and relative name is invalid.
    ctx.inputs.entries.append((str(second), second, "root"))
    with pytest.raises(ValueError, match="duplicate"):
        read_raw_files(ctx, ctx.workspace.path("normalization"))


def test_forwarded_root_level_marvin_keeps_only_selected_bytes_and_digest(tmp_path):
    import hashlib

    entries = downloaded(tmp_path / "cache", ["one.mcap"])
    ctx = context(tmp_path / "work", entries, source="jindian_marvin", mode="local")
    _, refs, metadata = read_raw_files(ctx, ctx.workspace.path("normalization"))
    record = NormalizedRecord(refs[0], None, SourceIdentity.capture(refs[0].mcap_path), "bad MCAP")
    batch = NormalizedBatchRef(ctx.workspace.path("normalization/normalized/manifest.json"), (record,))
    written = {}
    ctx.outputs = SimpleNamespace(file=lambda name, path: written.update(name=name, path=path))
    write_normalized_batch_ref(ctx, batch, metadata)
    from zipfile import ZipFile
    import json
    with ZipFile(written["path"]) as archive:
        assert set(archive.namelist()) == {"bundle.json", "payload/manifest.json", "payload/selection.json"}
        selection = json.loads(archive.read("payload/selection.json"))
        assert str(tmp_path) not in json.dumps(selection)
    member, = selection["members"]
    assert member["source"] == "/raw/one.mcap"
    assert member["source_ref"] == {
        "attempt_id": ATTEMPT, "input_name": "raw_files", "input_index": 0, "logical_key": "one.mcap",
        "sha256": hashlib.sha256(entries[0][1].read_bytes()).hexdigest(),
    }


def test_distinct_absolute_inputs_keep_their_full_names_across_granted_roots(tmp_path):
    paths = [tmp_path / part / "same.mcap" for part in ["b", "a"]]
    for path in paths:
        path.parent.mkdir()
        path.touch()
    rows = [(str(path), path, path.parent.name) for path in paths]
    ctx = context(tmp_path / "work", rows, source="jindian_marvin", mode="local", roots=[p.parent for p in paths])
    _, refs, metadata = read_raw_files(ctx, ctx.workspace.path("normalization"))
    assert len({ref.mcap_path for ref in refs}) == 2
    assert [m["source_ref"]["logical_key"] for m in metadata["members"]] == ["same.mcap", "same.mcap"]
    assert [m["source_ref"]["input_index"] for m in metadata["members"]] == [1, 0]



def test_absolute_nested_single_file_uses_granted_root_and_isolates_bytes(tmp_path):
    source = tmp_path / "capture/task/nested/one.mcap"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"frozen")
    ctx = context(tmp_path / "work", [(str(source), source, "root")], source="jindian_marvin",
                  mode="local", roots=[tmp_path / "capture"])
    _, refs, metadata = read_raw_files(ctx, ctx.workspace.path("normalization"))
    source.write_bytes(b"new bytes")
    assert refs[0].mcap_path.read_bytes() == b"frozen"
    assert metadata["members"][0]["source_ref"]["logical_key"] == "task/nested/one.mcap"
