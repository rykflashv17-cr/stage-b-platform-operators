"""Streaming P1(+P6)+P7: one episode in flight per worker.

Stage A: load → standardize → P6 → write parquet/video immediately.
Stage B identity_p1: parquet passthrough, skip P6 / review_corrections, apply keep_mask.
The parent only keeps lightweight export rows so a shard is not held in RAM.
"""
from __future__ import annotations

import os
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any

from robot_data_processing.export_standard import (
    FieldSchemaRegistry,
    _export_one_task,
    _init_export_pool,
    add_source_timestamps_declaration,
    episode_feature_meta,
    episode_field_dtypes,
    finalize_standard_export,
    prepare_standard_export_roots,
    select_export_keep_mask,
)
from robot_data_processing.frame_alignment import check_export_frame_alignment
from robot_data_processing.frame_removal import attach_quality_frame_removal, episode_discard_record
from robot_data_processing.normalize.standard_types import EpisodeRef
from robot_data_processing.phases.media import resolve_source_video, source_video_is_usable
from robot_data_processing.phases.video_geometry import CameraSizeRegistry

_STREAM_STATE: dict[str, Any] = {}


def _default_stream_workers(requested: int | None) -> int:
    if requested is not None and requested > 0:
        return int(requested)
    env = os.environ.get("STAGE_A_P1_WORKERS", "").strip()
    if env:
        return max(1, int(env))
    return min(16, os.cpu_count() or 4)


def _task_index_for(ep, *, source_task_map: dict[str, int], embodiment: str) -> int:
    names = ep.meta.get("tasks") or []
    if isinstance(names, str):
        names = [names]
    names = [str(x) for x in names if x is not None and str(x) != ""]
    if not names and ep.meta.get("task"):
        names = [str(ep.meta["task"])]
    if not names:
        names = [embodiment]
    ep.meta["tasks"] = names
    name = names[0]
    if name in source_task_map:
        return int(source_task_map[name])
    return 0


def _init_stream_pool(state: dict[str, Any]) -> None:
    from robot_data_processing.adapter_hooks import install_adapter_hooks
    from robot_data_processing.phases.orchestrator import (
        _build_adapter,
        _load_episode_geometry,
    )

    _STREAM_STATE.clear()
    _STREAM_STATE.update(state)
    package_dir = Path(state["package_dir"])
    install_adapter_hooks(package_dir)
    _STREAM_STATE["adapter"] = _build_adapter(package_dir)
    episode_geometry, _geom_cfg = _load_episode_geometry(package_dir, state["cfg"])
    _STREAM_STATE["episode_geometry"] = episode_geometry
    _init_export_pool(state["export_pool"])


def _p1_p6_export_one(job: dict[str, Any]) -> dict[str, Any]:
    try:
        return _p1_p6_export_one_impl(job)
    except Exception as exc:
        ref = job.get("ref")
        ep = int(getattr(ref, "episode_index", -1) or -1)
        print(f"[P7] export exception ep={ep}: {type(exc).__name__}: {exc}", flush=True)
        return {
            "status": "discard",
            "reason": f"export_exception:{type(exc).__name__}",
            "episode_index": ep,
        }


