from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from robot_data_processing.preprocess import replace_action_with_state
from robot_data_processing.schema import (
    DatasetSchema,
    EGODEX_SCHEMA,
    HUMANOID_SCHEMA,
    stack_column,
)
from robot_data_processing.transforms import (
    egodex_to_canonical,
    humanoid_action_arrays,
    humanoid_state_from_raw,
    robomind_ur_build_action,
    robomind_ur_build_state,
)


def episode_parquet_path(root: Path, episode_index: int) -> Path:
    """Resolve episode parquet for v2.1 (episode_*) or v3 (file-*) layouts."""
    root = Path(root)
    from robot_data_processing.v3_layout import is_v3_layout, resolve_v3_data_path

    # Cache layout probe on the Path object to avoid repeated PFS meta reads.
    cached = getattr(root, "_dataclean_is_v3", None)
    if cached is None:
        cached = is_v3_layout(root)
        try:
            setattr(root, "_dataclean_is_v3", cached)
        except Exception:
            pass
    if cached:
        return resolve_v3_data_path(root, episode_index)
    chunk = episode_index // 1000
    return root / f"data/chunk-{chunk:03d}/episode_{episode_index:06d}.parquet"


def list_episode_indices(root: Path, total_episodes: int | None = None) -> list[int]:
    root = Path(root)
    from robot_data_processing.v3_layout import is_v3_layout, load_episode_rows

    if is_v3_layout(root):
        return [int(r["episode_index"]) for r in load_episode_rows(root)]

    if total_episodes is not None:
        # Trust meta total_episodes — avoid O(N) exists() probes on large PFS trees.
        return list(range(int(total_episodes)))

    indices: list[int] = []
    data_root = root / "data"
    for chunk_dir in sorted(data_root.glob("chunk-*")):
        for path in sorted(chunk_dir.glob("episode_*.parquet")):
            indices.append(int(path.stem.split("_")[1]))
        for path in sorted(chunk_dir.glob("file-*.parquet")):
            # pseudo/aggregated without readable episodes meta: best-effort
            try:
                indices.append(int(path.stem.split("-")[1]) + int(chunk_dir.name.split("-")[1]) * 1000)
            except Exception:
                continue
    return sorted(set(indices))


