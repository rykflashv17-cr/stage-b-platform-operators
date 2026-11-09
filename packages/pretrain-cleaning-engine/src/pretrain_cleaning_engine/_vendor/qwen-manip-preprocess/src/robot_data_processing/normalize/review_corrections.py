"""Apply human-review corrections declared in dataset ``config.yaml``.

Interface (``datasets/*/config.yaml`` → ``review_corrections``)
--------------------------------------------------------------
``extrinsic_rotation_correction``
    After ``eef_direction`` review: per-arm rotation corrections for **only**
    these two extrinsic matrices (other extrinsics are untouched)::

        extrinsic_rotation_correction:
          camera_top.T_ArmLeft_CameraTop:  R_left_or_null
          camera_top.T_ArmRight_CameraTop: R_right_or_null

    Each ``R`` is a 3x3 or 4x4 matrix. Pipeline applies::

        T_corrected = R_4x4 @ T_original

    Use ``null`` (or omit / identity) for an arm that needs no correction.

``gripper_closedness_correction``
    After ``gripper_closedness`` review: separate affine maps for action vs state::

        gripper_closedness_correction:
          action: {scale: a, offset: b, clip: true}
          state:  {scale: c, offset: d, clip: true}

    Each side may be ``null`` (no correction). Pipeline applies only to fields whose
    names match ``action.gripper.*.closedness`` or ``observation.state.gripper.*.closedness``.

    When ``clip`` is true (default), corrected values are clipped to ``[0, 1]``.

Keys may live under ``review_corrections:`` or at the config top level.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from robot_data_processing.normalize.standard_types import StandardEpisode

_IDENTITY_4 = np.eye(4, dtype=np.float64)

EEF_DIRECTION_EXTRINSIC_KEYS: tuple[str, str] = (
    "camera_top.T_ArmLeft_CameraTop",
    "camera_top.T_ArmRight_CameraTop",
)

GRIPPER_CORRECTION_GROUPS: tuple[str, str] = ("action", "state")

_GRIPPER_GROUP_PREFIX: dict[str, str] = {
    "action": "action.gripper.",
    "state": "observation.state.gripper.",
}


def _as_homogeneous_rotation(matrix: Any) -> np.ndarray | None:
    if matrix is None:
        return None
    arr = np.asarray(matrix, dtype=np.float64)
    if arr.size == 0:
        return None
    if arr.shape == (4, 4):
        R = arr.copy()
    elif arr.shape == (3, 3):
        R = np.eye(4, dtype=np.float64)
        R[:3, :3] = arr
    else:
        raise ValueError(
            f"extrinsic rotation correction must be 3x3 or 4x4, got shape {arr.shape}"
        )
    if np.allclose(R, _IDENTITY_4):
        return None
    return R


def _parse_gripper_affine(spec: Any) -> tuple[float, float, bool] | None:
    if spec is None:
        return None
    if isinstance(spec, (list, tuple)) and len(spec) >= 2:
        scale, offset = float(spec[0]), float(spec[1])
        clip = True
    elif isinstance(spec, dict):
        if "scale" not in spec and "offset" not in spec:
            return None
        scale = float(spec.get("scale", 1.0))
        offset = float(spec.get("offset", 0.0))
        clip = bool(spec.get("clip", True))
    else:
        raise ValueError(
            "gripper closedness affine must be null, [scale, offset], "
            "or {scale, offset, clip?}"
        )
    if abs(scale - 1.0) < 1e-12 and abs(offset) < 1e-12:
        return None
    return scale, offset, clip


def _parse_gripper_correction_map(spec: Any) -> dict[str, tuple[float, float, bool] | None]:
    empty = {g: None for g in GRIPPER_CORRECTION_GROUPS}
    if spec is None:
        return empty
    if not isinstance(spec, dict):
        raise ValueError(
            "gripper_closedness_correction must be null or "
            '{"action": {...}|null, "state": {...}|null}'
        )
    if "action" not in spec and "state" not in spec and (
        "scale" in spec or "offset" in spec or isinstance(spec.get("clip"), bool)
    ):
        affine = _parse_gripper_affine(spec)
        return {g: affine for g in GRIPPER_CORRECTION_GROUPS}
    unknown = set(spec) - set(GRIPPER_CORRECTION_GROUPS)
    if unknown:
        raise ValueError(
            "gripper_closedness_correction only allows keys "
            f"{list(GRIPPER_CORRECTION_GROUPS)}; got unexpected: {sorted(unknown)}"
        )
    return {g: _parse_gripper_affine(spec.get(g)) for g in GRIPPER_CORRECTION_GROUPS}


def _parse_extrinsic_rotation_map(spec: Any) -> dict[str, Any]:
    if spec is None:
        return {k: None for k in EEF_DIRECTION_EXTRINSIC_KEYS}
    if not isinstance(spec, dict):
        raise ValueError(
            "extrinsic_rotation_correction must be null or a dict with keys "
            f"{list(EEF_DIRECTION_EXTRINSIC_KEYS)} (each a 3x3/4x4 R or null)"
        )
    unknown = set(spec) - set(EEF_DIRECTION_EXTRINSIC_KEYS)
    if unknown:
        raise ValueError(
            "extrinsic_rotation_correction only allows "
            f"{list(EEF_DIRECTION_EXTRINSIC_KEYS)}; got unexpected keys: {sorted(unknown)}"
        )
    return {k: spec.get(k) for k in EEF_DIRECTION_EXTRINSIC_KEYS}


def load_review_corrections(config: dict[str, Any] | None) -> dict[str, Any]:
    """Load corrections from dataset config (top-level or ``review_corrections``)."""
    config = config or {}
    nested = config.get("review_corrections") or {}
    if not isinstance(nested, dict):
        nested = {}
    ext = config.get("extrinsic_rotation_correction")
    if ext is None:
        ext = nested.get("extrinsic_rotation_correction")
    grip = config.get("gripper_closedness_correction")
    if grip is None:
        grip = nested.get("gripper_closedness_correction")
    return {
        "extrinsic_rotation_correction": _parse_extrinsic_rotation_map(ext),
        "gripper_closedness_correction": _parse_gripper_correction_map(grip),
    }


def _apply_one_extrinsic(T: np.ndarray, R: np.ndarray) -> np.ndarray:
    arr = np.asarray(T, dtype=np.float64)
    if arr.shape == (4, 4):
        return R @ arr
    if arr.ndim == 3 and arr.shape[-2:] == (4, 4):
        return np.einsum("ij,tjk->tik", R, arr)
    return arr


def apply_extrinsic_rotation_correction(
    extrinsic: dict[str, np.ndarray],
    rotation_map: Any,
) -> dict[str, np.ndarray]:
    mapping = _parse_extrinsic_rotation_map(rotation_map)
    if not extrinsic:
        return extrinsic
    out = dict(extrinsic)
    for key in EEF_DIRECTION_EXTRINSIC_KEYS:
        if key not in out:
            continue
        R = _as_homogeneous_rotation(mapping.get(key))
        if R is None:
            continue
        out[key] = _apply_one_extrinsic(out[key], R)
    return out


def _apply_affine(arr: np.ndarray, scale: float, offset: float, clip: bool) -> np.ndarray:
    y = (scale * np.asarray(arr, dtype=np.float32) + offset).astype(np.float32)
    if clip:
        y = np.clip(y, 0.0, 1.0)
    return y


def apply_gripper_closedness_correction(
    fields: dict[str, np.ndarray],
    spec: Any,
) -> dict[str, np.ndarray]:
    mapping = _parse_gripper_correction_map(spec)
    if all(v is None for v in mapping.values()):
        return fields
    out = dict(fields)
    for group, affine in mapping.items():
        if affine is None:
            continue
        prefix = _GRIPPER_GROUP_PREFIX[group]
        scale, offset, clip = affine
        for name, arr in fields.items():
            if not name.startswith(prefix) or not name.endswith(".closedness"):
                continue
            out[name] = _apply_affine(arr, scale, offset, clip)
    return out


def apply_review_corrections(
    episode: StandardEpisode,
    config: dict[str, Any] | None,
) -> StandardEpisode:
    """Apply corrections from dataset ``config.yaml`` (not keymap.json)."""
    corr = load_review_corrections(config)
    grip_map = corr["gripper_closedness_correction"]
    episode.fields = apply_gripper_closedness_correction(
        episode.fields, grip_map
    )
    episode.extrinsic = apply_extrinsic_rotation_correction(
        episode.extrinsic, corr["extrinsic_rotation_correction"]
    )
    applied_ext = {
        k: _as_homogeneous_rotation(v) is not None
        for k, v in corr["extrinsic_rotation_correction"].items()
    }
    episode.meta = {
        **episode.meta,
        "review_corrections_applied": {
            "extrinsic_rotation": applied_ext,
            "gripper_closedness": {
                g: grip_map.get(g) is not None for g in GRIPPER_CORRECTION_GROUPS
            },
        },
    }
    return episode
