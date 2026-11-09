from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcap.writer import Writer

import numpy as np
from stage_b_mcap_v4 import cli, operators
from stage_b_mcap_v4.contracts import NormalizedEpisode, SourceIdentity, StatisticsBundle
from stage_b_mcap_v4.spec import GripperGapConfig


def _source(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        writer = Writer(stream)
        writer.start()
        channel = writer.register_channel("/test", "raw", 0)
        writer.add_message(channel, 1_000_000_000, b"temporary-test", 1_000_000_000)
        writer.finish()
    return path.read_bytes()


def _runtime(monkeypatch, root: Path, output: Path, *, stats_failure=False) -> None:
    """Isolate orchestration from numerics; deletion and durable reports are real."""
    spec = SimpleNamespace(input_root=root, output_root=output, gripper_gap=GripperGapConfig("/left", "/right", 600_000_000))
    monkeypatch.setattr(cli, "load_spec", lambda *args, **kwargs: spec)
    monkeypatch.setattr(cli, "quality_config", lambda spec: None)
    monkeypatch.setattr(operators, "build_adapter", lambda spec: object())

    def read(self, ref, spec, cfg, adapter):
        if ref.mcap_path.stem == "error":
            raise ValueError("synthetic unreadable payload")
        values = np.ones((4, 1))
        times = np.arange(4, dtype=np.int64) * 1_000_000_000
        observed = {"/left": np.array([], dtype=np.int64) if ref.mcap_path.stem == "gap" else times, "/right": times}
        return NormalizedEpisode(ref, values, values, times, SourceIdentity.capture(ref.mcap_path), observed)

    def stats(self, batch, spec, cfg, records, **kwargs):
        if stats_failure:
            raise ValueError("synthetic statistics failure")
        return StatisticsBundle(SimpleNamespace(num_frames=len(records) * 4), None, None, 0, output / "cache")

    def quality(self, episode, spec, cfg, statistics):
        early = self.precheck(episode, spec)
        if early is not None:
            return early
        if statistics is None:
            raise ValueError("global statistics unavailable")
        reject = episode.ref.mcap_path.stem == "bad"
        record = {
            "episode_index": episode.ref.episode_index,
            "source": str(episode.ref.mcap_path),
            "status": "rejected" if reject else "kept",
            "reject_file": reject,
            "reasons": ["stage4:static"] if reject else [],
            "detected_ranges": [{"start_seconds": 2.0, "end_seconds": 3.0}] if reject else [],
        }
        return SimpleNamespace(to_record=lambda: record)

    monkeypatch.setattr(cli.McapReadOperator, "run", read)
    monkeypatch.setattr(cli.GlobalStatsOperator, "_fit", stats)
    monkeypatch.setattr(cli.QualityDetectionOperator, "run", quality)


def _args(mode: str, root: Path, output: Path, sources: list[Path]) -> list[str]:
    args = [mode, "--config", "unused.yaml", "--input-root", str(root), "--output-root", str(output)]
    for source in sources:
        args.extend(["--mcap", str(source)])
    return args


@pytest.mark.parametrize("mode", ["quality", "run"])
def test_batch_reports_before_deletion_keeps_good_and_error_sources(
    tmp_path: Path, monkeypatch, mode: str,
) -> None:
    root, output = tmp_path / "sources", tmp_path / "reports"
    good, bad, error = [root / f"{name}.mcap" for name in ("good", "bad", "error")]
    originals = {path: _source(path) for path in (good, bad, error)}
    _runtime(monkeypatch, root, output)
    real_unlink = os.unlink
    deletion_observed = []

    def unlink(path, *, dir_fd=None):
        decisions = [json.loads(line) for line in (output / "decisions.jsonl").read_text().splitlines()]
        plan = json.loads((output / "deletion_plan.json").read_text())
        assert len(decisions) == 3
        assert plan["rejected_files"] == 1
        assert plan["records"][0]["source"] == str(bad)
        assert json.loads((output / "deletion_events.jsonl").read_text().splitlines()[-1])["event"] == "delete_intent"
        deletion_observed.append(path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    # An unreadable Episode is reported and preserved, so the batch signals it.
    assert cli.main(_args(mode, root, output, [good, bad, error])) == 1
    assert good.read_bytes() == originals[good]
    assert error.read_bytes() == originals[error]
    if mode == "quality":
        assert bad.read_bytes() == originals[bad]
        assert deletion_observed == []
        assert not (output / "deletion_events.jsonl").exists()
    else:
        assert not bad.exists()
        assert deletion_observed == [bad.name]
    records = [json.loads(line) for line in (output / "decisions.jsonl").read_text().splitlines()]
    assert [record["status"] for record in records] == ["kept", "rejected", "error"]
    assert records[2]["retained_due_to_error"] is True
    assert records[2]["deletion_eligible"] is False
    summary = json.loads((output / "run_summary.json").read_text())
    assert summary["kept_files"] == 1
    assert summary["rejected_files"] == 1
    assert summary["failed_episodes"] == 1
    assert summary["deleted_files"] == (1 if mode == "run" else 0)
    assert (output / "accepted" / "good.mcap").read_bytes() == originals[good]
    assert list((output / "accepted").rglob("*.mcap")) == [output / "accepted" / "good.mcap"]
    assert summary["exported_files"] == 1


def test_global_statistics_failure_retains_all_sources(tmp_path: Path, monkeypatch) -> None:
    root, output = tmp_path / "sources", tmp_path / "reports"
    sources = [root / "good.mcap", root / "bad.mcap"]
    originals = {path: _source(path) for path in sources}
    _runtime(monkeypatch, root, output, stats_failure=True)
    assert cli.main(_args("run", root, output, sources)) == 1
    assert all(source.read_bytes() == original for source, original in originals.items())
    summary = json.loads((output / "run_summary.json").read_text())
    assert summary["deleted_files"] == 0
    assert summary["failed_episodes"] == 2
    assert "statistics failure" in summary["statistics_error"]


def test_statistics_failure_also_retains_final_gripper_rejection(tmp_path: Path, monkeypatch) -> None:
    root, output = tmp_path / "sources", tmp_path / "reports"
    gap, good = root / "gap.mcap", root / "good.mcap"
    originals = {path: _source(path) for path in (gap, good)}
    _runtime(monkeypatch, root, output, stats_failure=True)

    def precheck(self, episode, spec):
        if episode.ref.mcap_path == good:
            return None
        return SimpleNamespace(to_record=lambda: {
            "episode_index": episode.ref.episode_index,
            "source": str(episode.ref.mcap_path), "status": "rejected",
            "reject_file": True, "reasons": ["gripper_gap:invalid_frames=1"],
            "detected_ranges": [],
        })

    monkeypatch.setattr(cli.QualityDetectionOperator, "precheck", precheck)
    assert cli.main(_args("run", root, output, [gap, good])) == 1
    assert all(path.read_bytes() == original for path, original in originals.items())
    records = [json.loads(line) for line in (output / "decisions.jsonl").read_text().splitlines()]
    assert [record["status"] for record in records] == ["rejected", "error"]
    assert records[0]["deletion_eligible"] is False
    assert "global statistics" in records[0]["deletion_blocked_reason"]
    summary = json.loads((output / "run_summary.json").read_text())
    assert summary["deleted_files"] == 0
    assert summary["deletion_skipped_files"] == 1
    assert summary["statistics_error"] is not None


def test_cli_refuses_to_overwrite_existing_reports(tmp_path: Path, monkeypatch) -> None:
    root, output = tmp_path / "sources", tmp_path / "reports"
    bad = root / "bad.mcap"
    original = _source(bad)
    output.mkdir()
    report = output / "decisions.jsonl"
    report.write_text("previous report\n")
    _runtime(monkeypatch, root, output)
    with pytest.raises(FileExistsError):
        cli.main(_args("run", root, output, [bad]))
    assert bad.read_bytes() == original
    assert report.read_text() == "previous report\n"


def test_explicit_mcap_outside_input_root_is_rejected(tmp_path: Path, monkeypatch) -> None:
    root, output = tmp_path / "sources", tmp_path / "reports"
    root.mkdir()
    outside = tmp_path / "outside.mcap"
    original = _source(outside)
    _runtime(monkeypatch, root, output)
    with pytest.raises(ValueError, match="below --input-root"):
        cli.main(_args("run", root, output, [outside]))
    assert outside.read_bytes() == original
