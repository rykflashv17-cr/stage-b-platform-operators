import hashlib
from pathlib import Path
from types import SimpleNamespace

from ci.build import wheelhouse


def test_business_wheel_change_preserves_dependency_layer(tmp_path, monkeypatch):
    sdk = tmp_path / "sdk-1.0-py3-none-any.whl"
    sdk.write_bytes(b"stable-sdk")
    own = tmp_path / "example-1.0-py3-none-any.whl"
    project = SimpleNamespace(distribution="example", version="1.0", wheel=own.name, path=tmp_path)
    record = {"name": "sdk", "version": "1.0", "archive": {"path": sdk.name,
              "hashes": {"sha256": hashlib.sha256(sdk.read_bytes()).hexdigest()}}}
    monkeypatch.setattr(wheelhouse, "export_lock", lambda _: {"packages": [record]})
    target = {"markers": {"python_full_version": "3.12.14"}, "tags": ["py3-none-any"]}
    results = []
    for index in range(2):
        own.write_bytes(f"business-{index}".encode())
        context = tmp_path / str(index); context.mkdir()
        wheelhouse.prepare(project, [sdk, own], target, context, tmp_path / "cache")
        deps = {p.relative_to(context).as_posix(): p.read_bytes()
                for p in (context / "wheelhouse/dependencies").glob("*.whl")}
        assert deps == {"wheelhouse/dependencies/sdk-1.0-py3-none-any.whl": b"stable-sdk"}
        assert (context / "wheelhouse/projects" / own.name).read_bytes() == f"business-{index}".encode()
        results.append((deps, (context / "requirements.dependencies.txt").read_bytes(),
                        (context / "requirements.projects.txt").read_bytes()))
    assert results[0][:2] == results[1][:2]
    assert results[0][2] != results[1][2]