def _p1_p6_export_one_impl(job: dict[str, Any]) -> dict[str, Any]:
    from robot_data_processing.phases.orchestrator import (
        _apply_p6_one_episode,
        _standardize_one_ref,
    )

    st = _STREAM_STATE
    group = st["group_ctxs"][int(job["group_i"])]
    ref: EpisodeRef = job["ref"]
    out = _standardize_one_ref(
        ref=ref,
        adapter=st["adapter"],
        identity_p1=bool(st.get("identity_p1")),
        use_adapter_loader=bool(st.get("use_adapter_loader")),
        is_align=bool(st["is_align"]),
        result=job.get("result"),
        camera_map=st["camera_map"],
        require_top=bool(st["require_top"]),
        layout=str(st["layout"]),
        cfg=st["cfg"],
        yaml_cfg=st["yaml_cfg"],
        fill_missing_action_eef_flag=bool(st["fill_missing_action_eef_flag"]),
        align_lag=int(group["align_lag"]),
        p4_plan=st["p4_plan"],
        state_action_delay=int(group["state_action_delay"]),
        fps=float(st["fps"]),
        embodiment=str(st["embodiment"]),
        speed_cfg=st["speed_cfg"],
        episode_tasks_map=group["episode_tasks_map"],
        task_index_to_name=group["task_index_to_name"],
        ep_row_by_index=group.get("ep_row_by_index") or {},
    )
    lineage = {"dataset_root": str(ref.dataset_root), "rel_path": getattr(ref, "rel_path", None)}
    if out["status"] != "ok":
        return {
            "status": out["status"],
            "reason": out.get("reason"),
            "fps_fail": bool(out.get("fps_fail")),
            "episode_index": int(ref.episode_index),
            **lineage,
        }

    standard = out["standard"]
    result = out.get("result")
    extras: dict[str, Any] = {}
    wrist_view: list[str] = []
    if (
        st.get("geometry_enabled")
        and not st.get("identity_p1")
        and st.get("episode_geometry") is not None
    ):
        standard, result, _vmap, extras, wrist_view = _apply_p6_one_episode(
            standard,
            result,
            episode_geometry=st["episode_geometry"],
            geometry_cfg=st["geometry_cfg"],
            keymap=st["keymap"],
            ref=ref,
        )
        if result is not None and result.discard and st["is_align"]:
            return {
                "status": "discard",
                "reason": ";".join(result.discard_reasons),
                "discard_reasons": list(result.discard_reasons),
                "episode_index": int(ref.episode_index),
                **lineage,
            }

    if not st.get("export_extrinsics", True):
        from robot_data_processing.export_standard import (
            apply_untrusted_extrinsic_export,
            strip_extrinsic_keys_from_info_extras,
        )

        apply_untrusted_extrinsic_export(standard)
        extras = strip_extrinsic_keys_from_info_extras(extras)

    selected = select_export_keep_mask(
        standard,
        result,
        keep_all_frames=bool(st["keep_all_frames"]),
        ignore_quality_discard=bool(st["ignore_quality_discard"]),
    )
    if selected is None:
        return {
            "status": "skip",
            "reason": "export_filter",
            "discard_reasons": list(result.discard_reasons) if result is not None else [],
            "episode_index": int(ref.episode_index),
            **lineage,
        }
    keep_mask, n = selected
    src_root = Path(standard.meta.get("dataset_root", st["source_root"]))
    src_ep = int(standard.meta.get("source_episode_index", ref.episode_index))
    align = check_export_frame_alignment(
        num_frames=int(standard.num_frames),
        fields=standard.fields,
        extrinsic=standard.extrinsic,
        keep_mask=keep_mask,
        dataset_root=src_root,
        source_episode_index=src_ep,
        camera_map=st["camera_map"],
        fps=float(st["fps"]),
        check_videos=not bool(st["keep_all_frames"]),
    )
    if not align.ok:
        return {
            "status": "discard",
            "reason": "export_align",
            "skipped_align": True,
            "episode_index": int(ref.episode_index),
            **lineage,
        }

    missing_videos: list[str] = []
    for src_key in st["camera_map"]:
        src = resolve_source_video(src_root, src_ep, str(src_key))
        if source_video_is_usable(src):
            continue
        alt = resolve_source_video(src_root, src_ep, f"observation.images.{src_key}")
        if source_video_is_usable(alt):
            continue
        missing_videos.append(str(src_key))
    if missing_videos:
        return {
            "status": "discard",
            "reason": "empty_or_missing_video:" + ",".join(missing_videos),
            "episode_index": int(ref.episode_index),
            **lineage,
        }

    if st["renumber_episodes"]:
        out_ep_index = int(job["out_ep_index"])
    else:
        out_ep_index = int(ref.episode_index)
    standard.meta.setdefault("source_episode_index", ref.episode_index)
    source_task_map = dict(st.get("task_name_to_index") or {})
    source_task_map.update(group.get("task_name_to_index") or {})
    task_index = _task_index_for(
        standard,
        source_task_map=source_task_map,
        embodiment=str(st["embodiment"]),
    )
    if keep_mask is not None and result is not None:
        attach_quality_frame_removal(standard, result)
    row, camera_sizes, field_names = _export_one_task(
        (out_ep_index, standard, keep_mask, 0, task_index)
    )
    field_dims, extrinsic_keys, intrinsic = episode_feature_meta(standard)
    return {
        "status": "ok",
        "row": row,
        "camera_sizes": camera_sizes,
        "field_names": list(field_names),
        "field_dims": field_dims,
        "field_dtypes": episode_field_dtypes(standard),
        "extrinsic_keys": extrinsic_keys,
        "intrinsic": intrinsic,
        "info_extras": extras,
        "wrist_view": wrist_view,
        "speed_meta": out.get("speed_meta"),
        "length": int(n),
        "episode_index": int(ref.episode_index),
    }


