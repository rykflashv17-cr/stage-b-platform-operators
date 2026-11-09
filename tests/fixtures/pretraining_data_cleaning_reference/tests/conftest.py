from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_REPO = Path(os.environ.get(
    "SLM_UPSTREAM_REPO", str(PACKAGE_ROOT.parents[2] / "github" / "SLM_data_curation"),
))
sys.path.insert(0, str(PACKAGE_ROOT))
if (UPSTREAM_REPO / "qwen-manip-preprocess" / "src").is_dir():
    sys.path.insert(0, str(UPSTREAM_REPO / "qwen-manip-preprocess" / "src"))


@pytest.fixture
def upstream_repo() -> Path:
    if not (UPSTREAM_REPO / "qwen-manip-preprocess" / "src").is_dir():
        pytest.skip("Set SLM_UPSTREAM_REPO to the source repository for integration tests")
    return UPSTREAM_REPO
