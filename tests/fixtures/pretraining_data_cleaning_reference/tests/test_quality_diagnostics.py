"""Preserve completed quality evidence independently of diagnostic failures."""
import hashlib
import json
import sys
from types import SimpleNamespace as NS

import numpy as np
import pytest

from stage_b_mcap_v4 import operators, load_quality_batch
from stage_b_mcap_v4.delivery import McapDeliveryOperator
from test_p0_input_contract import input_runtime
from test_serial_pipeline import numerical_engine, refs
from test_mcap_integration import write_marvin_mcap


def prepare(runtime, tmp_path, paths=None):
    spec, cfg, adapter, _, _, _ = runtime
    if paths is None:
        paths = [tmp_path / 'sample.mcap']
        write_marvin_mcap(paths[0])
    batch = operators.McapReadOperator().run_batch(refs(paths), spec, cfg, adapter)
    context = operators.GlobalStatsOperator().run(batch, spec, cfg)
    assert context.status == 'ready'
    return spec, cfg, context, paths


def reject_first(monkeypatch, *, discard=False, broken_field=None):
    def run(state, *a, **kw):
        bad = np.zeros(len(state), dtype=bool)
        if not discard:
            bad[20] = True
        result = NS(abnormal_frames=bad, discard=discard, discard_reasons=['known discard'] if discard else [],
                    flagged_count=int(bad.sum()), hard_limit_hit=False, gripper_faults={}, rotation_joint_steps=0)
        if broken_field:
            delattr(result, broken_field)
        return result
    monkeypatch.setattr(sys.modules['robot_data_processing.stages.stage1_sudden_change'], 'run_stage1', run)


def read_records(output):
    return [json.loads(line) for line in (output / 'decisions.jsonl').read_text().splitlines()]


