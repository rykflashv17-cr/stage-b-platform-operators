from __future__ import annotations

import argparse
import sys
from pathlib import Path

from robot_data_processing.loader import list_episode_indices
from robot_data_processing.pipeline import load_config, pipeline_config_from_yaml, run_pipeline


def main(argv: list[str] | None = None) -> int:
    default_config = Path(__file__).resolve().parents[2] / "config" / "humanoid_merged.yaml"
    parser = argparse.ArgumentParser(description="Robot data quality processing pipeline")
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--dataset-root", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--output-mode",
        choices=["report", "filter", "both"],
        default=None,
        help="report: quality report only; filter: export LeRobot dataset; both: report + export",
    )
    parser.add_argument("--num-workers", type=int, default=None)
    parser.add_argument("--episode-limit", type=int, default=None, help="Process first N episodes by index")
    parser.add_argument(
        "--episode-indices",
        type=str,
        default=None,
        help="Comma-separated episode indices to process",
    )
    parser.add_argument(
        "--stats-episodes",
        choices=["all", "processed"],
        default=None,
        help="Stage3 stats scope: all dataset episodes, or only processed subset. "
        "Default: processed when --episode-limit/--episode-indices is set, else all.",
    )
    parser.add_argument("--recompute-stats", action="store_true", default=None)
    parser.add_argument("--no-recompute-stats", action="store_true")
    parser.add_argument("--stats-cache", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    yaml_cfg = load_config(args.config)
    overrides: dict = {"output_dir": str(args.output_dir)}
    if args.dataset_root:
        overrides["dataset_root"] = str(args.dataset_root)
    if args.output_mode:
        overrides["output_mode"] = args.output_mode
    if args.num_workers is not None:
        overrides["num_workers"] = args.num_workers
    if args.stats_cache:
        overrides["stats_cache_path"] = str(args.stats_cache)
    if args.recompute_stats:
        overrides["stats_recompute"] = True
    if args.no_recompute_stats:
        overrides["stats_recompute"] = False

    cfg = pipeline_config_from_yaml(yaml_cfg, overrides)

    total_eps = cfg.total_episodes
    all_indices = list_episode_indices(cfg.dataset_root, total_eps)

    if args.episode_indices:
        process_indices = [int(x.strip()) for x in args.episode_indices.split(",") if x.strip()]
    elif args.episode_limit is not None:
        process_indices = all_indices[: args.episode_limit]
    else:
        process_indices = all_indices

    is_subset = args.episode_limit is not None or args.episode_indices is not None
    if args.stats_episodes == "all":
        stats_indices = all_indices
        stats_scope = "all"
    elif args.stats_episodes == "processed":
        stats_indices = process_indices
        stats_scope = "processed"
    else:
        # subset test -> stats on same episodes; full run -> all episodes
        stats_indices = process_indices if is_subset else all_indices
        stats_scope = "processed" if is_subset else "all"

    print(f"Dataset root: {cfg.dataset_root}")
    print(f"Output dir:   {cfg.output_dir}")
    print(f"Output mode:  {cfg.output_mode}")
    print(f"Workers:      {cfg.num_workers}")
    align_cfg = cfg.state_action_alignment
    if align_cfg and align_cfg.enabled and cfg.schema.embodiment == "humanoid":
        print(f"Post-align:   state_action_temporal_alignment (humanoid only)")
    print(f"Stats episodes: {len(stats_indices)} ({stats_scope})")
    print(f"Process episodes: {len(process_indices)}")

    results = run_pipeline(
        cfg,
        process_indices,
        stats_episode_indices=stats_indices,
        show_progress=not args.quiet,
    )

    discarded = sum(1 for r in results if r.discard)
    kept_frames = sum(r.kept_frames for r in results if not r.discard)
    total_frames = sum(r.num_frames for r in results)
    print("\n=== Done ===")
    print(f"Processed: {len(results)} episodes")
    print(f"Discarded: {discarded} episodes ({100 * discarded / len(results):.2f}%)")
    print(f"Kept frames: {kept_frames}/{total_frames} ({100 * kept_frames / max(total_frames, 1):.2f}%)")
    print(f"Report: {cfg.output_dir / 'reports' / 'quality_report.json'}")
    if cfg.output_mode in ("filter", "both"):
        print(f"LeRobot export: {cfg.output_dir}")
        print(f"  data/: {cfg.output_dir / 'data'}")
        print(f"  videos/: {cfg.output_dir / 'videos'}")
        print(f"  meta/: {cfg.output_dir / 'meta'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
