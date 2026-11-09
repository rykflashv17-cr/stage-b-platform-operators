"""Per-frame bimanual and optional reference-camera EEF state statistics.

Counts every observation frame once. Unavailable groups are omitted with
``availability`` / ``unavailable_reasons`` and never zero-filled.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from robot_data_processing.relative_statistics import (
    REFERENCE_TRANSFORM_KEY,
    _Moments,
    _atomic_json_dump,
    _column_as_numpy,
    _data_path,
    _default_stats_workers,
    _episode_records,
    _matrices_to_rotation_6d,
    _read_json,
    _require_shared_frame_geometry,
    _rotation_6d_to_matrices,
)

STATE_STATISTICS_FILE = "statistics_state.json"


def _empty_state_moments(
    groups: dict[str, tuple[str, ...]],
) -> dict[str, dict[str, dict[str, _Moments]]]:
    return {
        group: {name: {"position": _Moments(3), "rotation_6d": _Moments(6)} for name in names}
        for group, names in groups.items()
    }


def _moments_to_state(
    moments: dict[str, dict[str, dict[str, _Moments]]],
) -> dict[str, Any]:
    return {
        group: {
            name: {key: entry.to_state() for key, entry in entries.items()}
            for name, entries in names.items()
        }
        for group, names in moments.items()
    }


def _accumulate_pose(
    moments: dict[str, dict[str, dict[str, _Moments]]],
    group: str,
    name: str,
    origin_p: np.ndarray,
    origin_r: np.ndarray,
    target_p: np.ndarray,
    target_r: np.ndarray,
) -> None:
    inverse_r = np.swapaxes(origin_r, -1, -2)
    position = np.einsum("nij,nj->ni", inverse_r, target_p - origin_p)
    rotation = inverse_r @ target_r
    moments[group][name]["position"].update(position)
    moments[group][name]["rotation_6d"].update(_matrices_to_rotation_6d(rotation))


def _accumulate_one_state_file(job: dict[str, Any]) -> dict[str, Any]:
    dataset_path = Path(job["dataset_path"])
    data_pattern = str(job["data_pattern"])
    shard = (int(job["chunk_index"]), int(job["file_index"]))
    rows = list(job["shard_records"])
    columns = list(job["columns"])
    roles = list(job["roles"])
    groups = {str(k): tuple(v) for k, v in job["groups"].items()}
    bimanual = bool(job["bimanual"])
    has_camera = bool(job["has_camera"])

    moments = _empty_state_moments(groups)
    path = _data_path(dataset_path, data_pattern, shard)
    table = pq.read_table(path, columns=columns)
    data = {key: _column_as_numpy(table.column(key)) for key in columns}
    start = rows[0]["dataset_from_index"]
    n = sum(row["length"] for row in rows)
    if table.num_rows != n or not np.array_equal(data["index"], np.arange(start, start + n)):
        raise ValueError(f"Non-contiguous or inconsistent shard rows in {path}.")
    offset = 0
    for row in rows:
        stop = offset + row["length"]
        if row["dataset_from_index"] != start + offset or not np.all(
            data["episode_index"][offset:stop] == row["episode_index"]
        ):
            raise ValueError(f"Episode boundaries disagree with data in {path}.")
        offset = stop
    poses: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for role in roles:
        p = data[f"observation.state.eef.{role}.position"].astype(np.float64)
        r = _rotation_6d_to_matrices(
            data[f"observation.state.eef.{role}.rotation_6d"], name=f"state.{role}"
        )
        if p.shape != (n, 3):
            raise ValueError(f"State positions for {role} must have shape [N,3].")
        poses[role] = (p, r)
    if bimanual:
        _accumulate_pose(
            moments, "bimanual_relative", "left_from_right", *poses["left"], *poses["right"]
        )
        _accumulate_pose(
            moments, "bimanual_relative", "right_from_left", *poses["right"], *poses["left"]
        )
    if has_camera:
        camera = data[REFERENCE_TRANSFORM_KEY].astype(np.float64)
        if camera.shape != (n, 4, 4) or not np.allclose(
            camera[:, 3], [0, 0, 0, 1], atol=1e-6, rtol=0
        ):
            raise ValueError("Reference-camera poses must be homogeneous [N,4,4] transforms.")
        camera_r = camera[:, :3, :3]
        if not np.allclose(
            np.swapaxes(camera_r, -1, -2) @ camera_r, np.eye(3), atol=1e-4, rtol=0
        ) or not np.allclose(np.linalg.det(camera_r), 1, atol=1e-4, rtol=0):
            raise ValueError("Reference-camera rotations must belong to SO(3).")
        for role in roles:
            _accumulate_pose(
                moments, "reference_camera", role, camera[:, :3, 3], camera_r, *poses[role]
            )
    return {"moments": _moments_to_state(moments), "n": n}


def prepare_eef_state_statistics(
    dataset_path: str | Path,
    *,
    rebuild: bool = False,
    num_workers: int | None = None,
) -> dict[str, Any]:
    """Count each observation once, without action windows or temporal offsets."""
    dataset_path = Path(dataset_path)
    output_path = dataset_path / "meta" / STATE_STATISTICS_FILE
    if output_path.exists() and not rebuild:
        raise FileExistsError(
            f"Statistics already exist at {output_path}; pass rebuild=True to replace them."
        )
    info = _read_json(dataset_path / "meta" / "info.json")
    geometry = _require_shared_frame_geometry(info)
    features = info["features"]
    roles = sorted(
        key.removeprefix("observation.state.eef.").removesuffix(".position")
        for key in features
        if key.startswith("observation.state.eef.") and key.endswith(".position")
    )
    if roles != ["primary"] and roles != ["left", "right"]:
        raise ValueError("State roles must be primary or left/right.")
    bimanual = roles == ["left", "right"]
    columns = ["index", "episode_index"]
    for role in roles:
        for suffix, width in (("position", 3), ("rotation_6d", 6)):
            key = f"observation.state.eef.{role}.{suffix}"
            if features.get(key, {}).get("shape") != [width]:
                raise ValueError(f"EEF state statistics require {key} with shape [{width}].")
            columns.append(key)
    camera_geometry = info.get("camera_geometry")
    has_camera = REFERENCE_TRANSFORM_KEY in features
    if (
        isinstance(camera_geometry, dict)
        and REFERENCE_TRANSFORM_KEY in camera_geometry.get("per_frame_extrinsic_keys", [])
        and not has_camera
    ):
        raise ValueError("Declared reference-camera extrinsic is missing.")
    if has_camera:
        if not isinstance(camera_geometry, dict):
            raise ValueError("Reference-camera transform requires camera_geometry.")
        if (
            info.get("camera_calibration") == "unavailable"
            or camera_geometry.get("mode", "calibrated") != "calibrated"
            or camera_geometry.get("transform_convention") != "T_A_B maps coordinates from B to A"
            or camera_geometry.get("camera_coordinate_convention")
            != "opencv_x_right_y_down_z_forward"
            or features.get(REFERENCE_TRANSFORM_KEY, {}).get("shape") != [4, 4]
            or REFERENCE_TRANSFORM_KEY not in camera_geometry.get("per_frame_extrinsic_keys", [])
        ):
            raise ValueError("Reference-camera geometry is declared but missing or inconsistent.")
        columns.append(REFERENCE_TRANSFORM_KEY)
    elif REFERENCE_TRANSFORM_KEY in features:
        raise ValueError(
            "Reference-camera transform requires an explicit camera_geometry declaration."
        )

    groups: dict[str, tuple[str, ...]] = (
        {"bimanual_relative": ("left_from_right", "right_from_left")} if bimanual else {}
    )
    if has_camera:
        groups["reference_camera"] = tuple(roles)
    moments = _empty_state_moments(groups)

    records = _episode_records(dataset_path)
    if len(records) != int(info["total_episodes"]):
        raise ValueError("Episode count disagrees with info.json.")
    by_shard: dict[tuple[int, int], list[dict[str, int]]] = {}
    expected_start = 0
    for row in records:
        if row["length"] <= 0 or row["dataset_from_index"] != expected_start:
            raise ValueError("Episode ranges must be positive and globally contiguous from zero.")
        expected_start = row["dataset_to_index"]
        by_shard.setdefault((row["data/chunk_index"], row["data/file_index"]), []).append(row)
    if expected_start != int(info["total_frames"]):
        raise ValueError("Episode frame count disagrees with info.json.")

    jobs = [
        {
            "dataset_path": str(dataset_path),
            "data_pattern": str(info["data_path"]),
            "chunk_index": shard[0],
            "file_index": shard[1],
            "shard_records": shard_records,
            "columns": columns,
            "roles": roles,
            "groups": {k: list(v) for k, v in groups.items()},
            "bimanual": bimanual,
            "has_camera": has_camera,
        }
        for shard, shard_records in sorted(by_shard.items())
    ]
    workers = (
        max(1, int(num_workers))
        if num_workers is not None
        else _default_stats_workers()
    )
    if len(jobs) <= 1 or workers <= 1:
        partials = [_accumulate_one_state_file(job) for job in jobs]
        workers = 1 if len(jobs) <= 1 else workers
    else:
        workers = min(workers, len(jobs))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            partials = list(pool.map(_accumulate_one_state_file, jobs, chunksize=1))

    seen_frames = 0
    for partial in partials:
        seen_frames += int(partial["n"])
        for group, names in partial["moments"].items():
            for name, entries in names.items():
                for key, state in entries.items():
                    moments[group][name][key].merge_from(_Moments.from_state(state))
    if seen_frames != expected_start:
        raise ValueError("Episode frame count disagrees with info.json.")

    payload: dict[str, Any] = {
        "schema": "eef_state_statistics",
        "population": "all_observation_frames_once",
        "episode_count": len(records),
        "source_frame_count": expected_start,
        "source_fps": info["fps"],
        "pose_storage_frame": geometry["episode_frame"],
        "translation_unit": "meter",
        "rotation_6d_convention": "first_two_rotation_matrix_rows_flattened",
        "transform_convention": "T_A_B maps coordinates from B to A",
        "m2_convention": "sum_of_squared_deviations_from_mean",
        "availability": {"bimanual_relative": bimanual, "reference_camera": has_camera},
        "relative_pose_conventions": {
            "left_from_right": "inverse(T_E_Left(t)) @ T_E_Right(t)",
            "right_from_left": "inverse(T_E_Right(t)) @ T_E_Left(t)",
        },
    }
    reasons: dict[str, str] = {}
    if not has_camera:
        reasons["reference_camera"] = "camera_extrinsics_unavailable"
    if not bimanual:
        reasons["bimanual_relative"] = "single_arm"
        payload.pop("relative_pose_conventions")
    if reasons:
        payload["unavailable_reasons"] = reasons
    for group, names in moments.items():
        payload[group] = {
            name: {key: entry.to_json() for key, entry in entries.items()}
            for name, entries in names.items()
        }
    if has_camera:
        payload["reference_camera"].update(
            {
                "frame": "camera_reference_at_state_timestamp",
                "pose_key": REFERENCE_TRANSFORM_KEY,
                "pose_convention": "inverse(T_E_CameraReference(t)) @ T_E_Hand(t)",
            }
        )
    _atomic_json_dump(output_path, payload)
    payload["num_workers"] = workers if len(jobs) > 1 else 1
    return payload