def rewrite_handoff(judged, records=None, summary=None, version=None):
    root = judged.manifest_path.parent
    manifest = json.loads(judged.manifest_path.read_text())
    if records is not None:
        path = root / manifest['decisions_file']
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        manifest['decisions_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    if summary is not None:
        path = root / manifest['summary_file']
        path.write_text(json.dumps(summary))
        manifest['summary_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    if version:
        manifest['format'] = version
    judged.manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize('discard', [False, True])
@pytest.mark.parametrize('failure', ['s4', 's2_fields', 's3_shape'])
def test_later_failure_retains_prior_rejection(numerical_engine, monkeypatch, tmp_path, discard, failure):
    spec, cfg, context, paths = prepare(numerical_engine, tmp_path)
    reject_first(monkeypatch, discard=discard)
    def fail(*a, **kw):
        raise RuntimeError('controlled later diagnostic failure')
    stage = failure.split('_')[0]
    if failure == 's4':
        monkeypatch.setattr(sys.modules['robot_data_processing.stages.stage4_static_interval'], 'run_stage4', fail)
    elif failure == 's2_fields':
        cfg.schema.embodiment = 'test_profile'
        monkeypatch.setattr(sys.modules['robot_data_processing.stages.stage2_trend_alignment'], 'canonical_to_stage2_fields', fail)
    else:
        module = sys.modules['robot_data_processing.stages.stage3_extreme_value']
        original = module.run_stage3
        def wrong_shape(*a, **kw):
            result = original(*a, **kw)
            result.remove_frames = result.remove_frames[:-1]
            return result
        monkeypatch.setattr(module, 'run_stage3', wrong_shape)
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    record = read_records(spec.output_root)[0]
    assert record['status'] == 'rejected' and record['reject_file']
    assert record['stage_results']['s1']['status'] == 'rejected'
    assert bool(record['stage_results']['s1']['detected_ranges']) == (not discard)
    assert record['diagnostic_errors'].keys() == {stage}
    assert record['stage_results'][stage]['status'] == 'error'
    later = ['s1', 's2', 's3', 's4'][['s1', 's2', 's3', 's4'].index(stage) + 1:]
    assert all(record['stage_results'][name]['status'] == 'skipped' for name in later)
    assert record['deletion_eligible'] is False and record['retained_due_to_error']
    assert judged.summary['rejected_files'] == judged.summary['failed_episodes'] == 1
    assert judged.summary['error_files'] == 0 and judged.summary['diagnostic_error_files'] == 1
    delivered = McapDeliveryOperator().run(load_quality_batch(judged.manifest_path), mode='run')
    assert delivered.exit_code == 1 and delivered.summary['deleted_files'] == delivered.summary['exported_files'] == 0
    assert paths[0].exists()
    print('PRESERVED_REJECTION', json.dumps({'failure':failure, 'discard':discard, 'status':record['status'],
        'rejected_files':delivered.summary['rejected_files'], 'failed_episodes':delivered.summary['failed_episodes'],
        'deleted_files':delivered.summary['deleted_files'], 'exit_code':delivered.exit_code}))


@pytest.mark.parametrize('broken_field', ['rotation_joint_steps', 'discard_reasons'])
def test_optional_metric_failure_cannot_erase_current_stage_rejection(numerical_engine, monkeypatch, tmp_path, broken_field):
    spec, cfg, context, paths = prepare(numerical_engine, tmp_path)
    reject_first(monkeypatch, broken_field=broken_field)
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    record = read_records(spec.output_root)[0]
    assert record['status'] == 'rejected' and record['detected_bad_frames'] == 1
    assert record['stage_results']['s1']['status'] == 'rejected' and 's1' in record['diagnostic_errors']
    assert record['stage_results']['s2']['status'] == 'skipped' and paths[0].exists()
    assert judged.exit_code == 1


def test_failure_without_known_rejection_is_error_not_kept(numerical_engine, monkeypatch, tmp_path):
    spec, cfg, context, paths = prepare(numerical_engine, tmp_path)
    def fail(*a, **kw):
        raise RuntimeError('controlled S2 failure')
    monkeypatch.setattr(sys.modules['robot_data_processing.stages.stage2_trend_alignment'], 'run_stage2_on_fields', fail)
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    record = read_records(spec.output_root)[0]
    assert record['status'] == 'error' and record['reject_file'] is False
    assert record['stage_results']['s1']['status'] == 'passed'
    assert record['stage_results']['s3']['status'] == record['stage_results']['s4']['status'] == 'skipped'
    result = McapDeliveryOperator().run(judged, mode='run')
    assert result.exit_code == 1 and result.summary['error_files'] == result.summary['failed_episodes'] == 1
    assert result.summary['kept_files'] == result.summary['rejected_files'] == result.summary['exported_files'] == 0
    assert paths[0].exists()


@pytest.mark.parametrize('misleading_eligibility', [False, True])
def test_mixed_batch_counts_and_diagnostic_deletion_guard(numerical_engine, monkeypatch, tmp_path, misleading_eligibility):
    paths = [tmp_path / (name + '.mcap') for name in ('good', 'bad_with_error', 'broken', 'bad')]
    for path in (paths[0], paths[1], paths[3]):
        write_marvin_mcap(path)
    paths[2].write_bytes(b'invalid MCAP')
    spec, cfg, context, _ = prepare(numerical_engine, tmp_path, paths)
    labels, current = iter(('good', 'bad_with_error', 'bad')), [None]
    first = sys.modules['robot_data_processing.stages.stage1_sudden_change']
    fourth = sys.modules['robot_data_processing.stages.stage4_static_interval']
    original_first, original_fourth = first.run_stage1, fourth.run_stage4
    def s1(*a, **kw):
        current[0] = next(labels)
        result = original_first(*a, **kw)
        if current[0] != 'good':
            result.abnormal_frames[20] = True
            result.flagged_count = 1
        return result
    def s4(*a, **kw):
        if current[0] == 'bad_with_error':
            raise RuntimeError('controlled S4 failure')
        return original_fourth(*a, **kw)
    monkeypatch.setattr(first, 'run_stage1', s1)
    monkeypatch.setattr(fourth, 'run_stage4', s4)
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    records = read_records(spec.output_root)
    assert [r['status'] for r in records] == ['kept', 'rejected', 'error', 'rejected']
    if misleading_eligibility:
        records[1]['deletion_eligible'] = True
        records[1]['deletion_blocked_reason'] = None
        rewrite_handoff(judged, records=records)
    result = McapDeliveryOperator().run(load_quality_batch(judged.manifest_path), mode='run')
    summary = result.summary
    assert [summary[k] for k in ('episodes','kept_files','rejected_files','error_files','failed_episodes')] == [4,1,2,1,2]
    assert summary['diagnostic_error_files'] == 1 and result.exit_code == 1
    assert summary['exported_files'] == summary['deleted_files'] == 1
    assert all(p.exists() for p in paths[:3]) and not paths[3].exists()


def test_legacy_v1_handoff_still_loads(numerical_engine, tmp_path):
    spec, cfg, context, paths = prepare(numerical_engine, tmp_path)
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    records = read_records(spec.output_root)
    for record in records:
        record.pop('diagnostic_errors', None)
    summary = dict(judged.summary)
    summary.pop('error_files')
    summary.pop('diagnostic_error_files')
    rewrite_handoff(judged, records=records, summary=summary, version='quality_batch_v1')
    reloaded = load_quality_batch(judged.manifest_path)
    assert reloaded.summary['error_files'] == reloaded.summary['diagnostic_error_files'] == 0
    result = McapDeliveryOperator().run(reloaded)
    assert result.exit_code == 0 and result.summary['exported_files'] == 1 and paths[0].exists()


def test_handoff_cannot_mark_diagnostic_failure_as_kept(numerical_engine, tmp_path):
    spec, cfg, context, _ = prepare(numerical_engine, tmp_path)
    judged = operators.QualityDetectionOperator().run_batch(context, spec, cfg)
    records = read_records(spec.output_root)
    records[0]['diagnostic_errors'] = {'s4':'controlled failure'}
    rewrite_handoff(judged, records=records)
    with pytest.raises(ValueError, match='cannot be marked kept'):
        McapDeliveryOperator().run(judged, mode='run')
    assert not (spec.output_root / 'accepted').exists()
