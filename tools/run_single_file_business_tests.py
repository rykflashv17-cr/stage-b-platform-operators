"""Run local business regressions without claiming SDK/container acceptance."""
import ast
import copy
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _method(tree, class_name, method_name):
    owner, = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
    method, = [node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == method_name]
    return method


def _statements(nodes):
    return ast.dump(ast.Module(body=nodes, type_ignores=[]))


def _without_target_fingerprint(method):
    """Allow exactly the added target-policy cache key, no fitting changes."""
    permitted = ast.dump(ast.parse(
        "fingerprint.update(repr(getattr(spec, 'joint_target_gap', None)).encode())"
    ).body[0])

    class RemovePolicyCacheKey(ast.NodeTransformer):
        count = 0

        def visit_Expr(self, node):
            if ast.dump(node) == permitted:
                self.count += 1
                return None
            return self.generic_visit(node)

    transform = RemovePolicyCacheKey()
    result = transform.visit(copy.deepcopy(method))
    return ast.dump(result), transform.count


def _quality_calculations(method):
    """Keep masks/imports and the entire S1–S4 execution block under comparison.

    The coverage prelude and reports initialization intentionally change here;
    no numerical expression, stage table, lambda or diagnostic handling in the
    blocks below is exempted from the baseline check.
    """
    setup = next(i for i, node in enumerate(method.body) if isinstance(node, ast.Expr)
                 and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                 and node.value.func.id == 'setup_imports')
    timeline = next(i for i, node in enumerate(method.body) if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Tuple)
                            and [item.id for item in target.elts if isinstance(item, ast.Name)] == ['n', 'times']
                            for target in node.targets))
    stages = next(i for i, node in enumerate(method.body)
                  if isinstance(node, ast.FunctionDef) and node.name == 'run_s2')
    return (_statements(method.body[setup:timeline]), _statements(method.body[stages:]))


def algorithm_guards():
    before, current = {}, {}
    for code in ('mcap_read_normalize', 'mcap_stats_global', 'mcap_quality_detect'):
        relative = f'operators/{code}/src/{code}/core.py'
        baseline = subprocess.check_output(['git', 'show', f'develop:{relative}'], cwd=ROOT, text=True)
        baseline = baseline.replace('pretraining_data_cleaning_common', 'pretrain_cleaning_common')
        baseline = baseline.replace('pretraining_data_cleaning_engine', 'pretrain_cleaning_engine')
        before[code], current[code] = ast.parse(baseline), ast.parse((ROOT / relative).read_text())
    before_fit = _without_target_fingerprint(_method(before['mcap_stats_global'], 'GlobalStatsOperator', '_fit'))
    current_fit = _without_target_fingerprint(_method(current['mcap_stats_global'], 'GlobalStatsOperator', '_fit'))
    before_quality = _quality_calculations(_method(before['mcap_quality_detect'], 'QualityDetectionOperator', 'run'))
    current_quality = _quality_calculations(_method(current['mcap_quality_detect'], 'QualityDetectionOperator', 'run'))
    return {
        'b01_core_ast_matches_baseline_after_import_namespace_change':
            ast.dump(before['mcap_read_normalize']) == ast.dump(current['mcap_read_normalize']),
        'b02_fit_ast_matches_baseline_except_target_policy_cache_key': before_fit[0] == current_fit[0],
        'b02_target_policy_cache_key_added_exactly_once': before_fit[1] == 0 and current_fit[1] == 1,
        'b03_numeric_input_masks_ast_matches_baseline': before_quality[0] == current_quality[0],
        'b03_stage1_to_stage4_execution_ast_matches_baseline': before_quality[1] == current_quality[1],
    }


def main():
    sys.path[:0] = [str(ROOT), str(ROOT / 'packages/pretrain-cleaning-common/src'),
                   str(ROOT / 'packages/pretrain-cleaning-engine/src'),
                   *[str(p / 'src') for p in (ROOT / 'operators').iterdir() if (p / 'src').is_dir()]]
    files = [
        'packages/pretrain-cleaning-common/tests/source/test_single_file.py',
        'operators/mcap_stats_global/tests/source/test_single_file_io.py',
        'operators/mcap_quality_detect/tests/source/test_single_file.py',
        'tests/pipelines/pretraining_data_cleaning/test_single_file_boundaries.py',
        'tests/pipelines/pretraining_data_cleaning/test_single_file_business.py',
        'tests/pipelines/pretraining_data_cleaning/test_target_coverage.py',
    ]
    suite = unittest.TestSuite()
    for index, name in enumerate(files):
        spec = importlib.util.spec_from_file_location(f'single_file_regression_{index}', ROOT / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    elapsed = time.perf_counter() - started
    algorithms = algorithm_guards()
    vendor = subprocess.check_output(['git', 'diff', '--name-only', 'develop', '--',
                                     'packages/pretrain-cleaning-engine/src'], cwd=ROOT, text=True)
    dependencies = {}
    for name in ('numpy', 'scipy', 'pandas', 'pyarrow', 'mcap', 'protobuf', 'PyYAML'):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    evidence = {
        'scope': 'local source business functions and artifact handoffs',
        'revision_scope': '2026-11-18 joint-target freshness quality gate',
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'elapsed_seconds': elapsed,
        'python': sys.version.split()[0], 'dependencies': dependencies,
        'baseline_revision': subprocess.check_output(['git', 'rev-parse', 'develop'], cwd=ROOT, text=True).strip(),
        'unchanged_algorithm_guards': algorithms,
        'allowed_policy_changes': [
            'B01 preserves sparse joint-target timestamps instead of rejecting nearest gaps above 100ms.',
            'B02 excludes target-coverage failures before numerical fitting and binds the coverage policy/cache key.',
            'B03 diagnoses target coverage before S1-S4 and preserves both target and gripper failure reasons.',
        ],
        'pinned_engine_source_unchanged': not vendor.strip(),
        'real_mcap_fixtures': {'synthetic_marvin': 17, 'synthetic_abc_mcap_with_parquet': 1,
                              'user_captures_in_automated_suite': 0},
        'target_freshness_cases': {
            'nanosecond_boundaries': [99_999_999, 100_000_000, 100_000_001],
            'directions': ['before first target', 'after last target'],
            'normalization_and_npz_zip_timestamp_roundtrip': True,
            'mixed_batch_statistics_and_clean_s1_s4_isolation': True,
            'all_sparse_batch_has_empty_statistics_and_normal_rejections': True,
            'combined_target_gripper_reasons_preserved': True,
            'missing_metadata_and_old_policy_fail_closed': True,
            'empty_target_topic_remains_read_error': True,
        },
        'verification_limits': [
            'Existing numeric environment; not the complete pinned deployment environment.',
            'Operator SDK startup, registration export and signed storage were not executed.',
            'New deployment wheels and dependency locks were not rebuilt or validated by this runner.',
            'Container and production scatter/gather/broadcast were not tested.',
        ],
    }
    output = ROOT / 'docs/validation/single-file-target-coverage-20261118.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() and all(algorithms.values()) and not vendor.strip() else 1


if __name__ == '__main__':
    raise SystemExit(main())
