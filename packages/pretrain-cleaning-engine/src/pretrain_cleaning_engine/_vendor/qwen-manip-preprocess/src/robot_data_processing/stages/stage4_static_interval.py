from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Stage4Config:
    max_static_duration_sec: float = 1.0
    fps: float = 30.0
    max_static_steps: int | None = None  # explicit override; else derived from duration * fps
    enabled: bool = True
    change_epsilon: float = 0.0

    @property
    def resolved_max_static_steps(self) -> int:
        if self.max_static_steps is not None:
            return max(1, int(self.max_static_steps))
        return max(1, int(round(float(self.fps) * float(self.max_static_duration_sec))))


@dataclass
class Stage4Result:
    remove_frames: np.ndarray  # True = drop this frame after shortening static runs
    removed_count: int
    static_runs_shortened: int
    static_frames_removed: int


def _vectors_equal(a: np.ndarray, b: np.ndarray, epsilon: float) -> bool:
    if epsilon <= 0:
        return np.array_equal(a, b)
    return bool(np.all(np.abs(a - b) <= epsilon))


def detect_static_interval_removals(
    state: np.ndarray,
    action: np.ndarray,
    max_static_steps: int = 5,
    change_epsilon: float = 0.0,
) -> Stage4Result:
    """Mark frames to remove when a static run (unchanged state & action) exceeds max_static_steps.

    Each maximal static run keeps the first ``max_static_steps`` frames; the rest are removed.
    """
    num_frames = state.shape[0]
    remove = np.zeros(num_frames, dtype=bool)
    if num_frames <= 1 or max_static_steps < 1:
        return Stage4Result(remove, 0, 0, 0)

    runs_shortened = 0
    frames_removed = 0
    t = 0
    while t < num_frames:
        end = t
        while end + 1 < num_frames and _vectors_equal(state[end], state[end + 1], change_epsilon) and _vectors_equal(
            action[end], action[end + 1], change_epsilon
        ):
            end += 1
        run_len = end - t + 1
        if run_len > max_static_steps:
            remove[t + max_static_steps : end + 1] = True
            runs_shortened += 1
            frames_removed += run_len - max_static_steps
        t = end + 1

    return Stage4Result(
        remove_frames=remove,
        removed_count=int(remove.sum()),
        static_runs_shortened=runs_shortened,
        static_frames_removed=frames_removed,
    )


def run_stage4(
    state: np.ndarray,
    action: np.ndarray,
    cfg: Stage4Config,
) -> Stage4Result:
    if not cfg.enabled:
        return Stage4Result(
            remove_frames=np.zeros(state.shape[0], dtype=bool),
            removed_count=0,
            static_runs_shortened=0,
            static_frames_removed=0,
        )
    return detect_static_interval_removals(
        state,
        action,
        max_static_steps=cfg.resolved_max_static_steps,
        change_epsilon=cfg.change_epsilon,
    )
