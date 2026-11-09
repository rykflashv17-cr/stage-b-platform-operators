"""The pretraining data cleaning ZIP index is a business file format shared by all four nodes."""
import hashlib
import json
import stat
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest


def test_old_index_restores_and_new_writer_preserves_its_layout(tmp_path):
    from pretrain_cleaning_common.archive import pack_bundle, read_bundle

    value = {"episode_index": 4, "mcap_path": "dataset/a.mcap"}
    member = "sources/dataset/a.mcap"
    index = {
        "format": "operator_bundle_v1",
        "value": value,
        "directories": ["sources", "sources/dataset", "sources/dataset/empty"],
        "files": {member: hashlib.sha256(b"payload").hexdigest()},
    }
    legacy = tmp_path / "legacy.zip"
    with ZipFile(legacy, "x") as archive:
        archive.writestr(member, b"payload")
        archive.writestr("bundle.json", json.dumps(index))
    restored = tmp_path / "restored"
    assert read_bundle(legacy, restored) == value
    assert (restored / member).read_bytes() == b"payload"
    assert (restored / "sources/dataset/empty").is_dir()

    written = tmp_path / "episode.zip"
    pack_bundle(written, value, {"sources": restored / "sources"})
    with ZipFile(written) as archive:
        assert archive.namelist() == [member, "bundle.json"]
        assert archive.read(member) == b"payload"
        assert json.loads(archive.read("bundle.json")) == index


@pytest.mark.parametrize("problem", ["escape", "extra", "digest", "symlink", "compressed", "duplicate_index_key"])
def test_rejects_unsafe_or_inconsistent_archive(tmp_path, problem):
    from pretrain_cleaning_common.archive import read_bundle

    data = b"payload"
    member = "sources/../../escape" if problem == "escape" else "sources/a.mcap"
    index = {
        "format": "operator_bundle_v1",
        "value": {},
        "directories": ["sources"],
        "files": {member: "0" * 64 if problem == "digest" else hashlib.sha256(data).hexdigest()},
    }
    info = ZipInfo(member)
    if problem == "symlink":
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
    if problem == "compressed":
        info.compress_type = ZIP_DEFLATED
    manifest = json.dumps(index)
    if problem == "duplicate_index_key":
        manifest = '{"files": {}, ' + manifest[1:]
    package = tmp_path / "bad.zip"
    with ZipFile(package, "x") as archive:
        archive.writestr(info, data)
        archive.writestr("bundle.json", manifest)
        if problem == "extra":
            archive.writestr("unlisted", b"untrusted")
    with pytest.raises(ValueError):
        read_bundle(package, tmp_path / "target")
    assert not (tmp_path / "escape").exists()


def test_refuses_to_pack_output_inside_source(tmp_path):
    from pretrain_cleaning_common.archive import pack_bundle

    with pytest.raises(ValueError, match="inside"):
        pack_bundle(tmp_path / "out.zip", {}, {"sources": tmp_path})
