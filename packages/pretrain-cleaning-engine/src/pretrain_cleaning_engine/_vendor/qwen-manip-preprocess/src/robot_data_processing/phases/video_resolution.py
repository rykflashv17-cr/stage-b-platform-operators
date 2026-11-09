"""Stage A video-resolution census and unification (one stored size per camera).

Rules (per output camera; an episode dropped by any camera is dropped as a whole):

1. All episodes share one source resolution → pass.
2. Otherwise take the smallest resolution (pixel area, then height, width) among the
   remaining episodes; if it covers < ``min_ratio`` of them, drop those episodes.
3. Repeat 2 until the smallest remaining resolution covers ≥ ``min_ratio``.
4. If the dropped episodes exceed ``max_discard_ratio`` of the census, fail.
5. Base size = smallest remaining resolution resized to short side ``short_side``
   (``target_short_side_size``). Larger resolutions are scaled to cover the base and
   center-cropped onto it; ``K`` follows the same scale + crop
   (``video_geometry.reconcile_camera_intrinsics``).

Multi-node: ``resolution_census`` (per rank, ffprobe headers only) writes a partial,
``resolution_plan`` (rank 0) merges them into ``<stats_cache>/video_resolution/plan.json``.
Every later phase (stats, export) loads the plan and removes the dropped episodes
before any work, so stats and export see the same episode set.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from robot_data_processing.normalize.episode_frame_geometry import target_short_side_size

RESOLUTION_PHASES = ("resolution_census", "resolution_plan")
PLAN_SCHEMA = "video_resolution_plan"
PLAN_KEY = "resolution_plan"  # inside video_output_settings
DEFAULT_MIN_RATIO = 0.10
DEFAULT_MAX_DISCARD_RATIO = 0.25


def resolution_unify_config(
    media_cfg: dict[str, Any],
    *,
    is_align: bool,
    normalize_videos: bool,
    short_side: int | None,
) -> dict[str, Any] | None:
    """``media.resolution_unify``; on by default in align (Stage A) mode."""
    raw = media_cfg.get("resolution_unify")
    if isinstance(raw, bool):
        raw = {"enabled": raw}
    raw = dict(raw or {})
    if not bool(raw.get("enabled", is_align)):
        return None
    if not normalize_videos:
        print("[P0/resolution] media.normalize_videos=false: resolution unify skipped", flush=True)
        return None
    return {
        "min_ratio": float(raw.get("min_ratio", DEFAULT_MIN_RATIO)),
        "max_discard_ratio": float(raw.get("max_discard_ratio", DEFAULT_MAX_DISCARD_RATIO)),
        "short_side": int(raw.get("short_side", short_side if short_side is not None else 384)),
        "probe_workers": int(raw.get("probe_workers", 16)),
    }


def refs_fingerprint(refs: Iterable[Any]) -> str:
    h = hashlib.sha1()
    for key in sorted(str(r.key) for r in refs):
        h.update(key.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def resolution_dir(stats_cache_dir: Path | None, output_dir: Path) -> Path:
    if stats_cache_dir is not None:
        return Path(stats_cache_dir) / "video_resolution"
    return Path(output_dir) / "_video_resolution"


def census_partial_path(root: Path, shard_id: int) -> Path:
    return Path(root) / f"census_shard{int(shard_id)}.json"


def plan_path(root: Path) -> Path:
    return Path(root) / "plan.json"


def _write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- census


class _SizeProber:
    """ffprobe (header only) per source file; v3 files shared by episodes probe once."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[int, int] | None] = {}
        self._lock = threading.Lock()

    def size(self, dataset_root: Path, episode_index: int, source_key: str) -> tuple[int, int] | None:
        from robot_data_processing.phases.media import (
            probe_video_size,
            resolve_source_video,
            source_video_is_usable,
        )

        keys = [str(source_key)]
        if not str(source_key).startswith("observation.images."):
            keys.append(f"observation.images.{source_key}")
        for key in keys:
            path = resolve_source_video(Path(dataset_root), int(episode_index), key)
            if not source_video_is_usable(path):
                continue
            cache_key = str(path)
            with self._lock:
                if cache_key in self._cache:
                    return self._cache[cache_key]
            try:
                h, w = probe_video_size(path)
                out: tuple[int, int] | None = (int(w), int(h))
            except Exception as exc:
                print(f"  [resolution] probe failed {path}: {exc}", flush=True)
                out = None
            with self._lock:
                self._cache[cache_key] = out
            return out
        return None


