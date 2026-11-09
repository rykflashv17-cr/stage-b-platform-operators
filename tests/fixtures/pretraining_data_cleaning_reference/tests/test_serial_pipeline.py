"""Real three-stage orchestration/storage; explicit numerical engine doubles."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import numpy as np
import pytest

from stage_b_mcap_v4 import cli, operators
from stage_b_mcap_v4.delivery import McapDeliveryOperator
from stage_b_mcap_v4.contracts import McapEpisodeRef
from stage_b_mcap_v4.storage import load_batch, load_episode
from test_p0_input_contract import input_runtime, modified_source
from test_mcap_integration import write_marvin_mcap, _CdrWriter, LEFT, STATE


@pytest.fixture
def numerical_engine(input_runtime, monkeypatch):
    """Loaders exercise the real normalized hook, not the private upstream math."""
    spec, cfg, adapter = input_runtime
    cfg.stage1, cfg.stage2, cfg.stage3 = NS(), NS(), NS()
    cfg.stage4, cfg.state_action_alignment, cfg.stats_num_bins = NS(enabled=True), NS(), 64
    calls = []

    def module(name, **attrs):
        value = ModuleType(name)
        value.__path__ = []
        for key, item in attrs.items():
            setattr(value, key, item)
        monkeypatch.setitem(sys.modules, name, value)
        parent, _, leaf = name.rpartition(".")
        if parent in sys.modules:
            monkeypatch.setattr(sys.modules[parent], leaf, value, raising=False)
        return value

    previous = object()
    hooks = module("robot_data_processing.adapter_hooks", _ADAPTER=previous, _PACKAGE_DIR=None)
    hooks.get_adapter = lambda: hooks._ADAPTER

    def loader(label):
        def run(root, cache, ids, schema, *a, **kw):
            assert Path(root).name == "normalized"
            arrays = [hooks.get_adapter().load_quality_arrays(root, index, schema) for index in ids]
            calls.append((label, list(ids), [len(x["state"]) for x in arrays]))
            return NS(num_frames=sum(len(x["state"]) for x in arrays))
        return run

    module("robot_data_processing.stats", load_or_compute_stats=loader("s3_fit"))
    module("robot_data_processing.stage1_stats", load_or_compute_stage1_stats=loader("s1_fit"))
    module("robot_data_processing.state_action_lag_stats", load_or_compute_action_state_lag=loader("lag_fit"))
    module("robot_data_processing.stages")
    module("robot_data_processing.stages.state_action_temporal_alignment", StateActionAlignConfig=NS,
           resolve_alignment_lag=lambda cfg, stats: 0)

    def s1(state, action, config, **kw):
        assert kw["startup_exclude_per_joint"].shape == action.shape
        bad = state[:, 0] > 3.5
        calls.append(("s1_judge", len(state)))
        return NS(abnormal_frames=bad, discard=False, discard_reasons=[], flagged_count=int(bad.sum()),
                  hard_limit_hit=False, gripper_faults={}, rotation_joint_steps=0)

    def s2(fields, config, **kw):
        return NS(discard=False, discard_reasons=[], da_mean=1.0, da_per_dim=[1.0] * 14,
                  dim_names=[], lags=[0] * 14, episode_lag=0, lag_source="controlled")

    def s3(state, action, stats, config, **kw):
        assert kw["startup_exclude_per_joint"].shape == action.shape
        return NS(remove_frames=np.zeros(len(state), dtype=bool), discard=False,
                  discard_reasons=[], excluded_count=0)

    def s4(state, action, config):
        return NS(remove_frames=np.zeros(len(state), dtype=bool), removed_count=0,
                  static_runs_shortened=0, static_frames_removed=0)

    module("robot_data_processing.stages.stage1_sudden_change", Stage1Config=NS, run_stage1=s1)
    module("robot_data_processing.stages.stage2_trend_alignment", Stage2Config=NS,
           canonical_to_stage2_fields=lambda *a, **kw: {}, run_stage2_on_fields=s2)
    module("robot_data_processing.stages.stage3_extreme_value", Stage3Config=NS, run_stage3=s3)
    module("robot_data_processing.stages.stage4_static_interval", Stage4Config=lambda: NS(enabled=True), run_stage4=s4)
    return spec, cfg, adapter, calls, hooks, previous


def refs(paths):
    return [McapEpisodeRef(i, p.parent, p, p.name) for i, p in enumerate(paths)]


def cli_runtime(monkeypatch, spec, cfg, adapter):
    monkeypatch.setattr(cli, "load_spec", lambda *a, **kw: spec)
    monkeypatch.setattr(cli, "quality_config", lambda spec: cfg)
    monkeypatch.setattr(operators, "build_adapter", lambda spec: adapter)


def arguments(mode, paths):
    result = [mode, "--config", "unused"]
    for path in paths:
        result += ["--mcap", str(path)]
    return result


def gap_source(tmp_path):
    empty = _CdrWriter().positions([], [])
    return modified_source(tmp_path, lambda topic, i, data: empty if topic == LEFT and i > 0 else data)


@pytest.mark.parametrize("mode", ["quality", "run"])
def test_strict_stage_barriers_decode_once_and_keep_complete_batch(numerical_engine, monkeypatch, tmp_path, mode):
    spec, cfg, adapter, engine_calls, hooks, previous = numerical_engine
    clean = tmp_path / "clean.mcap"
    write_marvin_mcap(clean)
    gap = gap_source(tmp_path / "gap")
    empty = _CdrWriter().positions([], [])
    state = modified_source(tmp_path / "state", lambda topic, i, data:
                            empty if topic == STATE and i == 50 else data)
    broken = tmp_path / "broken.mcap"
    broken.write_bytes(b"invalid MCAP")
    paths = [clean, gap, state, broken]
    originals = {p: p.read_bytes() for p in paths}
    cli_runtime(monkeypatch, spec, cfg, adapter)
    events, validated, decoded = [], [], []
    phase = [None]
    for name, cls, method in [("B01", operators.McapReadOperator, "run_batch"),
                              ("B02", operators.GlobalStatsOperator, "run"),
                              ("B03", operators.QualityDetectionOperator, "run_batch"),
                              ("B04", McapDeliveryOperator, "run")]:
        original = getattr(cls, method)
        def wrapped(self, *a, _name=name, _original=original, **kw):
            phase[0] = _name
            events.append(_name + ".start")
            result = _original(self, *a, **kw)
            events.append(_name + ".end")
            return result
        monkeypatch.setattr(cls, method, wrapped)
    original_validate = operators.validate_mcap
    def validate(path):
        assert phase[0] == "B01", "container admission belongs exclusively to B01"
        validated.append(paths.index(path))
        return original_validate(path)
    monkeypatch.setattr(operators, "validate_mcap", validate)
    original_read = adapter.load_raw
    def raw(ref):
        assert phase[0] == "B01"
        assert ref.episode_index in validated, "container admission must precede numerical decoding"
        decoded.append(ref.episode_index)
        return original_read(ref)
    monkeypatch.setattr(adapter, "load_raw", raw)
    original_precheck = operators.QualityDetectionOperator.precheck
    def precheck(self, *a):
        assert phase[0] == "B03", "quality must never run before statistics completes"
        return original_precheck(self, *a)
    monkeypatch.setattr(operators.QualityDetectionOperator, "precheck", precheck)
    for name in ("compute_per_joint_action_zero_exclude", "compute_per_joint_stage1_exclude"):
        def mask(action, *a):
            assert phase[0] == "B03", "quality masks belong exclusively to B03"
            return np.zeros_like(action, dtype=bool)
        monkeypatch.setattr(sys.modules["robot_data_processing.mask"], name, mask)
    assert cli.main(arguments(mode, paths)) == 1  # unreadable input is preserved/reported
    assert events == ["B01.start", "B01.end", "B02.start", "B02.end", "B03.start", "B03.end", "B04.start", "B04.end"]
    assert validated == [0, 1, 2, 3]
    assert decoded == [0, 1, 2]  # malformed input stops before the adapter decoder
    assert [c[1] for c in engine_calls if c[0].endswith("_fit")] == [[0, 2]] * 3
    assert hooks._ADAPTER is previous
    records = [json.loads(line) for line in (spec.output_root / "decisions.jsonl").read_text().splitlines()]
    assert [r["status"] for r in records] == ["kept", "rejected", "kept", "error"]
    assert records[1]["detected_bad_frames"] == 144
    assert gap.exists() == (mode == "quality")
    assert all(p.read_bytes() == originals[p] for p in (clean, state, broken))
    for path in (clean, state):
        assert (spec.output_root / "accepted" / path.relative_to(spec.input_root)).read_bytes() == originals[path]
    assert len(list((spec.output_root / "accepted").rglob("*.mcap"))) == 2
    batch = load_batch(spec.output_root / "normalized" / "manifest.json")
    assert len(batch.records) == 4 and batch.records[3].input_error
    normalized = load_episode(batch.records[2])
    indices = np.delete(np.arange(320), 50)[::2]
    np.testing.assert_array_equal(normalized.frame_times_ns, 1_000_000_000 + indices * 20_000_000)
    assert not hasattr(normalized, "stage1_exclude")
    for item in batch.records[:3]:
        with np.load(item.data_path, allow_pickle=False) as payload:
            assert not any("exclude" in name for name in payload.files)
    print("FOUR_STAGE_TRACE", json.dumps({"mode": mode, "events": events, "container_validations": validated,
                                      "source_decodes": decoded,
                                      "fit_ids": [0, 2], "statuses": [r["status"] for r in records]}))


def test_all_gap_runs_b02_empty_then_b03_without_engine(input_runtime, monkeypatch, tmp_path):
    spec, cfg, adapter = input_runtime
    path = gap_source(tmp_path)
    cli_runtime(monkeypatch, spec, cfg, adapter)
    calls = []
    original = operators.GlobalStatsOperator.run
    def b02(self, *a, **kw):
        context = original(self, *a, **kw)
        calls.append(context.status)
        return context
    monkeypatch.setattr(operators.GlobalStatsOperator, "run", b02)
    monkeypatch.setattr(operators.GlobalStatsOperator, "_fit", lambda *a, **kw: pytest.fail("empty fit must not invoke engine"))
    assert cli.main(arguments("run", [path])) == 0
    assert calls == ["empty"] and not path.exists()
    summary = json.loads((spec.output_root / "run_summary.json").read_text())
    assert summary["statistics_status"] == "empty" and summary["deleted_files"] == 1


def test_empty_batch_completes_all_three_stages(input_runtime):
    spec, cfg, adapter = input_runtime
    batch = operators.McapReadOperator().run_batch([], spec, cfg, adapter)
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    result = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    assert context.status == "empty"
    assert result.exit_code == 0 and result.summary["episodes"] == 0
    assert json.loads(batch.manifest_path.read_text())["records"] == []


def test_all_input_errors_remain_errors_not_quality_rejections(input_runtime, tmp_path):
    spec, cfg, adapter = input_runtime
    path = tmp_path / "broken.mcap"
    path.write_bytes(b"broken")
    batch = operators.McapReadOperator().run_batch(refs([path]), spec, cfg, adapter)
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    result = McapDeliveryOperator().run(judged, mode="run")
    assert context.status == "empty" and result.exit_code == 1 and path.exists()
    assert result.summary["failed_episodes"] == 1 and result.summary["rejected_files"] == 0


def test_disabled_gap_with_unknown_values_cannot_pass_or_enter_statistics(input_runtime, tmp_path):
    spec, cfg, adapter = input_runtime
    spec = replace(spec, gripper_gap=None)
    path = tmp_path / "unknown.mcap"
    write_marvin_mcap(path, missing_left_topic=True)
    batch = operators.McapReadOperator().run_batch(refs([path]), spec, cfg, adapter)
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    result = McapDeliveryOperator().run(judged, mode="run")
    assert context.status == "empty" and context.fit_records[0]["fit_status"] == "excluded"
    assert result.exit_code == 1 and result.summary["deleted_files"] == 0 and path.exists()


def test_stats_failure_still_diagnoses_gap_but_blocks_all_deletions(numerical_engine, monkeypatch, tmp_path):
    spec, cfg, adapter, _, hooks, previous = numerical_engine
    gap = gap_source(tmp_path / "gap")
    clean = tmp_path / "clean.mcap"
    write_marvin_mcap(clean)
    def fail(*a, **kw):
        raise RuntimeError("controlled engine failure")
    monkeypatch.setattr(sys.modules["robot_data_processing.stats"], "load_or_compute_stats", fail)
    batch = operators.McapReadOperator().run_batch(refs([gap, clean]), spec, cfg, adapter)
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    assert context.status == "failed" and hooks._ADAPTER is previous
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    result = McapDeliveryOperator().run(judged, mode="run")
    records = [json.loads(line) for line in (spec.output_root / "decisions.jsonl").read_text().splitlines()]
    assert [r["status"] for r in records] == ["rejected", "error"]
    assert result.exit_code == 1 and result.summary["deleted_files"] == 0
    assert gap.exists() and clean.exists()


def test_damaged_normalized_file_invalidates_fit_and_preserves_batch(input_runtime, tmp_path):
    spec, cfg, adapter = input_runtime
    clean, gap = tmp_path / "clean.mcap", gap_source(tmp_path / "gap")
    write_marvin_mcap(clean)
    batch = operators.McapReadOperator().run_batch(refs([clean, gap]), spec, cfg, adapter)
    batch.records[0].data_path.write_bytes(b"damaged intermediate")
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    result = McapDeliveryOperator().run(judged, mode="run")
    assert context.status == "failed" and result.exit_code == 1
    assert clean.exists() and gap.exists() and result.summary["deleted_files"] == 0


def test_quality_cannot_silently_change_statistics_admission_policy(input_runtime, tmp_path):
    spec, cfg, adapter = input_runtime
    path = gap_source(tmp_path)
    batch = operators.McapReadOperator().run_batch(refs([path]), spec, cfg, adapter)
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    with pytest.raises(ValueError, match="coverage policy changed"):
        operators.QualityDetectionOperator().run_batch(context, replace(spec, gripper_gap=None), cfg)
    assert path.exists() and not (spec.output_root / "decisions.jsonl").exists()


def test_b01_needs_no_quality_mask_configuration(input_runtime, tmp_path, monkeypatch):
    spec, cfg, adapter = input_runtime
    path = tmp_path / "clean.mcap"
    write_marvin_mcap(path)
    for name in ("compute_per_joint_action_zero_exclude", "compute_per_joint_stage1_exclude"):
        monkeypatch.setattr(sys.modules["robot_data_processing.mask"], name, lambda *a: pytest.fail("B01 must not prepare quality rules"))
    batch = operators.McapReadOperator().run_batch(refs([path]), spec, NS(schema=cfg.schema), adapter)
    assert batch.records[0].input_error is None
    saved = load_episode(batch.records[0])
    assert not hasattr(saved, "startup_exclude")


def test_stats_command_is_b01_then_b02_only(numerical_engine, monkeypatch, tmp_path):
    spec, cfg, adapter, calls, _, _ = numerical_engine
    path = tmp_path / "clean.mcap"
    write_marvin_mcap(path)
    cli_runtime(monkeypatch, spec, cfg, adapter)
    monkeypatch.setattr(operators.QualityDetectionOperator, "run_batch", lambda *a, **kw: pytest.fail("stats is the two-stage debug prefix"))
    assert cli.main(arguments("stats", [path])) == 0
    assert len([c for c in calls if c[0].endswith("_fit")]) == 3
    assert (spec.output_root / "normalized" / "manifest.json").exists()
    assert not (spec.output_root / "decisions.jsonl").exists()


def test_engine_without_fitted_frames_is_failure_not_ready(numerical_engine, monkeypatch, tmp_path):
    spec, cfg, adapter, _, hooks, previous = numerical_engine
    path = tmp_path / "clean.mcap"
    write_marvin_mcap(path)
    monkeypatch.setattr(sys.modules["robot_data_processing.stats"], "load_or_compute_stats",
                        lambda *a, **kw: NS(num_frames=0))
    batch = operators.McapReadOperator().run_batch(refs([path]), spec, cfg, adapter)
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    assert context.status == "failed" and "no fitted frames" in context.error
    assert hooks._ADAPTER is previous
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    result = McapDeliveryOperator().run(judged, mode="run")
    assert result.exit_code == 1 and result.summary["deleted_files"] == 0 and path.exists()


def test_intermediate_storage_failure_stops_before_statistics(input_runtime, monkeypatch, tmp_path):
    spec, cfg, adapter = input_runtime
    path = tmp_path / "clean.mcap"
    write_marvin_mcap(path)
    original = path.read_bytes()
    cli_runtime(monkeypatch, spec, cfg, adapter)
    def fail(*a, **kw):
        raise OSError("controlled normalized storage failure")
    monkeypatch.setattr(operators, "save_episode", fail)
    monkeypatch.setattr(operators.GlobalStatsOperator, "run", lambda *a, **kw: pytest.fail("B01 must complete first"))
    monkeypatch.setattr(operators.QualityDetectionOperator, "run_batch", lambda *a, **kw: pytest.fail("B01 must complete first"))
    with pytest.raises(OSError, match="normalized storage failure"):
        cli.main(arguments("run", [path]))
    assert path.read_bytes() == original
    assert not (spec.output_root / "normalized" / "manifest.json").exists()
    assert not (spec.output_root / "decisions.jsonl").exists()


def test_quality_owns_distinct_startup_and_grace_masks_and_whole_discard(numerical_engine, monkeypatch, tmp_path):
    spec, cfg, adapter, _, _, _ = numerical_engine
    path = tmp_path / "clean.mcap"
    write_marvin_mcap(path)
    batch = operators.McapReadOperator().run_batch(refs([path]), spec, cfg, adapter)
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    assert context.status == "ready"
    episode = load_episode(batch.records[0])
    startup, grace = np.zeros_like(episode.action, dtype=bool), np.zeros_like(episode.action, dtype=bool)
    startup[:5], grace[:10] = True, True
    def startup_rule(action, epsilon):
        assert epsilon == cfg.action_zero_epsilon
        return startup
    def grace_rule(action, epsilon, frames):
        assert epsilon == cfg.action_zero_epsilon and frames == cfg.stage1_post_zero_grace_frames
        return grace
    masks = sys.modules["robot_data_processing.mask"]
    monkeypatch.setattr(masks, "compute_per_joint_action_zero_exclude", startup_rule)
    monkeypatch.setattr(masks, "compute_per_joint_stage1_exclude", grace_rule)
    calls = []
    for module, method, label in [
        ("stage1_sudden_change", "run_stage1", "s1"),
        ("stage3_extreme_value", "run_stage3", "s3"),
        ("stage4_static_interval", "run_stage4", "s4"),
    ]:
        target = sys.modules["robot_data_processing.stages." + module]
        original = getattr(target, method)
        def wrapped(state, *a, _original=original, _label=label, **kw):
            assert len(state) == episode.num_frames
            if _label != "s4":
                np.testing.assert_array_equal(kw["startup_exclude_per_joint"], grace if _label == "s1" else startup)
            result = _original(state, *a, **kw)
            if _label == "s1":
                result.abnormal_frames[:] = False
                result.discard, result.discard_reasons = True, ["controlled whole-episode discard"]
            calls.append(_label)
            return result
        monkeypatch.setattr(target, method, wrapped)
    result = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    decision = json.loads((spec.output_root / "decisions.jsonl").read_text())
    assert result.exit_code == 0 and calls == ["s1", "s3", "s4"]
    assert decision["status"] == "rejected" and decision["detected_bad_frames"] == 0
    assert decision["effective_removed_ranges"][0]["end_frame_exclusive"] == episode.num_frames
    assert path.exists()
