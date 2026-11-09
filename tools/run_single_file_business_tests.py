"""Run local business regressions without claiming SDK/container acceptance."""
import ast
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


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
    algorithms = {}
    # The import path changes to the current shared package. All core numerical
    # statements must otherwise remain identical to the downloaded baseline.
    for code in ('mcap_read_normalize', 'mcap_stats_global', 'mcap_quality_detect'):
        relative = f'operators/{code}/src/{code}/core.py'
        before = subprocess.check_output(['git', 'show', f'develop:{relative}'], cwd=ROOT, text=True)
        before = before.replace('pretraining_data_cleaning_common', 'pretrain_cleaning_common')
        before = before.replace('pretraining_data_cleaning_engine', 'pretrain_cleaning_engine')
        algorithms[code] = ast.dump(ast.parse(before)) == ast.dump(ast.parse((ROOT / relative).read_text()))
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
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'elapsed_seconds': elapsed,
        'python': sys.version.split()[0], 'dependencies': dependencies,
        'core_ast_matches_baseline_after_import_namespace_change': algorithms,
        'pinned_engine_source_unchanged': not vendor.strip(),
        'real_mcap_fixtures': {'marvin': 4, 'abc_mcap_with_parquet': 1},
        'verification_limits': [
            'Existing numeric environment; not the complete pinned deployment environment.',
            'Operator SDK startup, registration export and signed storage were not executed.',
            'Nexus TLS certificate chain unavailable; new wheels and dependency locks were not rebuilt.',
            'Container and production scatter/gather/broadcast were not tested.',
        ],
    }
    output = ROOT / 'docs/validation/single-file-20261109.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() and all(algorithms.values()) and not vendor.strip() else 1


if __name__ == '__main__':
    raise SystemExit(main())
