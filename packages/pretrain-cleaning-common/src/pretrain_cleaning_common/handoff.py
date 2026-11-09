"""Fixed profiles and verified, pickle-free pretraining data cleaning handoffs."""
from dataclasses import asdict, replace
import hashlib
import json
import os
import math
import re
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
from pathlib import Path
import shutil

import numpy as np
from pretrain_cleaning_engine import root as engine_root, REVISION
from . import require
from .contracts import StatisticsBundle, StatisticsContext
from .filesystem import write_json_durable
from .spec import load_spec, discover
from .storage import load_batch, load_episode

SOURCE_REVISION = "5f42aecf39d0dabf1e57e6c79bc221db1dde9b32"
PROFILES = {"jindian_marvin": 16, "abc_130k": 32}
RESOURCES = Path(__file__).resolve().parent / "resources"


def sha256(path):
    result=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024), b""):
            result.update(block)
    return result.hexdigest()


def profile_digest(source):
    require(source in PROFILES, "unknown source profile")
    package = engine_root() / "datasets/abc_130k" if source == "abc_130k" else RESOURCES / "dataset_adapters/jindian_marvin"
    h=hashlib.sha256((REVISION+SOURCE_REVISION+"v4-handoff-1").encode())
    for path in [RESOURCES / "configs" / (source+".yaml"), *sorted(package.glob("*.yaml")), *sorted(package.glob("*.py")), *sorted(package.glob("*.json"))]:
        h.update(path.name.encode());h.update(path.read_bytes())
    return h.hexdigest()


def make_spec(source, input_root, output_root, workers):
    require(source in PROFILES, "unknown source profile")
    require(type(workers) is int and 1 <= workers <= 4, "workers must be an integer in 1..4")
    package=engine_root() / "datasets/abc_130k" if source == "abc_130k" else RESOURCES / "dataset_adapters/jindian_marvin"
    spec=load_spec(RESOURCES / "configs" / (source+".yaml"),input_root=Path(input_root),output_root=Path(output_root),repo_root=engine_root(),package_dir=package,quality_yaml=package / "quality_stage_b.yaml")
    return replace(spec,workers=workers)


def selection_metadata(source, input_dirs, records):
    roots=[str(Path(p)) for p in input_dirs]
    require(roots and all(Path(p).is_absolute() for p in roots), "absolute selected directories required")
    return {"format":"mcap_selection_v1", "source":source, "profile_digest":profile_digest(source),
            "source_revision":SOURCE_REVISION,"engine_revision":REVISION,"input_dirs":roots,
            "input_root":os.path.commonpath(roots),
            "members":[{"episode_index":r.ref.episode_index,"source_episode_index":r.ref.episode_index,
                        "source":str(r.ref.mcap_path),"dataset_root":str(r.ref.dataset_root),
                        "rel_path":r.ref.rel_path,"part":r.ref.part,"task":r.ref.task} for r in records]}


def discover_inputs(input_dirs, source, output_root, workers):
    roots=[Path(p) for p in input_dirs]
    require(roots, "at least one input directory required")
    for index,path in enumerate(roots):
        require(path.is_absolute() and path.is_dir() and ".." not in path.parts, "invalid input directory")
        require(not any(p.is_symlink() for p in [path,*path.parents]), "input symlink forbidden")
        require(not any(path.is_relative_to(old) or old.is_relative_to(path) for old in roots[:index]),"duplicate or overlapping directories")
    root=Path(os.path.commonpath(roots))
    spec=make_spec(source,root,output_root,workers)
    refs=[];original=[]
    for index,directory in enumerate(roots):
        selected=replace(spec,input_root=directory,output_root=Path(output_root) / ".discovery" / str(index))
        for ref in discover(selected):
            original.append(ref.episode_index)
            refs.append(replace(ref,episode_index=len(refs)))
    from .contracts import NormalizedRecord
    meta=selection_metadata(source,roots,[NormalizedRecord(r,None) for r in refs])
    for member,index in zip(meta["members"],original): member["source_episode_index"]=index
    return spec,refs,meta


