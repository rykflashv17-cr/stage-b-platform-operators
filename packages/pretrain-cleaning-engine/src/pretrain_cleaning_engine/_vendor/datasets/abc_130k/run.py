#!/usr/bin/env python3
"""Run Dataclean P0–P7 for abc_130k (converted LeRobot or MCAP layout)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DATACLEAN_ROOT = PACKAGE_DIR.parents[1]
sys.path.insert(0, str(DATACLEAN_ROOT / "engine"))
sys.path.insert(0, str(PACKAGE_DIR))

from robot_data_processing.phases.orchestrator import run_dataset_phases  # noqa: E402


def _parse_episode_range(raw: str | None) -> tuple[int, int] | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if ":" not in text:
        raise SystemExit("--episode-range must be START:END (END exclusive)")
    start_s, end_s = text.split(":", 1)
    start, end = int(start_s), int(end_s)
    if end <= start:
        raise SystemExit(f"--episode-range invalid: {raw!r}")
    return start, end


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PACKAGE_DIR / "config.yaml")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--force-skip-gate", action="store_true")
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-mode", choices=["report", "filter", "both"], default=None)
    parser.add_argument("--pipeline-mode", choices=["align", "clean", "legacy"], default=None)
    parser.add_argument("--input-root", type=Path, default=None)
    parser.add_argument("--input-format", choices=["raw", "pseudo_v3", "auto"], default=None)
    parser.add_argument("--episode-range", type=str, default=None)
    parser.add_argument("--shard-id", type=int, default=None)
    parser.add_argument("--num-shards", type=int, default=None)
    parser.add_argument("--stats-cache-dir", type=Path, default=None)
    parser.add_argument("--stats-only", action="store_true")
    parser.add_argument(
        "--stats-phase",
        choices=["resolution_census", "resolution_plan", "pass1", "prepare_pass2", "pass2", "finalize"],
        default=None,
    )
    parser.add_argument("--export-shard-only", action="store_true")
    parser.add_argument(
        "--export-layout",
        choices=["pseudo_v3", "aggregated_v3"],
        default=None,
    )
    args = parser.parse_args()

    if args.stats_phase is not None:
        args.stats_only = True
    if args.stats_phase in ("resolution_plan", "prepare_pass2", "finalize"):
        if args.num_shards is None:
            raise SystemExit(f"--stats-phase {args.stats_phase} requires --num-shards")
    elif (args.shard_id is None) ^ (args.num_shards is None):
        raise SystemExit("Use --shard-id and --num-shards together")
    if args.stats_only and args.stats_cache_dir is None:
        raise SystemExit("--stats-only requires --stats-cache-dir")
    if args.stats_phase in ("resolution_census", "pass1", "pass2") and (
        args.shard_id is None or args.num_shards is None
    ):
        raise SystemExit(f"--stats-phase {args.stats_phase} requires --shard-id and --num-shards")

    episode_range = _parse_episode_range(args.episode_range)
    need_runtime = any(
        x is not None
        for x in (
            args.output_mode,
            args.pipeline_mode,
            args.input_root,
            args.input_format,
            args.export_layout,
        )
    )
    if need_runtime:
        import yaml

        cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        if args.output_mode is not None:
            cfg.setdefault("pipeline", {})["output_mode"] = args.output_mode
        if args.pipeline_mode is not None:
            cfg.setdefault("pipeline", {})["mode"] = args.pipeline_mode
        if args.input_root is not None:
            cfg.setdefault("dataset", {})["root"] = str(args.input_root.resolve())
        if args.input_format is not None:
            cfg.setdefault("dataset", {})["input_format"] = args.input_format
        if args.export_layout is not None:
            export_cfg = cfg.setdefault("export", {})
            export_cfg["layout"] = args.export_layout
            export_cfg["lerobot_version"] = args.export_layout
            v30 = export_cfg.setdefault("v30", {})
            v30["aggregate_media"] = args.export_layout == "aggregated_v3"
        args.output_dir.mkdir(parents=True, exist_ok=True)
        tmp = args.output_dir / "_runtime_config.yaml"
        tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        config_path = tmp
    else:
        config_path = args.config

    return run_dataset_phases(
        package_dir=PACKAGE_DIR,
        config_path=config_path,
        output_dir=args.output_dir,
        force_skip_gate=args.force_skip_gate,
        sample_size=args.sample_size,
        seed=args.seed,
        episode_range=episode_range,
        shard_id=args.shard_id,
        num_shards=args.num_shards,
        stats_cache_dir=args.stats_cache_dir,
        stats_only=bool(args.stats_only),
        stats_phase=args.stats_phase,
        export_shard_only=bool(args.export_shard_only),
    )


if __name__ == "__main__":
    raise SystemExit(main())
