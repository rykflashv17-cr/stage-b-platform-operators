"""P0 regressions using real MCAP/CDR and production input/coverage code.

Only unavailable upstream containers and startup-mask functions are doubled.
These tests do not claim to validate upstream S1-S4 or fitted statistics.
"""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import numpy as np
import pytest
from mcap.reader import make_reader
from mcap.writer import Writer

from stage_b_mcap_v4 import cli, operators
from stage_b_mcap_v4.contracts import McapEpisodeRef, QualityEpisode, quality_timeline
from stage_b_mcap_v4.spec import GripperGapConfig, McapSpec
from stage_b_mcap_v4.storage import load_episode
from test_mcap_integration import _CdrWriter, write_marvin_mcap, LEFT, RIGHT, STATE


@pytest.fixture
def input_runtime(tmp_path, monkeypatch):
    """Explicit, scoped upstream type/mask doubles; no numerical engine."""
    def module(name, **attrs):
        value = ModuleType(name)
        value.__path__ = []
        for key, item in attrs.items():
            setattr(value, key, item)
        monkeypatch.setitem(sys.modules, name, value)
        parent, _, leaf = name.rpartition(".")
        if parent in sys.modules:
            monkeypatch.setattr(sys.modules[parent], leaf, value, raising=False)

    module("robot_data_processing")
    module("robot_data_processing.normalize")
    module("robot_data_processing.normalize.standard_types", EpisodeRef=NS, StandardEpisode=NS)
    module("robot_data_processing.mask",
           compute_per_joint_action_zero_exclude=lambda x, *a: np.zeros_like(x, dtype=bool),
           compute_per_joint_stage1_exclude=lambda x, *a: np.zeros_like(x, dtype=bool))
    module("robot_data_processing.preprocess", expand_compact_exclude_to_action=lambda x, *a: x)
    module("robot_data_processing.transforms", robomind_ur_compact_teleop=lambda s, a: (s, a))
    monkeypatch.setattr(operators, "setup_imports", lambda spec: None)
    package = Path(__file__).resolve().parents[1] / "dataset_adapters" / "jindian_marvin"
    definition = importlib.util.spec_from_file_location("p0_marvin_adapter", package / "adapter.py")
    adapter_module = importlib.util.module_from_spec(definition)
    definition.loader.exec_module(adapter_module)
    adapter = adapter_module.build_adapter(package)
    spec = McapSpec(tmp_path / "upstream", package, package / "quality_stage_b.yaml",
                    tmp_path, tmp_path / "reports", (STATE,), (), ("train",), 1,
                    gripper_gap=GripperGapConfig(LEFT, RIGHT, 600_000_000))
    cfg = NS(schema=NS(pipeline_state_dim=16, pipeline_action_dim=16, embodiment="jindian_marvin"),
             action_zero_epsilon=1e-4, stage1_post_zero_grace_frames=30)
    return spec, cfg, adapter


def modified_source(tmp_path, transform):
    original, target = tmp_path / "original.mcap", tmp_path / "fixture.mcap"
    write_marvin_mcap(original)
    with original.open("rb") as stream:
        messages = list(make_reader(stream).iter_messages())
    with target.open("wb") as stream:
        writer = Writer(stream)
        writer.start()
        channels = {}
        for _, channel, message in messages:
            if channel.topic not in channels:
                channels[channel.topic] = writer.register_channel(channel.topic, "cdr", 0)
            index = (message.log_time - 1_000_000_000) // 20_000_000
            data = transform(channel.topic, index, message.data)
            writer.add_message(channels[channel.topic], message.log_time, data,
                               message.publish_time, message.sequence)
        writer.finish()
    return target


def read(path, runtime):
    spec, cfg, adapter = runtime
    ref = McapEpisodeRef(0, path.parent, path, path.name)
    return operators.McapReadOperator().run(ref, spec, cfg, adapter)


def test_clean_source_preserves_arrays_and_exact_sampling(input_runtime, tmp_path):
    path = tmp_path / "clean.mcap"
    write_marvin_mcap(path)
    episode = read(path, input_runtime)
    assert episode.state.shape == episode.action.shape == (160, 16)
    np.testing.assert_array_equal(episode.state, episode.action)
    np.testing.assert_array_equal(episode.frame_times_ns,
                                  np.arange(160) * 40_000_000 + 1_000_000_000)
    assert all(len(v) == 320 for v in episode.valid_measurement_times_ns.values())
    assert operators.QualityDetectionOperator().precheck(episode, input_runtime[0]) is None