def _v21_episode_paths(root: Path, episode_index: int) -> tuple[Path, Path, Path]:
    chunk = f"chunk-{int(episode_index) // 1000:03d}"
    name = f"episode_{int(episode_index):06d}"
    return (
        root / "data" / chunk / f"{name}.parquet",
        root / "parameters" / chunk / name,
        root / "videos" / chunk,
    )


def compact_renumbered_episodes(dataset_root: Path, episodes_out: list[dict[str, Any]]) -> int:
    """Close the output-index gaps left by discarded episodes; return how many moved.

    Output ids are assigned before P1 (``out_ep_index = i``), so every discard (too_short,
    missing video, geometry, ...) leaves a hole, and LeRobot v3 requires 0..N-1. Renames the
    v2.1 staging parquet / parameters / videos in ascending order (new <= old, so a target
    is always free) and rewrites the parquet ``episode_index`` column; ``index`` and the
    v3 meta are rebuilt from the staged files by finalize. ``episodes_out`` must be sorted.
    """
    import shutil

    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = Path(dataset_root)
    moved = 0
    old_max = -1
    for new_i, row in enumerate(episodes_out):
        old_i = int(row["episode_index"])
        old_max = max(old_max, old_i)
        if old_i == new_i:
            continue
        if old_i < new_i:
            raise ValueError(f"episodes_out not sorted/unique at output index {old_i}")
        old_pq, old_params, old_videos = _v21_episode_paths(root, old_i)
        new_pq, new_params, new_videos = _v21_episode_paths(root, new_i)
        table = pq.read_table(old_pq)
        cols = {
            name: (
                pa.array(np.full(table.num_rows, new_i, dtype=np.int64))
                if name == "episode_index"
                else table[name]
            )
            for name in table.column_names
        }
        new_pq.parent.mkdir(parents=True, exist_ok=True)
        tmp = new_pq.with_name(f".{new_pq.name}.tmp")
        pq.write_table(pa.table(cols, metadata=table.schema.metadata), tmp)
        os.replace(tmp, new_pq)
        old_pq.unlink()
        if old_params.exists():
            if new_params.exists():
                shutil.rmtree(new_params)
            new_params.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_params, new_params)
        if old_videos.exists():
            for cam_dir in old_videos.iterdir():
                src = cam_dir / f"episode_{old_i:06d}.mp4"
                if src.exists():
                    dst = new_videos / cam_dir.name / src.name.replace(f"{old_i:06d}", f"{new_i:06d}")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(src, dst)
        row["episode_index"] = new_i
        moved += 1
    # Files a discarded episode may have written before failing (index >= N) must not
    # reappear as episodes when finalize lists the staged tree.
    for stale_i in range(len(episodes_out), old_max + 1):
        stale_pq, stale_params, stale_videos = _v21_episode_paths(root, stale_i)
        stale_pq.unlink(missing_ok=True)
        if stale_params.exists():
            shutil.rmtree(stale_params)
        if stale_videos.exists():
            for cam_dir in stale_videos.iterdir():
                (cam_dir / f"episode_{stale_i:06d}.mp4").unlink(missing_ok=True)
    return moved