def _pad_or_trim(arr: np.ndarray, dim: int) -> np.ndarray:
    arr = np.asarray(arr, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    if arr.shape[1] == dim:
        return arr
    out = np.zeros((arr.shape[0], dim), dtype=np.float64)
    n = min(arr.shape[1], dim)
    out[:, :n] = arr[:, :n]
    return out


def _transform_to_canonical(
    raw: dict[str, np.ndarray],
    schema: DatasetSchema,
) -> tuple[np.ndarray, np.ndarray]:
    if schema.embodiment in ("egodex", "egostandard", "egoverse"):
        state = egodex_to_canonical(raw[schema.state_column])
        action = egodex_to_canonical(raw[schema.action_column])
        return state, action
    if schema.embodiment == "humanoid":
        return (
            humanoid_state_from_raw(raw),
            humanoid_action_arrays(raw[schema.action_column]),
        )
    if schema.embodiment == "robomind_ur":
        return robomind_ur_build_state(raw), robomind_ur_build_action(raw)
    # Generic packed observation.state / action (e.g. abc_130k 14d joint+gripper).
    if schema.state_column in raw and schema.action_column in raw:
        return (
            _pad_or_trim(raw[schema.state_column], schema.pipeline_state_dim),
            _pad_or_trim(raw[schema.action_column], schema.pipeline_action_dim),
        )
    raise ValueError(f"Unsupported embodiment: {schema.embodiment}")


def read_episode_canonical(
    path: Path,
    schema: DatasetSchema = HUMANOID_SCHEMA,
    action_from_state: bool = False,
) -> dict[str, np.ndarray]:
    """Read parquet and return pipeline state/action arrays."""
    table = pq.read_table(path, columns=list(schema.raw_columns))
    raw: dict[str, np.ndarray] = {}
    for name in schema.raw_columns:
        if name not in table.column_names:
            continue
        col = table.column(name)
        if hasattr(col, "combine_chunks"):
            col = col.combine_chunks()
        raw[name] = stack_column(col.to_numpy(zero_copy_only=False))

    if action_from_state:
        replace_action_with_state(raw, schema)

    state, action = _transform_to_canonical(raw, schema)
    return {"state": state, "action": action, "raw": raw}


def read_episode_arrays(
    path: Path,
    columns: tuple[str, ...] | None = None,
    action_from_state: bool = False,
    schema: DatasetSchema = HUMANOID_SCHEMA,
) -> dict[str, np.ndarray]:
    """Backward-compatible read; returns canonical state/action plus legacy keys when available."""
    data = read_episode_canonical(path, schema=schema, action_from_state=action_from_state)
    out: dict[str, np.ndarray] = {
        "state": data["state"],
        "action": data["action"],
        "observation.state": data["state"],
    }
    raw = data.get("raw", {})
    for key in (
        "observation.state.arm.position",
        "observation.state.end.position",
        "action.arm.position",
    ):
        if key in raw:
            out[key] = raw[key]
    if schema.embodiment == "humanoid" and "observation.state" in raw:
        out["observation.state"] = raw["observation.state"]
        if "observation.state.arm.position" in raw:
            out["observation.state.arm.position"] = raw["observation.state.arm.position"]
        if "observation.state.end.position" in raw:
            out["observation.state.end.position"] = raw["observation.state.end.position"]
        if "action.arm.position" in raw:
            out["action.arm.position"] = raw["action.arm.position"]
    return out


def read_episode_table(path: Path, episode_index: int | None = None):
    """Read parquet table; optionally keep only rows for ``episode_index``.

    Aggregated LeRobot v3.0 ``file-*.parquet`` files contain multiple episodes.
    Pass ``episode_index`` to slice to a single episode before adapter / export.
    """
    table = pq.read_table(path)
    if episode_index is None or "episode_index" not in table.column_names:
        return table
    col = table.column("episode_index")
    if hasattr(col, "combine_chunks"):
        col = col.combine_chunks()
    vals = np.asarray(col.to_numpy(zero_copy_only=False)).reshape(-1)
    indices = np.flatnonzero(vals.astype(np.int64) == int(episode_index))
    if indices.size == 0:
        return table.slice(0, 0)
    return table.take(pa.array(indices, type=pa.int64()))


def valid_keep_length(table) -> int:
    """Number of frames marked keep (mask==1); supports prefix-only or sparse masks."""
    if "step_validity_mask" not in table.column_names:
        return table.num_rows
    col = table.column("step_validity_mask").combine_chunks()
    count = 0
    for i in range(table.num_rows):
        val = col[i].as_py()
        mask_val = val[0] if isinstance(val, (list, tuple, np.ndarray)) else int(val)
        if mask_val != 0:
            count += 1
    return count


def keep_indices_from_table(table) -> np.ndarray:
    if "step_validity_mask" not in table.column_names:
        return np.arange(table.num_rows, dtype=np.int64)
    col = table.column("step_validity_mask").combine_chunks()
    indices = []
    for i in range(table.num_rows):
        val = col[i].as_py()
        mask_val = val[0] if isinstance(val, (list, tuple, np.ndarray)) else int(val)
        if mask_val != 0:
            indices.append(i)
    return np.asarray(indices, dtype=np.int64)


def filter_table_by_indices(table, indices: np.ndarray):
    if indices.size == 0:
        return table.slice(0, 0)
    return table.take(pa.array(indices, type=pa.int64()))


def _standard_fields_to_quality(
    fields: dict[str, np.ndarray],
    schema: DatasetSchema,
) -> tuple[np.ndarray, np.ndarray]:
    """Pack standard eef/arm/gripper columns into quality-space arrays."""
    if schema.embodiment in ("egodex", "egostandard", "egoverse"):
        from robot_data_processing.transforms import egodex_to_canonical

        # Rebuild 20d then canonical 14d
        T = None
        for v in fields.values():
            T = int(np.asarray(v).shape[0])
            break
        if T is None:
            raise ValueError("empty standard fields")
        packed = np.zeros((T, 20), dtype=np.float64)

        def _fill(prefix: str, offset: int) -> None:
            pos = fields.get(f"{prefix}.position")
            rot = fields.get(f"{prefix}.rotation_6d")
            if pos is None or rot is None:
                return
            packed[:, offset : offset + 3] = np.asarray(pos, dtype=np.float64)
            packed[:, offset + 3 : offset + 9] = np.asarray(rot, dtype=np.float64)

        _fill("observation.state.eef.left", 0)
        g = fields.get("observation.state.gripper.left.closedness")
        if g is not None:
            packed[:, 9] = np.asarray(g).reshape(-1)
        _fill("observation.state.eef.right", 10)
        g = fields.get("observation.state.gripper.right.closedness")
        if g is not None:
            packed[:, 19] = np.asarray(g).reshape(-1)
        state = egodex_to_canonical(packed)
        packed_a = np.zeros((T, 20), dtype=np.float64)

        def _fill_a(prefix: str, offset: int) -> None:
            pos = fields.get(f"{prefix}.position")
            rot = fields.get(f"{prefix}.rotation_6d")
            if pos is None or rot is None:
                return
            packed_a[:, offset : offset + 3] = np.asarray(pos, dtype=np.float64)
            packed_a[:, offset + 3 : offset + 9] = np.asarray(rot, dtype=np.float64)

        _fill_a("action.eef.left", 0)
        g = fields.get("action.gripper.left.closedness")
        if g is not None:
            packed_a[:, 9] = np.asarray(g).reshape(-1)
        _fill_a("action.eef.right", 10)
        g = fields.get("action.gripper.right.closedness")
        if g is not None:
            packed_a[:, 19] = np.asarray(g).reshape(-1)
        action = egodex_to_canonical(packed_a)
        return state, action

    return pack_standard_joint_eef_quality(fields)


def pack_standard_joint_eef_quality(
    fields: dict[str, np.ndarray],
    T: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Pack standard arm/gripper/eef columns to 32d quality arrays.

    Bimanual layout: L6 R6 Lg Rg | Lxyz3 Lrot6d6 | Rxyz3 Rrot6d6.
    Single-arm (``*.primary.*``, e.g. Franka 7-DoF):
      [0:7] joints  [7] gripper  [8:14] pad | [14:17] xyz  [17:23] rot6d | [23:32] unused.
    Rotation is Stage-A ``rotation_6d`` (no quaternion conversion).
    """
    from robot_data_processing.schema import QUALITY_ROT6D_DIM

    if T is None:
        for v in fields.values():
            T = int(np.asarray(v).shape[0])
            break
    if T is None or T <= 0:
        raise ValueError("empty standard fields")

    def _put(dst: np.ndarray, sl: slice, arr: np.ndarray | None) -> None:
        if arr is None:
            return
        block = np.asarray(arr, dtype=np.float64)
        if block.ndim == 1:
            block = block.reshape(-1, 1)
        n = min(block.shape[1], sl.stop - sl.start)
        dst[:, sl.start : sl.start + n] = block[:, :n]

    state = np.zeros((T, QUALITY_ROT6D_DIM), dtype=np.float64)
    action = np.zeros((T, QUALITY_ROT6D_DIM), dtype=np.float64)

    primary_j = fields.get("observation.state.arm.primary.joint_position")
    if primary_j is not None and fields.get("observation.state.arm.left.joint_position") is None:
        def _pack_primary(dst: np.ndarray, prefix: str) -> None:
            _put(dst, slice(0, 7), fields.get(f"{prefix}.arm.primary.joint_position"))
            _put(dst, slice(7, 8), fields.get(f"{prefix}.gripper.primary.closedness"))
            _put(dst, slice(14, 17), fields.get(f"{prefix}.eef.primary.position"))
            _put(dst, slice(17, 23), fields.get(f"{prefix}.eef.primary.rotation_6d"))

        _pack_primary(state, "observation.state")
        _pack_primary(action, "action")
        return state, action

    def _left_or_primary(left_key: str):
        val = fields.get(left_key)
        if val is not None:
            return val
        return fields.get(left_key.replace(".left.", ".primary."))

    _put(state, slice(0, 6), _left_or_primary("observation.state.arm.left.joint_position"))
    _put(state, slice(6, 12), fields.get("observation.state.arm.right.joint_position"))
    _put(state, slice(12, 13), _left_or_primary("observation.state.gripper.left.closedness"))
    _put(state, slice(13, 14), fields.get("observation.state.gripper.right.closedness"))
    _put(state, slice(14, 17), _left_or_primary("observation.state.eef.left.position"))
    _put(state, slice(17, 23), _left_or_primary("observation.state.eef.left.rotation_6d"))
    _put(state, slice(23, 26), fields.get("observation.state.eef.right.position"))
    _put(state, slice(26, 32), fields.get("observation.state.eef.right.rotation_6d"))
    _put(action, slice(0, 6), _left_or_primary("action.arm.left.joint_position"))
    _put(action, slice(6, 12), fields.get("action.arm.right.joint_position"))
    _put(action, slice(12, 13), _left_or_primary("action.gripper.left.closedness"))
    _put(action, slice(13, 14), fields.get("action.gripper.right.closedness"))
    _put(action, slice(14, 17), _left_or_primary("action.eef.left.position"))
    _put(action, slice(17, 23), _left_or_primary("action.eef.left.rotation_6d"))
    _put(action, slice(23, 26), fields.get("action.eef.right.position"))
    _put(action, slice(26, 32), fields.get("action.eef.right.rotation_6d"))
    return state, action


def _parquet_column_names(path: Path) -> list[str]:
    return list(pq.ParquetFile(path).schema_arrow.names)


def _has_standard_quality_names(names: list[str] | set[str]) -> bool:
    """True when parquet already has Dataclean standard keys (not packed vectors)."""
    return any(
        n.startswith("observation.state.arm.")
        or n.startswith("observation.state.eef.")
        or n.startswith("action.arm.")
        or n.startswith("action.eef.")
        for n in names
    )


def _table_columns_to_raw(table, columns: list[str]) -> dict[str, np.ndarray]:
    raw: dict[str, np.ndarray] = {}
    for name in columns:
        if name not in table.column_names:
            continue
        col = table.column(name)
        if hasattr(col, "combine_chunks"):
            col = col.combine_chunks()
        raw[name] = stack_column(col.to_numpy(zero_copy_only=False))
    return raw


def nonempty_quality_pair(
    data: dict[str, np.ndarray] | None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Return ``(state, action)`` when both are nonempty 2D arrays; else None."""
    if not data:
        return None
    try:
        state = np.asarray(data["state"], dtype=np.float64)
        action = np.asarray(data["action"], dtype=np.float64)
    except Exception:
        return None
    if state.ndim != 2 or action.ndim != 2:
        return None
    if min(state.shape[0], action.shape[0], state.shape[1], action.shape[1]) == 0:
        return None
    return state, action


def load_quality_arrays(
    root: Path,
    episode_index: int,
    schema: DatasetSchema = HUMANOID_SCHEMA,
    *,
    input_format: str = "auto",
    action_from_state: bool = False,
) -> dict[str, np.ndarray]:
    """Load quality-space state/action for raw or pseudo_v3 inputs.

    ``input_format=auto`` detects LeRobot v3.0 roots (pseudo or aggregated) via
    ``meta/episodes``. Aggregated v3 roots that still store packed
    ``observation.state`` / ``action`` use the raw transform path (and slice by
    ``episode_index``). Language / non-numeric columns are not loaded.

    When a dataset package adapter is installed via ``adapter_hooks``, prefer
    ``adapter.load_quality_arrays`` / ``load_raw`` (e.g. ABC-130k MCAP oneshot).
    """
    from robot_data_processing.adapter_hooks import try_adapter_load_quality

    hooked = try_adapter_load_quality(
        Path(root),
        int(episode_index),
        schema=schema,
        input_format=input_format,
        action_from_state=action_from_state,
    )
    if hooked is not None:
        if action_from_state:
            hooked = dict(hooked)
            hooked["action"] = np.asarray(hooked["state"], dtype=np.float64).copy()
        return hooked

    root = Path(root)
    path = episode_parquet_path(root, int(episode_index))
    fmt = str(input_format or "auto").lower()
    if fmt == "auto":
        from robot_data_processing.v3_layout import is_v3_layout

        fmt = "pseudo_v3" if is_v3_layout(root) else "raw"

    names = _parquet_column_names(path)
    name_set = set(names)

    if fmt in ("pseudo_v3", "v3", "standard", "aggregated_v3") and _has_standard_quality_names(names):
        # Load only numeric standard / extrinsic columns (skip language_* etc.).
        cols = [
            n
            for n in names
            if (
                n.startswith("observation.")
                or n.startswith("action.")
                or n == "episode_index"
            )
            and not n.startswith("observation.images.")
        ]
        table = pq.read_table(path, columns=cols)
        if "episode_index" in table.column_names:
            col = table.column("episode_index")
            if hasattr(col, "combine_chunks"):
                col = col.combine_chunks()
            vals = np.asarray(col.to_numpy(zero_copy_only=False)).reshape(-1).astype(np.int64)
            indices = np.flatnonzero(vals == int(episode_index))
            if indices.size == 0:
                table = table.slice(0, 0)
            elif indices.size != table.num_rows:
                table = table.take(pa.array(indices, type=pa.int64()))
        fields = _table_columns_to_raw(table, [c for c in cols if c in table.column_names])
        fields.pop("episode_index", None)
        state, action = _standard_fields_to_quality(fields, schema)
        return {"state": state, "action": action, "raw": fields, "fields": fields}

    # Packed raw path (v2.1 episode files or aggregated v3 with observation.state/action).
    cols = [schema.state_column, schema.action_column]
    for extra in schema.raw_columns:
        if extra not in cols and extra in name_set:
            cols.append(extra)
    if "episode_index" in name_set:
        cols.append("episode_index")
    table = pq.read_table(path, columns=[c for c in cols if c in name_set])
    if "episode_index" in table.column_names:
        # Slice in-place without re-reading all columns
        col = table.column("episode_index")
        if hasattr(col, "combine_chunks"):
            col = col.combine_chunks()
        vals = np.asarray(col.to_numpy(zero_copy_only=False)).reshape(-1).astype(np.int64)
        indices = np.flatnonzero(vals == int(episode_index))
        if indices.size == 0:
            table = table.slice(0, 0)
        elif indices.size != table.num_rows:
            table = table.take(pa.array(indices, type=pa.int64()))
    raw = _table_columns_to_raw(table, [c for c in cols if c in table.column_names])
    raw.pop("episode_index", None)
    if action_from_state:
        replace_action_with_state(raw, schema)
    state, action = _transform_to_canonical(raw, schema)
    return {"state": state, "action": action, "raw": raw}