def validate_selection(metadata, source, batch=None):
    require(isinstance(metadata,dict) and metadata.get("format")=="mcap_selection_v1", "invalid source selection")
    require(metadata.get("source")==source and metadata.get("profile_digest")==profile_digest(source),"source profile mismatch")
    require(metadata.get("source_revision")==SOURCE_REVISION and metadata.get("engine_revision")==REVISION,"implementation revision mismatch")
    roots=[Path(p) for p in metadata["input_dirs"]]
    require(roots and all(p.is_absolute() and ".." not in p.parts for p in roots),"invalid source roots")
    require(metadata["input_root"]==os.path.commonpath(roots),"source root mismatch")
    ids=set();sources=set()
    for m in metadata["members"]:
        path=Path(m["source"])
        require(type(m["episode_index"]) is int and m["episode_index"]>=0 and m["episode_index"] not in ids,"invalid episode identity")
        require(path.is_absolute() and ".." not in path.parts and str(path) not in sources and any(path.is_relative_to(r) for r in roots),"source outside selected scope or duplicated")
        ids.add(m["episode_index"]);sources.add(str(path))
    if batch is not None:
        require(len(batch.records)==len(metadata["members"]),"selected member count mismatch")
        for record,member in zip(batch.records,metadata["members"]):
            actual={"episode_index":record.ref.episode_index,"source":str(record.ref.mcap_path),"dataset_root":str(record.ref.dataset_root),"rel_path":record.ref.rel_path,"part":record.ref.part,"task":record.ref.task}
            require(all(member[k]==v for k,v in actual.items()),"normalized source identity mismatch")
            require((record.data_path is None) == (record.input_error is not None),
                    "normalized record requires numeric data or an input error")
            for message in (record.input_error, record.deletion_blocked_reason):
                require(message is None or isinstance(message, str) and message, "invalid normalized diagnostic")
            if record.source_identity is not None:
                require(all(type(v) is int and 0 <= v < 2**64 for v in asdict(record.source_identity).values()),
                        "invalid source identity integers")
            if record.input_error is None:
                _validate_normalized_episode(record, source)
    return metadata


def save_selection(directory, metadata):
    validate_selection(metadata,metadata["source"])
    write_json_durable(Path(directory) / "selection.json",metadata)


def load_normalized(directory, source):
    directory=Path(directory)
    batch=load_batch(directory / "manifest.json")
    metadata=json.loads((directory / "selection.json").read_text())
    names = [r.data_path.name for r in batch.records if r.data_path is not None]
    require(len(names) == len(set(names)) and set(names) == {p.name for p in directory.glob("*.npz")},
            "normalized NPZ coverage mismatch")
    return batch,validate_selection(metadata,source,batch)


def _files(directory):
    files={}
    for path in sorted(Path(directory).rglob("*")):
        require(not path.is_symlink(),"handoff symlink forbidden")
        if path.is_file(): files[path.relative_to(directory).as_posix()]=sha256(path)
    return files


def save_context(context, directory, metadata):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=False)
    validate_selection(metadata,metadata["source"],context.batch)
    shutil.copytree(context.batch.manifest_path.parent,directory / "normalized")
    selection=directory / "normalized/selection.json"
    if selection.exists():
        require(json.loads(selection.read_text())==metadata,"selection changed")
    else: save_selection(selection.parent,metadata)
    if context.status=="ready":
        cache=directory / "cache";cache.mkdir()
        statistics=context.statistics
        statistics.global_stats.save(str(cache / "global_stats.npz"))
        statistics.stage1_stats.save(str(cache / "stage1_global_stats.npz"))
        if statistics.alignment_stats is not None:
            statistics.alignment_stats.save(str(cache / "state_action_lag.npz"))
        fingerprint=statistics.cache_dir / "source_fingerprint.json"
        if fingerprint.exists(): shutil.copyfile(fingerprint,cache / fingerprint.name)
    payload={"format":"statistics_context_v1","status":context.status,"error":context.error,
             "fit_records":context.fit_records,"coverage_policy":context.coverage_policy,
             "alignment_lag":context.statistics.alignment_lag if context.statistics else None,
             "has_alignment_stats":context.statistics is not None and context.statistics.alignment_stats is not None,
             "files":_files(directory)}
    write_json_durable(directory / "context.json",payload)


