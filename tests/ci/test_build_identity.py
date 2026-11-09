from pathlib import Path


def test_identity_needs_no_built_wheel_and_tracks_sources_tools_and_bases(tmp_path):
    from ci.build.identity import build_identity
    project=tmp_path/'operators/example';(project/'src/example').mkdir(parents=True)
    (project/'pyproject.toml').write_text('[project]\nname="example"\nversion="1.0.0"\n[tool.hatch.build.targets.wheel]\npackages=["src/example"]\n')
    (project/'operator.yaml').write_text('code: example\noperatorVersion: 1.0.0\n')
    source=project/'src/example/__init__.py';source.write_text('VALUE=1\n')
    (project/'uv.lock').write_text('version=1\n')
    ref='harbor.striding.internal/data/data-operators/base/runtime/python312-cpu:1.0.0'
    (project/'Dockerfile').write_text('FROM '+ref+'\n')
    bases={ref:{'reference':ref.rsplit(':',1)[0]+'@sha256:'+'a'*64,'digest':'sha256:'+'a'*64}}
    first=build_identity(tmp_path,'example/v1.0.0',bases)
    assert not (tmp_path/'build/wheels').exists()
    (tmp_path/'README.md').write_text('documentation only\n')
    assert build_identity(tmp_path,'example/v1.0.0',bases)==first
    source.write_text('VALUE=2\n')
    assert build_identity(tmp_path,'example/v1.0.0',bases)['digest']!=first['digest']
    source.write_text('VALUE=1\n')
    tool=tmp_path/'ci/build/context.py';tool.parent.mkdir(parents=True);tool.write_text('# changed builder\n')
    assert build_identity(tmp_path,'example/v1.0.0',bases)['digest']!=first['digest']
    tool.unlink()
    bases[ref]={'reference':ref.rsplit(':',1)[0]+'@sha256:'+'b'*64,'digest':'sha256:'+'b'*64}
    assert build_identity(tmp_path,'example/v1.0.0',bases)['digest']!=first['digest']
