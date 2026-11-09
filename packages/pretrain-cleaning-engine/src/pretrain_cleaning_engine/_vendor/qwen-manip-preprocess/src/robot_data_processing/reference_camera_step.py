"""Reference-camera single-frame step detection (REPORT EGX-R1).

A moving reference (head) camera whose pose jumps within one frame (tracking relocalization,
recording break) makes ``T_Episode_CameraReference`` and the ego action discontinuous, so the
episode is dropped. Hold-aware: frames whose pose repeats the previous one (pose stream slower
than video) are collapsed first and each real update is judged by its per-frame speed over the
frames it spans, so a held pose catching up is not a step.

A step at update k is flagged when
  translation > min_translation_m and > isolation_ratio x median(translation of the
  neighbour_frames updates on each side), or
  rotation > min_rotation_deg and > isolation_ratio x median(neighbour rotation).
Calibration: review/audits/egodex/20261010_stageA_261009_hand/STAGE_B_RULE_EGX_R1.md.

Config (``episode_geometry.extrinsics.reference_step``)::

    reference_step:
      enabled: true
      min_translation_m: 0.03
      min_rotation_deg: 10.0
      isolation_ratio: 5.0
      neighbour_frames: 5
"""
from __future__ import annotations

from typing import Any

import numpy as np

from robot_data_processing.frame_removal import REFERENCE_CAMERA_STEP

# Consecutive poses whose 4x4 entries differ by at most this (rotation entries and metres) count
# as one held pose. EgoStandard float32 holds sit at 1e-8..1e-5, real head motion at >= 3e-5
# (mostly > 1e-4) per frame (150 sampled episodes, 2026-10-12). Entries are compared directly:
# inv(T_t) @ T_t+1 on float32 poses leaves ~1e-7 roundoff even for exact repeats.
POSE_REPEAT_TOL = 1e-5
# Raw-pose check: only exact repeats count as held. A frozen pose drifts by 1e-9..1e-5 before a
# relocalization; with POSE_REPEAT_TOL that freeze would be merged and the jump averaged over
# it, so the raw poses are also judged with this strict tolerance (2026-10-12 video check:
# pose rotation 1.5-1.9x the optical-flow rotation on those episodes).
RAW_REPEAT_TOL = 1e-9


def pose_repeats(T: np.ndarray, tol: float = POSE_REPEAT_TOL) -> np.ndarray:
    """(N-1,) bool: frame t+1 repeats frame t (every 4x4 entry within ``tol``)."""
    T = np.asarray(T, dtype=np.float64).reshape(-1, 16)
    if len(T) < 2:
        return np.zeros(0, dtype=bool)
    return np.abs(np.diff(T, axis=0)).max(axis=1) <= tol


def parse_reference_step_config(extr_cfg: dict[str, Any] | None) -> dict[str, float] | None:
    cfg = (extr_cfg or {}).get("reference_step")
    if not cfg or not cfg.get("enabled", True):
        return None
    return {
        "min_translation_m": float(cfg.get("min_translation_m", 0.03)),
        "min_rotation_deg": float(cfg.get("min_rotation_deg", 10.0)),
        "isolation_ratio": float(cfg.get("isolation_ratio", 5.0)),
        "neighbour_frames": int(cfg.get("neighbour_frames", 5)),
    }


def _neighbour_median(x: np.ndarray, k: int, w: int) -> float:
    nb = np.r_[x[max(k - w, 0) : k], x[k + 1 : k + 1 + w]]
    return float(np.median(nb)) if nb.size else 0.0


def reference_step_reason(
    T: np.ndarray, cfg: dict[str, float] | None, *, repeat_tol: float = POSE_REPEAT_TOL, tag: str = ""
) -> str | None:
    """Discard reason ``reference_camera_step:[<tag>:]<cm>cm/<deg>deg@<frame>`` or None.

    ``repeat_tol`` sets which consecutive poses are merged as held before the per-frame speed
    is computed (POSE_REPEAT_TOL for repaired poses, RAW_REPEAT_TOL for the raw stream).
    """
    if cfg is None:
        return None
    T = np.asarray(T, dtype=np.float64).reshape(-1, 4, 4)
    if len(T) < 3:
        return None
    keep = np.r_[0, 1 + np.flatnonzero(~pose_repeats(T, repeat_tol))]
    if len(keep) < 2:
        return None
    gap = np.maximum(np.diff(keep).astype(np.float64), 1.0)
    Tk = T[keep]
    s = np.linalg.norm(np.diff(Tk[:, :3, 3], axis=0), axis=1) / gap
    cos = (np.einsum("nij,nij->n", Tk[:-1, :3, :3], Tk[1:, :3, :3]) - 1.0) / 2.0
    a = np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))) / gap
    w, iso = int(cfg["neighbour_frames"]), cfg["isolation_ratio"]
    min_m, min_deg = cfg["min_translation_m"], cfg["min_rotation_deg"]
    hits = [
        int(k)
        for k in np.flatnonzero((s > min_m) | (a > min_deg))
        if (s[k] > min_m and s[k] > iso * max(_neighbour_median(s, k, w), 1e-4))
        or (a[k] > min_deg and a[k] > iso * max(_neighbour_median(a, k, w), 1e-3))
    ]
    if not hits:
        return None
    k = max(hits, key=lambda i: s[i])
    return (
        f"{REFERENCE_CAMERA_STEP}:{tag + ':' if tag else ''}{s[k] * 100:.1f}cm/{a[k]:.1f}deg@{int(keep[hits[0]])}"
        f" steps={len(hits)}"
    )
