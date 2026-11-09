"""Per-hand, anchor-local EEF action statistics for LeRobot v3 datasets.

Stored state/action poses remain absolute in the shared episode frame. This
module publishes action moments only: each lead is expressed in that hand's
own state frame at the chunk anchor. Camera poses and joint deltas are not
included. Gripper closedness is optional and omitted when the feature is
absent (never zero-filled).

Ego datasets that exported ``action.ego.*`` also accumulate camera-local
relative ego-action moments. Teleop / humanoid datasets omit that extension.

Call ``prepare_dataset_statistics`` to also write independent EEF state
statistics to ``meta/statistics_state.json``.
"""
from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from robot_data_processing.ego_action import (
    EGO_EMBODIMENTS,
    EGO_POSITION_KEY,
    EGO_ROTATION_KEY,
    ego_action_metadata,
    is_ego_embodiment,
    reference_camera_ego_values,
)

STATISTICS_FILE = "statistics_relative.json"
SCHEMA = "episode_local_eef_action_statistics"
SCHEMA_VERSION = SCHEMA  # backward-compatible alias
DEFAULT_STATISTICS_HORIZON_SECONDS = 2.0
REFERENCE_TRANSFORM_KEY = "extrinsic.camera_reference.T_Episode_CameraReference"


def _default_stats_workers() -> int:
    env = os.environ.get("RELATIVE_STATS_WORKERS", "").strip()
    if env:
        return max(1, int(env))
    return max(1, min(32, (os.cpu_count() or 8)))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return payload


def _atomic_json_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, indent=2, sort_keys=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _column_as_numpy(column: pa.ChunkedArray) -> np.ndarray:
    values = column.combine_chunks()
    shape = [len(values)]
    while pa.types.is_fixed_size_list(values.type):
        shape.append(values.type.list_size)
        values = values.values
    if pa.types.is_floating(values.type) or pa.types.is_integer(values.type):
        output = np.asarray(values.to_numpy(zero_copy_only=False)).reshape(shape)
    else:
        output = np.asarray(column.to_pylist())
    if not np.isfinite(output).all():
        raise ValueError("Statistics input columns must contain only finite values.")
    return output


