from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from mcap.writer import Writer

from stage_b_mcap_v4.contracts import McapEpisodeRef, QualityEpisode, SourceIdentity, StatisticsBundle
from stage_b_mcap_v4.operators import QualityDetectionOperator
from stage_b_mcap_v4.spec import load_spec, quality_config


@pytest.fixture
def prepared(tmp_path: Path, upstream_repo: Path):
    root = tmp_path / "sources"
    root.mkdir()
    source = root / "test.mcap"
    with source.open("wb") as stream:
        writer = Writer(stream)
        writer.start()
        channel = writer.register_channel("/reference", "raw", 0)
        writer.add_message(channel, 1_000_000_000, b"test", 1_000_000_000)
        writer.finish()
    spec = load_spec(
        Path(__file__).resolve().parents[1] / "configs" / "jindian_marvin.yaml",
        input_root=root, output_root=tmp_path / "reports", repo_root=upstream_repo,
    )
    spec = replace(spec, gripper_gap=None)
    cfg = quality_config(spec)
    n = 160
    state = 0.2 + np.arange(n, dtype=float)[:, None] * np.ones((1, 16)) * 0.002
    episode = QualityEpisode(
        McapEpisodeRef(0, root, source, "."), state, state.copy(),
        np.arange(n, dtype=np.int64) * 40_000_000 + 1_000_000_000,
        source_identity=SourceIdentity.capture(source),
    )
    statistics = StatisticsBundle(None, None, None, 0, tmp_path / "cache")
    return episode, spec, cfg, statistics


def _stage_outcomes(monkeypatch, n: int, failure: str):
    from robot_data_processing.stages import (
        stage1_sudden_change as s1, stage2_trend_alignment as s2,
        stage3_extreme_value as s3, stage4_static_interval as s4,
    )
    flags = {}
    for name in ("stage1", "stage3", "stage4"):
        flags[name] = np.zeros(n, dtype=bool)
        if failure == name:
            flags[name][50] = True
    first = s1.Stage1Result(
        failure == "stage1_discard", ["synthetic fault"] if failure == "stage1_discard" else [],
        flags["stage1"], int(flags["stage1"].sum()), False,
    )
    second = s2.Stage2Result(
        failure == "stage2", ["synthetic misalignment"] if failure == "stage2" else [],
        0.0 if failure == "stage2" else 1.0, [1.0] * 14, [0] * 14,
    )
    third = s3.Stage3Result(
        failure == "stage3_discard", ["synthetic failure"] if failure == "stage3_discard" else [],
        flags["stage3"], int(flags["stage3"].sum()),
    )
    fourth = s4.Stage4Result(flags["stage4"], int(flags["stage4"].sum()), 0, int(flags["stage4"].sum()))
    calls = []

    def wrap(name, result):
        def run(*args, **kwargs):
            if name == "stage2":
                length = len(next(iter(args[0].values())))
            else:
                length = len(args[0])
            calls.append((name, length))
            return result
        return run

    monkeypatch.setattr(s1, "run_stage1", wrap("stage1", first))
    monkeypatch.setattr(s2, "run_stage2_on_fields", wrap("stage2", second))
    monkeypatch.setattr(s3, "run_stage3", wrap("stage3", third))
    monkeypatch.setattr(s4, "run_stage4", wrap("stage4", fourth))
    return calls


@pytest.mark.parametrize("failure", [
    "stage1", "stage1_discard", "stage2", "stage3", "stage3_discard", "stage4",
])
def test_any_individual_stage_failure_rejects_whole_file(
    prepared, monkeypatch, failure: str,
) -> None:
    episode, spec, cfg, statistics = prepared
    calls = _stage_outcomes(monkeypatch, episode.num_frames, failure)
    decision = QualityDetectionOperator().run(episode, spec, cfg, statistics)
    assert decision.reject_file
    assert decision.reasons
    # v4 checks full source quality rather than salvaging an accepted prefix.
    assert calls == [(name, episode.num_frames) for name in ("stage1", "stage2", "stage3", "stage4")]
    if failure in ("stage1", "stage3", "stage4"):
        assert np.flatnonzero(decision.bad_mask).tolist() == [50]
    record = decision.to_record()
    assert record["status"] == "rejected"
    assert len(record["effective_removed_ranges"]) == 1
    assert record["effective_removed_ranges"][0]["start_frame"] == 0
    assert record["effective_removed_ranges"][0]["end_frame_exclusive"] == episode.num_frames
    assert episode.ref.mcap_path.exists()  # judging alone is non-destructive


def test_full_pass_keeps_every_frame_and_does_not_write_new_mcap(prepared, monkeypatch) -> None:
    episode, spec, cfg, statistics = prepared
    original = episode.ref.mcap_path.read_bytes()
    _stage_outcomes(monkeypatch, episode.num_frames, "none")
    decision = QualityDetectionOperator().run(episode, spec, cfg, statistics)
    assert not decision.reject_file
    assert not decision.bad_mask.any()
    record = decision.to_record()
    assert record["status"] == "kept"
    assert record["detected_ranges"] == []
    assert record["effective_removed_ranges"] == []
    assert episode.ref.mcap_path.read_bytes() == original
    assert not list(spec.output_root.rglob("*.mcap"))


def test_source_identity_capture_matches_original_regular_file(tmp_path: Path) -> None:
    source = tmp_path / "source.mcap"
    source.write_bytes(b"identity fixture")
    identity = SourceIdentity.capture(source)
    actual = source.lstat()
    assert identity.st_dev == actual.st_dev
    assert identity.st_ino == actual.st_ino
    assert identity.st_size == actual.st_size
    assert identity.st_mtime_ns == actual.st_mtime_ns
