"""Enumerate ABC-130k MCAP+EEF episodes under data/{split}/{task}/episode_*.

Only directories that contain both ``episode.mcap`` and ``eef_pose.parquet``
(ABC-130k-EEF tree) are included.
"""
from __future__ import annotations

from pathlib import Path

from robot_data_processing.normalize.standard_types import EpisodeRef

from mcap_io import load_or_build_index


def discover(
    dataset_root: Path,
    layout: str = "mcap",
    *,
    splits: tuple[str, ...] | None = None,
    total_episodes: int | None = None,
    index_cache: Path | None = None,
    rebuild_index: bool = False,
    tasks: object = None,
    passed_traj_csv: object = None,
    **kwargs,
) -> list[EpisodeRef]:
    """Return EpisodeRefs with stable indices (sorted by split/task/episode_id)."""
    del layout, tasks, passed_traj_csv, kwargs
    root = Path(dataset_root)
    split_tuple = splits or ("train",)
    cache = Path(index_cache) if index_cache is not None else None
    if cache is not None and not cache.is_absolute():
        cache = Path(__file__).resolve().parent / cache
    infos = load_or_build_index(
        root,
        splits=split_tuple,
        cache_path=cache,
        rebuild=rebuild_index,
    )
    if total_episodes is not None:
        infos = infos[: int(total_episodes)]
    refs: list[EpisodeRef] = []
    for i, info in enumerate(infos):
        refs.append(
            EpisodeRef(
                episode_index=i,
                dataset_root=root,
                part=info.split,
                task=info.task,
                rel_path=info.rel_path,
            )
        )
    return refs
