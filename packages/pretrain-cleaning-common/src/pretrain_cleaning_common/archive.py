"""Read and write the indexed ZIP used by pretraining data cleaning MCAP business files."""
import hashlib
import json
from pathlib import Path
import re
import stat
from zipfile import ZIP_STORED, ZipFile

from . import require

_MANIFEST = "bundle.json"
_MAX_MANIFEST = 16 << 20
_MAX_ENTRIES = 100_000
_MAX_BYTES = 8 << 30


def _parts(name):
    require(isinstance(name, str) and name and all(p not in ("", ".", "..") for p in name.split("/"))
            and not any(c in ":\\" or ord(c) < 32 for c in name), "unsafe bundle path")
    return name.split("/")


def _copy(reader, writer):
    checksum = hashlib.sha256()
    while block := reader.read(1024 * 1024):
        checksum.update(block)
        writer.write(block)
    return checksum.hexdigest()


def pack_bundle(path, value, attachments):
    files, directories = {}, []
    total = 0
    require(all(not Path(path).resolve().is_relative_to(Path(folder).resolve()) for folder in attachments.values()),
            "bundle destination is inside its own attachments")
    with ZipFile(path, "x", compression=ZIP_STORED) as archive:
        for label, folder in sorted(attachments.items()):
            require(re.fullmatch(r"[a-z][a-z0-9_]*", label) and label != _MANIFEST, "invalid attachment name")
            folder = Path(folder)
            require(folder.is_dir() and not any(p.is_symlink() for p in (folder, *folder.parents)),
                    "unsafe attachment root")
            directories.append(label)
            for p in sorted(folder.rglob("*")):
                require(not p.is_symlink(), "attachment symlink forbidden")
                name = label + "/" + p.relative_to(folder).as_posix()
                _parts(name)
                if p.is_dir():
                    directories.append(name)
                else:
                    require(p.is_file(), "attachment must be a regular file")
                    size = p.stat().st_size
                    total += size
                    require(total <= _MAX_BYTES, "attachment size limit exceeded")
                    with p.open("rb") as reader, archive.open(name, "w", force_zip64=True) as writer:
                        files[name] = _copy(reader, writer)
                    require(archive.getinfo(name).file_size == size, "attachment changed while packing")
                require(len(directories) + len(files) <= _MAX_ENTRIES, "too many bundle members")
        index = {"format": "operator_bundle_v1", "value": value, "directories": directories, "files": files}
        raw = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        require(len(raw) <= _MAX_MANIFEST, "bundle manifest too large")
        archive.writestr(_MANIFEST, raw)


def read_index(raw):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate index key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(raw, object_pairs_hook=unique_keys, parse_constant=reject_constant)


def _destination(root, name):
    # Names have already passed _parts; reject existing symlinks inside the private root.
    path = root / name
    require(not any(p.is_symlink() for p in (path, *path.parents)), "unsafe bundle destination")
    return path


def read_bundle(path, root):
    """Merge explicitly listed members into a private directory; files may never overlap."""
    root = Path(root).absolute()
    require(not any(p.is_symlink() for p in (root, *root.parents)), "unsafe bundle destination")
    root.mkdir(parents=True, exist_ok=True)
    with ZipFile(path) as archive:
        entries = archive.infolist()
        names = set(archive.namelist())
        require(_MANIFEST in names and len(names) == len(entries) <= _MAX_ENTRIES + 1
                and archive.getinfo(_MANIFEST).file_size <= _MAX_MANIFEST, "invalid bundle index")
        require(all(i.compress_type == ZIP_STORED and not i.flag_bits & 1 and
                    not stat.S_ISLNK(i.external_attr >> 16) for i in entries), "unsupported bundle entry")
        record = read_index(archive.read(_MANIFEST))
        require(isinstance(record, dict) and set(record) == {"format", "directories", "files", "value"}
                and record["format"] == "operator_bundle_v1", "unknown bundle format")
        dirs, files = record["directories"], record["files"]
        require(isinstance(dirs, list) and isinstance(files, dict) and set(files) == names - {_MANIFEST}
                and len(dirs) == len(set(dirs)) and not set(dirs) & names
                and len(files) + len(dirs) <= _MAX_ENTRIES and sum(i.file_size for i in entries) <= _MAX_BYTES + _MAX_MANIFEST,
                "invalid bundle manifest")
        declared = set(dirs)
        for name in [*dirs, *files]:
            parts = _parts(name)
            require(re.fullmatch(r"[a-z][a-z0-9_]*", parts[0]) and parts[0] in declared and
                    (len(parts) == 1 or name.rsplit("/", 1)[0] in declared), "invalid attachment hierarchy")
        for name in sorted(dirs):
            _destination(root, name).mkdir(exist_ok=True)
        for name, checksum in files.items():
            dest = _destination(root, name)
            require(not dest.exists(), "duplicate file across input bundles")
            with archive.open(name) as reader, dest.open("xb") as writer:
                require(_copy(reader, writer) == checksum, "bundle file digest mismatch")
            dest.chmod(0o400)
    return record["value"]
