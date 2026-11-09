from __future__ import annotations

import pyarrow as pa

import numpy as np

from robot_data_processing.schema import ACTION_DIM, DatasetSchema, HUMANOID_SCHEMA, stack_column
from robot_data_processing.stages.state_action_temporal_alignment import (
    apply_state_action_temporal_alignment,
)
from robot_data_processing.transforms import (
    robomind_ur_apply_compact_to_action,
    robomind_ur_build_action,
    robomind_ur_build_state,
    robomind_ur_compact_teleop,
)


ROBOMIND_TELEOP_ACTION_INDICES = (0, 7, *range(14, 26))


def expand_compact_exclude_to_action(
    exclude_compact: np.ndarray,
    action_dim: int,
    indices: tuple[int, ...] = ROBOMIND_TELEOP_ACTION_INDICES,
) -> np.ndarray:
    full = np.zeros((exclude_compact.shape[0], action_dim), dtype=bool)
    for i, idx in enumerate(indices):
        if i < exclude_compact.shape[1]:
            full[:, idx] = exclude_compact[:, i]
    return full


def replace_action_with_state(
    data: dict[str, np.ndarray],
    schema: DatasetSchema = HUMANOID_SCHEMA,
) -> dict[str, np.ndarray]:
    """Discard original action; set action equal to state (canonical or raw columns)."""
    if schema.embodiment == "humanoid":
        state = data["observation.state"]
        if "observation.state.arm.position" in data:
            data["action.arm.position"] = data["observation.state.arm.position"].copy()
        if "observation.state.effector.position" in data:
            data["action.effector.position"] = data["observation.state.effector.position"].copy()
        if state.shape[1] >= ACTION_DIM:
            data["action"] = state[:, :ACTION_DIM].copy()
        elif "observation.state.arm.position" in data and "observation.state.effector.position" in data:
            data["action"] = np.concatenate(
                [data["observation.state.arm.position"], data["observation.state.effector.position"]],
                axis=1,
            )
        return data

    state = data[schema.state_column]
    data[schema.action_column] = state.copy()
    return data


def _list_array_from_2d(arr: np.ndarray) -> pa.Array:
    return pa.array([row.astype(np.float32).tolist() for row in arr])


def extract_state_action_from_table(
    table: pa.Table,
    schema: DatasetSchema = HUMANOID_SCHEMA,
) -> tuple[np.ndarray, np.ndarray]:
    if schema.embodiment == "robomind_ur":
        raw: dict[str, np.ndarray] = {}
        for name in schema.raw_columns:
            if name not in table.column_names:
                continue
            col = table.column(name).combine_chunks()
            raw[name] = stack_column(col.to_numpy(zero_copy_only=False))
        return robomind_ur_build_state(raw), robomind_ur_build_action(raw)

    state_col = table.column(schema.state_column).combine_chunks()
    action_col = table.column(schema.action_column).combine_chunks()
    state = stack_column(state_col.to_numpy(zero_copy_only=False))
    action = stack_column(action_col.to_numpy(zero_copy_only=False))
    return state, action


def update_table_action_columns(
    table: pa.Table,
    action: np.ndarray,
    schema: DatasetSchema = HUMANOID_SCHEMA,
) -> pa.Table:
    """Write aligned action arrays back into parquet action columns."""
    out = table
    if schema.embodiment == "robomind_ur":
        return update_robomind_table_master_action(out, action, schema)

    if schema.embodiment == "humanoid":
        column_map = {
            "action": action,
            "action.arm.position": action[:, :12] if action.shape[1] >= 12 else None,
            "action.effector.position": action[:, 12:14] if action.shape[1] >= 14 else None,
        }
        for name, values in column_map.items():
            if values is None or name not in out.column_names:
                continue
            idx = out.column_names.index(name)
            out = out.set_column(idx, name, _list_array_from_2d(values))
        return out

    idx = out.column_names.index(schema.action_column)
    return out.set_column(idx, schema.action_column, _list_array_from_2d(action))


def update_robomind_table_master_action(
    table: pa.Table,
    action: np.ndarray,
    schema: DatasetSchema = HUMANOID_SCHEMA,
) -> pa.Table:
    out = table
    column_map = {
        "master/end_effector_left_position_align": action[:, 0:1],
        "master/end_effector_right_position_align": action[:, 7:8],
        "master/arm_left_position_align": action[:, 14:20],
        "master/arm_right_position_align": action[:, 20:26],
    }
    for name, values in column_map.items():
        if name not in out.column_names:
            continue
        idx = out.column_names.index(name)
        out = out.set_column(idx, name, _list_array_from_2d(values))
    return out


def apply_robomind_temporal_alignment(
    state: np.ndarray,
    action: np.ndarray,
    lag: int,
) -> np.ndarray:
    state_c, action_c = robomind_ur_compact_teleop(state, action)
    aligned_c = apply_state_action_temporal_alignment(state_c, action_c, lag, state_c.shape[1])
    return robomind_ur_apply_compact_to_action(action, aligned_c)


def replace_action_with_state_table(
    table: pa.Table,
    schema: DatasetSchema = HUMANOID_SCHEMA,
) -> pa.Table:
    """Replace action columns in a parquet table with state values."""
    data: dict[str, np.ndarray] = {}
    if schema.embodiment == "humanoid":
        col_names = (
            "observation.state",
            "observation.state.arm.position",
            "observation.state.effector.position",
            "action",
            "action.arm.position",
            "action.effector.position",
        )
    else:
        col_names = (schema.state_column, schema.action_column)

    for name in col_names:
        if name not in table.column_names:
            continue
        col = table.column(name).combine_chunks()
        data[name] = stack_column(col.to_numpy(zero_copy_only=False))

    replace_action_with_state(data, schema)

    out = table
    if schema.embodiment == "humanoid":
        for name in ("action.arm.position", "action.effector.position", "action"):
            if name not in out.column_names:
                continue
            idx = out.column_names.index(name)
            out = out.set_column(idx, name, _list_array_from_2d(data[name]))
    else:
        idx = out.column_names.index(schema.action_column)
        out = out.set_column(idx, schema.action_column, _list_array_from_2d(data[schema.action_column]))
    return out
