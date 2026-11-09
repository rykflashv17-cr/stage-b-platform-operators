from __future__ import annotations

import json
from pathlib import Path

CALIBRATION_BUNDLE = "calibration_bundle_optimized.json"


def _indices_from_mapping(data: dict) -> set[int] | None:
    """Extract episode indices from common ignore / outlier JSON shapes."""
    if "episode_indices" in data:
        return {int(x) for x in data["episode_indices"]}
    if "episodes" in data:
        return {int(x) for x in data["episodes"]}
    if "flagged_episodes" in data:
        return {int(x) for x in data["flagged_episodes"]}

    analysis = data.get("analysis")
    if isinstance(analysis, dict) and "flagged_episodes" in analysis:
        return {int(x) for x in analysis["flagged_episodes"]}

    outliers = data.get("outliers")
    if isinstance(outliers, list) and outliers:
        if all(isinstance(x, int) for x in outliers):
            return {int(x) for x in outliers}
        if all(isinstance(x, dict) for x in outliers):
            out: set[int] = set()
            for row in outliers:
                if "episode_index" in row:
                    out.add(int(row["episode_index"]))
            if out:
                return out
    return None


def load_ignore_episode_list(path: Path | str | None) -> set[int]:
    if path is None:
        return set()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Ignore list not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        if not data:
            return set()
        if all(isinstance(x, int) for x in data):
            return {int(x) for x in data}
        if all(isinstance(x, dict) and "episode_index" in x for x in data):
            return {int(x["episode_index"]) for x in data}
        raise ValueError(f"Unsupported ignore list array format: {path}")
    if isinstance(data, dict):
        parsed = _indices_from_mapping(data)
        if parsed is not None:
            return parsed
    raise ValueError(f"Unsupported ignore list format: {path}")


def load_ignore_episode_lists(paths: list[Path | str | None] | None) -> set[int]:
    """Union episode indices from one or more ignore / outlier JSON files."""
    out: set[int] = set()
    for path in paths or []:
        if path is None:
            continue
        out |= load_ignore_episode_list(path)
    return out


def filter_episode_indices(indices: list[int], ignore: set[int]) -> list[int]:
    if not ignore:
        return list(indices)
    return [idx for idx in indices if idx not in ignore]


def scan_missing_calibration_episodes(dataset_root: Path, total_episodes: int) -> list[int]:
    """Episodes without parameters/.../calibration_bundle_optimized.json."""
    root = Path(dataset_root) / "parameters"
    missing: list[int] = []
    for ep in range(total_episodes):
        chunk = ep // 1000
        cal = root / f"chunk-{chunk:03d}" / f"episode_{ep:06d}" / CALIBRATION_BUNDLE
        if not cal.is_file():
            missing.append(ep)
    return missing


def write_ignore_episode_list(
    path: Path,
    episode_indices: list[int],
    *,
    reason: str,
    dataset_root: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": reason,
        "count": len(episode_indices),
        "dataset_root": dataset_root,
        "episode_indices": sorted(int(x) for x in episode_indices),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_quarantine_entries(paths: list[Path | str | None] | None) -> list[dict]:
    """Whole-episode quarantine lists (REPORT INT-03 / AGI-05 / EGO-04).

    Each file is JSON ``{"index_space": ..., "episodes": [{"episode_index": int,
    "reason": str, "dataset_root"?: str, "rel_path"?: str, ...}]}``. ``index_space``
    names the episode indices the list uses (e.g. ``stage_a_episode_index`` for a
    Stage-B run whose input is the Stage-A aggregate). An entry may give only
    ``rel_path`` (no ``episode_index``): it then matches that source file whatever
    index the discovery assigns. Entries keep every provenance field so the export
    can publish why an episode was removed.
    """
    entries: list[dict] = []
    for path in paths or []:
        if path is None:
            continue
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Quarantine list not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("episodes") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError(f"Unsupported quarantine list format: {path}")
        space = data.get("index_space") if isinstance(data, dict) else None
        for row in rows:
            if isinstance(row, int):
                row = {"episode_index": row}
            if not isinstance(row, dict) or ("episode_index" not in row and not row.get("rel_path")):
                raise ValueError(f"Quarantine entry needs episode_index or rel_path: {row!r} in {path}")
            if not row.get("reason") and not (isinstance(data, dict) and data.get("reason")):
                raise ValueError(f"Quarantine entry needs a reason: {row!r} in {path}")
            entry = dict(row)
            if entry.get("episode_index") is not None:
                entry["episode_index"] = int(entry["episode_index"])
            entry.setdefault("reason", data.get("reason") if isinstance(data, dict) else None)
            entry.setdefault("index_space", space)
            entry["list"] = str(path)
            entries.append(entry)
    return entries


def quarantine_match(ref, entries: list[dict]) -> dict | None:
    """Entry matching ``ref`` (episode index and/or leaf root / rel_path)."""
    for entry in entries:
        index = entry.get("episode_index")
        if index is not None and int(index) != int(ref.episode_index):
            continue
        root = entry.get("dataset_root")
        if root and not str(ref.dataset_root).rstrip("/").endswith(str(root).rstrip("/")):
            continue
        rel = entry.get("rel_path")
        if rel and str(getattr(ref, "rel_path", None) or "") != str(rel):
            continue
        return entry
    return None
