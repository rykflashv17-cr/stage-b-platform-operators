"""A pretraining data cleaning business file is one indexed ZIP, transported as a raw SDK file."""
from pathlib import Path
import re
from zipfile import ZIP_STORED, ZipFile

from .archive import pack_bundle, read_bundle, read_index
from .handoff import require


_NORMAL = r"(?:manifest\.json|selection\.json|episode_[0-9]{8,}\.npz)"
_CACHE = r"cache/(?:global_stats\.npz|stage1_global_stats\.npz|state_action_lag\.npz|source_fingerprint\.json)"
_MEMBERS = {
    "normalized_batch": (_NORMAL, {"manifest.json", "selection.json"}),
    "statistics_context": (rf"(?:context\.json|normalized/{_NORMAL}|{_CACHE})",
                           {"context.json", "normalized/manifest.json", "normalized/selection.json"}),
    "quality_batch": (r"(?:decisions\.jsonl|selection\.json|quality_summary\.json)",
                      {"decisions.jsonl", "selection.json", "quality_summary.json"}),
}
_FORMAT = "stage_b_mcap_artifact_v1"


def _check_members(kind, names):
    require(kind in _MEMBERS, "unknown pretraining data cleaning artifact kind")
    pattern, required = _MEMBERS[kind]
    require(required <= names and all(re.fullmatch(pattern, name) for name in names),
            "missing or unexpected pretraining data cleaning artifact member")


def pack_artifact(path, kind, folder):
    folder = Path(folder)
    names = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    _check_members(kind, names)
    pack_bundle(path, {"format": _FORMAT, "kind": kind}, {"payload": folder})


def unpack_artifact(path, kind, destination):
    """Restore only a complete, verified payload into a new private directory."""
    destination = Path(destination)
    require(not destination.exists(), "pretraining data cleaning artifact destination already exists")
    expected = {"format": _FORMAT, "kind": kind}
    with Path(path).open("rb") as stream, ZipFile(stream) as archive:
        entries = archive.infolist()
        require(0 < len(entries) <= 100_001 and "bundle.json" in archive.namelist(),
                "invalid pretraining data cleaning artifact index")
        index = archive.getinfo("bundle.json")
        require(index.file_size <= 16 << 20 and index.compress_type == ZIP_STORED, "invalid artifact index size/encoding")
        record = read_index(archive.read(index))
        require(record.get("value") == expected, "pretraining data cleaning artifact kind mismatch")
        files = {entry.filename for entry in entries if entry.filename != "bundle.json"}
        require(all(name.startswith("payload/") for name in files), "unexpected pretraining data cleaning artifact root")
        _check_members(kind, {name.removeprefix("payload/") for name in files})
        require(set(record["directories"]) == {str(Path(name).parent) for name in files},
                "unexpected pretraining data cleaning artifact directory")
        require(read_bundle(stream, destination) == expected, "pretraining data cleaning artifact changed while reading")
    return destination / "payload"
