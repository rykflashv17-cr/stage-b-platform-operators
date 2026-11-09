"""Typed data passed between the four v4 operators."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


def _source_times(values, *, label: str) -> np.ndarray:
    times = np.asarray(values)
    if times.ndim != 1 or times.dtype.kind not in "iu":
        raise ValueError(f"{label} must be a one-dimensional integer nanosecond array")
    if np.any(times < 0) or np.any(times > np.iinfo(np.int64).max):
        raise ValueError(f"{label} must fit nonnegative int64 source timestamps")
    # Compare before subtracting: unsigned np.diff can hide a backward clock.
    if np.any(times[1:] < times[:-1]):
        raise ValueError(f"{label} must not go backward")
    return times.astype(np.int64, copy=True)


def quality_timeline(timeline: Mapping, num_frames: int) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Validate metadata produced alongside the adapter's final quality rows.

    Missing topic metadata means an unsupported adapter contract; an explicit
    empty integer array means the adapter found no valid measurements.
    """
    if not isinstance(timeline, Mapping) or "frame_times_ns" not in timeline:
        raise ValueError("adapter quality timeline must provide frame_times_ns")
    times = _source_times(timeline["frame_times_ns"], label="frame_times_ns")
    if len(times) != num_frames:
        raise ValueError("one source timestamp is required per final quality row")
    observed = timeline.get("valid_measurement_times_ns", {})
    if not isinstance(observed, Mapping):
        raise ValueError("valid_measurement_times_ns must map topic names to timestamp arrays")
    measurements = {}
    for topic, values in observed.items():
        if not isinstance(topic, str) or not topic:
            raise ValueError("measurement topic names must be nonempty strings")
        measurements[topic] = _source_times(values, label=f"valid_measurement_times_ns[{topic}]")
    return times, measurements


@dataclass(frozen=True)
class SourceIdentity:
    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int

    @classmethod
    def capture(cls, path: Path) -> "SourceIdentity":
        value = Path(path).lstat()
        return cls(*(int(getattr(value, name)) for name in cls.__dataclass_fields__))


@dataclass(frozen=True)
class McapEpisodeRef:
    episode_index: int
    dataset_root: Path
    mcap_path: Path
    rel_path: str
    part: str = ""
    task: str = ""


@dataclass(frozen=True)
class NormalizedEpisode:
    ref: McapEpisodeRef
    state: np.ndarray
    action: np.ndarray
    frame_times_ns: np.ndarray
    source_identity: SourceIdentity | None = None
    valid_measurement_times_ns: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        state, action = np.asarray(self.state, dtype=np.float64), np.asarray(self.action, dtype=np.float64)
        if state.ndim != 2 or action.ndim != 2 or state.shape[0] != action.shape[0]:
            raise ValueError("state/action must be aligned two-dimensional arrays")
        times, measurements = quality_timeline({
            "frame_times_ns": self.frame_times_ns,
            "valid_measurement_times_ns": self.valid_measurement_times_ns,
        }, len(state))
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "frame_times_ns", times)
        object.__setattr__(self, "valid_measurement_times_ns", measurements)

    @property
    def episode_index(self) -> int:
        return self.ref.episode_index

    @property
    def num_frames(self) -> int:
        return len(self.state)


@dataclass(frozen=True)
class StatisticsBundle:
    global_stats: Any
    stage1_stats: Any
    alignment_stats: Any
    alignment_lag: int
    cache_dir: Path


# Import compatibility only; the normalized contract no longer contains
# stage-specific masks. Construct those inside the quality operator.
QualityEpisode = NormalizedEpisode


@dataclass(frozen=True)
class NormalizedRecord:
    ref: McapEpisodeRef
    data_path: Path | None
    source_identity: SourceIdentity | None = None
    input_error: str | None = None
    deletion_blocked_reason: str | None = None


@dataclass(frozen=True)
class NormalizedBatchRef:
    manifest_path: Path
    records: tuple[NormalizedRecord, ...]


@dataclass(frozen=True)
class StatisticsContext:
    batch: NormalizedBatchRef
    status: str  # ready, empty, failed
    statistics: StatisticsBundle | None
    fit_records: tuple[dict[str, Any], ...]
    coverage_policy: tuple[str, str, int] | None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"ready", "empty", "failed"}:
            raise ValueError("invalid statistics context status")
        if (self.statistics is not None) != (self.status == "ready"):
            raise ValueError("only a ready context may contain fitted statistics")


@dataclass(frozen=True)
class BatchQualityResult:
    summary: dict[str, Any]
    exit_code: int
    manifest_path: Path


@dataclass(frozen=True)
class BatchDeliveryResult:
    summary: dict[str, Any]
    exit_code: int


def frame_ranges(mask: np.ndarray, times: np.ndarray) -> list[dict[str, int]]:
    """Diagnostic frame ranges; a rejected file is always removed in full."""
    mask = np.asarray(mask, dtype=bool)
    if mask.shape != times.shape or mask.ndim != 1:
        raise ValueError("range mask must match the source timeline")
    if not len(mask):
        return []
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    terminal_dt = max(0, int(times[-1] - times[-2])) if len(times) > 1 else 0
    return [{
        "start_frame": int(start), "end_frame_exclusive": int(end),
        "start_time_ns": int(times[start]),
        "end_time_ns": int(times[end]) if end < len(times) else int(times[-1]) + terminal_dt,
    } for start, end in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))]


def _plain(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return _plain(value.tolist())
    if isinstance(value, np.generic):
        return _plain(value.item())
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


@dataclass(frozen=True)
class QualityDecision:
    ref: McapEpisodeRef
    frame_times_ns: np.ndarray
    bad_mask: np.ndarray
    reject_file: bool
    reasons: tuple[str, ...]
    stage_results: dict[str, dict[str, Any]]
    source_identity: SourceIdentity | None = None
    diagnostic_errors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        mask = np.asarray(self.bad_mask, dtype=bool)
        if mask.ndim != 1 or mask.shape != self.frame_times_ns.shape:
            raise ValueError("bad_mask must match the source timeline")
        if mask.any() and not self.reject_file:
            raise ValueError("v4 must reject the entire MCAP when any frame is bad")
        object.__setattr__(self, "bad_mask", mask)

    @property
    def num_frames(self) -> int:
        return len(self.frame_times_ns)

    def to_record(self) -> dict[str, Any]:
        detected = []
        for name, result in self.stage_results.items():
            for interval in result.get("detected_ranges", []):
                detected.append({"stage": name, **interval})
        record = {
            "episode_index": self.ref.episode_index,
            "source": str(self.ref.mcap_path),
            "status": "rejected" if self.reject_file else "error" if self.diagnostic_errors else "kept",
            "reject_file": bool(self.reject_file),
            "reasons": list(self.reasons),
            "num_frames": self.num_frames,
            "detected_bad_frames": int(np.count_nonzero(self.bad_mask)),
            "detected_ranges": detected,
            "effective_removed_ranges": frame_ranges(
                np.ones(self.num_frames, dtype=bool), self.frame_times_ns,
            ) if self.reject_file else [],
            "stage_results": self.stage_results,
            "source_identity": asdict(self.source_identity) if self.source_identity else None,
            "diagnostic_errors": self.diagnostic_errors,
        }
        if self.diagnostic_errors:
            record.update({"error": "; ".join(f"{name}: {error}" for name, error in self.diagnostic_errors.items()),
                           "retained_due_to_error": True})
        return _plain(record)