def load_context(directory, source):
    directory=Path(directory)
    payload=json.loads((directory / "context.json").read_text())
    require(payload.get("format")=="statistics_context_v1", "invalid statistics context format")
    expected=payload["files"];actual=_files(directory);actual.pop("context.json",None)
    require(expected==actual,"statistics handoff checksum mismatch")
    batch,metadata=load_normalized(directory / "normalized",source)
    status=payload["status"]
    fits=payload["fit_records"]
    require(isinstance(fits,list) and len(fits)==len(batch.records),"statistics fit coverage mismatch")
    for fit,record in zip(fits,batch.records):
        require(set(fit) == {"episode_index", "fit_status", "reason"} and type(fit["episode_index"]) is int
                and fit["episode_index"] == record.ref.episode_index and fit["fit_status"] in {"included", "excluded"},
                "statistics fit identity mismatch")
        require((fit["reason"] is None) if fit["fit_status"] == "included" else isinstance(fit["reason"], str) and fit["reason"],
                "invalid statistics fit reason")
        require(record.input_error is None or fit["fit_status"] == "excluded", "invalid fit for an unreadable episode")
    included = sum(fit["fit_status"] == "included" for fit in fits)
    require(type(payload["has_alignment_stats"]) is bool, "invalid alignment flag")
    stats=None
    if status=="ready":
        import sys
        path=str(engine_root() / "qwen-manip-preprocess/src")
        if path not in sys.path: sys.path.insert(0,path)
        from robot_data_processing.types import GlobalStats
        from robot_data_processing.stage1_stats import Stage1GlobalStats
        from robot_data_processing.stages.state_action_temporal_alignment import StateActionAlignStats
        cache=directory / "cache"
        global_vectors = {f"{field}_{stat}" for field in ("state", "action") for stat in ("q01", "q99", "min", "max")}
        g = _load_stats(GlobalStats, cache / "global_stats.npz", global_vectors)
        require(g.num_frames > 0 and g.num_episodes == included > 0 and all(
            getattr(g, field).shape == (PROFILES[source],) and np.isfinite(getattr(g, field)).all()
            for field in global_vectors), "statistics dimensions/counts mismatch")
        stage_vectors = {"thr_residual", "thr_accel", "thr_jerk"}
        s = _load_stats(Stage1GlobalStats, cache / "stage1_global_stats.npz", stage_vectors)
        require(s.num_frames >= 0 and 0 <= s.num_episodes <= included and all(
            getattr(s, field).shape == (s.num_channels,) for field in stage_vectors), "invalid stage1 statistics")
        align_vectors = {"per_dim_lag_mean", "per_dim_lag_min", "per_dim_lag_max"}
        a = (_load_stats(StateActionAlignStats, cache / "state_action_lag.npz", align_vectors)
             if payload["has_alignment_stats"] else None)
        if a is not None:
            require(0 <= a.num_episodes <= included and a.lag_min <= a.lag_mean <= a.lag_max
                    and all(getattr(a, field).shape == a.per_dim_lag_mean.shape for field in align_vectors),
                    "invalid alignment statistics")
        require((cache / "state_action_lag.npz").exists() == payload["has_alignment_stats"],
                "alignment cache coverage mismatch")
        lag=payload["alignment_lag"]
        require(type(lag) is int,"invalid alignment lag")
        stats=StatisticsBundle(g,s,a,lag,cache)
    else:
        require(payload["alignment_lag"] is None and not payload["has_alignment_stats"]
                and not (directory / "cache").exists(), "nonready context contains statistics")
        require(status != "empty" or included == 0, "empty statistics with included episodes")
    policy=payload["coverage_policy"]
    require(policy is None or isinstance(policy, list) and len(policy) == 3
            and all(isinstance(topic, str) and topic for topic in policy[:2]) and policy[0] != policy[1]
            and type(policy[2]) is int and 0 <= policy[2] < 2**63, "invalid statistics coverage policy")
    return StatisticsContext(batch,status,stats,tuple(fits),tuple(policy) if policy is not None else None,payload["error"]),metadata