@pytest.mark.parametrize("side", [LEFT, RIGHT])
@pytest.mark.parametrize("invalid", ["empty", "wrong_field", "nan", "infinity"])
def test_invalid_gripper_messages_do_not_refresh_measurement_age(input_runtime, tmp_path, invalid, side):
    name = "left_gripper_width" if side == LEFT else "right_gripper_width"
    names, values = {
        "empty": ([], []),
        "wrong_field": (["unrelated_sensor"], [0.04]),
        "nan": ([name], [float("nan")]),
        "infinity": ([name], [float("inf")]),
    }[invalid]
    invalid_data = _CdrWriter().positions(names, values)
    path = modified_source(tmp_path, lambda topic, i, data:
                           invalid_data if topic == side and i > 0 else data)
    episode = read(path, input_runtime)
    assert episode.valid_measurement_times_ns[side].tolist() == [1_000_000_000]
    assert np.isfinite(episode.state).all()
    decision = operators.QualityDetectionOperator().precheck(episode, input_runtime[0])
    assert decision.reject_file
    expected = episode.frame_times_ns - 1_000_000_000 > 600_000_000
    np.testing.assert_array_equal(decision.bad_mask, expected)
    assert decision.bad_mask.sum() == 144
    assert all(decision.stage_results[s]["status"] == "skipped" for s in ("s1", "s2", "s3", "s4"))


def test_all_empty_measurements_are_explicitly_missing(input_runtime, tmp_path):
    empty = _CdrWriter().positions([], [])
    path = modified_source(tmp_path, lambda topic, i, data: empty if topic == LEFT else data)
    episode = read(path, input_runtime)
    assert episode.valid_measurement_times_ns[LEFT].dtype == np.int64
    assert len(episode.valid_measurement_times_ns[LEFT]) == 0
    assert np.isnan(episode.state[:, 14]).all()
    decision = operators.QualityDetectionOperator().precheck(episode, input_runtime[0])
    assert decision.reject_file and decision.bad_mask.all()


def test_missing_topic_is_an_explicit_empty_measurement_array(input_runtime, tmp_path):
    path = tmp_path / "absent_left.mcap"
    write_marvin_mcap(path, missing_left_topic=True)
    episode = read(path, input_runtime)
    assert LEFT in episode.valid_measurement_times_ns
    assert len(episode.valid_measurement_times_ns[LEFT]) == 0
    assert operators.QualityDetectionOperator().precheck(episode, input_runtime[0]).bad_mask.all()


@pytest.mark.parametrize("empty_indices", [(0,), (50,), (319,), (50, 51)])
def test_empty_state_keeps_adapter_times_instead_of_reconstructing_them(input_runtime, tmp_path, empty_indices):
    empty = _CdrWriter().positions([], [])
    path = modified_source(tmp_path, lambda topic, i, data:
                           empty if topic == STATE and i in empty_indices else data)
    episode = read(path, input_runtime)
    indices = np.delete(np.arange(320), empty_indices)[::2]
    expected = 1_000_000_000 + indices * 20_000_000
    np.testing.assert_array_equal(episode.frame_times_ns, expected)
    np.testing.assert_allclose(episode.state[:, 0], 0.2 + indices * 0.002)
    if empty_indices == (50,):
        assert episode.frame_times_ns[25] == 2_020_000_000
        assert episode.num_frames == 160


def test_wrong_reference_topic_cannot_change_adapter_timeline(input_runtime, tmp_path):
    path = tmp_path / "source.mcap"
    write_marvin_mcap(path)
    spec, cfg, adapter = input_runtime
    expected = read(path, input_runtime)
    changed = read(path, (replace(spec, reference_topics=("/not-a-quality-clock",)), cfg, adapter))
    np.testing.assert_array_equal(changed.frame_times_ns, expected.frame_times_ns)


def test_quality_coverage_uses_only_normalized_measurements(input_runtime, tmp_path):
    path = tmp_path / "source.mcap"
    write_marvin_mcap(path)
    episode = read(path, input_runtime)
    # B03 must be usable after B01, without reopening the source MCAP.
    absent = replace(episode.ref, mcap_path=tmp_path / "no-source-access.mcap")
    episode = replace(episode, ref=absent)
    assert operators.QualityDetectionOperator().precheck(episode, input_runtime[0]) is None


def test_missing_measurement_metadata_is_an_error_not_a_quality_rejection(input_runtime, tmp_path):
    path = tmp_path / "source.mcap"
    write_marvin_mcap(path)
    episode = replace(read(path, input_runtime), valid_measurement_times_ns={})
    with pytest.raises(ValueError, match="did not provide valid measurement timestamps"):
        operators.QualityDetectionOperator().precheck(episode, input_runtime[0])


def test_coverage_threshold_remains_in_detector_and_has_exact_boundary(input_runtime, tmp_path):
    spec, _, _ = input_runtime
    times = np.array([1_000_000_000, 1_600_000_000, 1_600_000_001], dtype=np.int64)
    data = np.ones((3, 16))
    episode = QualityEpisode(McapEpisodeRef(0, tmp_path, tmp_path / "unused.mcap", "."),
                             data, data, times,
                             valid_measurement_times_ns={LEFT: times[:1], RIGHT: times})
    bad, _ = operators._gripper_gap(episode, spec)
    assert bad.tolist() == [False, False, True]
    relaxed = replace(spec, gripper_gap=replace(spec.gripper_gap, max_nearest_gap_ns=700_000_000))
    assert operators.QualityDetectionOperator().precheck(episode, relaxed) is None


