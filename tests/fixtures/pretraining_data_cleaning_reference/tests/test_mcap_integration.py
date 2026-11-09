from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import struct

import numpy as np
import pytest
import pytest
from mcap.writer import Writer

from stage_b_mcap_v4.cli import main
from stage_b_mcap_v4.contracts import McapEpisodeRef
from stage_b_mcap_v4.operators import GlobalStatsOperator, McapReadOperator, QualityDetectionOperator
from stage_b_mcap_v4.spec import build_adapter, load_spec, quality_config
from stage_b_mcap_v4.storage import load_episode


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PACKAGE_ROOT / "configs" / "jindian_marvin.yaml"
JOINTS = [f"Joint{i}_{side}" for side in ("L", "R") for i in range(1, 8)]
STATE = "/raw/marvin/joint_state"
TARGET = "/command/marvin/joint_target"
LEFT = "/raw/das/left/state"
RIGHT = "/raw/das/right/state"


class _CdrWriter:
    def __init__(self):
        self.data = bytearray(b"\x00\x01\x00\x00")

    def scalar(self, fmt: str, value) -> None:
        size = struct.calcsize(fmt)
        padding = (-(len(self.data) - 4)) % size
        self.data.extend(b"\x00" * padding)
        self.data.extend(struct.pack("<" + fmt, value))

    def string(self, value: str) -> None:
        encoded = value.encode() + b"\x00"
        self.scalar("I", len(encoded))
        self.data.extend(encoded)

    def positions(self, names: list[str], values: list[float], *, target=False) -> bytes:
        self.scalar("i", 0)
        self.scalar("I", 0)
        self.string("")
        self.scalar("I", len(names))
        for name in names:
            self.string(name)
        if target:
            self.scalar("I", 1)  # one JointTrajectoryPoint
        self.scalar("I", len(values))
        for value in values:
            self.scalar("d", value)
        return bytes(self.data)