def validate_source_ref(ref):
    keys = {"attempt_id", "input_name", "input_index", "logical_key", "sha256"}
    require(isinstance(ref, dict) and set(ref) == keys, "invalid source_ref fields")
    attempt = ref["attempt_id"]
    require(isinstance(attempt, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", attempt),
            "invalid B01 attempt_id")
    require(ref["input_name"] == "raw_files" and type(ref["input_index"]) is int and ref["input_index"] >= 0,
            "invalid frozen input binding")
    key = ref["logical_key"]
    require(isinstance(key, str) and key and key == Path(key).as_posix() and not Path(key).is_absolute()
            and ".." not in Path(key).parts and Path(key).suffix == ".mcap"
            and not any(c in ":\\" or ord(c) < 32 or ord(c) == 127 for c in key), "invalid MCAP logical_key")
    require(isinstance(ref["sha256"], str) and re.fullmatch("[0-9a-f]{64}", ref["sha256"]), "invalid source SHA-256")
    return ref


def validate_source_members(source, members, *, check_cancelled=None):
    require(source in PROFILES and isinstance(members, list) and members, "invalid source selection")
    slot_count = len(members) * (2 if source == "abc_130k" else 1)
    attempts, indexes, by_episode = set(), set(), {}
    for member in members:
        if check_cancelled is not None:
            check_cancelled()
        index = member["episode_index"]
        require(type(index) is int and index >= 0 and index not in by_episode, "invalid selected episode identity")
        ref = validate_source_ref(member["source_ref"])
        require(source != "abc_130k" or Path(ref["logical_key"]).name == "episode.mcap", "invalid ABC source key")
        slot = ref["input_index"]
        require(slot < slot_count and slot not in indexes, "duplicate or out-of-range frozen input slot")
        attempts.add(ref["attempt_id"])
        indexes.add(slot)
        by_episode[index] = ref
    require(len(attempts) == 1, "selection contains multiple B01 attempts")
    return by_episode


def validate_source_refs(metadata):
    validate_selection(metadata, metadata["source"])
    require(metadata["input_root"] == "/raw" and metadata["input_dirs"] == ["/raw"], "invalid virtual source root")
    members = metadata["members"]
    public = metadata["business_refs"]
    require(isinstance(public, list) and len(public) == len(members), "business source coverage mismatch")
    validate_source_members(metadata["source"], members)
    for member, ref in zip(members, public):
        locator = member["source_ref"]
        source = Path(member["source"])
        require(member["dataset_root"] == "/raw" and source.is_relative_to("/raw"), "invalid virtual source path")
        virtual = source.relative_to("/raw").as_posix()
        key = locator["logical_key"]
        require(virtual == key or (re.fullmatch(r"\.origin-[0-9]+", virtual.split("/")[0])
                                    and virtual.partition("/")[2] == key), "source_ref logical_key mismatch")
        relative = Path(virtual).parent if metadata["source"] == "abc_130k" else Path(virtual)
        require(member["rel_path"] == relative.as_posix() and type(member["source_episode_index"]) is int
                and member["source_episode_index"] >= 0 and all(isinstance(member[k], str) for k in ("part", "task")),
                "invalid selected episode")
        require(ref == {"episode_index": member["episode_index"], "dataset_root": "raw",
                        "mcap_path": source.relative_to("/").as_posix(), "rel_path": member["rel_path"],
                        "part": member["part"], "task": member["task"]}, "business source identity mismatch")
    return metadata


def _npz_headers(path):
    # Verify nested NPY headers before np.load can allocate from an untrusted
    # shape. The outer ZIP bounds the compressed NPZ bytes, not the arrays.
    max_bytes = 8 << 30  # same payload ceiling as the pretraining data cleaning archive
    headers = {}
    with ZipFile(path) as archive:
        entries = archive.infolist()
        require(0 < len(entries) <= 100_000 and sum(i.file_size for i in entries) <= max_bytes,
                "NPZ expansion limit exceeded")
        for info in entries:
            name = info.filename
            require(re.fullmatch(r"[a-z][a-z0-9_]*\.npy", name) and name[:-4] not in headers
                    and info.compress_type in (ZIP_STORED, ZIP_DEFLATED), "invalid NPZ member")
            with archive.open(info) as stream:
                version = np.lib.format.read_magic(stream)
                require(version in {(1, 0), (2, 0), (3, 0)}, "unsupported NPY version")
                # V3 differs from V2 only in header encoding. Primitive dtypes used here have ASCII descriptors.
                read_header = (np.lib.format.read_array_header_1_0 if version == (1, 0)
                               else np.lib.format.read_array_header_2_0)
                shape, _, dtype = read_header(stream)
                require(len(shape) <= 2 and all(0 <= size <= max_bytes for size in shape)
                        and dtype.kind in "fiuU" and 0 < dtype.itemsize <= max_bytes
                        and stream.tell() + math.prod(shape) * dtype.itemsize == info.file_size,
                        "invalid NPY dtype, shape or size")
            headers[name[:-4]] = (shape, dtype.kind)
    return headers


def _validate_normalized_episode(record, source):
    headers = _npz_headers(record.data_path)
    topic_shape, kind = headers["measurement_topics"]
    require(len(topic_shape) == 1 and kind == "U", "invalid measurement topics")
    count, = topic_shape
    require(count + 4 == len(headers) and set(headers) == {"state", "action", "frame_times_ns", "measurement_topics",
                                               *(f"measurement_{i}" for i in range(count))},
            "normalized NPZ measurement coverage mismatch")
    for name in ("state", "action"):
        shape, kind = headers[name]
        require(len(shape) == 2 and shape[0] > 0 and shape[1] == PROFILES[source] and kind in "fui",
                "normalized dimensions or dtype mismatch source profile")
    episode = load_episode(record)  # validates aligned rows, integer ns and monotonicity, preserves non-finite input
    require(len(episode.valid_measurement_times_ns) == count, "duplicate measurement topics")


def _load_stats(cls, path, vectors):
    headers = _npz_headers(path)
    require(set(headers) == set(cls.__dataclass_fields__), "invalid statistics NPZ members")
    for name, (shape, kind) in headers.items():
        if name in vectors:
            require(len(shape) == 1 and 0 < shape[0] <= 1024 and kind in "fiu", "invalid statistics vector")
        else:
            require(shape == (1,) and kind in "iu", "invalid statistics integer")
    return cls.load(str(path))


def make_handoff_spec(ctx, name, metadata, output):
    # The normalized-only providers take their root from the restored payload.
    # Keep the virtual name for diagnostics and fingerprints; raw files are absent.
    validate_source_refs(metadata)
    spec = make_spec(ctx.parameters["source"], Path(output).parent, output, ctx.parameters["workers"])
    return replace(spec, input_root=Path(metadata["input_root"]))
