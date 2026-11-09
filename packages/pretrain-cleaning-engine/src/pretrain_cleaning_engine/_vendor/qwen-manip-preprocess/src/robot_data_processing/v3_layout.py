"""Helpers for LeRobot pseudo-v3 (one episode per file) and aggregated v3 roots."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from robot_data_processing.normalize.standard_types import EpisodeRef
from robot_data_processing.schema import is_passthrough_column, stack_column

DEFAULT_DATA_PATH = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
DEFAULT_VIDEO_PATH = "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"

# EgoDex info.json can be tens of MB; never re-parse per episode.
_INFO_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}
_IS_V3_CACHE: dict[str, bool] = {}
# Stage B reads Stage-A pseudo_v3: resolve_* without row= used to reload meta every ep.
_EPISODE_ROWS_CACHE: dict[str, tuple[int, int, list[dict[str, Any]]]] = {}
_EPISODE_MAP_CACHE: dict[str, tuple[int, int, dict[int, dict[str, Any]]]] = {}


def _root_key(root: Path) -> str:
    root = Path(root)
    try:
        return str(root.resolve())
    except OSError:
        return str(root)


def _episodes_meta_fingerprint(root: Path) -> tuple[int, int]:
    """mtime/size fingerprint of meta/episodes parquet files."""
    episodes_dir = Path(root) / "meta" / "episodes"
    if not episodes_dir.is_dir():
        return (0, 0)
    max_mtime = 0
    total_size = 0
    for path in episodes_dir.glob("chunk-*/file-*.parquet"):
        try:
            st = path.stat()
        except OSError:
            continue
        max_mtime = max(max_mtime, int(st.st_mtime_ns))
        total_size += int(st.st_size)
    return (max_mtime, total_size)


def load_info(root: Path) -> dict[str, Any]:
    path = Path(root) / "meta" / "info.json"
    key = _root_key(path)
    try:
        st = path.stat()
        fingerprint = (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return json.loads(path.read_text(encoding="utf-8"))
    cached = _INFO_CACHE.get(key)
    if cached is not None and cached[0] == fingerprint[0] and cached[1] == fingerprint[1]:
        return cached[2]
    data = json.loads(path.read_text(encoding="utf-8"))
    _INFO_CACHE[key] = (fingerprint[0], fingerprint[1], data)
    return data


def is_v3_layout(root: Path) -> bool:
    """True when root is LeRobot v3.0 with meta/episodes parquet layout.

    Cached per dataset root: callers (e.g. resolve_source_video) invoke this per
    episode; re-parsing a large info.json each time is prohibitively slow.
    """
    root = Path(root)
    key = _root_key(root)
    cached = _IS_V3_CACHE.get(key)
    if cached is not None:
        return cached
    info_path = root / "meta" / "info.json"
    if not info_path.exists():
        _IS_V3_CACHE[key] = False
        return False
    info = load_info(root)
    ver = str(info.get("codebase_version", ""))
    if ver != "v3.0":
        _IS_V3_CACHE[key] = False
        return False
    episodes_dir = root / "meta" / "episodes"
    ok = episodes_dir.is_dir() and any(episodes_dir.glob("chunk-*/file-*.parquet"))
    _IS_V3_CACHE[key] = ok
    return ok


def load_episode_rows(root: Path) -> list[dict[str, Any]]:
    root = Path(root)
    key = _root_key(root)
    fingerprint = _episodes_meta_fingerprint(root)
    cached = _EPISODE_ROWS_CACHE.get(key)
    if cached is not None and cached[0] == fingerprint[0] and cached[1] == fingerprint[1]:
        return cached[2]
    rows: list[dict[str, Any]] = []
    for path in sorted((root / "meta" / "episodes").glob("chunk-*/file-*.parquet")):
        rows.extend(pq.read_table(path).to_pylist())
    rows.sort(key=lambda r: int(r["episode_index"]))
    _EPISODE_ROWS_CACHE[key] = (fingerprint[0], fingerprint[1], rows)
    return rows


def discover_v3_root(root: Path) -> list[EpisodeRef]:
    root = Path(root)
    rows = load_episode_rows(root)
    return [EpisodeRef(episode_index=int(r["episode_index"]), dataset_root=root) for r in rows]


def episode_row_map(root: Path) -> dict[int, dict[str, Any]]:
    root = Path(root)
    key = _root_key(root)
    fingerprint = _episodes_meta_fingerprint(root)
    cached = _EPISODE_MAP_CACHE.get(key)
    if cached is not None and cached[0] == fingerprint[0] and cached[1] == fingerprint[1]:
        return cached[2]
    mapping = {int(r["episode_index"]): r for r in load_episode_rows(root)}
    _EPISODE_MAP_CACHE[key] = (fingerprint[0], fingerprint[1], mapping)
    return mapping


def resolve_v3_data_path(
    root: Path,
    episode_index: int,
    info: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
) -> Path:
    """Resolve parquet path for one episode.

    Pass ``row`` (from ``load_episode_rows``) when resolving many episodes to avoid
    reloading ``meta/episodes`` on every call (O(N^2) on large roots).
    """
    root = Path(root)
    info = info or load_info(root)
    pattern = str(info.get("data_path") or DEFAULT_DATA_PATH)
    if row is None:
        row = episode_row_map(root).get(int(episode_index))
    if row is None:
        # Fall back to pseudo indexing without meta (rare)
        c, f = int(episode_index) // 1000, int(episode_index) % 1000
        return root / pattern.format(chunk_index=c, file_index=f)
    return root / pattern.format(
        chunk_index=int(row["data/chunk_index"]),
        file_index=int(row["data/file_index"]),
    )


def _normalize_video_key(video_key: str) -> str:
    if video_key.startswith("observation.images."):
        return video_key
    return f"observation.images.{video_key}"


def _video_key_candidates(video_key: str) -> list[str]:
    """LeRobot default keys are observation.images.*; InternData-A1 uses images.rgb.*."""
    key = str(video_key)
    out: list[str] = []
    for cand in (key, _normalize_video_key(key)):
        if cand not in out:
            out.append(cand)
    if key.startswith("observation.images."):
        stripped = key[len("observation.images.") :]
        if stripped and stripped not in out:
            out.append(stripped)
    return out


def _video_row_prefix(row: dict[str, Any], video_key: str) -> str:
    for cand in _video_key_candidates(video_key):
        prefix = f"videos/{cand}"
        if f"{prefix}/chunk_index" in row:
            return prefix
    return f"videos/{_normalize_video_key(video_key)}"


def resolve_v3_video_path(
    root: Path,
    episode_index: int,
    video_key: str,
    info: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
) -> Path:
    root = Path(root)
    info = info or load_info(root)
    pattern = str(info.get("video_path") or DEFAULT_VIDEO_PATH)
    if row is None:
        row = episode_row_map(root).get(int(episode_index))
    if row is None:
        full_key = _normalize_video_key(video_key)
        c, f = int(episode_index) // 1000, int(episode_index) % 1000
        return root / pattern.format(video_key=full_key, chunk_index=c, file_index=f)
    prefix = _video_row_prefix(row, video_key)
    video_dir_key = prefix[len("videos/") :]
    return root / pattern.format(
        video_key=video_dir_key,
        chunk_index=int(row[f"{prefix}/chunk_index"]),
        file_index=int(row[f"{prefix}/file_index"]),
    )


def resolve_v3_video_time_window(
    root: Path,
    episode_index: int,
    video_key: str,
    *,
    row: dict[str, Any] | None = None,
) -> tuple[float, float] | None:
    """Return ``(from_timestamp, to_timestamp)`` for an aggregated v3 video segment.

    LeRobot v3 ``to_timestamp`` is exclusive. Missing / invalid windows return None
    (caller treats the mp4 as a per-episode file).
    """
    root = Path(root)
    if not is_v3_layout(root):
        return None
    if row is None:
        row = episode_row_map(root).get(int(episode_index))
    if row is None:
        return None
    prefix = _video_row_prefix(row, video_key)
    fr = row.get(f"{prefix}/from_timestamp")
    to = row.get(f"{prefix}/to_timestamp")
    if fr is None or to is None:
        return None
    start = float(fr)
    end = float(to)
    if not (end > start):
        return None
    return start, end


def read_standard_fields(path: Path) -> dict[str, np.ndarray]:
    """Read all non-index columns from a standard-schema parquet."""
    table = pq.read_table(path)
    skip = {"timestamp", "frame_index", "episode_index", "index", "task_index"}
    out: dict[str, np.ndarray] = {}
    for name in table.column_names:
        if name in skip:
            continue
        col = table.column(name)
        if hasattr(col, "combine_chunks"):
            col = col.combine_chunks()
        try:
            values = col.to_numpy(zero_copy_only=False)
            if isinstance(values, np.ndarray) and values.dtype == object:
                out[name] = stack_column(values, keep_integer=is_passthrough_column(name))
            else:
                arr = np.asarray(values)
                if arr.ndim == 1 and name.startswith("extrinsic."):
                    out[name] = np.stack(
                        [np.asarray(x, dtype=np.float64).reshape(4, 4) for x in col.to_pylist()],
                        axis=0,
                    )
                else:
                    out[name] = arr
        except Exception:
            vals = col.to_pylist()
            sample = vals[0] if vals else None
            if isinstance(sample, (list, np.ndarray)):
                out[name] = np.stack([np.asarray(x, dtype=np.float64) for x in vals], axis=0)
            else:
                continue
    return out
