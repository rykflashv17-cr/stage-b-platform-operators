"""P0 Ingest: discover episodes under single-root or part/task layouts."""
from __future__ import annotations

import json
from pathlib import Path

from robot_data_processing.loader import list_episode_indices
from robot_data_processing.normalize.standard_types import EpisodeRef


def discover_single_root(root: Path, total_episodes: int | None = None) -> list[EpisodeRef]:
    root = Path(root)
    from robot_data_processing.v3_layout import discover_v3_root, is_v3_layout

    if is_v3_layout(root):
        return discover_v3_root(root)
    info_path = root / "meta" / "info.json"
    if total_episodes is None and info_path.exists():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        total_episodes = int(info.get("total_episodes", 0)) or None
    indices = list_episode_indices(root, total_episodes)
    return [EpisodeRef(episode_index=i, dataset_root=root) for i in indices]


def discover_part_task(root: Path) -> list[EpisodeRef]:
    """EgoDex-style: root/{part}/{task}/meta/info.json."""
    root = Path(root)
    refs: list[EpisodeRef] = []
    for part in sorted(p for p in root.iterdir() if p.is_dir()):
        for task_dir in sorted(p for p in part.iterdir() if p.is_dir()):
            info_path = task_dir / "meta" / "info.json"
            if not info_path.exists():
                continue
            info = json.loads(info_path.read_text(encoding="utf-8"))
            total = int(info.get("total_episodes", 0))
            rel = f"{part.name}/{task_dir.name}"
            for ep in range(total):
                refs.append(
                    EpisodeRef(
                        episode_index=ep,
                        dataset_root=task_dir,
                        part=part.name,
                        task=task_dir.name,
                        rel_path=rel,
                    )
                )
    return refs
