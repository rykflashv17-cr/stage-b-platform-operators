"""Drop reviewed episodes at merge time (``merge_shards.py --exclude-list``).

A published Stage-B root is never edited in place: the shards are merged again into a
new version directory with the listed episodes removed, so renumbering, the data
parquet ``episode_index`` / ``index`` columns, frame_removal, source map and all
statistics are rebuilt from the filtered episode set.

List format is the quarantine list (``ignore_list.load_quarantine_entries``) with
``index_space: stage_a_episode_index``; ``episode_index`` is the shard source map's
``source_episode_index``. Datasets whose source ids restart per leaf (InternData)
match on ``(dataset_root, source_episode_index)`` and every entry must carry
``dataset_root`` (a suffix of the source map's root is enough).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from robot_data_processing.ignore_list import load_quarantine_entries

MERGE_EXCLUDE_RULE = "merge_exclude"
MERGE_EXCLUDE_RULE_TEXT = (
    "removed at merge by a reviewed exclusion list; see meta/episode_discard.json (list, reason)"
)
MERGE_EXCLUDE_INDEX_SPACE = "stage_a_episode_index"


def _root_key(dataset_root: Any) -> str:
    return str(dataset_root or "").rstrip("/")


def load_merge_exclude_entries(paths: list[Path | str] | None) -> list[dict[str, Any]]:
    entries = load_quarantine_entries(list(paths or []))
    no_index = [e for e in entries if e.get("episode_index") is None]
    if no_index:
        raise ValueError(f"merge exclusion entries need episode_index (rel_path-only entries are for discovery): {no_index[:3]}")
    for entry in entries:
        space = entry.get("index_space")
        if space != MERGE_EXCLUDE_INDEX_SPACE:
            raise ValueError(
                f"{entry['list']}: index_space={space!r}, merge exclusion needs "
                f"{MERGE_EXCLUDE_INDEX_SPACE!r} (source_episode_index of the shard source map)"
            )
    return entries


def check_exclude_output_dir(
    output_dir: Path,
    shard_dirs: list[Path],
    entries: list[dict[str, Any]],
    *,
    overwrite: bool,
) -> None:
    """Refuse to write an exclusion merge over a published root or a shard."""
    out = Path(output_dir).resolve()
    protected = {Path(p).resolve() for p in shard_dirs}
    protected |= {Path(e["stage_b_root"]).resolve() for e in entries if e.get("stage_b_root")}
    for p in protected:
        if out == p or p in out.parents or out in p.parents:
            raise ValueError(f"--output-dir {out} overlaps {p}; write the exclusion merge to a new version directory")
    if (out / "meta" / "info.json").exists() and not overwrite:
        raise ValueError(
            f"--output-dir {out} already holds a dataset (meta/info.json); "
            "use a new version directory, or --overwrite for a previous exclusion merge"
        )


def apply_merge_exclude(
    rows: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    *,
    allow_missing: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split collected merge rows into (kept, excluded records, missing entries).

    Every entry must hit exactly one row; misses raise unless ``allow_missing``.
    """
    if not entries:
        return rows, [], []
    ids_unique = len({int(r["source_episode_index"]) for r in rows}) == len(rows)
    by_id: dict[int, list[int]] = {}
    for i, row in enumerate(rows):
        by_id.setdefault(int(row["source_episode_index"]), []).append(i)

    hit: dict[int, dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []
    for entry in entries:
        src = int(entry["episode_index"])
        if entry.get("dataset_root"):
            # suffix match, as quarantine_match and utils/exclude_episodes.py do
            want = _root_key(entry["dataset_root"])
            idx = [i for i in by_id.get(src, []) if _root_key(rows[i].get("dataset_root")).endswith(want)]
        elif not ids_unique:
            raise ValueError(
                f"{entry['list']}: source ids restart per leaf in these shards; "
                f"entry episode_index={src} needs dataset_root"
            )
        else:
            idx = by_id.get(src, [])
        if len(idx) > 1:
            raise ValueError(f"{entry['list']}: episode_index={src} matches {len(idx)} episodes")
        if not idx:
            missing.append(entry)
            continue
        if idx[0] in hit:
            raise ValueError(f"{entry['list']}: episode_index={src} is listed more than once")
        hit[idx[0]] = entry
    if missing and not allow_missing:
        ids = [(e.get("dataset_root"), int(e["episode_index"])) if e.get("dataset_root") else int(e["episode_index"]) for e in missing]
        raise ValueError(
            f"{len(missing)} exclusion entries match no collected episode: {ids} "
            "(wrong index space, or already dropped by cleaning; --exclude-allow-missing to accept)"
        )

    kept = [row for i, row in enumerate(rows) if i not in hit]
    records: list[dict[str, Any]] = []
    for i in sorted(hit):
        entry = hit[i]
        rec: dict[str, Any] = {
            "source_episode_index": int(rows[i]["source_episode_index"]),
            "rule": MERGE_EXCLUDE_RULE,
            "reasons": [str(entry["reason"])],
            "list": str(entry["list"]),
        }
        if entry.get("dataset_root"):
            rec["dataset_root"] = str(rows[i].get("dataset_root") or entry["dataset_root"])
        if entry.get("stage_b_episode_index") is not None:
            rec["stage_b_episode_index"] = int(entry["stage_b_episode_index"])
        records.append(rec)
    print(
        f"[merge] exclude: configured={len(entries)} removed={len(records)} "
        f"missing={len(missing)} kept={len(kept)}",
        flush=True,
    )
    return kept, records, missing


def declare_merge_exclude(
    info: dict[str, Any],
    discarded: list[dict[str, Any]] | None,
    records: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Append exclusion records to the merged ``episode_discard`` and declare the rule.

    With no shard-level declaration the records only reach ``lerobot_v30_export.json``.
    """
    if not records:
        return discarded
    if discarded is None or not isinstance(info.get("episode_discard"), dict):
        print(
            f"[merge] WARN episode_discard not declared; {len(records)} merge_exclude records "
            "are only in lerobot_v30_export.json",
            flush=True,
        )
        return discarded
    decl = info["episode_discard"]
    decl["rules"] = {**(decl.get("rules") or {}), MERGE_EXCLUDE_RULE: MERGE_EXCLUDE_RULE_TEXT}
    return [*discarded, *records]


def exclude_summary(
    paths: list[Path | str],
    entries: list[dict[str, Any]],
    records: list[dict[str, Any]],
    missing: list[dict[str, Any]],
) -> dict[str, Any]:
    """``lerobot_v30_export.json::exclude`` (records kept here in case episode_discard is not declared)."""
    return {
        "lists": [os.fspath(p) for p in paths],
        "configured": len(entries),
        "removed": len(records),
        "missing": [
            {k: e.get(k) for k in ("episode_index", "dataset_root", "reason", "list") if e.get(k) is not None}
            for e in missing
        ],
        "episodes": records,
    }
