"""Isolated test subprocess. Only stdlib is imported before origin validation."""
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import sys


def main(config_path):
    config = json.loads(Path(config_path).read_text())
    report = {"collected": 0, "passed": 0, "failed": 0, "skipped": 0, "exit_code": 1}
    destination = Path(config["report"])
    try:
        sys.path[:0] = config["sources"]
        for name, directory in config["modules"].items():
            spec = importlib.util.find_spec(name)
            if not spec or not spec.origin or not Path(spec.origin).resolve().is_relative_to(Path(directory).resolve()):
                raise ValueError(f"module origin mismatch: {name}: {spec.origin if spec else 'missing'}")
        report['module_origins']={name:importlib.util.find_spec(name).origin for name in config['modules']}
        report['distributions']={name:importlib.metadata.version(name) for name in config.get('distributions',{})}
        if report['distributions']!=config.get('distributions',{}):
            raise ValueError('installed distribution version mismatch')
        sys.path[:0] = config.get("helpers", [])
        if config.get("declaration"):
            from tools.check_operators import check_one
            from tools.export_registration import registration
            report['declaration']=check_one(config['declaration'])
            registration(Path(config['declaration']))
        import pytest
        counts = report

        class Results:
            def pytest_collection_finish(self, session):
                report["collected"] = len(session.items)

            def pytest_runtest_logreport(self, report):
                if report.failed:
                    counts["failed"] += 1
                elif report.skipped:
                    counts["skipped"] += 1
                elif report.when == "call" and report.passed:
                    counts["passed"] += 1

        # Keep repository-level pythonpath/conftest out of wheel tests. A project's
        # own tests/conftest remains available, including its fixtures/parameters.
        tests = config["test_paths"]
        test_root = os.path.commonpath(tests)
        config_file = destination.with_suffix(".ini")
        config_file.write_text("[pytest]\naddopts = --import-mode=importlib\n")
        code = int(pytest.main(["-q", "-c", str(config_file), "--rootdir", test_root,
                                "--confcutdir", test_root, *tests], plugins=[Results()]))
        report["exit_code"] = code if code else (0 if report["passed"] else 5)
        for name,directory in config['modules'].items():
            module=sys.modules.get(name)
            if module is not None and not Path(module.__file__).resolve().is_relative_to(Path(directory).resolve()):
                report['exit_code']=1
                raise ValueError(f'module origin changed during tests: {name}')
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    destination.write_text(json.dumps(report, indent=2) + "\n")
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
