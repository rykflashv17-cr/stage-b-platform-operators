"""P0–P7 phase orchestrator for Dataclean dataset packages."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import yaml

from robot_data_processing.frame_alignment import check_import_frame_alignment, summarize_alignment_failure
from robot_data_processing.gates.manual_gate import (
    assert_eef_direction_or_raise,
    assert_gate_or_raise,
    extrinsic_confidence_status,
    should_export_extrinsics,
)
from robot_data_processing.loader import episode_parquet_path, read_episode_table
from robot_data_processing.meta_tasks import load_episode_task_meta, resolve_episode_tasks
from robot_data_processing.normalize.review_corrections import apply_review_corrections
from robot_data_processing.normalize.standard_types import EpisodeRef, StandardEpisode
from robot_data_processing.phases.ingest import discover_part_task, discover_single_root
from robot_data_processing.phases.video_geometry import (
    VIDEO_OUTPUT_KEY,
    reconcile_camera_intrinsics,
    resolve_calibration_geometry_cfg,
    video_output_settings,
)
from robot_data_processing.phases.params import (
    DEFAULT_HUMANOID_GOLDEN_EE_SPEED,
    check_timestamp_fps,
    compute_speed_scale,
    params_meta_from_speed,
)
from robot_data_processing.pipeline import load_config, pipeline_config_from_yaml, run_pipeline
from robot_data_processing.report import (
    build_quality_report,
    write_exclusion_log,
    write_optimal_lags,
    write_quality_report,
)
from robot_data_processing.schema import (
    SOURCE_TIMESTAMPS_COLUMN,
    SOURCE_TIMESTAMPS_FIELD,
    is_passthrough_column,
    stack_column,
)
from robot_data_processing.stages.state_action_temporal_alignment import (
    StateActionAlignStats,
    apply_p4_delay_to_standard_episode,
    parse_temporal_align_config,
    resolve_alignment_lag,
    resolve_p4_plan,
    resolve_state_action_delay,
)
from robot_data_processing.stages.stage2_trend_alignment import (
    Stage2Config,
    fill_missing_action_eef,
    parse_stage2_fill_config,
)
from robot_data_processing.stages.stage1_sudden_change import Stage1Config
from robot_data_processing.stats_distributed import STATS_PHASES, run_stats_phase
from robot_data_processing.phases import video_resolution as vres
from robot_data_processing.types import EpisodeResult


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_package_path(package_dir: Path, value: Any) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = Path(package_dir) / path
    return path


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _is_4x4_matrix_column(name: str) -> bool:
    return str(name).startswith("extrinsic.") or str(name).startswith("extra_info.extrinsic.")


def _ensure_extra_info_fields(standard, raw: dict[str, np.ndarray] | None) -> None:
    """Passthrough extra_info.* from parquet/raw if the adapter omitted them."""
    if raw is None:
        return
    for key, val in raw.items():
        name = str(key)
        if name.startswith("extra_info.") and name not in standard.fields:
            standard.fields[name] = np.asarray(val)
    _keep_source_timestamps(standard, raw)


# Stage A (mode=align) drops episodes shorter than this at the dataset's nominal fps.
DEFAULT_MIN_EPISODE_SECONDS = 2.0


def _short_episode_reason(standard, cfg: dict[str, Any], *, fps: float, is_align: bool) -> str | None:
    """Discard reason when a Stage A episode has fewer than ``min_episode_seconds * fps`` frames.

    ``pipeline.min_episode_seconds`` overrides the default; 0 disables. Stage B (identity input)
    is not re-filtered.
    """
    if not is_align:
        return None
    seconds = (cfg.get("pipeline") or {}).get("min_episode_seconds", DEFAULT_MIN_EPISODE_SECONDS)
    min_frames = int(np.ceil(float(seconds) * float(fps) - 1e-9))
    frames = int(standard.num_frames)
    if min_frames > 0 and frames < min_frames:
        return f"too_short:{frames}<{min_frames}frames({float(seconds):g}s@{float(fps):g}fps)"
    return None


def _source_timestamp_gap_reason(standard, cfg: dict[str, Any]) -> str | None:
    """Discard reason when the source clock has a gap over ``pipeline.max_source_timestamp_gap_ms``.

    The exported ``timestamp`` is a fixed-fps grid, so ``action = state[t+lag]`` would take its
    target across the gap; the whole episode is dropped instead of splitting or interpolating.
    """
    limit_ms = (cfg.get("pipeline") or {}).get("max_source_timestamp_gap_ms")
    ts = standard.fields.get(SOURCE_TIMESTAMPS_FIELD)
    if limit_ms is None or ts is None:
        return None
    dt = np.diff(np.asarray(ts, dtype=np.int64).reshape(-1))
    if not dt.size:
        return None
    max_ms = float(dt.max()) / 1e6
    if max_ms > float(limit_ms):
        at = int(np.argmax(dt))
        return f"source_timestamp_gap:{max_ms:.1f}ms>{float(limit_ms):g}ms:frame={at}->{at + 1}"
    return None


def _keep_source_timestamps(standard, raw: dict[str, np.ndarray]) -> None:
    """Carry integer source ``timestamps`` verbatim as ``extra_info.timestamps``.

    ``timestamp`` is rewritten to frame_index / fps; the real capture clock (with its
    gaps) is kept unmodified next to it and summarized in the episode audit (EGO-03).
    """
    ts = raw.get(SOURCE_TIMESTAMPS_COLUMN)
    if ts is None or SOURCE_TIMESTAMPS_FIELD in standard.fields:
        return
    ts = np.asarray(ts).reshape(-1)
    if not np.issubdtype(ts.dtype, np.integer) or ts.shape[0] != int(standard.num_frames):
        return
    ts = ts.astype(np.int64, copy=False)
    standard.fields[SOURCE_TIMESTAMPS_FIELD] = ts
    dt = np.diff(ts)
    summary: dict[str, Any] = {"first": int(ts[0]), "last": int(ts[-1])}
    if dt.size:
        median = int(np.median(dt))
        summary.update(
            median_dt=median,
            max_dt=int(dt.max()),
            min_dt=int(dt.min()),
            # steps longer than 1.5x the median frame interval
            gaps=int(np.count_nonzero(dt > 1.5 * median)) if median > 0 else None,
        )
    audit = dict(standard.meta.get("audit") or {})
    audit["source_timestamps"] = summary
    standard.meta["audit"] = audit


_STAGE_A_SOURCE_MAPS: dict[str, dict[int, dict[str, Any]]] = {}


def _stage_a_source_row(dataset_root: Path, episode_index: int) -> dict[str, Any]:
    """Stage-A ``episode_source_map.jsonl`` row (B→A→raw lineage and audit)."""
    key = str(dataset_root)
    if key not in _STAGE_A_SOURCE_MAPS:
        rows: dict[int, dict[str, Any]] = {}
        path = Path(dataset_root) / "meta" / "episode_source_map.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    rows[int(row["episode_index"])] = row
        _STAGE_A_SOURCE_MAPS[key] = rows
    return _STAGE_A_SOURCE_MAPS[key].get(int(episode_index), {})


def _table_non_frame_metadata(table) -> Any:
    """``extra_info_non_frame`` parquet schema metadata (REPORT AGI-03)."""
    meta = table.schema.metadata or {}
    payload = meta.get(b"extra_info_non_frame")
    if not payload:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload.decode("utf-8", errors="replace")


def _table_to_raw(table) -> dict[str, np.ndarray]:
    raw: dict[str, np.ndarray] = {}
    for name in table.column_names:
        col = table.column(name)
        if hasattr(col, "combine_chunks"):
            col = col.combine_chunks()
        try:
            values = col.to_numpy(zero_copy_only=False)
            if isinstance(values, np.ndarray) and values.dtype == object:
                if _is_4x4_matrix_column(name):
                    raw[name] = np.stack(
                        [np.asarray(x, dtype=np.float64).reshape(4, 4) for x in col.to_pylist()],
                        axis=0,
                    )
                else:
                    raw[name] = stack_column(values, keep_integer=is_passthrough_column(name))
            else:
                arr = np.asarray(values)
                if arr.ndim == 1 and name in (
                    "timestamp",
                    "frame_index",
                    "episode_index",
                    "index",
                    "task_index",
                ):
                    raw[name] = arr
                elif _is_4x4_matrix_column(name) and arr.ndim == 2 and arr.shape[-1] == 16:
                    raw[name] = arr.reshape(arr.shape[0], 4, 4)
                else:
                    raw[name] = arr if arr.ndim >= 1 else stack_column(values)
        except Exception:
            continue
    return raw


def _looks_standard_raw(raw: dict[str, np.ndarray]) -> bool:
    """True if this table already has Stage-A EEF fields (not source pose7 / H5)."""
    return any(
        key in raw
        for key in (
            "observation.state.eef.left.position",
            "observation.state.eef.primary.position",
            "observation.state.eef.left.rotation_6d",
            "observation.state.eef.primary.rotation_6d",
        )
    )


def _resolve_export_layout(export_cfg: dict[str, Any], *, export_shard_only: bool) -> str:
    layout = export_cfg.get("layout")
    version = export_cfg.get("lerobot_version", "aggregated_v3")
    chosen = str(layout or version or "aggregated_v3").lower()
    if export_shard_only:
        return "pseudo_v3"
    if chosen in ("v3.0", "v3"):
        v30 = export_cfg.get("v30") or {}
        if v30.get("aggregate_media") is False:
            return "pseudo_v3"
        return "aggregated_v3"
    return chosen


def _group_refs_by_root(refs: list[EpisodeRef]) -> dict[Path, list[EpisodeRef]]:
    groups: dict[Path, list[EpisodeRef]] = {}
    for ref in refs:
        groups.setdefault(ref.dataset_root, []).append(ref)
    return groups


def _resolve_quality_config(package_dir: Path, cfg: dict[str, Any], embodiment: str) -> Path:
    quality_path = cfg.get("quality_config_path")
    if quality_path:
        qpath = Path(quality_path)
        if qpath.is_absolute() and qpath.exists():
            return qpath
        dataclean_root = package_dir.parents[1]
        for cand in (dataclean_root / qpath, package_dir / quality_path, qpath):
            if cand.exists():
                return cand
    local = package_dir / "quality.yaml"
    if local.exists():
        return local
    cfg_dir = package_dir.parents[1] / "qwen-manip-preprocess" / "config"
    mapping = {
        "humanoid": cfg_dir / "humanoid_merged.yaml",
        "egodex": cfg_dir / "egodex_22T.yaml",
        "robomind_ur": cfg_dir / "robomind_ur_lerobot.yaml",
    }
    path = mapping.get(embodiment)
    if path and path.exists():
        return path
    raise FileNotFoundError(f"No quality config for embodiment={embodiment}")


def _build_adapter(package_dir: Path, adapter_factory: Callable[[], Any] | None = None):
    if adapter_factory is not None:
        return adapter_factory()
    adapter_mod = _load_module(package_dir / "adapter.py", f"dataclean_adapter_{package_dir.name}")
    if hasattr(adapter_mod, "build_adapter"):
        return adapter_mod.build_adapter(package_dir)
    for cls_name in (
        "HumanoidAdapter",
        "EgoDexAdapter",
        "RobomindAdapter",
        "EgoStandardAdapter",
        "EgoVerseAdapter",
        "AgilexH5Adapter",
    ):
        if hasattr(adapter_mod, cls_name):
            return getattr(adapter_mod, cls_name)(package_dir)
    raise RuntimeError(f"No adapter class in {package_dir / 'adapter.py'}")


def video_export_map_from_geometry_cfg(geometry_cfg: dict[str, Any]) -> dict[str, str]:
    """P6 video_export_map from yaml cameras.reference / cameras.aux (no episode needed)."""
    cameras = geometry_cfg.get("cameras") or {}
    out: dict[str, str] = {}
    ref_key = cameras.get("reference")
    if ref_key:
        out[str(ref_key)] = "camera_reference"
    for i, aux in enumerate(cameras.get("aux") or []):
        out[str(aux)] = f"camera_aux_{i}"
    return out


def _load_episode_geometry(package_dir: Path, cfg: dict[str, Any]):
    geom_cfg = cfg.get("episode_geometry") or {}
    if not geom_cfg.get("enabled", False):
        return None, geom_cfg
    mod_name = str(geom_cfg.get("module", "episode_geometry"))
    mod_path = package_dir / f"{mod_name}.py"
    if not mod_path.exists():
        print(f"[P6] episode_geometry.enabled but {mod_path} missing — skip", flush=True)
        return None, geom_cfg
    mod = _load_module(mod_path, f"dataclean_geom_{package_dir.name}")
    if hasattr(mod, "build_episode_geometry"):
        return mod.build_episode_geometry(package_dir), geom_cfg
    for cls_name in (
        "HumanoidEpisodeGeometry",
        "EgoDexEpisodeGeometry",
        "EgoStandardEpisodeGeometry",
        "AgilexH5EpisodeGeometry",
        "RCTableEpisodeGeometry",
        "EpisodeGeometry",
        "TemplateEpisodeGeometry",
    ):
        if hasattr(mod, cls_name):
            return getattr(mod, cls_name)(package_dir), geom_cfg
    raise RuntimeError(f"No episode geometry class in {mod_path}")


def _mark_export_discard(
    all_pairs: list[tuple[StandardEpisode, EpisodeResult | None]],
    index: int,
    standard: StandardEpisode,
    result: EpisodeResult | None,
    reasons: list[str],
) -> None:
    """Ensure P7 skips this episode even when quality never produced a result."""
    if result is None:
        result = EpisodeResult(
            episode_index=int(standard.episode_index),
            num_frames=int(standard.num_frames),
            discard=True,
            discard_reasons=list(reasons),
        )
        all_pairs[index] = (standard, result)
        return
    result.discard = True
    result.discard_reasons.extend(reasons)


def _run_episode_geometry(
    standard: StandardEpisode,
    *,
    ref: EpisodeRef,
    episode_geometry,
    geometry_cfg: dict[str, Any],
    keymap: dict[str, Any],
):
    """Dataset P6 plus per-episode K sizing against the real source video (EGO-02/INT-01)."""
    ep_cfg, stored_size = resolve_calibration_geometry_cfg(standard, ref, geometry_cfg)
    kwargs: dict[str, Any] = {}
    if stored_size is not None:
        kwargs["stored_video_size"] = stored_size
    geom = episode_geometry.apply_episode_frame_geometry(
        standard,
        ref=ref,
        geometry_cfg=ep_cfg,
        keymap=keymap,
        **kwargs,
    )
    if geom.discard:
        return geom
    reasons = reconcile_camera_intrinsics(
        standard,
        ref,
        geom.camera_intrinsics or {},
        dict(geom.video_export_map or {}),
        geometry_cfg,
    )
    if reasons:
        geom.discard = True
        geom.discard_reasons = list(geom.discard_reasons or []) + reasons
        return geom
    geom.geometry_meta = {
        **(geom.geometry_meta or {}),
        "camera_image_sizes": {
            cam: {
                k: rec[k]
                for k in ("source_calibration_image_size", "source_video_size", "stored_image_size")
                if k in rec
            }
            for cam, rec in (geom.camera_intrinsics or {}).items()
        },
    }
    return geom


def _apply_p6_one_episode(
    standard: StandardEpisode,
    result: EpisodeResult | None,
    *,
    episode_geometry,
    geometry_cfg: dict[str, Any],
    keymap: dict[str, Any],
    ref: EpisodeRef | None,
) -> tuple[StandardEpisode, EpisodeResult | None, dict[str, str], dict[str, Any], list[str]]:
    """Apply P6 to one episode. On hard failure, ``result.discard`` is set."""
    if ref is None:
        ref = EpisodeRef(
            episode_index=standard.episode_index,
            dataset_root=Path(standard.meta.get("dataset_root", ".")),
        )
    try:
        geom = _run_episode_geometry(
            standard,
            ref=ref,
            episode_geometry=episode_geometry,
            geometry_cfg=geometry_cfg,
            keymap=keymap,
        )
    except Exception as exc:
        print(f"  [P6] discard ep{standard.episode_index}: {exc}", flush=True)
        dummy = [(standard, result)]
        _mark_export_discard(dummy, 0, standard, result, [f"p6_geometry_error:{exc}"])
        return dummy[0][0], dummy[0][1], {}, {}, []

    if geom.discard:
        print(
            f"  [P6] discard ep{standard.episode_index}: {geom.discard_reasons}",
            flush=True,
        )
        dummy = [(standard, result)]
        _mark_export_discard(dummy, 0, standard, result, list(geom.discard_reasons))
        return dummy[0][0], dummy[0][1], {}, {}, []

    standard.fields = geom.fields
    standard.extrinsic = geom.extrinsic
    standard.camera_keys = geom.camera_keys
    standard.intrinsic = {}
    standard.meta = {
        **standard.meta,
        "camera_intrinsics": geom.camera_intrinsics,
        "episode_frame_definition": geom.episode_frame_definition,
        "geometry_meta": geom.geometry_meta,
    }
    return (
        standard,
        result,
        dict(geom.video_export_map or {}),
        dict(geom.info_extras or {}),
        list(geom.wrist_view_cameras or []),
    )


def _standardize_one_ref(
    *,
    ref: EpisodeRef,
    adapter: Any,
    identity_p1: bool,
    use_adapter_loader: bool,
    is_align: bool,
    result: EpisodeResult | None,
    camera_map: dict[str, str],
    require_top: bool,
    layout: str,
    cfg: dict[str, Any],
    yaml_cfg: dict[str, Any],
    fill_missing_action_eef_flag: bool,
    align_lag: int,
    p4_plan: Any,
    state_action_delay: int,
    fps: float,
    embodiment: str,
    speed_cfg: dict[str, Any],
    episode_tasks_map: Any,
    task_index_to_name: dict[Any, Any],
    ep_row_by_index: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """P1: load one episode and convert to StandardEpisode."""
    table_non_frame = None
    if use_adapter_loader:
        try:
            raw = adapter.load_raw(ref)
        except Exception as exc:
            print(f"  [P1] skip ep{ref.episode_index}: adapter load_raw failed: {exc}", flush=True)
            return {"status": "skip", "reason": f"load_raw:{exc}"}
        if not raw or int(np.asarray(next(iter(raw.values()))).shape[0]) == 0:
            print(f"  [P1] skip ep{ref.episode_index}: empty adapter episode", flush=True)
            return {"status": "skip", "reason": "empty"}
    else:
        path = episode_parquet_path(ref.dataset_root, ref.episode_index)
        if not path.exists():
            return {"status": "skip", "reason": "missing_parquet"}
        table = read_episode_table(path, episode_index=ref.episode_index)
        raw = _table_to_raw(table)
        table_non_frame = _table_non_frame_metadata(table)
        if table.num_rows == 0:
            print(f"  [P1] skip ep{ref.episode_index}: empty after episode slice", flush=True)
            return {"status": "skip", "reason": "empty"}
    parquet_task_index = None
    if "task_index" in raw:
        try:
            parquet_task_index = int(np.asarray(raw["task_index"]).reshape(-1)[0])
        except Exception:
            parquet_task_index = None
    tasks_list = resolve_episode_tasks(
        episode_index=ref.episode_index,
        episode_tasks=episode_tasks_map,
        task_index_to_name=task_index_to_name,
        fallback_task=ref.task,
        parquet_task_index=parquet_task_index,
    )
    meta: dict[str, Any] = {
        "fps": fps,
        "embodiment": embodiment,
        "dataset_root": str(ref.dataset_root),
        "part": ref.part,
        "task": ref.task or (tasks_list[0] if tasks_list else None),
        "tasks": tasks_list,
        "rel_path": ref.rel_path,
    }
    if identity_p1:
        row = ep_row_by_index.get(ref.episode_index) or {}
        if row.get("camera_intrinsics") is not None:
            meta["camera_intrinsics"] = row["camera_intrinsics"]
        if row.get("tasks"):
            meta["tasks"] = list(row["tasks"]) if not isinstance(row["tasks"], list) else row["tasks"]
        meta["source_episode_index"] = ref.episode_index
        meta["camera_keys"] = list(camera_map.keys())
        # Keep Stage-A lineage and audit so B→A→raw stays traceable.
        stage_a_row = _stage_a_source_row(Path(ref.dataset_root), ref.episode_index)
        if stage_a_row:
            meta["audit"] = {"stage_a": stage_a_row}
    if table_non_frame is not None:
        meta["extra_info_non_frame"] = table_non_frame
    try:
        use_identity = identity_p1 or _looks_standard_raw(raw)
        if use_identity and hasattr(adapter, "to_standard_episode_from_aligned"):
            standard = adapter.to_standard_episode_from_aligned(ref, raw, meta)
        else:
            standard = adapter.to_standard_episode(ref, raw, meta)
    except Exception as exc:
        print(f"  [P1] skip ep{ref.episode_index}: {exc}", flush=True)
        return {"status": "skip", "reason": str(exc)}

    _ensure_extra_info_fields(standard, raw)
    gap_reason = _short_episode_reason(
        standard, cfg, fps=fps, is_align=is_align
    ) or _source_timestamp_gap_reason(standard, cfg)
    if gap_reason:
        print(f"  [P1] discard ep{ref.episode_index}: {gap_reason}", flush=True)
        if result is not None:
            result.discard = True
            result.discard_reasons.append(gap_reason)
        return {"status": "discard", "reason": gap_reason, "result": result}
    if not identity_p1:
        standard = apply_review_corrections(standard, cfg)
    # MCAP/H5 have no on-disk mp4 until resolve_source_video materializes cache.
    require_videos = (layout not in ("mcap", "h5")) and bool(
        require_top or standard.camera_keys
    )
    import_root = Path(standard.meta.get("dataset_root") or ref.dataset_root)
    import_ep = int(standard.meta.get("source_episode_index", ref.episode_index))
    import_align = check_import_frame_alignment(
        raw=raw,
        standard_num_frames=int(standard.num_frames),
        fields=standard.fields,
        extrinsic=standard.extrinsic,
        dataset_root=import_root,
        episode_index=import_ep,
        camera_keys=list(standard.camera_keys),
        require_videos=require_videos,
    )
    if not import_align.ok:
        reason = summarize_alignment_failure(import_align, stage="import")
        print(f"  [P1] discard ep{ref.episode_index}: {reason}", flush=True)
        r = result
        if r is not None:
            r.discard = True
            r.discard_reasons.append(reason)
        return {"status": "discard", "reason": reason, "result": r}

    s2_fill_cfg = Stage2Config(fill_missing_action_eef=fill_missing_action_eef_flag)
    q_s2 = yaml_cfg.get("stage2") or {}
    if q_s2:
        align = q_s2.get("alignment") or {}
        smooth = q_s2.get("smoothing") or {}
        s2_fill_cfg.max_lag_frames = int(align.get("max_lag_frames", s2_fill_cfg.max_lag_frames))
        s2_fill_cfg.diff_epsilon = float(align.get("diff_epsilon", s2_fill_cfg.diff_epsilon))
        s2_fill_cfg.min_active_samples = int(
            align.get("min_active_samples", s2_fill_cfg.min_active_samples)
        )
        s2_fill_cfg.median_kernel = int(smooth.get("median_kernel", s2_fill_cfg.median_kernel))
        s2_fill_cfg.savgol_window = int(smooth.get("savgol_window", s2_fill_cfg.savgol_window))
        s2_fill_cfg.savgol_polyorder = int(
            smooth.get("savgol_polyorder", s2_fill_cfg.savgol_polyorder)
        )
        s2_fill_cfg.action_type = str(q_s2.get("action_type", s2_fill_cfg.action_type))
    s2_fill_cfg.fill_lag = int(align_lag)
    _, filled_eef, fill_lags = fill_missing_action_eef(
        standard.fields, s2_fill_cfg, fill_lag=align_lag
    )
    standard.meta = {
        **standard.meta,
        "stage2_filled_action_eef": filled_eef,
        "stage2_fill_lags": fill_lags,
    }
    # P4: shift action; tail rows repeat the last valid action (no frames dropped).
    apply_p4_delay_to_standard_episode(
        standard, p4_plan, lag=align_lag, target_delay=1
    )
    standard.meta = {
        **standard.meta,
        "state_action_delay": state_action_delay,
        "p4_plan": p4_plan.name,
        "source_episode_index": standard.meta.get("source_episode_index", ref.episode_index),
    }
    if require_top and not standard.require_camera_top():
        print(f"  [P1] discard ep{ref.episode_index}: missing camera_top", flush=True)
        r = result
        if r is not None:
            r.discard = True
            r.discard_reasons.append("missing_camera_top")
        return {"status": "discard", "reason": "missing_camera_top", "result": r}

    ts = raw.get("timestamp")
    fps_check = check_timestamp_fps(ts, expected_fps=fps)
    if not fps_check.ok:
        r = result
        if r is not None:
            r.discard = True
            r.discard_reasons.append(f"fps_unstable:{fps_check.message}")
        if is_align:
            print(f"  [P1] discard ep{ref.episode_index}: {fps_check.message}", flush=True)
        return {
            "status": "discard",
            "reason": fps_check.message,
            "result": r,
            "fps_fail": True,
        }

    speed_meta = None
    xyz = standard.fields.get("action.eef.left.position")
    if xyz is None:
        xyz = standard.fields.get("action.eef.primary.position")
    if speed_cfg.get("enabled") and xyz is not None:
        sp = compute_speed_scale(
            xyz,
            fps,
            golden_speed_mps=float(
                speed_cfg.get("golden_speed_mps", DEFAULT_HUMANOID_GOLDEN_EE_SPEED)
            ),
            target_fps=float(speed_cfg.get("target_fps", 30)),
        )
        standard.meta.update(params_meta_from_speed(sp))
        speed_meta = params_meta_from_speed(sp)

    return {
        "status": "ok",
        "standard": standard,
        "result": result,
        "speed_meta": speed_meta,
        "fps_fail": False,
    }


def _apply_p6_geometry(
    all_pairs: list[tuple[StandardEpisode, EpisodeResult | None]],
    *,
    episode_geometry,
    geometry_cfg: dict[str, Any],
    keymap: dict[str, Any],
    refs_by_ep: dict[int, EpisodeRef],
) -> tuple[dict[str, str], dict[str, Any], list[str]]:
    """Run P6 on all episodes; return (video_export_map, info_extras, wrist_view)."""
    video_export_map: dict[str, str] = {}
    info_extras: dict[str, Any] = {}
    wrist_view: list[str] = []
    geom_discards = 0

    for idx, (standard, result) in enumerate(all_pairs):
        ref = refs_by_ep.get(standard.episode_index)
        if ref is None:
            ref = EpisodeRef(
                episode_index=standard.episode_index,
                dataset_root=Path(standard.meta.get("dataset_root", ".")),
            )
        try:
            geom = _run_episode_geometry(
                standard,
                ref=ref,
                episode_geometry=episode_geometry,
                geometry_cfg=geometry_cfg,
                keymap=keymap,
            )
        except Exception as exc:
            geom_discards += 1
            print(f"  [P6] discard ep{standard.episode_index}: {exc}", flush=True)
            _mark_export_discard(
                all_pairs,
                idx,
                standard,
                result,
                [f"p6_geometry_error:{exc}"],
            )
            continue

        if geom.discard:
            geom_discards += 1
            print(
                f"  [P6] discard ep{standard.episode_index}: {geom.discard_reasons}",
                flush=True,
            )
            _mark_export_discard(
                all_pairs,
                idx,
                standard,
                result,
                list(geom.discard_reasons),
            )
            continue

        standard.fields = geom.fields
        standard.extrinsic = geom.extrinsic
        standard.camera_keys = geom.camera_keys
        standard.intrinsic = {}
        standard.meta = {
            **standard.meta,
            "camera_intrinsics": geom.camera_intrinsics,
            "episode_frame_definition": geom.episode_frame_definition,
            "geometry_meta": geom.geometry_meta,
        }
        if not video_export_map:
            video_export_map = dict(geom.video_export_map)
        if not info_extras and geom.info_extras:
            info_extras = dict(geom.info_extras)
        if not wrist_view and geom.wrist_view_cameras:
            wrist_view = list(geom.wrist_view_cameras)

    print(
        f"[P6] episode geometry applied; discards={geom_discards} "
        f"video_map={video_export_map}",
        flush=True,
    )
    return video_export_map, info_extras, wrist_view


def _apply_episode_range(
    refs: list[EpisodeRef], episode_range: tuple[int, int] | None
) -> list[EpisodeRef]:
    if episode_range is None:
        return refs
    start, end = episode_range
    out = [r for r in refs if start <= int(r.episode_index) < end]
    print(
        f"[P0] episode-range [{start}, {end}): {len(refs)} -> {len(out)}",
        flush=True,
    )
    return out


def _apply_shard(
    refs: list[EpisodeRef],
    *,
    shard_id: int | None,
    num_shards: int | None,
) -> list[EpisodeRef]:
    if shard_id is None or num_shards is None:
        return refs
    if num_shards < 1:
        raise ValueError(f"num_shards must be >= 1, got {num_shards}")
    if not (0 <= shard_id < num_shards):
        raise ValueError(f"shard_id must be in [0, {num_shards}), got {shard_id}")
    out = [r for i, r in enumerate(refs) if i % num_shards == shard_id]
    print(
        f"[P0] shard {shard_id}/{num_shards}: {len(refs)} -> {len(out)} "
        f"(by list position after sample/range)",
        flush=True,
    )
    return out


def _stats_cache_paths(stats_cache_dir: Path) -> dict[str, Path]:
    root = Path(stats_cache_dir)
    return {
        "stats_cache_path": root / "global_stats.npz",
        "stage1_stats_cache_path": root / "stage1_global_stats.npz",
        "state_action_lag_cache_path": root / "state_action_lag.npz",
    }


def run_dataset_phases(
    *,
    package_dir: Path,
    config_path: Path,
    output_dir: Path,
    force_skip_gate: bool = False,
    sample_size: int | None = None,
    seed: int = 42,
    episode_range: tuple[int, int] | None = None,
    shard_id: int | None = None,
    num_shards: int | None = None,
    stats_cache_dir: Path | None = None,
    stats_only: bool = False,
    stats_phase: str | None = None,
    export_shard_only: bool = False,
    adapter_factory: Callable[[], Any] | None = None,
) -> int:
    package_dir = Path(package_dir)
    cfg = _load_yaml(config_path)
    ds = cfg.get("dataset") or {}
    pipe = cfg.get("pipeline") or {}
    media_cfg = cfg.get("media") or {}
    speed_cfg = cfg.get("speed_align") or {}

    dataset_root = Path(ds["root"])
    layout = ds.get("layout", "single_root")
    embodiment = ds.get("embodiment", "unknown")
    fps = float(ds.get("fps", 30))
    output_mode = pipe.get("output_mode", "both")
    force_skip = bool(force_skip_gate or pipe.get("force_skip_gate", False))
    pipeline_mode = str(pipe.get("mode", "clean")).lower()
    if pipeline_mode not in ("align", "clean", "legacy"):
        raise ValueError(f"Unsupported pipeline.mode={pipeline_mode!r} (align|clean|legacy)")
    is_align = pipeline_mode == "align"
    input_format = str(ds.get("input_format", "raw")).lower()
    if input_format in ("auto",):
        from robot_data_processing.v3_layout import is_v3_layout

        input_format = "pseudo_v3" if is_v3_layout(dataset_root) else "raw"
    identity_p1 = input_format in ("pseudo_v3", "v3", "standard", "aggregated_v3")

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    checklist = package_dir / "review_checklist.yaml"
    # Front-loaded: eef direction human review before any pipeline work / eef transforms
    eef_gate = assert_eef_direction_or_raise(checklist, force_skip=force_skip)
    print(f"[P2/eef] {eef_gate.message}", flush=True)

    require_export = output_mode in ("filter", "both")
    gate = assert_gate_or_raise(
        checklist, force_skip=force_skip, require_for_export=require_export
    )
    print(f"[P2] {gate.message}", flush=True)
    export_extrinsics = should_export_extrinsics(checklist)
    print(
        f"[P2] extrinsic_confidence={extrinsic_confidence_status(checklist)} "
        f"export_extrinsic_keys={export_extrinsics}",
        flush=True,
    )

    # P0 Ingest
    if layout in ("mcap", "multi_leaf", "h5"):
        # Package-local discover (MCAP trees, InternData-A1 multi-leaf v3, H5 trees, …).
        if str(package_dir) not in sys.path:
            sys.path.insert(0, str(package_dir))
        disc_mod = _load_module(package_dir / "discover.py", f"dataclean_discover_{package_dir.name}")
        splits = ds.get("splits") or ("train",)
        if isinstance(splits, str):
            splits = (splits,)
        total = ds.get("total_episodes")
        refs = disc_mod.discover(
            dataset_root,
            layout=layout,
            splits=tuple(splits),
            total_episodes=int(total) if total is not None else None,
            index_cache=Path(ds["index_cache"]) if ds.get("index_cache") else None,
            rebuild_index=bool(ds.get("rebuild_index", False)),
            # Optional package filters (e.g. AgiBotWorld task_ids=["327"]).
            tasks=ds.get("tasks") or ds.get("task_ids"),
            passed_traj_csv=_resolve_package_path(package_dir, ds.get("passed_traj_csv")),
        )
    elif layout == "part_task":
        refs = discover_part_task(dataset_root)
    else:
        total = ds.get("total_episodes")
        refs = discover_single_root(dataset_root, int(total) if total is not None else None)

    # Quarantined episodes are removed from stats and export (whole episode, never a
    # single frame) before sampling, so a sample never spends slots on them; the list
    # is published in info.json["quarantine"].
    from robot_data_processing.ignore_list import load_quarantine_entries, quarantine_match

    quarantine_entries = load_quarantine_entries(
        [
            p if p is None or Path(p).is_absolute() else package_dir / p
            for p in (pipe.get("quarantine_paths") or [])
        ]
    )
    quarantined: list[dict[str, Any]] = []
    if quarantine_entries:
        by_index: dict[int, list[dict]] = {}
        by_rel_path: dict[str, list[dict]] = {}
        for entry in quarantine_entries:
            if entry.get("episode_index") is not None:
                by_index.setdefault(int(entry["episode_index"]), []).append(entry)
            else:
                by_rel_path.setdefault(str(entry["rel_path"]), []).append(entry)
        kept_refs = []
        for r in refs:
            candidates = by_index.get(int(r.episode_index), []) + by_rel_path.get(
                str(getattr(r, "rel_path", None) or ""), []
            )
            hit = quarantine_match(r, candidates)
            if hit is None:
                kept_refs.append(r)
            else:
                quarantined.append({**hit, "matched_ref": r.key})
        refs = kept_refs
        print(f"[P0] quarantine: removed {len(quarantined)} episode(s)", flush=True)

    sample_mode = str(pipe.get("sample_mode", "random")).lower()
    if sample_size is not None and sample_size < len(refs):
        if sample_mode in ("sequential", "seq", "head", "first"):
            refs = refs[: int(sample_size)]
            print(
                f"[P0] sequential sample: first {len(refs)} episodes "
                f"(indices {refs[0].episode_index}..{refs[-1].episode_index})",
                flush=True,
            )
        elif sample_mode in ("uniform", "even", "linspace", "strided"):
            # Evenly spaced indices across the discovered list (inclusive endpoints).
            n = len(refs)
            k = int(sample_size)
            pick = np.unique(np.rint(np.linspace(0, n - 1, num=k)).astype(int))
            if len(pick) < k:
                used = set(int(i) for i in pick.tolist())
                for i in range(n):
                    if i not in used:
                        used.add(i)
                        if len(used) >= k:
                            break
                pick = np.array(sorted(used)[:k], dtype=int)
            refs = [refs[int(i)] for i in pick]
            span = refs[-1].episode_index - refs[0].episode_index if len(refs) > 1 else 0
            step = span / max(len(refs) - 1, 1)
            print(
                f"[P0] uniform sample: {len(refs)} episodes "
                f"(indices {refs[0].episode_index}..{refs[-1].episode_index}, "
                f"mean_step≈{step:.1f})",
                flush=True,
            )
        else:
            rng = np.random.default_rng(seed)
            pick = sorted(rng.choice(len(refs), size=sample_size, replace=False).tolist())
            refs = [refs[i] for i in pick]
            print(f"[P0] random sample: {len(refs)} episodes (seed={seed})", flush=True)

    refs = _apply_episode_range(refs, episode_range)
    # Full sample (after range, before shard) — used for shared global stats.
    stats_refs = list(refs)
    refs = _apply_shard(refs, shard_id=shard_id, num_shards=num_shards)
    if stats_phase is not None:
        stats_phase = str(stats_phase).lower()
        if stats_phase not in STATS_PHASES + vres.RESOLUTION_PHASES:
            raise ValueError(
                f"Unsupported stats_phase={stats_phase!r}; "
                f"expected {STATS_PHASES + vres.RESOLUTION_PHASES}"
            )
        stats_only = True
    print(f"[P0] discovered {len(refs)} episodes (layout={layout})", flush=True)
    if not refs and not stats_only:
        raise RuntimeError("No episodes left after sample/range/shard selection")
    if stats_cache_dir is not None:
        Path(stats_cache_dir).mkdir(parents=True, exist_ok=True)
        print(f"[P0] stats-cache-dir={Path(stats_cache_dir).resolve()}", flush=True)
    if stats_only:
        n_stats = len(refs) if stats_phase in ("pass1", "pass2") else len(stats_refs)
        print(
            f"[P0] stats-only: phase={stats_phase or 'single'} "
            f"episodes={n_stats} (process/export skipped)",
            flush=True,
        )
    if export_shard_only:
        print("[P0] export-shard-only: pseudo_v3 shard export (no renumber/aggregate/relative)", flush=True)
    print(
        f"[P0] pipeline.mode={pipeline_mode} input_format={input_format} identity_p1={identity_p1}",
        flush=True,
    )

    adapter = _build_adapter(package_dir, adapter_factory)

    # Optional I/O hooks (quality load / video resolve) for packages like abc_130k MCAP.
    from robot_data_processing.adapter_hooks import install_adapter_hooks

    install_adapter_hooks(package_dir)

    camera_map = adapter.camera_name_map()
    if identity_p1 and hasattr(adapter, "camera_name_map_from_root"):
        camera_map = adapter.camera_name_map_from_root(dataset_root)
        print(f"[P0] identity camera_map={camera_map}", flush=True)
    require_top = bool(media_cfg.get("require_camera_top", True))
    # Stage B: geometry cameras may not include literal camera_top
    if identity_p1 and require_top:
        require_top = any("camera_top" in k or "camera_reference" in k for k in camera_map)
    keymap_path = package_dir / "keymap.json"
    if keymap_path.exists():
        keymap = json.loads(keymap_path.read_text(encoding="utf-8"))
    else:
        keymap = getattr(adapter, "keymap", {}) or {}
    episode_geometry, geometry_cfg = _load_episode_geometry(package_dir, cfg)
    geometry_enabled = bool(geometry_cfg.get("enabled", False)) and episode_geometry is not None
    # P6 needs the exporter's resize policy to size K for the real stored pixels (EGO-02).
    video_settings = video_output_settings(
        media_cfg, geometry_cfg, geometry_enabled=geometry_enabled, is_align=is_align
    )
    if geometry_enabled:
        geometry_cfg = {**geometry_cfg, VIDEO_OUTPUT_KEY: video_settings}

    # P0/resolution: one stored size per output camera (Stage A). Runs before stats so
    # stats and export see the same (possibly reduced) episode set.
    res_cfg = vres.resolution_unify_config(
        media_cfg,
        is_align=is_align,
        normalize_videos=bool(video_settings.get("normalize_videos")),
        short_side=video_settings.get("target_short_side"),
    )
    res_summary: dict[str, Any] | None = None
    if res_cfg is not None:
        res_camera_map = (
            video_export_map_from_geometry_cfg(geometry_cfg) if geometry_enabled else {}
        ) or dict(camera_map)
        res_root = vres.resolution_dir(stats_cache_dir, output_dir)
        if stats_phase in vres.RESOLUTION_PHASES:
            t_res = time.perf_counter()
            summary = vres.run_resolution_phase(
                phase=stats_phase,
                root=res_root,
                refs=refs,
                stats_refs=stats_refs,
                camera_map=res_camera_map,
                cfg=res_cfg,
                shard_id=shard_id,
                num_shards=num_shards,
            )
            (output_dir / "run_meta.json").write_text(
                json.dumps(
                    {
                        "stats_phase": stats_phase,
                        "resolution_phase_summary": summary,
                        "resolution_config": res_cfg,
                        "camera_map": res_camera_map,
                        "elapsed_sec": round(time.perf_counter() - t_res, 3),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"=== Resolution phase {stats_phase} done → {res_root} ===", flush=True)
            return 0
        plan, plan_file = vres.load_or_build_plan(
            root=res_root, stats_refs=stats_refs, camera_map=res_camera_map, cfg=res_cfg
        )
        vres.log_plan(plan)
        vres.check_plan(plan)
        dropped = set(plan.get("discarded") or [])
        if dropped:
            refs = [r for r in refs if str(r.key) not in dropped]
            stats_refs = [r for r in stats_refs if str(r.key) not in dropped]
            print(
                f"[P0/resolution] dropped {len(dropped)} episodes; "
                f"this run: {len(refs)} episodes, stats: {len(stats_refs)}",
                flush=True,
            )
        video_settings = {**video_settings, vres.PLAN_KEY: vres.camera_transforms(plan)}
        if geometry_enabled:
            geometry_cfg = {**geometry_cfg, VIDEO_OUTPUT_KEY: video_settings}
        res_summary = vres.info_summary(plan, plan_file)
    elif stats_phase in vres.RESOLUTION_PHASES:
        print(f"[P0/resolution] {stats_phase}: resolution unify disabled — nothing to do", flush=True)
        return 0
    qpath = _resolve_quality_config(package_dir, cfg, embodiment)
    yaml_cfg = load_config(qpath)
    if layout != "part_task":
        yaml_cfg.setdefault("dataset", {})["root"] = str(dataset_root)

    temporal_align_cfg = cfg.get("temporal_align") or {}
    align_cfg = parse_temporal_align_config(
        temporal_align_cfg, yaml_cfg.get("state_action_alignment") or {}
    )
    p4_plan = resolve_p4_plan(align_cfg)
    fill_missing_action_eef_flag = parse_stage2_fill_config(
        cfg, (yaml_cfg.get("stage2") or {})
    )
    print(
        f"[P4] plan={p4_plan.name} apply_delay={align_cfg.apply_delay} "
        f"mode={align_cfg.mode} manual_delay={align_cfg.manual_delay}",
        flush=True,
    )
    print(
        f"[P3/stage2] fill_missing_action_eef={fill_missing_action_eef_flag}",
        flush=True,
    )

    run_meta: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "package_dir": str(package_dir),
        "dataset_root": str(dataset_root),
        "output_dir": str(output_dir),
        "layout": layout,
        "output_mode": output_mode,
        "num_episodes": len(refs),
        "num_stats_episodes": len(stats_refs),
        "quality_config": str(qpath),
        "sharding": {
            "episode_range": list(episode_range) if episode_range is not None else None,
            "shard_id": shard_id,
            "num_shards": num_shards,
            "stats_cache_dir": str(stats_cache_dir) if stats_cache_dir is not None else None,
            "stats_only": bool(stats_only),
            "stats_phase": stats_phase,
            "export_shard_only": bool(export_shard_only),
        },
        "pipeline": {
            "mode": pipeline_mode,
            "input_format": input_format,
            "num_workers": int(pipe.get("num_workers", 64)),
            "export_workers": pipe.get("export_workers"),
            "parallel_videos": pipe.get("parallel_videos", True),
        },
        "temporal_align": {
            "plan": p4_plan.name,
            "apply_delay": align_cfg.apply_delay,
            "mode": align_cfg.mode,
            "manual_delay": align_cfg.manual_delay,
        },
        "stage2": {"fill_missing_action_eef": fill_missing_action_eef_flag},
        "gate": {"ok": gate.ok, "force_skip": force_skip, "message": gate.message},
        "video_resolution": (
            {k: v for k, v in res_summary.items() if k != "cameras"} if res_summary else None
        ),
    }
    (output_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")

    timings: dict[str, float] = {}

    def _tick(name: str, t_start: float) -> float:
        dt = time.perf_counter() - t_start
        timings[name] = round(dt, 3)
        print(f"[timing] {name}: {dt:.2f}s ({dt / 60:.2f} min)", flush=True)
        return time.perf_counter()

    t0 = time.perf_counter()
    all_results: list[EpisodeResult] = []
    fps_failures = 0
    speed_meta_samples: list[dict[str, Any]] = []
    total_standard = 0
    all_pairs: list[tuple[StandardEpisode, EpisodeResult | None]] = []
    delay_samples: list[int] = []
    # Stage A/B export default: process-pool stream P1(+P6)+P7.
    # Stage B identity_p1 still streams; load_raw is optional (Stage A only).
    stream_p1_export = output_mode in ("filter", "both")
    stream_jobs: list[dict[str, Any]] = []
    stream_group_ctxs: list[dict[str, Any]] = []
    if stream_p1_export and not stats_only:
        kind = "identity P1+P7" if identity_p1 else "P1+P6+P7"
        print(
            f"[P0] stream {kind}: process pool, export each episode immediately "
            "(no shard-wide StandardEpisode list)",
            flush=True,
        )
    export_workers = int(pipe["export_workers"]) if pipe.get("export_workers") is not None else None
    parallel_videos = bool(pipe.get("parallel_videos", True))
    # Flat LeRobot root: renumber for part_task, or when explicitly requested.
    # Relative-statistics needs contiguous 0..N-1; enable via export.renumber_episodes
    # or relative_statistics.enabled=true.
    export_cfg_early = cfg.get("export") or {}
    rel_cfg_early = cfg.get("relative_statistics") or {}
    renumber_episodes = layout in ("part_task", "multi_leaf") or bool(
        export_cfg_early.get("renumber_episodes", False)
    )
    if bool(rel_cfg_early.get("enabled", False)):
        renumber_episodes = True
    if export_shard_only:
        # Keep source episode_index as on-disk id so shards never collide.
        renumber_episodes = False
    merged_task_name_to_index: dict[str, int] = {}

    # Stats-only: single-node full compute, or multi-node map-reduce phases.
    if stats_only:
        if stats_cache_dir is None:
            raise RuntimeError("stats_only requires stats_cache_dir")
        cache_paths = _stats_cache_paths(Path(stats_cache_dir))

        if stats_phase is not None:
            # Build quality PipelineConfig for schema / stage1 / bins / ignore.
            quality_out = output_dir / "_quality"
            quality_out.mkdir(parents=True, exist_ok=True)
            overrides = {
                "dataset_root": str(dataset_root),
                "output_dir": str(quality_out),
                "output_mode": "report",
                "num_workers": int(pipe.get("num_workers", 64)),
                "temporal_align": temporal_align_cfg,
                "stage2_fill_missing_action_eef": fill_missing_action_eef_flag,
            }
            if pipe.get("ignore_episodes_paths") is not None:
                overrides["ignore_episodes_paths"] = pipe.get("ignore_episodes_paths")
            elif pipe.get("ignore_episodes_path") is not None:
                overrides["ignore_episodes_path"] = pipe.get("ignore_episodes_path")
            if identity_p1 and "ignore_episodes_paths" not in overrides:
                overrides["ignore_episodes_paths"] = list(pipe.get("ignore_episodes_paths") or [])
            pcfg = pipeline_config_from_yaml(yaml_cfg, overrides)

            # pass1/pass2 scan the sharded episode list; prepare/finalize only merge.
            if stats_phase in ("pass1", "pass2"):
                shard_eps = sorted({r.episode_index for r in refs})
            else:
                shard_eps = []
            # Apply ignore list the same way run_pipeline does for stats.
            from robot_data_processing.ignore_list import (
                filter_episode_indices,
                load_ignore_episode_lists,
            )

            ignore_set = load_ignore_episode_lists(getattr(pcfg, "ignore_episodes_paths", None))
            if ignore_set and shard_eps:
                before = len(shard_eps)
                shard_eps = filter_episode_indices(shard_eps, ignore_set)
                print(
                    f"[P3/stats] ignore filter: {before} -> {len(shard_eps)}",
                    flush=True,
                )

            t_p3 = time.perf_counter()
            summary = run_stats_phase(
                phase=stats_phase,
                stats_cache_dir=Path(stats_cache_dir),
                dataset_root=Path(pcfg.dataset_root),
                episode_indices=shard_eps,
                schema=pcfg.schema,
                num_workers=int(pcfg.num_workers),
                num_bins=int(pcfg.stats_num_bins),
                s1_cfg=pcfg.stage1 or Stage1Config(),
                align_cfg=pcfg.state_action_alignment or align_cfg,
                shard_id=shard_id,
                num_shards=num_shards,
                show_progress=True,
            )
            _tick(f"p3_stats_{stats_phase}", t_p3)
            elapsed = time.perf_counter() - t0
            timings["total"] = round(elapsed, 3)
            run_meta.update(
                {
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_sec": round(elapsed, 2),
                    "stats_only": True,
                    "stats_phase": stats_phase,
                    "stats_phase_summary": summary,
                    "stats_cache_files": {k: str(v) for k, v in cache_paths.items()},
                    "phase_timing": {"sec": timings, "total_min": round(elapsed / 60.0, 3)},
                }
            )
            (output_dir / "run_meta.json").write_text(
                json.dumps(run_meta, indent=2), encoding="utf-8"
            )
            (output_dir / "phase_timing.json").write_text(
                json.dumps(run_meta["phase_timing"], indent=2), encoding="utf-8"
            )
            print(
                f"=== Stats phase {stats_phase} done in {elapsed/60:.1f} min → {stats_cache_dir} ===",
                flush=True,
            )
            return 0

        groups_stats = _group_refs_by_root(stats_refs)
        for gi, (root, group_refs) in enumerate(sorted(groups_stats.items(), key=lambda x: str(x[0])), start=1):
            eps = sorted({r.episode_index for r in group_refs})
            quality_out = output_dir / "_quality"
            if layout == "part_task":
                part = group_refs[0].part or "part"
                task = group_refs[0].task or "task"
                quality_out = output_dir / "_quality" / part / task
            quality_out.mkdir(parents=True, exist_ok=True)
            print(
                f"[P3/stats-only] [{gi}/{len(groups_stats)}] root={root} n={len(eps)} "
                f"→ {stats_cache_dir}",
                flush=True,
            )
            overrides = {
                "dataset_root": str(root),
                "output_dir": str(quality_out),
                "output_mode": "report",
                "num_workers": int(pipe.get("num_workers", 64)),
                "temporal_align": temporal_align_cfg,
                "stage2_fill_missing_action_eef": fill_missing_action_eef_flag,
                "stats_recompute": True,
                "stage1_stats_recompute": True,
                "state_action_lag_recompute": True,
                **{k: str(v) for k, v in cache_paths.items()},
            }
            if pipe.get("ignore_episodes_paths") is not None:
                overrides["ignore_episodes_paths"] = pipe.get("ignore_episodes_paths")
            elif pipe.get("ignore_episodes_path") is not None:
                overrides["ignore_episodes_path"] = pipe.get("ignore_episodes_path")
            if identity_p1 and "ignore_episodes_paths" not in overrides:
                overrides["ignore_episodes_paths"] = list(pipe.get("ignore_episodes_paths") or [])
            pcfg = pipeline_config_from_yaml(yaml_cfg, overrides)
            p3_timings: dict[str, float] = {}
            t_p3 = time.perf_counter()
            # Empty process list: only stats / lag caches are written.
            run_pipeline(
                pcfg,
                [],
                stats_episode_indices=eps,
                show_progress=True,
                timings=p3_timings,
            )
            _tick(f"p3_stats_only_group_{gi}", t_p3)
            for k, v in p3_timings.items():
                timings[f"group{gi}_{k}"] = v
        elapsed = time.perf_counter() - t0
        timings["total"] = round(elapsed, 3)
        run_meta.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_sec": round(elapsed, 2),
                "stats_only": True,
                "stats_cache_files": {k: str(v) for k, v in cache_paths.items()},
                "phase_timing": {"sec": timings, "total_min": round(elapsed / 60.0, 3)},
            }
        )
        (output_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
        (output_dir / "phase_timing.json").write_text(
            json.dumps(run_meta["phase_timing"], indent=2), encoding="utf-8"
        )
        print(f"=== Stats-only done in {elapsed/60:.1f} min → {stats_cache_dir} ===", flush=True)
        return 0

    groups = _group_refs_by_root(refs)
    for gi, (root, group_refs) in enumerate(sorted(groups.items(), key=lambda x: str(x[0])), start=1):
        eps = sorted({r.episode_index for r in group_refs})
        episode_tasks_map, task_name_to_index, task_index_to_name = load_episode_task_meta(root)
        for name, ti in task_name_to_index.items():
            merged_task_name_to_index.setdefault(name, ti)
        # Quality caches stay under _quality/; final export is always flat at output_dir.
        if layout == "part_task":
            part = group_refs[0].part or "part"
            task = group_refs[0].task or "task"
            quality_out = output_dir / "_quality" / part / task
        else:
            quality_out = output_dir / "_quality"
        quality_out.mkdir(parents=True, exist_ok=True)

        print(
            f"[P3] [{gi}/{len(groups)}] quality filter root={root} n={len(eps)} "
            f"mode={pipeline_mode}",
            flush=True,
        )
        overrides = {
            "dataset_root": str(root),
            "output_dir": str(quality_out),
            "output_mode": "report",
            "num_workers": int(pipe.get("num_workers", 64)),
            "temporal_align": temporal_align_cfg,
            "stage2_fill_missing_action_eef": fill_missing_action_eef_flag,
        }
        if export_workers is not None:
            overrides["export_workers"] = export_workers
        # Prefer Dataclean package config ignore lists when present.
        # Stage B (pseudo_v3): source ignore lists no longer apply to renumbered indices
        # unless the run config explicitly sets them.
        if identity_p1:
            overrides["ignore_episodes_paths"] = list(pipe.get("ignore_episodes_paths") or [])
        elif pipe.get("ignore_episodes_paths") is not None:
            overrides["ignore_episodes_paths"] = pipe.get("ignore_episodes_paths")
        elif pipe.get("ignore_episodes_path") is not None:
            overrides["ignore_episodes_path"] = pipe.get("ignore_episodes_path")
        if stats_cache_dir is not None:
            cache_paths = _stats_cache_paths(Path(stats_cache_dir))
            overrides.update({k: str(v) for k, v in cache_paths.items()})
            # Lag npz is only written for humanoid/robomind_ur. Requiring it for
            # egodex made have_all always false → shard runs recomputed stats on
            # 1/N episodes and overwrote the shared global cache.
            required = [
                cache_paths["stats_cache_path"],
                cache_paths["stage1_stats_cache_path"],
            ]
            need_lag = embodiment in ("humanoid", "robomind_ur") and bool(
                getattr(align_cfg, "enabled", True)
            ) and bool(p4_plan.compute_stats)
            if need_lag:
                required.append(cache_paths["state_action_lag_cache_path"])
            have_all = all(p.exists() for p in required)
            if not have_all and not is_align:
                missing = [str(p) for p in required if not p.exists()]
                raise FileNotFoundError(
                    "Shared stats cache incomplete. Run once with --stats-only "
                    f"--stats-cache-dir {stats_cache_dir} first. Missing: {missing}"
                )
            if have_all:
                overrides["stats_recompute"] = False
                overrides["stage1_stats_recompute"] = False
                overrides["state_action_lag_recompute"] = False
                print(f"[P3] using shared stats cache: {stats_cache_dir}", flush=True)
        pcfg = pipeline_config_from_yaml(yaml_cfg, overrides)
        p3_timings: dict[str, float] = {}
        t_p3 = time.perf_counter()
        if is_align:
            # Align: compute lag/stats only (empty process list); discard/mask unused.
            process_eps: list[int] = []
            print("[P3] align mode: skip per-episode quality process (keep all frames)", flush=True)
        else:
            process_eps = eps
        results = run_pipeline(
            pcfg,
            process_eps,
            stats_episode_indices=eps,
            show_progress=True,
            timings=p3_timings,
        )
        _tick(f"p3_quality_group_{gi}", t_p3)
        for k, v in p3_timings.items():
            timings[f"group{gi}_{k}"] = v
        result_by_ep = {r.episode_index: r for r in results}
        all_results.extend(results)

        align_stats: StateActionAlignStats | None = None
        lag_cache = quality_out / "cache" / "state_action_lag.npz"
        if stats_cache_dir is not None:
            shared_lag = Path(stats_cache_dir) / "state_action_lag.npz"
            if shared_lag.exists():
                lag_cache = shared_lag
        if p4_plan.compute_stats and lag_cache.exists():
            try:
                align_stats = StateActionAlignStats.load(str(lag_cache))
            except Exception:
                align_stats = None
        align_lag = resolve_alignment_lag(align_cfg, align_stats)
        state_action_delay = resolve_state_action_delay(align_cfg, align_stats, p4_plan)
        delay_samples.append(int(state_action_delay))
        print(
            f"[P4] lag_mean={align_stats.lag_mean if align_stats else None} "
            f"align_lag={align_lag} state_action_delay={state_action_delay}",
            flush=True,
        )

        if not is_align:
            write_quality_report(
                quality_out / "reports" / "quality_report.json",
                build_quality_report(
                    results,
                    stats_meta={"root": str(root)},
                    config_summary={"phase": "P3", "output_mode": "report"},
                ),
            )
            write_exclusion_log(quality_out / "reports" / "exclusion_log.jsonl", results)
            write_optimal_lags(quality_out / "reports" / "optimal_lags.json", results)

        ep_row_by_index: dict[int, dict[str, Any]] = {}
        if identity_p1:
            try:
                from robot_data_processing.v3_layout import episode_row_map

                ep_row_by_index = episode_row_map(root)
            except Exception:
                ep_row_by_index = {}

        group_pairs: list[tuple[StandardEpisode, EpisodeResult | None]] = []
        t_std = time.perf_counter()
        use_adapter_loader = (not identity_p1) and hasattr(adapter, "load_raw")
        if stream_p1_export:
            stream_group_ctxs.append(
                {
                    "episode_tasks_map": episode_tasks_map,
                    "task_index_to_name": task_index_to_name,
                    "task_name_to_index": task_name_to_index,
                    "align_lag": int(align_lag),
                    "state_action_delay": int(state_action_delay),
                    "ep_row_by_index": ep_row_by_index,
                }
            )
            gi_ctx = len(stream_group_ctxs) - 1
            for ref in group_refs:
                stream_jobs.append(
                    {
                        "ref": ref,
                        "result": None if is_align else result_by_ep.get(ref.episode_index),
                        "group_i": gi_ctx,
                    }
                )
            _tick(f"p1_standardize_group_{gi}", t_std)
            continue

        for ref in group_refs:
            r = None if is_align else result_by_ep.get(ref.episode_index)
            if use_adapter_loader:
                try:
                    raw = adapter.load_raw(ref)
                except Exception as exc:
                    print(f"  [P1] skip ep{ref.episode_index}: adapter load_raw failed: {exc}", flush=True)
                    continue
                if not raw or int(np.asarray(next(iter(raw.values()))).shape[0]) == 0:
                    print(f"  [P1] skip ep{ref.episode_index}: empty adapter episode", flush=True)
                    continue
            else:
                path = episode_parquet_path(ref.dataset_root, ref.episode_index)
                if not path.exists():
                    continue
                table = read_episode_table(path, episode_index=ref.episode_index)
                raw = _table_to_raw(table)
                if table.num_rows == 0:
                    print(f"  [P1] skip ep{ref.episode_index}: empty after episode slice", flush=True)
                    continue
            parquet_task_index = None
            if "task_index" in raw:
                try:
                    parquet_task_index = int(np.asarray(raw["task_index"]).reshape(-1)[0])
                except Exception:
                    parquet_task_index = None
            tasks_list = resolve_episode_tasks(
                episode_index=ref.episode_index,
                episode_tasks=episode_tasks_map,
                task_index_to_name=task_index_to_name,
                fallback_task=ref.task,
                parquet_task_index=parquet_task_index,
            )
            meta = {
                "fps": fps,
                "embodiment": embodiment,
                "dataset_root": str(ref.dataset_root),
                "part": ref.part,
                "task": ref.task or (tasks_list[0] if tasks_list else None),
                "tasks": tasks_list,
            }
            if identity_p1:
                row = ep_row_by_index.get(ref.episode_index) or {}
                if row.get("camera_intrinsics") is not None:
                    meta["camera_intrinsics"] = row["camera_intrinsics"]
                if row.get("tasks"):
                    meta["tasks"] = list(row["tasks"]) if not isinstance(row["tasks"], list) else row["tasks"]
                meta["source_episode_index"] = ref.episode_index
                meta["camera_keys"] = list(camera_map.keys())
            try:
                use_identity = identity_p1 or _looks_standard_raw(raw)
                if use_identity and hasattr(adapter, "to_standard_episode_from_aligned"):
                    standard = adapter.to_standard_episode_from_aligned(ref, raw, meta)
                else:
                    standard = adapter.to_standard_episode(ref, raw, meta)
            except Exception as exc:
                print(f"  [P1] skip ep{ref.episode_index}: {exc}", flush=True)
                continue

            _ensure_extra_info_fields(standard, raw)
            gap_reason = _short_episode_reason(
                standard, cfg, fps=fps, is_align=is_align
            ) or _source_timestamp_gap_reason(standard, cfg)
            if gap_reason:
                print(f"  [P1] discard ep{ref.episode_index}: {gap_reason}", flush=True)
                if r is not None:
                    r.discard = True
                    r.discard_reasons.append(gap_reason)
                continue
            if not identity_p1:
                standard = apply_review_corrections(standard, cfg)
            # MCAP source has no per-episode mp4 until convert.py; skip video checks.
            # MCAP/H5: no on-disk mp4 until adapter.resolve_source_video materializes cache.
            require_videos = (layout not in ("mcap", "h5")) and bool(
                require_top or standard.camera_keys
            )
            import_root = Path(standard.meta.get("dataset_root") or ref.dataset_root)
            import_ep = int(standard.meta.get("source_episode_index", ref.episode_index))
            import_align = check_import_frame_alignment(
                raw=raw,
                standard_num_frames=int(standard.num_frames),
                fields=standard.fields,
                extrinsic=standard.extrinsic,
                dataset_root=import_root,
                episode_index=import_ep,
                camera_keys=list(standard.camera_keys),
                require_videos=require_videos,
            )
            if not import_align.ok:
                reason = summarize_alignment_failure(import_align, stage="import")
                print(f"  [P1] discard ep{ref.episode_index}: {reason}", flush=True)
                if r is not None:
                    r.discard = True
                    r.discard_reasons.append(reason)
                continue
            # Stage2: fill missing action.eef from state.eef using joint lag (same policy as P3)
            s2_fill_cfg = Stage2Config(fill_missing_action_eef=fill_missing_action_eef_flag)
            # Copy DA-related thresholds from quality yaml when present
            q_s2 = yaml_cfg.get("stage2") or {}
            if q_s2:
                align = q_s2.get("alignment") or {}
                smooth = q_s2.get("smoothing") or {}
                s2_fill_cfg.max_lag_frames = int(align.get("max_lag_frames", s2_fill_cfg.max_lag_frames))
                s2_fill_cfg.diff_epsilon = float(align.get("diff_epsilon", s2_fill_cfg.diff_epsilon))
                s2_fill_cfg.min_active_samples = int(
                    align.get("min_active_samples", s2_fill_cfg.min_active_samples)
                )
                s2_fill_cfg.median_kernel = int(smooth.get("median_kernel", s2_fill_cfg.median_kernel))
                s2_fill_cfg.savgol_window = int(smooth.get("savgol_window", s2_fill_cfg.savgol_window))
                s2_fill_cfg.savgol_polyorder = int(
                    smooth.get("savgol_polyorder", s2_fill_cfg.savgol_polyorder)
                )
                s2_fill_cfg.action_type = str(q_s2.get("action_type", s2_fill_cfg.action_type))
            s2_fill_cfg.fill_lag = int(align_lag)
            _, filled_eef, fill_lags = fill_missing_action_eef(
                standard.fields, s2_fill_cfg, fill_lag=align_lag
            )
            standard.meta = {
                **standard.meta,
                "stage2_filled_action_eef": filled_eef,
                "stage2_fill_lags": fill_lags,
            }
            apply_p4_delay_to_standard_episode(
                standard, p4_plan, lag=align_lag, target_delay=1
            )
            standard.meta = {
                **standard.meta,
                "state_action_delay": state_action_delay,
                "p4_plan": p4_plan.name,
                "source_episode_index": standard.meta.get("source_episode_index", ref.episode_index),
            }
            if require_top and not standard.require_camera_top():
                print(f"  [P1] discard ep{ref.episode_index}: missing camera_top", flush=True)
                if r is not None:
                    r.discard = True
                    r.discard_reasons.append("missing_camera_top")
                continue

            ts = raw.get("timestamp")
            fps_check = check_timestamp_fps(ts, expected_fps=fps)
            if not fps_check.ok:
                fps_failures += 1
                if r is not None:
                    r.discard = True
                    r.discard_reasons.append(f"fps_unstable:{fps_check.message}")
                if is_align:
                    # Hard failure: still skip export for corrupt timestamps
                    print(f"  [P1] discard ep{ref.episode_index}: {fps_check.message}", flush=True)
                    continue
                continue

            xyz = standard.fields.get("action.eef.left.position")
            if xyz is None:
                xyz = standard.fields.get("action.eef.primary.position")
            if speed_cfg.get("enabled") and xyz is not None:
                sp = compute_speed_scale(
                    xyz,
                    fps,
                    golden_speed_mps=float(
                        speed_cfg.get("golden_speed_mps", DEFAULT_HUMANOID_GOLDEN_EE_SPEED)
                    ),
                    target_fps=float(speed_cfg.get("target_fps", 30)),
                )
                standard.meta.update(params_meta_from_speed(sp))
                if len(speed_meta_samples) < 20:
                    speed_meta_samples.append(params_meta_from_speed(sp))

            group_pairs.append((standard, r))

        _tick(f"p1_standardize_group_{gi}", t_std)
        total_standard += len(group_pairs)
        all_pairs.extend(group_pairs)

    # P6 EpisodeFrameGeometry (before P7 export)
    geom_info_extras: dict[str, Any] = {}
    geom_wrist_view: list[str] = []
    if stream_p1_export and not identity_p1:
        yaml_map = video_export_map_from_geometry_cfg(geometry_cfg)
        if yaml_map:
            camera_map = dict(yaml_map)
        wrist_cfg = (geometry_cfg.get("cameras") or {}).get("wrist_view") or []
        geom_wrist_view = [str(x) for x in wrist_cfg]
        print(
            f"[P6] stream: camera_map from yaml={camera_map} wrist_view={geom_wrist_view}",
            flush=True,
        )
    elif geometry_enabled and all_pairs:
        refs_by_ep = {r.episode_index: r for r in refs}
        video_export_map, geom_info_extras, geom_wrist_view = _apply_p6_geometry(
            all_pairs,
            episode_geometry=episode_geometry,
            geometry_cfg=geometry_cfg,
            keymap=keymap,
            refs_by_ep=refs_by_ep,
        )
        if video_export_map:
            camera_map = dict(video_export_map)
        if is_align:
            # keep_all_frames ignores quality discard; still drop hard P6 failures
            before = len(all_pairs)
            all_pairs = [
                (ep, None)
                for ep, r in all_pairs
                if r is None or not r.discard
            ]
            dropped = before - len(all_pairs)
            if dropped:
                print(f"[P6] align: dropped {dropped} hard geometry failures", flush=True)
    elif identity_p1:
        # Preserve Stage-A geometry contract fields for gr00t / training loaders.
        src_info_path = dataset_root / "meta" / "info.json"
        if src_info_path.exists():
            src_info = json.loads(src_info_path.read_text(encoding="utf-8"))
            # Reviewed info fixes for a published Stage A that must not be rewritten in place:
            # ``pipeline.stage_a_info_patch: {info_key: <json file holding the value>}``.
            for key, val in ((cfg.get("pipeline") or {}).get("stage_a_info_patch") or {}).items():
                patch_path = Path(val) if Path(val).is_absolute() else package_dir / val
                src_info[str(key)] = json.loads(patch_path.read_text(encoding="utf-8"))
                print(f"[P6] identity: info.{key} from stage_a_info_patch {patch_path}", flush=True)
            from robot_data_processing.pose_contract import CONTRACT_INFO_KEYS

            for key in CONTRACT_INFO_KEYS:
                if key in src_info and src_info[key] is not None:
                    geom_info_extras[key] = src_info[key]
            # Dataset-specific Stage-A declarations carried over verbatim (provenance such as sync or
            # base-geometry notes): ``pipeline.stage_a_info_carry: [info_key, ...]``. A listed key that
            # Stage A does not declare is a config error, not a silent skip.
            carry = [str(k) for k in ((cfg.get("pipeline") or {}).get("stage_a_info_carry") or [])]
            missing = [k for k in carry if src_info.get(k) is None]
            if missing:
                raise ValueError(f"pipeline.stage_a_info_carry: Stage A info.json has no {missing} ({src_info_path})")
            for key in carry:
                geom_info_extras[key] = src_info[key]
            if carry:
                print(f"[P6] identity: carried Stage A info keys {carry}", flush=True)
            wrist_from_feat: list[str] = []
            for key, spec in (src_info.get("features") or {}).items():
                if not (isinstance(spec, dict) and spec.get("dtype") == "video"):
                    continue
                info_blk = spec.get("info") if isinstance(spec.get("info"), dict) else {}
                if info_blk.get("camera.is_wrist_view") or spec.get("camera.is_wrist_view"):
                    short = key.split(".")[-1] if key.startswith("observation.images.") else key
                    wrist_from_feat.append(short)
            if wrist_from_feat:
                geom_wrist_view = wrist_from_feat
            print(
                f"[P6] identity: camera_geometry={bool(geom_info_extras.get('camera_geometry'))} "
                f"wrist_view={geom_wrist_view}",
                flush=True,
            )

    from robot_data_processing.pose_contract import contract_extras_from_config

    for key, val in contract_extras_from_config(geometry_cfg).items():
        geom_info_extras.setdefault(key, val)
    if quarantine_entries:
        previous = geom_info_extras.get("quarantine")
        geom_info_extras["quarantine"] = {
            **({"upstream": previous} if previous else {}),
            "policy": "whole_episode_removed_before_export",
            "lists": sorted({str(e["list"]) for e in quarantine_entries}),
            "configured": len(quarantine_entries),
            "removed_in_this_run": quarantined,
        }
    if res_summary is not None:
        geom_info_extras["video_resolution"] = res_summary

    if not export_extrinsics:
        from robot_data_processing.export_standard import (
            apply_untrusted_extrinsic_export,
            strip_extrinsic_keys_from_info_extras,
        )

        n_stashed = 0
        for ep, _result in all_pairs:
            n_stashed = max(n_stashed, len(apply_untrusted_extrinsic_export(ep)))
        geom_info_extras = strip_extrinsic_keys_from_info_extras(geom_info_extras)
        extra_note = (
            f"; stashed {n_stashed} extra_info.extrinsic.* column(s)"
            if n_stashed
            else ""
        )
        print(
            "[P7] extrinsic_confidence is not trusted; omitting extrinsic.* from export"
            f"{extra_note}",
            flush=True,
        )

    # P7: one flat LeRobot dataset at output_dir (no per-task subfolders)
    export_cfg = cfg.get("export") or {}
    lerobot_version = _resolve_export_layout(export_cfg, export_shard_only=export_shard_only)
    v30_cfg = export_cfg.get("v30") or {}
    keep_all_frames = bool(export_cfg.get("keep_all_frames", False)) or is_align
    if not keep_all_frames:
        from robot_data_processing.frame_removal import episode_discard_declaration, frame_removal_declaration

        geom_info_extras["frame_removal"] = frame_removal_declaration(pcfg)
        geom_info_extras["episode_discard"] = episode_discard_declaration(pcfg)
    else:
        from robot_data_processing.frame_removal import align_episode_discard_declaration

        # Stage A drops no episode for quality, but P6 / export can (e.g. moving head camera).
        geom_info_extras["episode_discard"] = align_episode_discard_declaration()
    ignore_quality_discard = bool(is_align)
    normalize_videos = bool(video_settings["normalize_videos"])
    export_summary = None
    if delay_samples:
        state_action_delay = max(set(delay_samples), key=delay_samples.count)
    else:
        state_action_delay = 1
    v30_output_root = None
    if lerobot_version == "both":
        v30_output_root = output_dir.parent / f"{output_dir.name}_v30"
    short_side = video_settings["target_short_side"]

    if stream_p1_export and stream_jobs:
        from robot_data_processing.export_standard import resolve_standard_info_path
        from robot_data_processing.phases.stream_export import run_streaming_p1_export

        std_info = resolve_standard_info_path(package_dir)
        if std_info is None:
            print("[P7] standard_info.json not found; features.names / prompt_template skipped", flush=True)
        else:
            print(f"[P7] standard_info={std_info}", flush=True)
        print(
            f"[P7] stream export → {output_dir} ({len(stream_jobs)} candidates); "
            f"state_action_delay={state_action_delay} "
            f"export_workers={export_workers} renumber={renumber_episodes} "
            f"v2_geometry={geometry_enabled} layout={lerobot_version} "
            f"keep_all_frames={keep_all_frames} normalize_videos={normalize_videos}"
            f"{' shard_only' if export_shard_only else ''}",
            flush=True,
        )
        t_stream = time.perf_counter()
        export_summary = run_streaming_p1_export(
            jobs=stream_jobs,
            group_ctxs=stream_group_ctxs,
            package_dir=package_dir,
            cfg=cfg,
            yaml_cfg=yaml_cfg,
            geometry_cfg=geometry_cfg,
            keymap=keymap,
            camera_map=camera_map,
            output_root=output_dir,
            source_root=dataset_root,
            fps=fps,
            embodiment=embodiment,
            layout=layout,
            require_top=require_top,
            is_align=is_align,
            fill_missing_action_eef_flag=fill_missing_action_eef_flag,
            p4_plan=p4_plan,
            speed_cfg=speed_cfg,
            geometry_enabled=geometry_enabled,
            export_workers=export_workers,
            parallel_videos=parallel_videos,
            renumber_episodes=renumber_episodes,
            keep_all_frames=keep_all_frames,
            ignore_quality_discard=ignore_quality_discard,
            normalize_videos=normalize_videos,
            target_height=int(
                media_cfg.get(
                    "target_height",
                    media_cfg.get("target_width", 384),
                )
            ),
            target_short_side=int(short_side) if short_side is not None else None,
            max_keyframe_interval=int(media_cfg.get("max_keyframe_interval", 10)),
            skip_parameters=geometry_enabled or identity_p1,
            info_extras=geom_info_extras or None,
            wrist_view_cameras=geom_wrist_view or None,
            standard_info_path=std_info if std_info is not None and std_info.exists() else None,
            lerobot_version=lerobot_version,
            v30_output_root=v30_output_root,
            v30_data_file_size_in_mb=int(v30_cfg.get("data_file_size_in_mb", 100)),
            v30_video_file_size_in_mb=int(v30_cfg.get("video_file_size_in_mb", 200)),
            v30_aggregate_media=v30_cfg.get("aggregate_media"),
            task_name_to_index=merged_task_name_to_index or None,
            delay_samples=delay_samples,
            use_adapter_loader=(not identity_p1) and hasattr(adapter, "load_raw"),
            identity_p1=bool(identity_p1),
            export_extrinsics=bool(export_extrinsics),
            allow_mixed_camera_sizes=bool(export_cfg.get("allow_mixed_camera_sizes", False)),
            video_resolution_plan=video_settings.get(vres.PLAN_KEY),
        )
        _tick("p1_p6_p7_stream", t_stream)
        total_standard = int(export_summary.get("standard_episodes") or 0)
        fps_failures += int(export_summary.get("fps_failures") or 0)
        for sample in export_summary.get("speed_align_samples") or []:
            if len(speed_meta_samples) < 20:
                speed_meta_samples.append(sample)
    elif output_mode in ("filter", "both") and all_pairs:
        from robot_data_processing.export_standard import (
            export_standard_dataset,
            resolve_standard_info_path,
        )

        std_info = resolve_standard_info_path(package_dir)
        if std_info is None:
            print("[P7] standard_info.json not found; features.names / prompt_template skipped", flush=True)
        else:
            print(f"[P7] standard_info={std_info}", flush=True)
        print(
            f"[P7] export → {output_dir} ({len(all_pairs)} candidates); "
            f"state_action_delay={state_action_delay} "
            f"export_workers={export_workers} renumber={renumber_episodes} "
            f"v2_geometry={geometry_enabled} layout={lerobot_version} "
            f"keep_all_frames={keep_all_frames} normalize_videos={normalize_videos}"
            f"{' shard_only' if export_shard_only else ''}",
            flush=True,
        )
        t_p7 = time.perf_counter()
        export_summary = export_standard_dataset(
            output_root=output_dir,
            episodes=all_pairs,
            camera_map=camera_map,
            source_root=dataset_root,
            fps=fps,
            embodiment=embodiment,
            state_action_delay=int(state_action_delay),
            target_height=int(
                media_cfg.get(
                    "target_height",
                    media_cfg.get("target_width", 384),  # legacy alias
                )
            ),
            target_short_side=int(short_side) if short_side is not None else None,
            max_keyframe_interval=int(media_cfg.get("max_keyframe_interval", 10)),
            standard_info_path=std_info if std_info.exists() else None,
            normalize_videos=normalize_videos,
            export_workers=export_workers,
            parallel_videos=parallel_videos,
            renumber_episodes=renumber_episodes,
            task_name_to_index=merged_task_name_to_index or None,
            show_progress=True,
            skip_parameters=geometry_enabled or identity_p1,
            info_extras=geom_info_extras or None,
            wrist_view_cameras=geom_wrist_view or None,
            lerobot_version=lerobot_version,
            v30_output_root=v30_output_root,
            v30_data_file_size_in_mb=int(v30_cfg.get("data_file_size_in_mb", 100)),
            v30_video_file_size_in_mb=int(v30_cfg.get("video_file_size_in_mb", 200)),
            v30_aggregate_media=v30_cfg.get("aggregate_media"),
            keep_all_frames=keep_all_frames,
            ignore_quality_discard=ignore_quality_discard,
            adapter_package_dir=package_dir,
            allow_mixed_camera_sizes=bool(export_cfg.get("allow_mixed_camera_sizes", False)),
            video_resolution_plan=video_settings.get(vres.PLAN_KEY),
        )
        _tick("p7_standard_export", t_p7)

    if export_summary is not None:
        if export_shard_only:
            manifest = {
                "export_shard_only": True,
                "shard_id": shard_id,
                "num_shards": num_shards,
                "output_root": str(output_dir),
                "total_episodes": export_summary.get("total_episodes"),
                "total_frames": export_summary.get("total_frames"),
                "lerobot_version": "pseudo_v3",
                "layout": "pseudo_v3",
                "renumber_episodes": False,
            }
            map_path = output_dir / "meta" / "episode_source_map.jsonl"
            if map_path.exists():
                src_ids = []
                for line in map_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    src_ids.append(int(row.get("source_episode_index", row["episode_index"])))
                manifest["source_episode_indices"] = sorted(src_ids)
            (output_dir / "shard_manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
            print(
                f"[P7/shard] wrote shard_manifest.json episodes={manifest.get('total_episodes')}",
                flush=True,
            )
        if export_summary.get("lerobot_v30"):
            v30_summary = export_summary["lerobot_v30"]
            print(
                f"[P7/v30] done: {v30_summary.get('codebase_version')} "
                f"layout={export_summary.get('layout') or lerobot_version} "
                f"episodes={v30_summary.get('total_episodes')} → {v30_summary.get('output_root')}",
                flush=True,
            )
            rel_cfg = cfg.get("relative_statistics") or {}
            if not export_shard_only and bool(rel_cfg.get("enabled", False)):
                from robot_data_processing.relative_statistics import (
                    prepare_dataset_statistics,
                )

                v30_root = Path(
                    v30_summary.get("output_root")
                    or (v30_output_root if lerobot_version == "both" else output_dir)
                )
                horizon_s = float(
                    rel_cfg.get(
                        "statistics_horizon_seconds",
                        2.0,
                    )
                )
                t_rel = time.perf_counter()
                rel_payload = prepare_dataset_statistics(
                    v30_root,
                    statistics_horizon_seconds=horizon_s,
                    rebuild=True,
                )
                _tick("p7_relative_statistics", t_rel)
                print(
                    f"[P7/rel] statistics_relative.json: anchors={rel_payload['anchor_count']} "
                    f"frames={rel_payload['source_frame_count']} → {v30_root / 'meta' / 'statistics_relative.json'}",
                    flush=True,
                )
                print(
                    f"[P7/rel] statistics_state.json: frames="
                    f"{rel_payload['eef_state_statistics']['source_frame_count']} "
                    f"availability={rel_payload['eef_state_statistics']['availability']} "
                    f"→ {v30_root / 'meta' / 'statistics_state.json'}",
                    flush=True,
                )
                export_summary["relative_statistics"] = {
                    "anchor_count": rel_payload["anchor_count"],
                    "source_frame_count": rel_payload["source_frame_count"],
                    "output_path": str(v30_root / "meta" / "statistics_relative.json"),
                    "eef_state_statistics": rel_payload["eef_state_statistics"],
                }
        del export_summary

    elapsed = time.perf_counter() - t0
    timings["total"] = round(elapsed, 3)
    # Aggregate top-level phase sums for quick reading
    summary_keys = {
        "p3_quality": [k for k in timings if k.startswith("p3_quality_group_")],
        "p1_standardize": [k for k in timings if k.startswith("p1_standardize_group_")],
        "p1_p6_p7_stream": [k for k in timings if k == "p1_p6_p7_stream"],
        "p7_standard_export": [k for k in timings if k == "p7_standard_export" or k.startswith("p7_standard_export_")],
        "p7_relative_statistics": [k for k in timings if k == "p7_relative_statistics"],
        "p3_global_stats": [k for k in timings if k.endswith("p3_global_stats")],
        "p3_stage1_stats": [k for k in timings if k.endswith("p3_stage1_stats")],
        "p3_state_action_lag_stats": [k for k in timings if k.endswith("p3_state_action_lag_stats")],
        "p3_process_episodes": [k for k in timings if k.endswith("p3_process_episodes")],
    }
    phase_summary = {
        name: round(sum(timings[k] for k in keys), 3) for name, keys in summary_keys.items() if keys
    }
    timing_payload = {
        "sec": timings,
        "summary_sec": phase_summary,
        "summary_min": {k: round(v / 60.0, 3) for k, v in phase_summary.items()},
        "total_min": round(elapsed / 60.0, 3),
    }
    (output_dir / "phase_timing.json").write_text(
        json.dumps(timing_payload, indent=2), encoding="utf-8"
    )
    print("[timing] === phase summary (min) ===", flush=True)
    for k, v in sorted(phase_summary.items(), key=lambda kv: -kv[1]):
        print(f"[timing]   {k}: {v / 60:.2f} min ({v:.1f}s)", flush=True)
    print(f"[timing]   TOTAL: {elapsed / 60:.2f} min", flush=True)

    run_meta.update(
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_sec": round(elapsed, 2),
            "processed_quality": len(all_results),
            "standard_episodes": total_standard,
            "fps_failures": fps_failures,
            "speed_align_samples": speed_meta_samples,
            "phase_timing": timing_payload,
        }
    )
    (output_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")

    if all_results:
        write_quality_report(
            output_dir / "reports" / "quality_report.json",
            build_quality_report(
                all_results,
                stats_meta={"phases": "P0-P7"},
                config_summary=run_meta,
            ),
        )
        write_exclusion_log(output_dir / "reports" / "exclusion_log.jsonl", all_results)
        write_optimal_lags(output_dir / "reports" / "optimal_lags.json", all_results)

    print(f"=== Phases done in {elapsed/60:.1f} min ===", flush=True)
    return 0
