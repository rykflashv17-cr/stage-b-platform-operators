"""Exact operator versions approved for release; never a default-version map."""
from pathlib import Path
import yaml

from ci.publish.tags import PUBLICATION_VERSION


def load_versions(root, projects):
    from ci.plan.history import load_history
    available = set(projects) | set(load_history(root, projects))
    path = Path(root) / 'ci/release-versions.yaml'
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('release version scope is too large')
    versions = yaml.safe_load(path.read_text())
    if (not isinstance(versions, list) or not versions or
            any(not isinstance(key, str) or key not in available or
                not PUBLICATION_VERSION.fullmatch(key.rsplit('/v', 1)[-1]) for key in versions) or
            len(versions) != len(set(versions))):
        raise ValueError('invalid release version scope')
    return sorted(versions)
