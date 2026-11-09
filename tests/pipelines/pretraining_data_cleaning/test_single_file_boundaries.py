"""SDK-free business handoff boundaries, runnable with stdlib unittest.

Inputs below are tiny byte fixtures for staging and numeric handoffs. This is
not MCAP decoding or a substitute for the real-MCAP/core integration suite.
No operator_sdk imports or fabricated SDK modules are used.
"""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4
from zipfile import ZipFile

import numpy as np

from mcap_read_normalize.io import read_raw_files, write_normalized_episode_ref
from mcap_delivery_accepted.core import delivery_manifest
from mcap_delivery_accepted.io import read_batch_quality_result
from pretrain_cleaning_common.artifact import pack_artifact, unpack_artifact
from pretrain_cleaning_common.contracts import (
    NormalizedBatchRef, NormalizedEpisode, NormalizedRecord, SourceIdentity,
)
from pretrain_cleaning_common.filesystem import write_json_durable
from pretrain_cleaning_common.handoff import load_normalized
from pretrain_cleaning_common.reporting import quality_counts
from pretrain_cleaning_common.single_file import merge_normalized_episodes, write_cohort
from pretrain_cleaning_common.storage import load_episode, save_episode


class _Workspace:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, name):
        result = self.root / name
        result.parent.mkdir(parents=True, exist_ok=True)
        return result


class _RawInputs:
    """Only the business I/O ports needed by raw_files, not an SDK emulator."""
    def __init__(self, entries):
        self.entries = entries
        self.materialized_accesses = 0

    def value(self, name):
        assert name == 'raw_files'
        return [key for key, _, _ in self.entries]

    def files(self, name):
        assert name == 'raw_files'
        self.materialized_accesses += 1
        return [path for _, path, _ in self.entries]

    def file_origins(self, name):
        assert name == 'raw_files'
        return [origin for _, _, origin in self.entries]