def run_streaming_p1_export(
    *,
    jobs: list[dict[str, Any]],
    group_ctxs: list[dict[str, Any]],
    package_dir: Path,
    cfg: dict[str, Any],
    yaml_cfg: dict[str, Any],
    geometry_cfg: dict[str, Any],
    keymap: dict[str, Any],
    camera_map: dict[str, str],
    output_root: Path,
    source_root: Path,
    fps: float,
    embodiment: str,
    layout: str,
    require_top: bool,
    is_align: bool,
    fill_missing_action_eef_flag: bool,
    p4_plan: Any,
    speed_cfg: dict[str, Any],
    geometry_enabled: bool,
    export_workers: int | None,
    parallel_videos: bool,
    renumber_episodes: bool,
    keep_all_frames: bool,
    ignore_quality_discard: bool,
    normalize_videos: bool,
    target_height: int,
    target_short_side: int | None,
    max_keyframe_interval: int,
    skip_parameters: bool,
    info_extras: dict[str, Any] | None,
    wrist_view_cameras: list[str] | None,
    standard_info_path: Path | None,
    lerobot_version: str,
    v30_output_root: Path | None,
    v30_data_file_size_in_mb: int,
    v30_video_file_size_in_mb: int,
    v30_aggregate_media: bool | None,
    task_name_to_index: dict[str, int] | None,
    delay_samples: list[int],
    use_adapter_loader: bool = False,
    identity_p1: bool = False,
    export_extrinsics: bool = True,
    show_progress: bool = True,
    allow_mixed_camera_sizes: bool = False,
    video_resolution_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Process-pool P1(+P6)+write; finalize info/stats after all workers finish.

    Stage A: identity_p1=False (source → standard, optional P6).
    Stage B: identity_p1=True (pseudo_v3 passthrough, no P6 / review_corrections).
    """
    from tqdm import tqdm

    version, write_v30, aggregate_media, dataset_root, cache_root = prepare_standard_export_roots(
        Path(output_root),
        lerobot_version=lerobot_version,
        v30_aggregate_media=v30_aggregate_media,
    )
    if version == "aggregated_v3":
        renumber_episodes = True

    workers = _default_stream_workers(export_workers)
    if delay_samples:
        state_action_delay = max(set(delay_samples), key=delay_samples.count)
    else:
        state_action_delay = 1

    export_pool = {
        "output_root": str(dataset_root),
        "camera_map": camera_map,
        "fps": fps,
        "target_height": target_height,
        "target_short_side": target_short_side,
        "max_keyframe_interval": max_keyframe_interval,
        "normalize_videos": normalize_videos,
        "parallel_videos": parallel_videos,
        "skip_parameters": skip_parameters,
        "source_root": str(source_root),
        "adapter_package_dir": str(package_dir),
        "video_resolution_plan": video_resolution_plan,
    }
    init_state = {
        "package_dir": str(package_dir),
        "cfg": cfg,
        "yaml_cfg": yaml_cfg,
        "geometry_cfg": geometry_cfg,
        "keymap": keymap,
        "camera_map": camera_map,
        "source_root": str(source_root),
        "fps": fps,
        "embodiment": embodiment,
        "layout": layout,
        "require_top": require_top,
        "is_align": is_align,
        "fill_missing_action_eef_flag": fill_missing_action_eef_flag,
        "p4_plan": p4_plan,
        "speed_cfg": speed_cfg,
        "geometry_enabled": geometry_enabled,
        "keep_all_frames": keep_all_frames,
        "ignore_quality_discard": ignore_quality_discard,
        "renumber_episodes": renumber_episodes,
        "task_name_to_index": dict(task_name_to_index or {}),
        "group_ctxs": group_ctxs,
        "export_pool": export_pool,
        "use_adapter_loader": bool(use_adapter_loader),
        "identity_p1": bool(identity_p1),
        "export_extrinsics": bool(export_extrinsics),
    }

    work_jobs = list(jobs)
    if renumber_episodes:
        for i, job in enumerate(work_jobs):
            job["out_ep_index"] = i

    print(
        f"[P1/stream] P1+P6+P7 workers={workers} jobs={len(work_jobs)} "
        f"identity_p1={bool(identity_p1)} renumber={renumber_episodes} layout={version}",
        flush=True,
    )

    episodes_out: list[dict[str, Any]] = []
    fields_present: set[str] = set()
    field_dims: dict[str, int] = {}
    extrinsic_keys: list[str] = []
    camera_sizes: dict[str, tuple[int, int]] = {}
    size_registry = CameraSizeRegistry()
    field_registry = FieldSchemaRegistry()
    total_frames = 0
    skipped = 0
    skipped_align = 0
    fps_failures = 0
    dropped: list[dict[str, Any]] = []
    speed_meta_samples: list[dict[str, Any]] = []
    used_task_index_to_name: dict[int, str] = {}
    shard_task_to_index: dict[str, int] = {}
    resolved_extras = dict(info_extras or {})
    resolved_wrist = list(wrist_view_cameras or [])
    if not export_extrinsics:
        from robot_data_processing.export_standard import strip_extrinsic_keys_from_info_extras

        resolved_extras = strip_extrinsic_keys_from_info_extras(resolved_extras)

    def _consume(payload: dict[str, Any]) -> None:
        nonlocal skipped, skipped_align, fps_failures, total_frames
        if payload.get("fps_fail"):
            fps_failures += 1
        if payload.get("status") != "ok":
            skipped += 1
            dropped.append(
                episode_discard_record(
                    int(payload["episode_index"]),
                    payload.get("discard_reasons")
                    or [str(payload.get("reason") or payload.get("status"))],
                    dataset_root=payload.get("dataset_root"),
                    rel_path=payload.get("rel_path"),
                )
            )
            if payload.get("skipped_align"):
                skipped_align += 1
            if skipped <= 20:
                print(
                    f"[P1/stream] skip ep{payload.get('episode_index')}: "
                    f"{payload.get('reason') or payload.get('status')}",
                    flush=True,
                )
            return
        row = payload["row"]
        episodes_out.append(row)
        total_frames += int(row["length"])
        fields_present.update(payload.get("field_names") or [])
        # Every episode counts: optional fields / dtypes are not taken from the first one
        # (REPORT AGI-02 / AGI-04).
        field_registry.add(payload.get("field_dims"), payload.get("field_dtypes"))
        if export_extrinsics and not extrinsic_keys and payload.get("extrinsic_keys"):
            extrinsic_keys.extend(payload["extrinsic_keys"])
        # Count every episode; first-wins would hide mixed resolutions (REPORT EGO-02 / INT-01).
        size_registry.add(payload.get("camera_sizes"))
        for key, val in (payload.get("info_extras") or {}).items():
            # Config-level declarations (eef_reference, ...) win over adapter defaults.
            resolved_extras.setdefault(key, val)
        if not resolved_wrist and payload.get("wrist_view"):
            resolved_wrist.extend(payload["wrist_view"])
        if payload.get("speed_meta") is not None and len(speed_meta_samples) < 20:
            speed_meta_samples.append(payload["speed_meta"])
        ti = row.get("task_index")
        tasks = row.get("tasks") or []
        if ti is not None and tasks:
            # Workers fall back to task_index 0 for tasks without a source index, so two
            # different tasks can share an index (REPORT IA-R3a). Give every task text its
            # own index in the shard meta; merge renumbers globally by text and rewrites
            # the data parquet ``task_index`` column.
            name = str(tasks[0])
            if name not in shard_task_to_index:
                idx = int(ti)
                if idx in used_task_index_to_name:
                    idx = max(used_task_index_to_name) + 1
                shard_task_to_index[name] = idx
                used_task_index_to_name[idx] = name
            row["task_index"] = shard_task_to_index[name]

    if workers <= 1 or len(work_jobs) <= 1:
        _init_stream_pool(init_state)
        iterator: Any = work_jobs
        if show_progress:
            iterator = tqdm(work_jobs, desc="P1+P6+P7 stream")
        for job in iterator:
            _consume(_p1_p6_export_one(job))
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_stream_pool,
            initargs=(init_state,),
        ) as pool:
            inflight: dict[Any, int] = {}
            pbar = (
                tqdm(total=len(work_jobs), desc=f"P1+P6+P7 stream ({workers}w)")
                if show_progress
                else None
            )

            def _drain_one() -> None:
                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    inflight.pop(fut)
                    _consume(fut.result())
                    if pbar is not None:
                        pbar.update(1)

            for job in work_jobs:
                while len(inflight) >= workers:
                    _drain_one()
                inflight[pool.submit(_p1_p6_export_one, job)] = int(
                    job["ref"].episode_index
                )
            while inflight:
                _drain_one()
            if pbar is not None:
                pbar.close()

    if not episodes_out:
        print(
            f"[P7] stream: 0 episodes exported ({skipped} skipped, "
            f"{skipped_align} align-fail); skip finalize",
            flush=True,
        )
        return {
            "fps_failures": fps_failures,
            "speed_align_samples": speed_meta_samples,
            "standard_episodes": 0,
            "total_episodes": 0,
            "total_frames": 0,
            "skipped_episodes": skipped,
            "skipped_frame_alignment": skipped_align,
        }

    episodes_out.sort(key=lambda r: int(r["episode_index"]))
    if renumber_episodes:
        moved = compact_renumbered_episodes(dataset_root, episodes_out)
        if moved:
            print(
                f"[P7] compacted output episode_index after {skipped} discard(s): "
                f"{moved} episode(s) renumbered to 0..{len(episodes_out) - 1}",
                flush=True,
            )
    camera_sizes.update(size_registry.resolve(allow_mixed=allow_mixed_camera_sizes))
    resolved_extras["camera_image_size_counts"] = size_registry.summary()
    resolved_dims, field_dtypes = field_registry.resolve()
    field_dims.update(resolved_dims)
    if field_registry.availability():
        resolved_extras["field_availability"] = field_registry.availability()
    add_source_timestamps_declaration(resolved_extras, fields_present)
    tasks_out = [
        {"task_index": int(ti), "task": used_task_index_to_name[ti]}
        for ti in sorted(used_task_index_to_name)
    ]
    summary = finalize_standard_export(
        output_root=Path(output_root),
        dataset_root=dataset_root,
        cache_root=cache_root,
        version=version,
        write_v30=write_v30,
        aggregate_media=aggregate_media,
        episodes_out=episodes_out,
        fields_present=fields_present,
        camera_sizes=camera_sizes,
        camera_map=camera_map,
        fps=fps,
        embodiment=embodiment,
        state_action_delay=int(state_action_delay),
        tasks_out=tasks_out,
        total_frames=total_frames,
        skip_parameters=skip_parameters,
        info_extras=resolved_extras or None,
        wrist_view_cameras=resolved_wrist or None,
        standard_info_path=standard_info_path,
        skipped=skipped,
        skipped_frame_alignment=skipped_align,
        export_workers=workers,
        parallel_videos=parallel_videos,
        renumber_episodes=renumber_episodes,
        keep_all_frames=keep_all_frames,
        v30_output_root=v30_output_root,
        v30_data_file_size_in_mb=v30_data_file_size_in_mb,
        v30_video_file_size_in_mb=v30_video_file_size_in_mb,
        field_dims=field_dims or None,
        extrinsic_keys=extrinsic_keys or None,
        field_dtypes=field_dtypes or None,
        dropped_episodes=dropped,
    )
    summary["fps_failures"] = fps_failures
    summary["speed_align_samples"] = speed_meta_samples
    summary["standard_episodes"] = len(episodes_out)
    return summary
