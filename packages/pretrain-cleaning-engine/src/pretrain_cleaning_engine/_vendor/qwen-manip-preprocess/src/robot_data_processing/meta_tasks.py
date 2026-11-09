"""Load LeRobot-style meta/episodes.jsonl and meta/tasks.jsonl."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def load_episode_task_meta(
    dataset_root: Path,
) -> tuple[dict[int, list[str]], dict[str, int], dict[int, str]]:
    """Load source task labels.

    Returns
    -------
    episode_tasks
        ``episode_index → list[task_string]`` from ``meta/episodes.jsonl``.
    task_name_to_index
        ``task_string → task_index`` from ``meta/tasks.jsonl``.
    task_index_to_name
        ``task_index → task_string``.
    """
    root = Path(dataset_root)
    episode_tasks: dict[int, list[str]] = {}
    for row in load_jsonl(root / "meta" / "episodes.jsonl"):
        try:
            ep_i = int(row["episode_index"])
        except (KeyError, TypeError, ValueError):
            continue
        tasks = row.get("tasks") or []
        if isinstance(tasks, str):
            tasks = [tasks]
        episode_tasks[ep_i] = [str(t) for t in tasks]

    task_name_to_index: dict[str, int] = {}
    task_index_to_name: dict[int, str] = {}
    for row in load_jsonl(root / "meta" / "tasks.jsonl"):
        try:
            ti = int(row["task_index"])
            name = str(row["task"])
        except (KeyError, TypeError, ValueError):
            continue
        task_index_to_name[ti] = name
        # First wins if duplicate names
        task_name_to_index.setdefault(name, ti)

    return episode_tasks, task_name_to_index, task_index_to_name


def resolve_episode_tasks(
    *,
    episode_index: int,
    episode_tasks: dict[int, list[str]],
    task_index_to_name: dict[int, str],
    fallback_task: str | None = None,
    parquet_task_index: int | None = None,
) -> list[str]:
    """Resolve task strings for one episode (prefer episodes.jsonl)."""
    tasks = list(episode_tasks.get(episode_index) or [])
    if tasks:
        return tasks
    if parquet_task_index is not None and parquet_task_index in task_index_to_name:
        return [task_index_to_name[parquet_task_index]]
    if fallback_task:
        return [str(fallback_task)]
    return []
