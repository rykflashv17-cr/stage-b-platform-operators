"""Load per-episode calibration JSON and map keys via dataset keymap."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

CALIBRATION_BUNDLE = "calibration_bundle_optimized.json"


def calibration_bundle_path(dataset_root: Path, episode_index: int) -> Path:
    chunk = episode_index // 1000
    return (
        Path(dataset_root)
        / "parameters"
        / f"chunk-{chunk:03d}"
        / f"episode_{episode_index:06d}"
        / CALIBRATION_BUNDLE
    )


def get_by_dotted_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def load_calibration_bundle(dataset_root: Path, episode_index: int) -> dict[str, Any] | None:
    path = calibration_bundle_path(dataset_root, episode_index)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def map_calibration_from_keymap(
    calib: dict[str, Any],
    keymap: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Map source calibration JSON → standard intrinsic / extrinsic dicts.

    Intrinsic keys keep keymap destination names (e.g. ``intrinsic.camera_top.matrix``).
    Extrinsic keys use keymap destination names without a leading ``extrinsic.`` prefix
    (e.g. ``camera_top.T_ArmLeft_CameraTop``), matching review_corrections.
    """
    intrinsic: dict[str, Any] = {}
    for dst, src_path in (keymap.get("intrinsic") or {}).items():
        val = get_by_dotted_path(calib, str(src_path))
        if val is None:
            continue
        arr = np.asarray(val, dtype=np.float64)
        intrinsic[str(dst)] = arr.tolist()

    extrinsic: dict[str, np.ndarray] = {}
    for dst, src_path in (keymap.get("extrinsic") or {}).items():
        val = get_by_dotted_path(calib, str(src_path))
        if val is None:
            continue
        mat = np.asarray(val, dtype=np.float64)
        if mat.shape != (4, 4):
            mat = mat.reshape(4, 4)
        key = str(dst)
        if key.startswith("extrinsic."):
            key = key[len("extrinsic.") :]
        extrinsic[key] = mat
    return intrinsic, extrinsic


def load_mapped_calibration(
    dataset_root: Path,
    episode_index: int,
    keymap: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, np.ndarray], Path | None]:
    """Return (intrinsic, extrinsic, bundle_path_or_none)."""
    keymap = keymap or {}
    path = calibration_bundle_path(dataset_root, episode_index)
    calib = load_calibration_bundle(dataset_root, episode_index)
    if calib is None:
        return {}, {}, None
    intrinsic, extrinsic = map_calibration_from_keymap(calib, keymap)
    return intrinsic, extrinsic, path
