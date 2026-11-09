from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class EpisodeResult:
    episode_index: int
    num_frames: int
    discard: bool = False
    discard_reasons: list[str] = field(default_factory=list)
    step_validity_mask: np.ndarray | None = None  # 1 = keep frame, 0 = drop frame
    first_abnormal_frame: int | None = None
    stage1_flagged_frames: int = 0
    stage2_da_mean: float | None = None
    stage2_da_per_dim: list[float] | None = None
    stage2_lags: list[int | None] | None = None
    stage3_excluded_frames: int = 0
    stage4_removed_frames: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def kept_frames(self) -> int:
        if self.step_validity_mask is None:
            return 0 if self.discard else self.num_frames
        return int(self.step_validity_mask.sum())

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_index": self.episode_index,
            "num_frames": self.num_frames,
            "discard": self.discard,
            "discard_reasons": self.discard_reasons,
            "kept_frames": self.kept_frames,
            "first_abnormal_frame": self.first_abnormal_frame,
            "stage1_flagged_frames": self.stage1_flagged_frames,
            "stage2_da_mean": self.stage2_da_mean,
            "stage2_da_per_dim": self.stage2_da_per_dim,
            "stage2_lags": self.stage2_lags,
            "stage3_excluded_frames": self.stage3_excluded_frames,
            "stage4_removed_frames": self.stage4_removed_frames,
            "metadata": self.metadata,
        }


@dataclass
class GlobalStats:
    state_q01: np.ndarray
    state_q99: np.ndarray
    action_q01: np.ndarray
    action_q99: np.ndarray
    state_min: np.ndarray
    state_max: np.ndarray
    action_min: np.ndarray
    action_max: np.ndarray
    num_frames: int
    num_episodes: int

    def save(self, path: str) -> None:
        np.savez_compressed(
            path,
            state_q01=self.state_q01,
            state_q99=self.state_q99,
            action_q01=self.action_q01,
            action_q99=self.action_q99,
            state_min=self.state_min,
            state_max=self.state_max,
            action_min=self.action_min,
            action_max=self.action_max,
            num_frames=np.array([self.num_frames]),
            num_episodes=np.array([self.num_episodes]),
        )

    @classmethod
    def load(cls, path: str) -> GlobalStats:
        data = np.load(path)
        return cls(
            state_q01=data["state_q01"],
            state_q99=data["state_q99"],
            action_q01=data["action_q01"],
            action_q99=data["action_q99"],
            state_min=data["state_min"],
            state_max=data["state_max"],
            action_min=data["action_min"],
            action_max=data["action_max"],
            num_frames=int(data["num_frames"][0]),
            num_episodes=int(data["num_episodes"][0]),
        )
