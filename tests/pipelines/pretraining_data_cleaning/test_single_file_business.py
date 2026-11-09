"""Real MCAP/ZIP/numerical regression without substituting an Operator SDK.

These tests exercise business functions with local file bindings. They do not
claim to validate SDK startup, remote storage, dependency locks, or containers.
"""
import ast
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import UUID
from zipfile import ZipFile

import numpy as np

from mcap_read_normalize.core import McapReadOperator
from mcap_read_normalize.io import read_raw_files, write_normalized_episode_ref
from mcap_stats_global.core import GlobalStatsOperator
from mcap_stats_global.io import read_normalized_episode_refs, write_statistics_context
from mcap_quality_detect.core import QualityDetectionOperator
from mcap_quality_detect.io import read_statistics_context, portable_decisions, write_episode_quality_result
from mcap_delivery_accepted.io import read_batch_quality_result
from mcap_delivery_accepted.core import delivery_manifest
from mcap_delivery_accepted.archive import build_archive
from pretrain_cleaning_common.handoff import make_handoff_spec
from pretrain_cleaning_common.reporting import read_quality_batch
from pretrain_cleaning_common.spec import quality_config
from pretrain_cleaning_common.storage import load_episode


ROOT = Path(__file__).resolve().parents[3]


def marvin_fixture(path, **kwargs):
    # Reuse only the pinned fixture generator; importing its pytest module would
    # unnecessarily require a second test runner in this unittest-only check.
    fixture = ROOT / 'tests/fixtures/pretraining_data_cleaning_reference/tests/test_mcap_integration.py'
    source = ast.parse(fixture.read_text())
    names = {'JOINTS', 'STATE', 'TARGET', 'LEFT', 'RIGHT'}
    nodes = []
    for node in source.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
            if module in {'__future__', 'pathlib', 'struct', 'mcap.writer'}:
                nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            nodes.append(node)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in {'_CdrWriter', 'write_marvin_mcap'}:
            nodes.append(node)
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(fixture), 'exec'), namespace)
    return namespace['write_marvin_mcap'](path, **kwargs)


def file_context(root, inputs, *, source='jindian_marvin', keys=None, attempt=1):
    root.mkdir(parents=True)
    saved = {}

    def work(name):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    ctx = SimpleNamespace(
        workspace=SimpleNamespace(path=work),
        inputs=SimpleNamespace(
            file=lambda name: inputs[name], files=lambda name: inputs[name],
            value=lambda name: keys if name == 'raw_files' else inputs[name],
            file_origins=lambda name: ['raw-location'] * len(inputs[name])),
        outputs=SimpleNamespace(file=lambda name, path: saved.__setitem__(name, path)),
        parameters={'source': source, 'workers': 1},
        execution={'mode': 'pipeline', 'attemptId': str(UUID(int=attempt))},
        check_cancelled=lambda: None,
    )
    return ctx, saved


class SingleFileBusinessPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='single-file-business-')
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def run_pipeline(self, paths, keys, source='jindian_marvin'):
        hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in zip(keys, paths)}
        singles, normalized = [], []
        for i, (key, path) in enumerate(zip(keys, paths)):
            raw_keys, raw_paths = [key], [path]
            if source == 'abc_130k':
                raw_keys.insert(0, str(Path(key).with_name('eef_pose.parquet')))
                raw_paths.insert(0, path.with_name('eef_pose.parquet'))
            ctx, out = file_context(self.root / f'b1-{i}', {'raw_files': raw_paths}, source=source, keys=raw_keys, attempt=i+1)
            spec, refs, meta = read_raw_files(ctx, ctx.workspace.path('normalization'))
            batch = McapReadOperator().run_batch(refs, spec, quality_config(spec))
            if batch.records[0].input_error:
                raise AssertionError(batch.records[0].input_error)
            normalized.append(load_episode(batch.records[0]))
            write_normalized_episode_ref(ctx, batch, meta)
            singles.append(out['normalized_episode_ref'])

        ctx, out = file_context(self.root / 'b2', {'normalized_episode_refs': singles[::-1]}, source=source)
        batch, meta, cohort = read_normalized_episode_refs(ctx)
        folder = ctx.workspace.path('statistics'); folder.mkdir()
        spec = make_handoff_spec(ctx, 'normalized_episode_refs', meta, folder)
        cfg = quality_config(spec)
        context = GlobalStatsOperator().run(batch, spec, cfg, recompute=True)
        self.assertNotEqual(context.status, 'failed', context.error)
        write_statistics_context(ctx, context, meta, ctx.workspace.path('context'), cohort)
        statistics = out['statistics_context']
        results, decisions = [], []
        for i, single in enumerate(singles):
            ctx, out = file_context(self.root / f'b3-{i}', {
                'normalized_episode_ref': single, 'statistics_context': statistics}, source=source)
            selected, selected_meta = read_statistics_context(ctx)
            self.assertEqual(len(selected.batch.records), 1)
            if context.statistics is None:
                self.assertIsNone(selected.statistics)
            else:
                self.assertEqual(selected.statistics.global_stats.num_episodes, context.statistics.global_stats.num_episodes)
            folder = ctx.workspace.path('quality'); folder.mkdir()
            spec3 = make_handoff_spec(ctx, 'statistics_context', selected_meta, folder)
            result = QualityDetectionOperator().run_batch(selected, spec3, quality_config(spec3))
            self.assertEqual(result.exit_code, 0, result.summary)
            _, summary, records = read_quality_batch(result.manifest_path)
            portable = portable_decisions(records, selected_meta)
            self.assertEqual(len(portable), 1)
            write_episode_quality_result(ctx, portable, selected_meta, summary['statistics_status'])
            results.append(out['episode_quality_result']); decisions.extend(portable)

        ctx, _ = file_context(self.root / 'b4', {'episode_quality_results': results[::-1]}, source=source)
        selection, records, summary = read_batch_quality_result(ctx)
        kept = delivery_manifest(selection, records, summary, ctx.check_cancelled)['accepted']
        mapping = dict(zip(keys, paths))
        ctx.storage = SimpleNamespace(limits=SimpleNamespace(max_upload_bytes=1 << 30))
        archive = build_archive(ctx, kept, [mapping[item['source_ref']['logical_key']] for item in kept])
        with ZipFile(archive) as zipped:
            delivery = json.loads(zipped.read('delivery_manifest.json'))
            for item in delivery['files']:
                self.assertEqual(hashlib.sha256(zipped.read(item['delivery_path'])).hexdigest(),
                                 hashes[item['business']['source_ref']['logical_key']])
        for key, path in zip(keys, paths):
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), hashes[key])
        return batch, context, decisions, normalized, summary, delivery

    def test_real_marvin_matches_batch_numerical_judgments(self):
        raw = self.root / 'raw'
        keys = ['z/episode.mcap', 'a/episode.mcap', 'm/episode.mcap']
        paths = [raw / key for key in keys]
        marvin_fixture(paths[0])
        marvin_fixture(paths[1], missing_left=set(range(50, 180)))
        marvin_fixture(paths[2], missing_left_topic=True)
        batch, context, decisions, normalized, summary, delivery = self.run_pipeline(paths, keys)
        self.assertEqual((summary['kept_files'], summary['rejected_files']), (1, 2))
        self.assertEqual([m['business']['episode_index'] for m in delivery['files']], [2])
        # A whole-batch call with the SAME shared statistics must produce exactly
        # the same masks/reasons/stage results as the separate single calls.
        target = self.root / 'batch-reference'; target.mkdir()
        spec = replace(make_handoff_spec(
            SimpleNamespace(parameters={'source':'jindian_marvin','workers':1}), '',
            # The same validated metadata is available in the merged payload.
            json.loads((batch.manifest_path.parent / 'selection.json').read_text()), target), output_root=target)
        result = QualityDetectionOperator().run_batch(context, spec, quality_config(spec))
        _, _, expected = read_quality_batch(result.manifest_path)
        actual = {row['episode_index']: row for row in decisions}
        for row in expected:
            got = actual[row['episode_index']]
            for key in ('status', 'reject_file', 'reasons', 'num_frames', 'detected_bad_frames',
                        'detected_ranges', 'effective_removed_ranges', 'stage_results', 'diagnostic_errors'):
                self.assertEqual(got.get(key), row.get(key), (row['episode_index'], key))
        # Global renumbering must not alter state/action or source timestamps.
        original = {key: episode for key, episode in zip(keys, normalized)}
        for record in batch.records:
            restored = load_episode(record)
            before = original[str(record.ref.mcap_path.relative_to('/raw'))]
            np.testing.assert_array_equal(restored.state, before.state)
            np.testing.assert_array_equal(restored.action, before.action)
            np.testing.assert_array_equal(restored.frame_times_ns, before.frame_times_ns)

    def test_actual_abc_sidecar_roundtrip_and_empty_delivery(self):
        from tests.support.pretraining_samples import write_abc
        mcap, _, _ = write_abc(self.root / 'raw')
        key = mcap.relative_to(self.root / 'raw').as_posix()
        _, _, decisions, _, summary, delivery = self.run_pipeline([mcap], [key], 'abc_130k')
        self.assertEqual(decisions[0]['source_ref']['input_index'], 1)
        self.assertEqual(summary['rejected_files'], 1)
        self.assertEqual(delivery['files'], [])

    def test_all_missing_gripper_uses_empty_statistics_and_rejects_normally(self):
        key = 'capture/missing.mcap'
        path = self.root / 'raw' / key
        marvin_fixture(path, missing_left_topic=True)
        _, context, decisions, _, summary, delivery = self.run_pipeline([path], [key])
        self.assertEqual(context.status, 'empty')
        self.assertEqual(decisions[0]['stage_results']['gripper_gap']['status'], 'rejected')
        self.assertEqual(summary['rejected_files'], 1)
        self.assertEqual(summary['failed_episodes'], 0)
        self.assertEqual(delivery['files'], [])


if __name__ == '__main__':
    unittest.main()