def write_marvin_mcap(
    path: Path, *, native_frames=320, missing_left: set[int] | None = None,
    missing_left_topic=False,
) -> bytes:
    """Actual MCAP + raw CDR topics consumed by the production adapter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        writer = Writer(stream)
        writer.start()
        channels = {topic: writer.register_channel(topic, "cdr", 0) for topic in (
            STATE, TARGET, LEFT, RIGHT,
        )}
        for i in range(native_frames):
            timestamp = 1_000_000_000 + i * 20_000_000
            values = [0.2 + 0.002 * i + col * 0.01 for col in range(14)]
            for topic in (STATE, TARGET):
                payload = _CdrWriter().positions(JOINTS, values, target=topic == TARGET)
                writer.add_message(channels[topic], timestamp, payload, timestamp)
            if not missing_left_topic and i not in (missing_left or set()):
                payload = _CdrWriter().positions(["left_gripper_width"], [0.04])
                writer.add_message(channels[LEFT], timestamp, payload, timestamp)
            payload = _CdrWriter().positions(["right_gripper_width"], [0.04])
            writer.add_message(channels[RIGHT], timestamp, payload, timestamp)
        writer.finish()
    return path.read_bytes()


def _spec(root: Path, output: Path, repo: Path):
    return load_spec(CONFIG, input_root=root, output_root=output, repo_root=repo)


@pytest.mark.parametrize("parent_relative_root", [False, True])
def test_real_read_statistics_and_four_quality_stages_keep_clean_source(
    tmp_path: Path, upstream_repo: Path, parent_relative_root: bool,
) -> None:
    source = tmp_path / "sources" / "clean.mcap"
    original = write_marvin_mcap(source)
    input_root = source.parent / ".." / "sources" if parent_relative_root else source.parent
    spec = _spec(input_root, tmp_path / "reports", upstream_repo)
    cfg = quality_config(spec)
    ref = McapEpisodeRef(0, input_root, source, ".")
    batch = McapReadOperator().run_batch([ref], spec, cfg, build_adapter(spec))
    episode = load_episode(batch.records[0])
    assert episode.state.shape == (160, 16)
    assert episode.action.shape == (160, 16)
    np.testing.assert_array_equal(episode.state, episode.action)
    np.testing.assert_array_equal(
        episode.frame_times_ns, np.arange(160, dtype=np.int64) * 40_000_000 + 1_000_000_000,
    )
    assert episode.source_identity is not None
    context = GlobalStatsOperator().run(batch, spec, cfg, recompute=True)
    assert context.status == "ready", context.error
    statistics = context.statistics
    assert statistics.global_stats.num_frames == 160
    decision = QualityDetectionOperator().run(episode, spec, cfg, statistics)
    assert not decision.reject_file, decision.to_record()
    assert not decision.bad_mask.any()
    assert source.read_bytes() == original
    assert decision.to_record()["status"] == "kept"


def test_actual_batch_run_deletes_gap_files_keeps_complete_clean_file(
    tmp_path: Path, upstream_repo: Path,
) -> None:
    root, output = tmp_path / "sources", tmp_path / "reports"
    clean, gap, missing = [root / f"{name}.mcap" for name in ("clean", "gap", "missing")]
    original = write_marvin_mcap(clean)
    write_marvin_mcap(gap, missing_left=set(range(50, 180)))
    write_marvin_mcap(missing, missing_left_topic=True)
    args = [
        "run", "--config", str(CONFIG), "--repo-root", str(upstream_repo),
        "--input-root", str(root), "--output-root", str(output),
    ]
    for source in (clean, gap, missing):
        args.extend(["--mcap", str(source)])
    assert main(args) == 0
    assert clean.read_bytes() == original
    assert not gap.exists()
    assert not missing.exists()
    records = [json.loads(line) for line in (output / "decisions.jsonl").read_text().splitlines()]
    assert [record["status"] for record in records] == ["kept", "rejected", "rejected"]
    assert all(record["effective_removed_ranges"] for record in records[1:])
    summary = json.loads((output / "run_summary.json").read_text())
    assert summary["kept_files"] == 1
    assert summary["deleted_files"] == 2
    assert summary["failed_episodes"] == 0
    assert (output / "accepted" / "clean.mcap").read_bytes() == original
    assert summary["exported_files"] == 1


def test_entirely_missing_gripper_remains_nan_and_requires_diagnosed_gap(
    tmp_path: Path, upstream_repo: Path,
) -> None:
    source = tmp_path / "sources" / "missing.mcap"
    original = write_marvin_mcap(source, missing_left_topic=True)
    spec = _spec(source.parent, tmp_path / "reports", upstream_repo)
    cfg = quality_config(spec)
    ref = McapEpisodeRef(0, source.parent, source, ".")
    episode = McapReadOperator().run(ref, spec, cfg, build_adapter(spec))
    assert np.isnan(episode.state[:, 14]).all()
    assert np.isnan(episode.action[:, 14]).all()
    assert np.isfinite(episode.state[:, :14]).all()
    assert np.isfinite(episode.state[:, 15]).all()
    detector = QualityDetectionOperator()
    decision = detector.precheck(episode, spec)
    assert decision is not None
    assert decision.reject_file
    assert decision.bad_mask.all()
    assert all(decision.stage_results[name]["status"] == "skipped" for name in ("s1", "s2", "s3", "s4"))
    # Without coverage diagnosis a NaN cannot pass as a valid zero measurement.
    disabled_gap = replace(spec, gripper_gap=None)
    with pytest.raises(ValueError, match="non-finite quality values"):
        detector.run(episode, disabled_gap, cfg, None)
    assert source.read_bytes() == original


def test_all_gap_batch_completes_b02_without_numerical_fit(
    tmp_path: Path, upstream_repo: Path, monkeypatch,
) -> None:
    root, output = tmp_path / "sources", tmp_path / "reports"
    gap, missing = root / "gap.mcap", root / "missing.mcap"
    write_marvin_mcap(gap, missing_left=set(range(50, 180)))
    write_marvin_mcap(missing, missing_left_topic=True)
    stats_calls = []

    def forbidden_statistics(*args, **kwargs):
        stats_calls.append(True)
        raise AssertionError("gap-only rejection must not fit global statistics")

    monkeypatch.setattr(GlobalStatsOperator, "_fit", forbidden_statistics)
    assert main([
        "run", "--config", str(CONFIG), "--repo-root", str(upstream_repo),
        "--input-root", str(root), "--output-root", str(output),
        "--mcap", str(gap), "--mcap", str(missing),
    ]) == 0
    assert stats_calls == []
    assert not gap.exists()
    assert not missing.exists()
    summary = json.loads((output / "run_summary.json").read_text())
    assert summary["deleted_files"] == 2
    assert summary["failed_episodes"] == 0
    assert summary["statistics_error"] is None