def test_statistics_hook_uses_same_normalized_arrays(input_runtime, tmp_path, monkeypatch):
    empty = _CdrWriter().positions([], [])
    path = modified_source(tmp_path, lambda topic, i, data:
                           empty if topic == STATE and i == 50 else data)
    spec, cfg, adapter = input_runtime
    ref = McapEpisodeRef(0, path.parent, path, path.name)
    batch = operators.McapReadOperator().run_batch([ref], spec, cfg, adapter)
    episode = load_episode(batch.records[0])
    def forbidden(*args):
        raise AssertionError("statistics must not decode MCAP")
    monkeypatch.setattr(adapter, "load_raw", forbidden)
    hook = operators._NormalizedQualityHook(batch.records)
    arrays = hook.load_quality_arrays(spec.input_root, 0, schema=cfg.schema)
    np.testing.assert_array_equal(arrays["state"], episode.state)
    np.testing.assert_array_equal(arrays["action"], episode.action)


def test_legacy_adapter_must_not_fall_back_to_stride_inference(input_runtime, tmp_path):
    path = tmp_path / "legacy.mcap"
    write_marvin_mcap(path)
    spec, cfg, adapter = input_runtime
    adapter.to_quality_timeline = None
    with pytest.raises(ValueError, match="must implement to_quality_timeline"):
        read(path, (spec, cfg, adapter))


def test_cli_excludes_empty_measurements_from_fit_then_judges_in_b03(input_runtime, tmp_path, monkeypatch):
    empty = _CdrWriter().positions([], [])
    path = modified_source(tmp_path, lambda topic, i, data:
                           empty if topic == LEFT and i > 0 else data)
    original = path.read_bytes()
    spec, cfg, adapter = input_runtime
    monkeypatch.setattr(cli, "load_spec", lambda *a, **kw: spec)
    monkeypatch.setattr(cli, "quality_config", lambda spec: cfg)
    monkeypatch.setattr(operators, "build_adapter", lambda spec: adapter)

    def forbidden(*args, **kwargs):
        raise AssertionError("coverage-rejected sources must not enter global statistics")

    monkeypatch.setattr(cli.GlobalStatsOperator, "_fit", forbidden)
    assert cli.main(["quality", "--config", "unused", "--mcap", str(path)]) == 0
    record = json.loads((spec.output_root / "decisions.jsonl").read_text())
    assert record["status"] == "rejected" and record["detected_bad_frames"] == 144
    assert path.read_bytes() == original


def test_cli_retains_source_for_adapter_contract_error(input_runtime, tmp_path, monkeypatch):
    path = tmp_path / "legacy.mcap"
    original = write_marvin_mcap(path)
    spec, cfg, adapter = input_runtime
    adapter.to_quality_timeline = None
    monkeypatch.setattr(cli, "load_spec", lambda *a, **kw: spec)
    monkeypatch.setattr(cli, "quality_config", lambda spec: cfg)
    monkeypatch.setattr(operators, "build_adapter", lambda spec: adapter)
    assert cli.main(["run", "--config", "unused", "--mcap", str(path)]) == 1
    record = json.loads((spec.output_root / "decisions.jsonl").read_text())
    assert record["status"] == "error" and not record["deletion_eligible"]
    assert "to_quality_timeline" in record["error"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("times, count", [
    (np.array([2, 1], dtype=np.int64), 2),
    (np.array([2, 1], dtype=np.uint64), 2),
    (np.array([2**63], dtype=np.uint64), 1),
    (np.array([-1], dtype=np.int64), 1),
    (np.array([1.0, 2.0]), 2),
    (np.array([1, 2], dtype=np.int64), 3),
    (np.array([[1, 2]], dtype=np.int64), 2),
])
def test_invalid_timeline_is_rejected(times, count):
    with pytest.raises(ValueError):
        quality_timeline({"frame_times_ns": times}, count)


@pytest.mark.parametrize("case", ["empty_gripper", "empty_state"])
def test_p0_cases_with_real_upstream_containers_and_masks(tmp_path, upstream_repo, case):
    """Engine-side handoff: no test doubles, skipped only without the engine."""
    from stage_b_mcap_v4.spec import build_adapter, load_spec, quality_config

    empty = _CdrWriter().positions([], [])
    topic = LEFT if case == "empty_gripper" else STATE
    path = modified_source(tmp_path, lambda t, i, data:
                           empty if t == topic and (i > 0 if case == "empty_gripper" else i == 50)
                           else data)
    config = Path(__file__).resolve().parents[1] / "configs" / "jindian_marvin.yaml"
    spec = load_spec(config, input_root=tmp_path, output_root=tmp_path / "reports", repo_root=upstream_repo)
    episode = read(path, (spec, quality_config(spec), build_adapter(spec)))
    detector = operators.QualityDetectionOperator()
    if case == "empty_gripper":
        decision = detector.precheck(episode, spec)
        assert decision.reject_file and decision.bad_mask.sum() == 144
    else:
        indices = np.delete(np.arange(320), 50)[::2]
        np.testing.assert_array_equal(episode.frame_times_ns, 1_000_000_000 + indices * 20_000_000)
        np.testing.assert_allclose(episode.state[:, 0], 0.2 + indices * 0.002)
        assert detector.precheck(episode, spec) is None
