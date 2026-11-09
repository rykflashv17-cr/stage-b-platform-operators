"""Shared types for Dataclean standard pipeline (P0–P7)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np


@dataclass
class EpisodeRef:
    """Unified episode pointer across single-root and part/task layouts."""

    episode_index: int
    dataset_root: Path
    part: str | None = None
    task: str | None = None
    rel_path: str | None = None  # e.g. part1/add_remove_lid

    @property
    def key(self) -> str:
        if self.rel_path:
            return f"{self.rel_path}:{self.episode_index:06d}"
        return f"{self.episode_index:06d}"


@dataclass
class StandardEpisode:
    """Episode in Dataclean standard field space (processing.md / standard_info.json)."""

    episode_index: int
    num_frames: int
    fields: dict[str, np.ndarray] = field(default_factory=dict)  # name -> (T, D)
    camera_keys: list[str] = field(default_factory=list)  # observation.images.*
    intrinsic: dict[str, Any] = field(default_factory=dict)
    extrinsic: dict[str, np.ndarray] = field(default_factory=dict)  # name -> (4,4) or (T,4,4)
    meta: dict[str, Any] = field(default_factory=dict)
    has_camera_top: bool = False

    def require_camera_top(self) -> bool:
        return self.has_camera_top or any(
            k.endswith("camera_top") or "camera_top" in k for k in self.camera_keys
        )


class DatasetAdapter(Protocol):
    """Per-dataset mapping between source and Dataclean / quality spaces."""

    def to_standard_episode(
        self, ref: EpisodeRef, raw: dict[str, np.ndarray], meta: dict[str, Any]
    ) -> StandardEpisode: ...

    def to_quality_arrays(self, standard: StandardEpisode) -> tuple[np.ndarray, np.ndarray]:
        """Return (state, action) for Stage1–5."""
        ...

    def camera_name_map(self) -> dict[str, str]:
        """Source video key → standard camera key (without observation.images. prefix)."""
        ...


@dataclass
class EpisodeGeometryResult:
    """Output of P6 episode-frame geometry transform (v2 contract)."""

    fields: dict[str, np.ndarray]
    extrinsic: dict[str, np.ndarray]  # name -> (T,4,4) or (4,4)
    camera_keys: list[str]
    camera_mapping: dict[str, str]
    camera_intrinsics: dict[str, Any]
    video_export_map: dict[str, str]  # source observation.images.* -> output short name
    episode_frame_definition: str
    geometry_meta: dict[str, Any] = field(default_factory=dict)
    wrist_view_cameras: list[str] = field(default_factory=list)
    info_extras: dict[str, Any] = field(default_factory=dict)
    discard: bool = False
    discard_reasons: list[str] = field(default_factory=list)


class EpisodeGeometry(Protocol):
    """Per-dataset episode-frame geometry module (datasets/*/episode_geometry.py)."""

    def apply_episode_frame_geometry(
        self,
        standard: StandardEpisode,
        *,
        ref: EpisodeRef,
        geometry_cfg: dict[str, Any],
        keymap: dict[str, Any],
        stored_video_size: tuple[int, int] | None = None,
    ) -> EpisodeGeometryResult: ...
