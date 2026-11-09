import json
import subprocess
import pytest


def test_build_pulls_only_frozen_digest_not_the_mutable_tag(monkeypatch):
    from ci.build.bases import materialize
    ref='harbor.striding.internal/data/data-operators/base/runtime/python312-cpu:1.0.0'
    fixed=ref.rsplit(':',1)[0]+'@sha256:'+'a'*64
    calls=[]
    monkeypatch.setattr('ci.build.bases.pull',lambda value:calls.append(value) or 'sha256:'+'a'*64)
    monkeypatch.setattr('ci.build.bases.inspect_image',lambda value:{'Id':'sha256:'+'b'*64,'Os':'linux','Architecture':'amd64','RepoDigests':[fixed]})
    actual=materialize(ref,{'reference':fixed,'digest':'sha256:'+'a'*64})
    assert calls==[fixed] and actual['image_id']=='sha256:'+'b'*64
    with pytest.raises(ValueError,match='frozen'):
        materialize(ref,{'reference':fixed,'digest':'sha256:'+'c'*64})