class SingleFileBoundaries(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='mcap-single-boundary-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.serial = 0

    def _next(self, name):
        self.serial += 1
        return self.root / f'{self.serial:02d}-{name}'

    def _read(self, keys, source='jindian_marvin', origins=None):
        case = self._next('b01')
        entries = []
        for index, key in enumerate(keys):
            path = case / 'download' / str(index) / 'blob'
            path.parent.mkdir(parents=True)
            path.write_bytes(('frozen:' + key).encode())
            entries.append((key, path, (origins or ['origin'] * len(keys))[index]))
        outputs = {}
        ctx = SimpleNamespace(
            inputs=_RawInputs(entries), parameters={'source': source, 'workers': 1},
            workspace=_Workspace(case / 'work'),
            execution={'mode': 'pipeline', 'attemptId': str(uuid4())},
            check_cancelled=lambda: None,
            outputs=SimpleNamespace(file=lambda name, path: outputs.update({name: path})),
        )
        return ctx, entries, outputs

    def _singleton(self, key, source='jindian_marvin', value=0.1, reverse_abc=False):
        keys = [key]
        if source == 'abc_130k':
            keys.append(str(Path(key).with_name('eef_pose.parquet')))
            if reverse_abc:
                keys.reverse()
        ctx, entries, outputs = self._read(keys, source)
        original = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for _, path, _ in entries}
        _, refs, metadata = read_raw_files(ctx, ctx.workspace.path('normalization'))
        self.assertEqual(len(refs), 1)
        ref, = refs
        dims = 32 if source == 'abc_130k' else 16
        data = ctx.workspace.path('normalization/normalized/episode_00000000.npz')
        times = np.arange(4, dtype=np.int64) * 40_000_000 + 1_000_000_000
        numeric = NormalizedEpisode(ref, np.full((4, dims), value), np.full((4, dims), value),
                                    times, SourceIdentity.capture(ref.mcap_path), {})
        save_episode(data, numeric)
        batch = NormalizedBatchRef(data.parent / 'manifest.json', (
            NormalizedRecord(ref, data, numeric.source_identity),
        ))
        write_normalized_episode_ref(ctx, batch, metadata)
        self.assertEqual(set(outputs), {'normalized_episode_ref'})
        artifact = outputs['normalized_episode_ref']
        with ZipFile(artifact) as archive:
            self.assertTrue(all(not name.endswith(('.mcap', '.parquet')) for name in archive.namelist()))
        folder = unpack_artifact(artifact, 'normalized_batch', self._next('restored'))
        result = load_normalized(folder, source)
        self.assertEqual(len(result[0].records), 1)
        for _, path, _ in entries:
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original[str(path)])
        return result, entries, ctx

    def _merged(self, keys=('b.mcap', 'a.mcap')):
        items = [self._singleton(key, value=(index + 1) / 10)[0] for index, key in enumerate(keys)]
        return merge_normalized_episodes(items, self._next('merged'), 'jindian_marvin')

    def _quality(self, cohort, index, status='kept', statistics_digest='a' * 64):
        folder = self._next('quality')
        folder.mkdir()
        source_ref = cohort['members'][index]['source_ref']
        decision = {'episode_index': index, 'source': source_ref['logical_key'],
                    'source_ref': source_ref, 'status': status,
                    'reject_file': status == 'rejected',
                    'reasons': ['quality threshold'] if status == 'rejected' else [],
                    'diagnostic_errors': {}}
        write_json_durable(folder / 'selection.json', {
            'source': cohort['source'], 'members': [{'episode_index': index, 'source_ref': source_ref}],
        })
        write_json_durable(folder / 'quality_summary.json', {
            'statistics_status': 'ready', 'statistics_context_digest': statistics_digest,
            **quality_counts([decision]),
        })
        write_cohort(folder, cohort)
        (folder / 'decisions.jsonl').write_text(json.dumps(decision) + '\n', encoding='utf-8')
        artifact = self._next('quality.zip')
        pack_artifact(artifact, 'quality_batch', folder)
        return artifact

    def _gather(self, artifacts):
        def files(name):
            self.assertEqual(name, 'episode_quality_results')
            return artifacts
        ctx = SimpleNamespace(inputs=SimpleNamespace(files=files),
                              workspace=_Workspace(self._next('gather')), check_cancelled=lambda: None)
        return read_batch_quality_result(ctx)

    def test_multi_episode_rejected_before_file_binding_or_staging(self):
        cases = [
            ('jindian_marvin', ['a.mcap', 'b.mcap']),
            ('abc_130k', ['a/episode.mcap', 'a/eef_pose.parquet', 'b/episode.mcap', 'b/eef_pose.parquet']),
            ('abc_130k', ['a/episode.mcap', 'b/eef_pose.parquet']),
        ]
        for source, keys in cases:
            with self.subTest(source=source, keys=keys):
                ctx, _, _ = self._read(keys, source)
                with self.assertRaises(ValueError):
                    read_raw_files(ctx, ctx.workspace.path('normalization'))
                self.assertEqual(ctx.inputs.materialized_accesses, 0)
                self.assertFalse((ctx.workspace.root / 'raw-input').exists())

    def test_marvin_one_record_roundtrip_keeps_original_source_binding(self):
        (batch, metadata), entries, ctx = self._singleton('task/one.mcap')
        record, = batch.records
        member, = metadata['members']
        self.assertEqual(record.ref.episode_index, 0)
        self.assertEqual(record.ref.mcap_path, Path('/raw/task/one.mcap'))
        self.assertEqual(member['source_ref'], {
            'attempt_id': ctx.execution['attemptId'], 'input_name': 'raw_files',
            'input_index': 0, 'logical_key': 'task/one.mcap',
            'sha256': hashlib.sha256(entries[0][1].read_bytes()).hexdigest(),
        })
        np.testing.assert_array_equal(load_episode(record).state, np.full((4, 16), 0.1))

    def test_abc_one_record_roundtrip_preserves_primary_input_slot(self):
        (batch, metadata), _, _ = self._singleton('data/train/task/ep/episode.mcap', 'abc_130k', reverse_abc=True)
        record, = batch.records
        self.assertEqual(record.ref.rel_path, 'data/train/task/ep')
        self.assertEqual(record.ref.part, 'train')
        self.assertEqual(record.ref.task, 'task')
        self.assertEqual(metadata['members'][0]['source_ref']['input_index'], 1)
        self.assertEqual(load_episode(record).state.shape, (4, 32))

    def test_abc_cross_origin_pair_rejected_before_staging(self):
        ctx, _, _ = self._read(['ep/episode.mcap', 'ep/eef_pose.parquet'], 'abc_130k', ['one', 'two'])
        with self.assertRaisesRegex(ValueError, 'origin'):
            read_raw_files(ctx, ctx.workspace.path('normalization'))
        self.assertFalse((ctx.workspace.root / 'raw-input').exists())

    def test_merge_assigns_unique_global_ids_and_npz_names(self):
        batch, metadata, cohort = self._merged()
        self.assertEqual([record.ref.episode_index for record in batch.records], [0, 1])
        self.assertEqual([record.data_path.name for record in batch.records], ['episode_00000000.npz', 'episode_00000001.npz'])
        self.assertEqual([member['source_ref']['logical_key'] for member in metadata['members']], ['a.mcap', 'b.mcap'])
        self.assertEqual([member['source_episode_index'] for member in metadata['members']], [0, 0])
        self.assertEqual([member['episode_index'] for member in cohort['members']], [0, 1])
        np.testing.assert_array_equal(load_episode(batch.records[0]).state, np.full((4, 16), 0.2))
        np.testing.assert_array_equal(load_episode(batch.records[1]).state, np.full((4, 16), 0.1))

    def test_merge_rejects_duplicate_logical_key_across_distinct_attempts(self):
        left = self._singleton('same.mcap')[0]
        right = self._singleton('same.mcap')[0]
        self.assertNotEqual(left[1]['members'][0]['source_ref']['attempt_id'], right[1]['members'][0]['source_ref']['attempt_id'])
        destination = self._next('bad-merge')
        with self.assertRaisesRegex(ValueError, 'duplicate logical_key'):
            merge_normalized_episodes([left, right], destination, 'jindian_marvin')
        self.assertFalse(destination.exists())

    def test_b04_rejects_missing_cohort_result(self):
        _, _, cohort = self._merged()
        with self.assertRaisesRegex(ValueError, 'missing episode'):
            self._gather([self._quality(cohort, 0)])

    def test_b04_rejects_duplicate_cohort_result(self):
        _, _, cohort = self._merged()
        artifact = self._quality(cohort, 0)
        with self.assertRaisesRegex(ValueError, 'duplicate or unknown'):
            self._gather([artifact, artifact])

    def test_b04_rejects_mixed_cohorts(self):
        _, _, first = self._merged()
        _, _, second = self._merged(('c.mcap', 'd.mcap'))
        with self.assertRaisesRegex(ValueError, 'different statistics cohorts'):
            self._gather([self._quality(first, 0), self._quality(second, 1)])

    def test_b04_rejects_different_statistics_contexts_for_same_cohort(self):
        _, _, cohort = self._merged()
        artifacts = [self._quality(cohort, 0), self._quality(cohort, 1, statistics_digest='b' * 64)]
        with self.assertRaisesRegex(ValueError, 'different fitted statistics contexts'):
            self._gather(artifacts)

    def test_b04_complete_gather_accepts_mixed_pass_reject_and_all_reject(self):
        _, _, cohort = self._merged()
        for statuses in [('kept', 'rejected'), ('rejected', 'rejected')]:
            with self.subTest(statuses=statuses):
                artifacts = [self._quality(cohort, index, status) for index, status in enumerate(statuses)]
                selection, records, summary = self._gather(artifacts[::-1])
                self.assertEqual([record['episode_index'] for record in records], [0, 1])
                manifest = delivery_manifest(selection, records, summary, lambda: None)
                self.assertEqual(len(manifest['accepted']), statuses.count('kept'))


if __name__ == '__main__':
    unittest.main()
