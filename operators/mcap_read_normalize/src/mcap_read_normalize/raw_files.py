"""Translate a selected raw file list into the two supported episode layouts."""
from collections import Counter
from dataclasses import dataclass
import hashlib
from pathlib import Path

from pretrain_cleaning_common.contracts import McapEpisodeRef, SourceIdentity
from pretrain_cleaning_common.handoff import require


@dataclass(frozen=True)
class RawFile:
    key: Path
    path: Path
    origin: str
    index: int


def _keys(values, pipeline):
    require(isinstance(values, list) and values, "raw_files must be a nonempty file array")
    keys = []
    for value in values:
        require(isinstance(value, str) and value, "raw_files must contain only file path strings")
        path = Path(value)
        require(value == path.as_posix() and ".." not in path.parts and path.name
                and not value.startswith("//") and not any(
                    c in ":\\" or ord(c) < 32 or ord(c) == 127 for c in value),
                "unsafe raw file path")
        require(not pipeline or not path.is_absolute(), "Pipeline raw file keys must be relative")
        keys.append(path)
    require(len(keys) == len(set(keys)), "duplicate raw file key")
    return keys


def _logical_key(ctx, entry):
    if not entry.key.is_absolute():
        return entry.key
    require(entry.key == entry.path, "absolute raw file differs from its binding")
    # Local fixture paths are absolute. Strip the outermost authorized directory
    # so neither a worker pathname nor the set of other selected files affects the key.
    root = ctx.inputs.authorized_path(entry.path.parent, directory=True)
    for parent in root.parents:
        try:
            ctx.inputs.authorized_path(parent, directory=True)
        except ValueError:
            break
        root = parent
    return entry.path.relative_to(root)


def _episodes(files, abc, logical):
    def sort_key(key):
        entry = files[key]
        return logical[entry.index].as_posix(), entry.key.as_posix()

    if not abc:
        require(all(key.suffix == ".mcap" for key in files), "Marvin raw_files requires MCAP files only")
        return sorted(files, key=sort_key)
    require(all(key.name in {"episode.mcap", "eef_pose.parquet"} for key in files),
            "unexpected ABC raw file")
    primary = sorted((key for key in files if key.name == "episode.mcap"), key=sort_key)
    for key in primary:
        sidecar = files.get(key.with_name("eef_pose.parquet"))
        require(sidecar is not None, "missing eef_pose.parquet in selected ABC episode")
        require(sidecar.origin == files[key].origin, "ABC companion files have different origins")
    require(len(files) == 2 * len(primary), "unpaired ABC eef_pose.parquet")
    return primary


def _stage(ctx, entry, key):
    path = ctx.workspace.path("raw-input/" + key.as_posix())
    identity = SourceIdentity.capture(entry.path)
    checksum = hashlib.sha256()
    with entry.path.open("rb") as source, path.open("xb") as destination:
        while block := source.read(1024 * 1024):
            ctx.check_cancelled()
            checksum.update(block)
            destination.write(block)
    require(SourceIdentity.capture(entry.path) == identity and path.stat().st_size == identity.st_size,
            "raw file changed while staging")
    return path, (identity, SourceIdentity.capture(path)), checksum.hexdigest()


def select_episodes(ctx):
    pipeline = ctx.execution.get("mode") == "pipeline"
    keys = _keys(ctx.inputs.value("raw_files"), pipeline)
    source = ctx.parameters["source"]
    require(source in {"abc_130k", "jindian_marvin"}, "unknown source profile")
    abc = source == "abc_130k"
    # Reject multiple episodes from logical keys before accessing materialized
    # files or copying their bytes into this invocation's workspace.
    if abc:
        require(len(keys) == 2, "ABC B01 requires exactly one episode: one MCAP and one eef_pose.parquet")
        require({key.name for key in keys} == {"episode.mcap", "eef_pose.parquet"}
                and keys[0].parent == keys[1].parent,
                "ABC B01 requires episode.mcap and eef_pose.parquet in the same logical directory")
    else:
        require(len(keys) == 1 and keys[0].suffix == ".mcap",
                "Marvin B01 requires exactly one MCAP per invocation")
    paths = ctx.inputs.files("raw_files")
    origins = ctx.inputs.file_origins("raw_files")
    entries = [RawFile(key, Path(path), origin, index)
               for index, (key, path, origin) in enumerate(zip(keys, paths, origins, strict=True))]
    seen = set()
    for entry in entries:
        ctx.check_cancelled()
        require(entry.path.is_absolute() and entry.path.is_file()
                and not any(p.is_symlink() for p in (entry.path, *entry.path.parents)),
                "raw file missing or unsafe")
        stat = entry.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        require(identity not in seen, "duplicate materialized raw file")
        seen.add(identity)

    logical = {e.index: _logical_key(ctx, e) for e in entries}
    counts = Counter(logical.values())
    # Distinct absolute fixtures can have identical root-relative names. Only
    # namespace these origins; use the original keys to keep order stable.
    collisions = {e.origin for e in entries if counts[logical[e.index]] > 1}
    ranked = sorted(collisions, key=lambda origin: min(str(e.key) for e in entries if e.origin == origin))
    prefixes = {origin: f".origin-{i}" for i, origin in enumerate(ranked)}
    files = {}
    for entry in entries:
        key = logical[entry.index]
        if entry.origin in prefixes:
            key = Path(prefixes[entry.origin]) / key
        require(key not in files, "ambiguous or duplicate raw file key")
        files[key] = entry
    primary = _episodes(files, abc, logical)
    require(len(primary) == 1, "B01 requires exactly one episode per invocation")
    staged = {key: _stage(ctx, entry, key) for key, entry in files.items()}
    root = ctx.workspace.path("raw-input")
    refs, source_refs, identities = [], [], []
    for index, key in enumerate(primary):
        ctx.check_cancelled()
        entry = files[key]
        path, evidence, digest = staged[key]
        relative = key.parent if abc else key
        source_dir = logical[entry.index].parent
        native_abc = abc and len(source_dir.parts) >= 4 and source_dir.parts[-4] == "data"
        part = source_dir.parts[-3] if native_abc else ""
        task = source_dir.parent.name if native_abc else relative.parent.name
        refs.append(McapEpisodeRef(index, root, path, relative.as_posix(), part, task))
        identities.append(evidence)
        source_refs.append({"attempt_id": ctx.execution["attemptId" if pipeline else "attempt_id"],
                            "input_name": "raw_files", "input_index": entry.index,
                            "logical_key": logical[entry.index].as_posix(), "sha256": digest})
    return refs, source_refs, identities