def run_census(
    refs: list[Any],
    camera_map: dict[str, str],
    *,
    workers: int = 16,
) -> list[list[Any]]:
    """Return ``[[ref.key, {out_cam: "WxH" | None}], ...]`` in ``refs`` order.

    Bounded in-flight probes (one ffprobe subprocess per thread).
    """
    prober = _SizeProber()

    def _one(ref: Any) -> list[Any]:
        sizes: dict[str, str | None] = {}
        for src_key, out_cam in camera_map.items():
            wh = prober.size(ref.dataset_root, ref.episode_index, src_key)
            sizes[str(out_cam)] = f"{wh[0]}x{wh[1]}" if wh else None
        return [str(ref.key), sizes]

    out: list[list[Any]] = []
    n = len(refs)
    step = max(1, n // 20)
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        # pool.map keeps order and at most ``workers`` probes run at once.
        for i, rec in enumerate(pool.map(_one, refs), start=1):
            out.append(rec)
            if i % step == 0 or i == n:
                print(f"  [resolution] census {i}/{n}", flush=True)
    return out


def write_census_partial(
    root: Path,
    *,
    shard_id: int,
    num_shards: int,
    fingerprint: str,
    records: list[list[Any]],
) -> Path:
    path = census_partial_path(root, shard_id)
    _write_json_atomic(
        path,
        {
            "schema": "video_resolution_census",
            "shard_id": int(shard_id),
            "num_shards": int(num_shards),
            "fingerprint": fingerprint,
            "records": records,
        },
    )
    return path


def load_census_partials(root: Path, *, num_shards: int, fingerprint: str) -> list[list[Any]]:
    records: list[list[Any]] = []
    missing = []
    for k in range(int(num_shards)):
        path = census_partial_path(root, k)
        if not path.exists():
            missing.append(str(path))
            continue
        part = json.loads(path.read_text(encoding="utf-8"))
        if part.get("fingerprint") != fingerprint:
            raise RuntimeError(
                f"{path}: census was run on a different episode list "
                "(sample/range/quarantine changed); rerun resolution_census"
            )
        records.extend(part.get("records") or [])
    if missing:
        raise FileNotFoundError(f"Missing resolution census partials ({len(missing)}): {missing[:3]}")
    return records


# ------------------------------------------------------------------------------- plan


def _parse_wh(text: str) -> tuple[int, int]:
    w, h = str(text).lower().split("x")
    return int(w), int(h)


def _size_order(wh: tuple[int, int]) -> tuple[int, int, int]:
    return (wh[0] * wh[1], wh[1], wh[0])


def _even_ceil(x: float) -> int:
    v = int(math.ceil(x - 1e-6))
    return v + (v % 2)


def cover_crop_transform(
    src_w: int, src_h: int, base_w: int, base_h: int
) -> dict[str, int]:
    """Scale ``src`` to cover ``base`` (even dims) then center-crop onto ``base``."""
    s = max(base_w / src_w, base_h / src_h)
    sw = max(base_w, _even_ceil(src_w * s))
    sh = max(base_h, _even_ceil(src_h * s))
    return {
        "scaled_width": int(sw),
        "scaled_height": int(sh),
        "crop_x": int((sw - base_w) // 2),
        "crop_y": int((sh - base_h) // 2),
    }


def build_resolution_plan(
    records: list[list[Any]],
    *,
    min_ratio: float,
    max_discard_ratio: float,
    short_side: int,
    fingerprint: str,
) -> dict[str, Any]:
    total = len(records)
    per_cam: dict[str, dict[str, tuple[int, int]]] = {}
    missing: Counter = Counter()
    for key, sizes in records:
        for cam, wh in (sizes or {}).items():
            if wh is None:
                missing[cam] += 1
                continue
            per_cam.setdefault(str(cam), {})[str(key)] = _parse_wh(wh)

    census = {
        cam: {f"{w}x{h}": n for (w, h), n in Counter(m.values()).most_common()}
        for cam, m in per_cam.items()
    }
    alive = {str(k) for k, _ in records}
    rounds: list[dict[str, Any]] = []
    # Fixed point over cameras: a drop for one camera can shrink another's groups.
    changed = True
    while changed:
        changed = False
        for cam in sorted(per_cam):
            sizes = per_cam[cam]
            while True:
                counts = Counter(wh for k, wh in sizes.items() if k in alive)
                if len(counts) <= 1:
                    break
                n_cam = sum(counts.values())
                smallest = min(counts, key=_size_order)
                ratio = counts[smallest] / n_cam
                if ratio >= min_ratio:
                    break
                drop = {k for k, wh in sizes.items() if k in alive and wh == smallest}
                alive -= drop
                changed = True
                rounds.append(
                    {
                        "camera": cam,
                        "size": f"{smallest[0]}x{smallest[1]}",
                        "count": len(drop),
                        "ratio": round(ratio, 6),
                        "remaining_before": n_cam,
                    }
                )

    cameras: dict[str, Any] = {}
    for cam in sorted(per_cam):
        counts = Counter(wh for k, wh in per_cam[cam].items() if k in alive)
        if not counts:
            continue
        ref_w, ref_h = min(counts, key=_size_order)
        base_w, base_h = target_short_side_size(ref_w, ref_h, short_side=int(short_side))
        transforms: dict[str, Any] = {}
        for (w, h), n in sorted(counts.items(), key=lambda kv: _size_order(kv[0])):
            if (w, h) == (ref_w, ref_h):
                # Same resize as the plain short-side path: no crop.
                t = {"scaled_width": base_w, "scaled_height": base_h, "crop_x": 0, "crop_y": 0}
            else:
                t = cover_crop_transform(w, h, base_w, base_h)
            transforms[f"{w}x{h}"] = {**t, "count": int(n)}
        cameras[cam] = {
            "source_census": census.get(cam, {}),
            "missing_video": int(missing.get(cam, 0)),
            "reference_source_size": f"{ref_w}x{ref_h}",
            "base": {"width": int(base_w), "height": int(base_h)},
            "uniform_source": len(counts) == 1,
            "transforms": transforms,
        }

    discarded = sorted({str(k) for k, _ in records} - alive)
    discard_ratio = (len(discarded) / total) if total else 0.0
    status = "fail" if discard_ratio > max_discard_ratio else "pass"
    return {
        "schema": PLAN_SCHEMA,
        "status": status,
        "fingerprint": fingerprint,
        "rule": {
            "scope": "per output camera; episode dropped when any camera drops it",
            "smallest": "pixel area, then height, width",
            "min_ratio": float(min_ratio),
            "ratio_denominator": "episodes still kept that have this camera",
            "max_discard_ratio": float(max_discard_ratio),
            "short_side": int(short_side),
            "larger_sizes": "scale to cover base, center crop to base; K scaled and principal point shifted",
        },
        "total_episodes": int(total),
        "kept_episodes": int(len(alive)),
        "discarded_episodes": int(len(discarded)),
        "discard_ratio": round(discard_ratio, 6),
        "rounds": rounds,
        "cameras": cameras,
        "discarded": discarded,
    }


def check_plan(plan: dict[str, Any]) -> None:
    if plan.get("status") != "pass":
        raise RuntimeError(
            "[resolution] FAIL: discarded "
            f"{plan.get('discarded_episodes')}/{plan.get('total_episodes')} episodes "
            f"({float(plan.get('discard_ratio', 0)) * 100:.2f}%) > "
            f"{float(plan['rule']['max_discard_ratio']) * 100:.0f}%; rounds={plan.get('rounds')}"
        )


def log_plan(plan: dict[str, Any]) -> None:
    print(
        f"[P0/resolution] status={plan['status']} total={plan['total_episodes']} "
        f"kept={plan['kept_episodes']} discarded={plan['discarded_episodes']} "
        f"({float(plan['discard_ratio']) * 100:.2f}%)",
        flush=True,
    )
    for r in plan.get("rounds") or []:
        print(
            f"  drop {r['camera']} {r['size']}: {r['count']} eps "
            f"({r['ratio'] * 100:.2f}% of {r['remaining_before']})",
            flush=True,
        )
    for cam, c in (plan.get("cameras") or {}).items():
        print(
            f"  {cam}: base={c['base']['width']}x{c['base']['height']} "
            f"ref={c['reference_source_size']} sizes={list(c['transforms'])}",
            flush=True,
        )


def camera_transforms(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """Compact per-camera view shipped to P6/P7 (no discard list)."""
    if not plan:
        return None
    return {
        cam: {"base": c["base"], "transforms": c["transforms"]}
        for cam, c in (plan.get("cameras") or {}).items()
    }


def planned_transform(
    cam_plan: dict[str, Any], src_w: int, src_h: int, *, camera: str = ""
) -> dict[str, int]:
    """Scale + crop for one source size; unknown sizes are a hard error."""
    t = (cam_plan.get("transforms") or {}).get(f"{int(src_w)}x{int(src_h)}")
    if t is None:
        raise ValueError(
            f"video size {src_w}x{src_h} of {camera} is not in the resolution plan "
            f"(known: {sorted(cam_plan.get('transforms') or {})})"
        )
    base = cam_plan["base"]
    return {
        "width": int(base["width"]),
        "height": int(base["height"]),
        "scaled_width": int(t["scaled_width"]),
        "scaled_height": int(t["scaled_height"]),
        "crop_x": int(t["crop_x"]),
        "crop_y": int(t["crop_y"]),
    }


def info_summary(plan: dict[str, Any], path: Path | None) -> dict[str, Any]:
    return {
        "schema": PLAN_SCHEMA,
        "rule": plan["rule"],
        "total_episodes": plan["total_episodes"],
        "kept_episodes": plan["kept_episodes"],
        "discarded_episodes": plan["discarded_episodes"],
        "discard_ratio": plan["discard_ratio"],
        "rounds": plan["rounds"],
        "cameras": plan["cameras"],
        "plan_path": str(path) if path is not None else None,
    }


def load_or_build_plan(
    *,
    root: Path,
    stats_refs: list[Any],
    camera_map: dict[str, str],
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], Path]:
    """Load the rank-0 plan; without one (single-node runs) census inline and save it."""
    fp = refs_fingerprint(stats_refs)
    path = plan_path(root)
    if path.exists():
        plan = json.loads(path.read_text(encoding="utf-8"))
        if plan.get("fingerprint") == fp:
            print(f"[P0/resolution] using plan {path}", flush=True)
            return plan, path
        print(
            f"[P0/resolution] plan {path} is for a different episode list; recomputing inline",
            flush=True,
        )
    print(
        f"[P0/resolution] no plan: inline census of {len(stats_refs)} episodes "
        "(multi-node runs should use --stats-phase resolution_census/resolution_plan)",
        flush=True,
    )
    records = run_census(stats_refs, camera_map, workers=cfg["probe_workers"])
    plan = build_resolution_plan(
        records,
        min_ratio=cfg["min_ratio"],
        max_discard_ratio=cfg["max_discard_ratio"],
        short_side=cfg["short_side"],
        fingerprint=fp,
    )
    _write_json_atomic(path, plan)
    return plan, path


def run_resolution_phase(
    *,
    phase: str,
    root: Path,
    refs: list[Any],
    stats_refs: list[Any],
    camera_map: dict[str, str],
    cfg: dict[str, Any],
    shard_id: int | None,
    num_shards: int | None,
) -> dict[str, Any]:
    fp = refs_fingerprint(stats_refs)
    if phase == "resolution_census":
        sid = int(shard_id) if shard_id is not None else 0
        ns = int(num_shards) if num_shards is not None else 1
        records = run_census(refs, camera_map, workers=cfg["probe_workers"])
        path = write_census_partial(
            root, shard_id=sid, num_shards=ns, fingerprint=fp, records=records
        )
        return {"phase": phase, "shard_id": sid, "num_episodes": len(records), "wrote": str(path)}
    if phase == "resolution_plan":
        if num_shards is None or int(num_shards) < 1:
            raise ValueError("resolution_plan requires --num-shards")
        records = load_census_partials(root, num_shards=int(num_shards), fingerprint=fp)
        got = {str(k) for k, _ in records}
        want = {str(r.key) for r in stats_refs}
        if got != want:
            raise RuntimeError(
                f"resolution census covers {len(got)} episodes, expected {len(want)} "
                f"(missing={len(want - got)} extra={len(got - want)})"
            )
        plan = build_resolution_plan(
            records,
            min_ratio=cfg["min_ratio"],
            max_discard_ratio=cfg["max_discard_ratio"],
            short_side=cfg["short_side"],
            fingerprint=fp,
        )
        path = plan_path(root)
        _write_json_atomic(path, plan)
        log_plan(plan)
        check_plan(plan)
        return {
            "phase": phase,
            "wrote": str(path),
            "total_episodes": plan["total_episodes"],
            "discarded_episodes": plan["discarded_episodes"],
            "discard_ratio": plan["discard_ratio"],
        }
    raise ValueError(f"Unsupported resolution phase {phase!r}; expected {RESOLUTION_PHASES}")