def _welford_merge_arrays(
    count_a: np.ndarray,
    mean_a: np.ndarray,
    m2_a: np.ndarray,
    min_a: np.ndarray,
    max_a: np.ndarray,
    count_b: np.ndarray,
    mean_b: np.ndarray,
    m2_b: np.ndarray,
    min_b: np.ndarray,
    max_b: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Element-wise parallel Welford merge of two moment accumulators."""
    na = count_a.astype(np.float64)
    nb = count_b.astype(np.float64)
    total = na + nb
    only_b = (na == 0) & (nb > 0)
    both = (na > 0) & (nb > 0)
    mean = mean_a.copy()
    m2 = m2_a.copy()
    if np.any(only_b):
        mean = np.where(only_b, mean_b, mean)
        m2 = np.where(only_b, m2_b, m2)
    if np.any(both):
        delta = mean_b - mean_a
        mean_both = mean_a + delta * nb / total
        m2_both = m2_a + m2_b + delta**2 * na * nb / total
        mean = np.where(both, mean_both, mean)
        m2 = np.where(both, m2_both, m2)
    return (
        total.astype(np.int64),
        mean,
        m2,
        np.minimum(min_a, min_b),
        np.maximum(max_a, max_b),
    )


class _LeadMoments:
    def __init__(self, horizon: int, dimension: int) -> None:
        shape = (horizon, dimension)
        self.count = np.zeros(shape, dtype=np.int64)
        self.mean = np.zeros(shape, dtype=np.float64)
        self.m2 = np.zeros(shape, dtype=np.float64)
        self.minimum = np.full(shape, np.inf, dtype=np.float64)
        self.maximum = np.full(shape, -np.inf, dtype=np.float64)

    def update(self, lead: int, values: np.ndarray) -> None:
        values = np.asarray(values, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != self.mean.shape[1]:
            raise ValueError(f"Unexpected statistics batch shape {values.shape}.")
        if len(values) == 0:
            return
        batch_count = len(values)
        batch_mean = values.mean(axis=0)
        batch_m2 = ((values - batch_mean) ** 2).sum(axis=0)
        old_count = self.count[lead].astype(np.float64)
        total_count = old_count + batch_count
        delta = batch_mean - self.mean[lead]
        self.mean[lead] += delta * batch_count / total_count
        self.m2[lead] += batch_m2 + delta**2 * old_count * batch_count / total_count
        self.count[lead] = total_count.astype(np.int64)
        self.minimum[lead] = np.minimum(self.minimum[lead], values.min(axis=0))
        self.maximum[lead] = np.maximum(self.maximum[lead], values.max(axis=0))

    def merge_from(self, other: "_LeadMoments") -> None:
        if other.mean.shape != self.mean.shape:
            raise ValueError(
                f"LeadMoments shape mismatch {other.mean.shape} vs {self.mean.shape}"
            )
        self.count, self.mean, self.m2, self.minimum, self.maximum = _welford_merge_arrays(
            self.count,
            self.mean,
            self.m2,
            self.minimum,
            self.maximum,
            other.count,
            other.mean,
            other.m2,
            other.minimum,
            other.maximum,
        )

    def to_state(self) -> dict[str, np.ndarray]:
        return {
            "count": self.count,
            "mean": self.mean,
            "m2": self.m2,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }

    @classmethod
    def from_state(cls, state: dict[str, np.ndarray]) -> "_LeadMoments":
        mean = np.asarray(state["mean"], dtype=np.float64)
        obj = cls(mean.shape[0], mean.shape[1])
        obj.count = np.asarray(state["count"], dtype=np.int64)
        obj.mean = mean
        obj.m2 = np.asarray(state["m2"], dtype=np.float64)
        obj.minimum = np.asarray(state["minimum"], dtype=np.float64)
        obj.maximum = np.asarray(state["maximum"], dtype=np.float64)
        return obj

    def to_json(self) -> dict[str, list]:
        if np.any(self.count == 0):
            raise ValueError("Every relative-action lead and dimension must have samples.")
        return {
            "count": self.count.tolist(),
            "mean": self.mean.tolist(),
            "m2": self.m2.tolist(),
            "min": self.minimum.tolist(),
            "max": self.maximum.tolist(),
        }


class _Moments:
    def __init__(self, dimension: int) -> None:
        self.count = np.zeros(dimension, dtype=np.int64)
        self.mean = np.zeros(dimension, dtype=np.float64)
        self.m2 = np.zeros(dimension, dtype=np.float64)
        self.minimum = np.full(dimension, np.inf, dtype=np.float64)
        self.maximum = np.full(dimension, -np.inf, dtype=np.float64)

    def update(self, values: np.ndarray) -> None:
        values = np.asarray(values, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != len(self.mean):
            raise ValueError(f"Unexpected state statistics batch shape {values.shape}.")
        if len(values) == 0:
            return
        batch_count = len(values)
        batch_mean = values.mean(axis=0)
        batch_m2 = ((values - batch_mean) ** 2).sum(axis=0)
        old_count = self.count.astype(np.float64)
        total_count = old_count + batch_count
        delta = batch_mean - self.mean
        self.mean += delta * batch_count / total_count
        self.m2 += batch_m2 + delta**2 * old_count * batch_count / total_count
        self.count = total_count.astype(np.int64)
        self.minimum = np.minimum(self.minimum, values.min(axis=0))
        self.maximum = np.maximum(self.maximum, values.max(axis=0))

    def merge_from(self, other: "_Moments") -> None:
        if other.mean.shape != self.mean.shape:
            raise ValueError(
                f"Moments shape mismatch {other.mean.shape} vs {self.mean.shape}"
            )
        self.count, self.mean, self.m2, self.minimum, self.maximum = _welford_merge_arrays(
            self.count,
            self.mean,
            self.m2,
            self.minimum,
            self.maximum,
            other.count,
            other.mean,
            other.m2,
            other.minimum,
            other.maximum,
        )

    def to_state(self) -> dict[str, np.ndarray]:
        return {
            "count": self.count,
            "mean": self.mean,
            "m2": self.m2,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }

    @classmethod
    def from_state(cls, state: dict[str, np.ndarray]) -> "_Moments":
        mean = np.asarray(state["mean"], dtype=np.float64)
        obj = cls(int(mean.shape[0]))
        obj.count = np.asarray(state["count"], dtype=np.int64)
        obj.mean = mean
        obj.m2 = np.asarray(state["m2"], dtype=np.float64)
        obj.minimum = np.asarray(state["minimum"], dtype=np.float64)
        obj.maximum = np.asarray(state["maximum"], dtype=np.float64)
        return obj

    def to_json(self) -> dict[str, list]:
        if np.any(self.count == 0):
            raise ValueError("Every state dimension must have samples.")
        return {
            "count": self.count.tolist(),
            "mean": self.mean.tolist(),
            "m2": self.m2.tolist(),
            "min": self.minimum.tolist(),
            "max": self.maximum.tolist(),
        }


def _rotation_6d_to_matrices(values: np.ndarray, *, name: str) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 6 or not np.isfinite(values).all():
        raise ValueError(f"{name} must have finite shape [N,6], got {values.shape}.")
    rows = values.reshape(-1, 2, 3)
    first = rows[:, 0]
    first_norm = np.linalg.norm(first, axis=1, keepdims=True)
    if np.any(first_norm <= 1e-8):
        raise ValueError(f"{name} contains a zero first row.")
    first = first / first_norm
    second = rows[:, 1] - np.sum(first * rows[:, 1], axis=1, keepdims=True) * first
    second_norm = np.linalg.norm(second, axis=1, keepdims=True)
    if np.any(second_norm <= 1e-8):
        raise ValueError(f"{name} contains collinear Rotation 6D rows.")
    second = second / second_norm
    matrices = np.stack([first, second, np.cross(first, second)], axis=1)
    if not np.allclose(matrices[:, :2], rows, atol=1e-4, rtol=0):
        raise ValueError(f"{name} is not an unnormalized raw Rotation 6D field.")
    return matrices


def _matrices_to_rotation_6d(matrices: np.ndarray) -> np.ndarray:
    return np.asarray(matrices, dtype=np.float64)[:, :2, :].reshape(-1, 6)


def _require_shared_frame_geometry(info: dict[str, Any]) -> dict[str, Any]:
    if info.get("codebase_version") != "v3.0":
        raise ValueError("Local EEF statistics require LeRobot v3.0 prepared data.")
    geometry = info.get("pose_geometry") or info.get("camera_geometry") or {}
    if (
        not geometry.get("episode_frame")
        or geometry.get("translation_unit") != "meter"
        or geometry.get("eef_position_format") != "absolute xyz in meters"
        or geometry.get("eef_rotation_format")
        != "absolute Rotation 6D, first two matrix rows flattened"
    ):
        raise ValueError(
            "Declare shared episode-frame absolute EEF poses in meters and Rotation-6D."
        )
    return geometry


def _arm_roles(
    info: dict[str, Any], *, require_camera: bool = False, require_gripper: bool = False
) -> list[str]:
    features = info.get("features")
    if not isinstance(features, dict):
        raise ValueError("info.json must declare a features object.")
    roles = sorted(
        key.removeprefix("action.eef.").removesuffix(".position")
        for key in features
        if key.startswith("action.eef.") and key.endswith(".position")
    )
    if not roles:
        raise ValueError("Dataset has no action.eef.<role>.position fields.")
    required = {REFERENCE_TRANSFORM_KEY} if require_camera else set()
    for role in roles:
        required.update(
            {
                f"observation.state.eef.{role}.position",
                f"observation.state.eef.{role}.rotation_6d",
                f"action.eef.{role}.position",
                f"action.eef.{role}.rotation_6d",
            }
        )
        if require_gripper:
            required.add(f"action.gripper.{role}.closedness")
    missing = sorted(required - set(features))
    if missing:
        raise ValueError(f"Local EEF action statistics fields are missing: {missing}.")
    return roles


def _episode_records(dataset_path: Path) -> list[dict[str, int]]:
    paths = sorted((dataset_path / "meta" / "episodes").glob("chunk-*/file-*.parquet"))
    if not paths:
        raise ValueError("LeRobot v3 episode metadata parquet files are missing.")
    columns = [
        "episode_index",
        "data/chunk_index",
        "data/file_index",
        "dataset_from_index",
        "dataset_to_index",
        "length",
    ]
    rows: list[dict[str, int]] = []
    for path in paths:
        for raw in pq.read_table(path, columns=columns).to_pylist():
            rows.append({key: int(value) for key, value in raw.items()})
    rows.sort(key=lambda row: row["episode_index"])
    if not rows:
        raise ValueError("Dataset contains no episodes.")
    indices = [row["episode_index"] for row in rows]
    if indices != list(range(len(rows))):
        raise ValueError("Episode indices must be contiguous from zero.")
    for row in rows:
        if row["dataset_to_index"] - row["dataset_from_index"] != row["length"]:
            raise ValueError(
                f"Episode {row['episode_index']} has inconsistent global bounds and length."
            )
    return rows


def _data_path(dataset_path: Path, pattern: str, shard: tuple[int, int]) -> Path:
    chunk_index, file_index = shard
    path = dataset_path / pattern.format(
        chunk_index=chunk_index,
        file_index=file_index,
    )
    if not path.is_file():
        raise FileNotFoundError(f"Dataset parquet shard is missing: {path}")
    return path


def _shard_anchors(
    records: list[dict[str, int]],
    horizon: int,
) -> tuple[np.ndarray, int]:
    file_start = min(row["dataset_from_index"] for row in records)
    ranges = []
    for row in records:
        # Complete window through boundary frame a + H; actions occupy a ... a+H-1.
        count = max(0, row["length"] - horizon)
        local_start = row["dataset_from_index"] - file_start
        if count:
            ranges.append(np.arange(local_start, local_start + count, dtype=np.int64))
    if not ranges:
        return np.empty(0, dtype=np.int64), file_start
    return np.concatenate(ranges), file_start


def _empty_action_accumulators(
    roles: list[str],
    gripper_roles: list[str],
    horizon: int,
    *,
    has_ego: bool = False,
) -> dict[str, _LeadMoments]:
    fields: dict[str, _LeadMoments] = {}
    gripper_set = set(gripper_roles)
    for role in roles:
        fields[f"eef.{role}.position"] = _LeadMoments(horizon, 3)
        fields[f"eef.{role}.rotation_6d"] = _LeadMoments(horizon, 6)
        if role in gripper_set:
            fields[f"gripper.{role}.closedness"] = _LeadMoments(horizon, 1)
    if has_ego:
        fields["ego.position"] = _LeadMoments(horizon, 3)
        fields["ego.rotation_6d"] = _LeadMoments(horizon, 6)
    return fields


def _resolve_has_ego(info: dict[str, Any], geometry: dict[str, Any]) -> bool:
    features = info.get("features") or {}
    has_columns = any(key in features for key in (EGO_POSITION_KEY, EGO_ROTATION_KEY))
    has_meta = "ego_action" in info
    if not has_columns and not has_meta:
        return False
    embodiment = str(info.get("embodiment") or info.get("robot_type") or "")
    if not is_ego_embodiment(embodiment):
        raise ValueError(
            "action.ego.* / ego_action is only valid for ego datasets "
            f"(got embodiment={embodiment!r}; allowed={sorted(EGO_EMBODIMENTS)})."
        )
    if (
        info.get("ego_action") != ego_action_metadata(geometry["episode_frame"])
        or features.get(EGO_POSITION_KEY, {}).get("shape") != [3]
        or features.get(EGO_ROTATION_KEY, {}).get("shape") != [6]
        or features.get(REFERENCE_TRANSFORM_KEY, {}).get("shape") != [4, 4]
    ):
        raise ValueError(
            "Ego action requires both pose fields and consistent camera provenance."
        )
    return True


def _accumulate_one_data_file(job: dict[str, Any]) -> dict[str, Any]:
    """Process one data parquet file; return picklable accumulator state."""
    dataset_path = Path(job["dataset_path"])
    data_pattern = str(job["data_pattern"])
    shard = (int(job["chunk_index"]), int(job["file_index"]))
    shard_records = list(job["shard_records"])
    columns = list(job["columns"])
    roles = list(job["roles"])
    gripper_roles = list(job["gripper_roles"])
    horizon = int(job["horizon"])
    has_ego = bool(job.get("has_ego"))

    fields = _empty_action_accumulators(roles, gripper_roles, horizon, has_ego=has_ego)
    path = _data_path(dataset_path, data_pattern, shard)
    table = pq.read_table(path, columns=columns)
    cached = {column: _column_as_numpy(table.column(column)) for column in columns}
    anchors, file_start = _shard_anchors(shard_records, horizon)
    expected_rows = sum(record["length"] for record in shard_records)
    if table.num_rows != expected_rows or not np.array_equal(
        cached["index"], np.arange(file_start, file_start + expected_rows)
    ):
        raise ValueError(f"Non-contiguous or inconsistent shard rows in {path}.")
    next_start = file_start
    for record in sorted(shard_records, key=lambda row: row["dataset_from_index"]):
        start = record["dataset_from_index"]
        stop = record["dataset_to_index"]
        if start != next_start or not np.all(
            cached["episode_index"][start - file_start : stop - file_start]
            == record["episode_index"]
        ):
            raise ValueError(f"Episode boundaries disagree with data in {path}.")
        next_start = stop
    total_frames = expected_rows

    if has_ego:
        camera_p, camera_r6 = reference_camera_ego_values(cached[REFERENCE_TRANSFORM_KEY])
        camera_r = _rotation_6d_to_matrices(camera_r6, name="camera_reference")
        ego_p = cached[EGO_POSITION_KEY].astype(np.float64)
        ego_r6 = cached[EGO_ROTATION_KEY]
        ego_r = _rotation_6d_to_matrices(ego_r6, name="action.ego")
        if ego_p.shape != (table.num_rows, 3) or not np.isfinite(ego_p).all():
            raise ValueError("Ego action positions must be finite [N,3].")
        for record in shard_records:
            start = record["dataset_from_index"] - file_start
            stop = record["dataset_to_index"] - file_start
            if not np.allclose(
                ego_p[start : stop - 1], camera_p[start + 1 : stop], atol=1e-6, rtol=0
            ) or not np.allclose(
                ego_r6[start : stop - 1], camera_r6[start + 1 : stop], atol=1e-6, rtol=0
            ):
                raise ValueError("Ego action disagrees with the next reference-camera pose.")
            if (
                stop - start < 2
                or not np.array_equal(ego_p[stop - 1], ego_p[stop - 2])
                or not np.array_equal(ego_r6[stop - 1], ego_r6[stop - 2])
            ):
                raise ValueError("Ego terminal action must repeat the previous absolute target.")
        if len(anchors):
            inverse_camera_r = np.swapaxes(camera_r[anchors], -1, -2)
            for lead in range(horizon):
                targets = anchors + lead
                fields["ego.position"].update(
                    lead,
                    np.einsum(
                        "nij,nj->ni", inverse_camera_r, ego_p[targets] - camera_p[anchors]
                    ),
                )
                fields["ego.rotation_6d"].update(
                    lead, _matrices_to_rotation_6d(inverse_camera_r @ ego_r[targets])
                )

    if len(anchors) == 0:
        return {
            "anchor_count": 0,
            "total_frames": total_frames,
            "relative": {k: v.to_state() for k, v in fields.items()},
        }

    gripper_set = set(gripper_roles)
    for role in roles:
        state_position = cached[f"observation.state.eef.{role}.position"].astype(np.float64)
        action_position = cached[f"action.eef.{role}.position"].astype(np.float64)
        if (
            state_position.shape != (table.num_rows, 3)
            or action_position.shape != state_position.shape
        ):
            raise ValueError(f"EEF position fields must be [N,3] for {role}.")
        state_rotation = _rotation_6d_to_matrices(
            cached[f"observation.state.eef.{role}.rotation_6d"], name=f"state.{role}"
        )
        action_rotation = _rotation_6d_to_matrices(
            cached[f"action.eef.{role}.rotation_6d"], name=f"action.{role}"
        )
        gripper = None
        if role in gripper_set:
            gripper = cached[f"action.gripper.{role}.closedness"].reshape(table.num_rows, -1)
            if gripper.shape[1] != 1 or np.any((gripper < 0) | (gripper > 1)):
                raise ValueError(f"Gripper {role} must contain absolute closedness in [0,1].")
        inverse_anchor_rotation = np.swapaxes(state_rotation[anchors], -1, -2)
        anchor_position = state_position[anchors]
        for lead in range(horizon):
            targets = anchors + lead
            local_position = np.einsum(
                "nij,nj->ni",
                inverse_anchor_rotation,
                action_position[targets] - anchor_position,
            )
            local_rotation = inverse_anchor_rotation @ action_rotation[targets]
            fields[f"eef.{role}.position"].update(lead, local_position)
            fields[f"eef.{role}.rotation_6d"].update(
                lead, _matrices_to_rotation_6d(local_rotation)
            )
            if gripper is not None:
                fields[f"gripper.{role}.closedness"].update(lead, gripper[targets])
    return {
        "anchor_count": int(len(anchors)),
        "total_frames": total_frames,
        "relative": {k: v.to_state() for k, v in fields.items()},
    }


def prepare_relative_action_statistics(
    dataset_path: Path | str,
    *,
    statistics_horizon_seconds: float = DEFAULT_STATISTICS_HORIZON_SECONDS,
    rebuild: bool = False,
    num_workers: int | None = None,
) -> dict[str, Any]:
    """Write R_state.T @ (p_target-p_state), R_state.T @ R_target per lead."""
    dataset_path = Path(dataset_path)
    output_path = dataset_path / "meta" / STATISTICS_FILE
    if output_path.exists() and not rebuild:
        raise FileExistsError(
            f"Statistics already exist at {output_path}; pass rebuild=True to replace them."
        )
    info = _read_json(dataset_path / "meta" / "info.json")
    geometry = _require_shared_frame_geometry(info)
    fps = float(info["fps"])
    seconds = float(statistics_horizon_seconds)
    if not np.isfinite([fps, seconds]).all() or fps <= 0 or seconds <= 0:
        raise ValueError("FPS and statistics horizon must be finite and positive.")
    exact_horizon = fps * seconds
    horizon = int(round(exact_horizon))
    if horizon < 1 or not np.isclose(exact_horizon, horizon, atol=1e-6, rtol=0):
        raise ValueError("statistics_horizon_seconds * FPS must be a positive integer.")

    roles = _arm_roles(info, require_camera=False, require_gripper=False)
    records = _episode_records(dataset_path)
    if len(records) != int(info["total_episodes"]):
        raise ValueError("Episode metadata count disagrees with info.json.")
    by_shard: dict[tuple[int, int], list[dict[str, int]]] = {}
    for record in records:
        by_shard.setdefault(
            (record["data/chunk_index"], record["data/file_index"]), []
        ).append(record)

    features = info["features"]
    gripper_roles = [
        role for role in roles if f"action.gripper.{role}.closedness" in features
    ]
    has_ego = _resolve_has_ego(info, geometry)
    fields = _empty_action_accumulators(roles, gripper_roles, horizon, has_ego=has_ego)
    columns = ["episode_index", "index"]
    for role in roles:
        for suffix in ("position", "rotation_6d"):
            columns.extend(
                (
                    f"observation.state.eef.{role}.{suffix}",
                    f"action.eef.{role}.{suffix}",
                )
            )
        if role in gripper_roles:
            columns.append(f"action.gripper.{role}.closedness")
    if has_ego:
        columns.extend((REFERENCE_TRANSFORM_KEY, EGO_POSITION_KEY, EGO_ROTATION_KEY))

    jobs = [
        {
            "dataset_path": str(dataset_path),
            "data_pattern": str(info["data_path"]),
            "chunk_index": shard[0],
            "file_index": shard[1],
            "shard_records": shard_records,
            "columns": columns,
            "roles": roles,
            "gripper_roles": gripper_roles,
            "horizon": horizon,
            "has_ego": has_ego,
        }
        for shard, shard_records in sorted(by_shard.items())
    ]
    workers = (
        max(1, int(num_workers))
        if num_workers is not None
        else _default_stats_workers()
    )
    if len(jobs) <= 1 or workers <= 1:
        partials = [_accumulate_one_data_file(job) for job in jobs]
        workers = 1 if len(jobs) <= 1 else workers
    else:
        workers = min(workers, len(jobs))
        from robot_data_processing.stats_quantiles import frozen_gc

        with frozen_gc(), ProcessPoolExecutor(max_workers=workers) as pool:
            partials = list(pool.map(_accumulate_one_data_file, jobs, chunksize=1))

    anchor_count = 0
    total_frames = 0
    for partial in partials:
        anchor_count += int(partial["anchor_count"])
        total_frames += int(partial["total_frames"])
        for key, state in partial["relative"].items():
            fields[key].merge_from(_LeadMoments.from_state(state))

    if total_frames != int(info["total_frames"]):
        raise ValueError("Episode frame count disagrees with info.json.")
    if anchor_count == 0:
        raise ValueError("Dataset has no complete statistics windows.")

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "source_fps": fps,
        "statistics_horizon_seconds": seconds,
        "statistics_horizon_frames": horizon,
        "anchor_frame": "each_arm_eef_at_anchor",
        "pose_storage_frame": geometry["episode_frame"],
        "anchor_population": "all_complete_chunks_with_boundary_frame",
        "required_terminal_frame_offset": horizon,
        "incomplete_tail_policy": "exclude",
        "action_row_convention": "stored action at row a+k; no additional temporal offset",
        "position_delta_convention": "inverse(R_state_at_anchor) @ (p_action - p_state_at_anchor)",
        "rotation_delta_convention": "inverse(R_state_at_anchor) @ R_action",
        "rotation_6d_convention": "first_two_rotation_matrix_rows_flattened",
        "relative_action_rotation_representation": "rotation_6d",
        "rotation_source_fields": {
            role: {
                "state": f"observation.state.eef.{role}.rotation_6d",
                "action": f"action.eef.{role}.rotation_6d",
            }
            for role in roles
        },
        "translation_unit": "meter",
        "gripper_semantics": "absolute_target_closedness",
        "action_target": info.get("action_target", {}),
        "episode_count": len(records),
        "contributing_episode_count": sum(record["length"] > horizon for record in records),
        "source_frame_count": total_frames,
        "anchor_count": anchor_count,
        "relative_action": {key: value.to_json() for key, value in fields.items()},
    }
    if has_ego:
        payload["ego_action"] = {
            **info["ego_action"],
            "anchor_frame": "camera_reference_at_chunk_anchor",
            "action_row_convention": "stored action at row a+k; no additional temporal offset",
            "position_delta_convention": (
                "inverse(R_camera_at_anchor) @ (p_ego_action - p_camera_at_anchor)"
            ),
            "rotation_delta_convention": "inverse(R_camera_at_anchor) @ R_ego_action",
        }
    _atomic_json_dump(output_path, payload)
    payload["num_workers"] = workers if len(jobs) > 1 else 1
    return payload


prepare_local_relative_action_statistics = prepare_relative_action_statistics


def prepare_dataset_statistics(
    dataset_path: Path | str,
    *,
    statistics_horizon_seconds: float = DEFAULT_STATISTICS_HORIZON_SECONDS,
    rebuild: bool = False,
    num_workers: int | None = None,
) -> dict[str, Any]:
    """Write local action stats and independent EEF state stats for one v3 dataset."""
    dataset_path = Path(dataset_path)
    action = prepare_relative_action_statistics(
        dataset_path,
        statistics_horizon_seconds=statistics_horizon_seconds,
        rebuild=rebuild,
        num_workers=num_workers,
    )
    from robot_data_processing.eef_state_statistics import prepare_eef_state_statistics

    state = prepare_eef_state_statistics(
        dataset_path,
        rebuild=rebuild,
        num_workers=num_workers,
    )
    combined = dict(action)
    combined["eef_state_statistics"] = {
        "schema": state.get("schema"),
        "source_frame_count": state["source_frame_count"],
        "availability": state.get("availability"),
        "unavailable_reasons": state.get("unavailable_reasons"),
        "output_path": str(dataset_path / "meta" / "statistics_state.json"),
    }
    combined["action_output_path"] = str(dataset_path / "meta" / STATISTICS_FILE)
    combined["state_output_path"] = str(dataset_path / "meta" / "statistics_state.json")
    return combined


def compare_relative_statistics(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    rtol: float = 1e-5,
    atol: float = 1e-6,
) -> list[str]:
    """Return human-readable mismatch messages; empty list means match."""
    mismatches: list[str] = []
    scalar_keys = [
        "schema",
        "source_fps",
        "statistics_horizon_seconds",
        "statistics_horizon_frames",
        "anchor_frame",
        "pose_storage_frame",
        "anchor_population",
        "required_terminal_frame_offset",
        "incomplete_tail_policy",
        "action_row_convention",
        "position_delta_convention",
        "rotation_delta_convention",
        "rotation_6d_convention",
        "relative_action_rotation_representation",
        "translation_unit",
        "gripper_semantics",
        "episode_count",
        "contributing_episode_count",
        "source_frame_count",
        "anchor_count",
    ]
    for key in scalar_keys:
        if actual.get(key) != expected.get(key):
            mismatches.append(f"scalar {key}: {actual.get(key)!r} != {expected.get(key)!r}")

    if actual.get("rotation_source_fields") != expected.get("rotation_source_fields"):
        mismatches.append("rotation_source_fields mismatch")

    actual_section = actual.get("relative_action") or {}
    expected_section = expected.get("relative_action") or {}
    if set(actual_section) != set(expected_section):
        mismatches.append(
            f"relative_action keys differ: {sorted(actual_section)} vs {sorted(expected_section)}"
        )
    else:
        for field, actual_stats in actual_section.items():
            expected_stats = expected_section[field]
            for stat_name in ("count", "mean", "m2", "min", "max"):
                a = np.asarray(actual_stats[stat_name], dtype=np.float64)
                e = np.asarray(expected_stats[stat_name], dtype=np.float64)
                if a.shape != e.shape:
                    mismatches.append(
                        f"relative_action.{field}.{stat_name} shape {a.shape} != {e.shape}"
                    )
                    continue
                if not np.allclose(a, e, rtol=rtol, atol=atol):
                    err = float(np.max(np.abs(a - e)))
                    mismatches.append(
                        f"relative_action.{field}.{stat_name} max_abs_err={err:.6e}"
                    )
    return mismatches
