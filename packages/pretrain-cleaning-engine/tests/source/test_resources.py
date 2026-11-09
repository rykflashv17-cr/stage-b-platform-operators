"""The renamed engine must install every byte of its pinned upstream distribution."""
import hashlib
import json
from pathlib import Path
from pretrain_cleaning_engine import REVISION, root


def test_upstream_revision_and_installed_resources():
    assert REVISION == "0e694dbc9706b2a5bac8e550f8cd9de18f5b7473"
    expected = json.loads(Path(__file__).with_name("vendor-sha256.json").read_text())
    actual = {p.relative_to(root()).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root().rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    assert actual == expected
    assert "LICENSE" in actual
