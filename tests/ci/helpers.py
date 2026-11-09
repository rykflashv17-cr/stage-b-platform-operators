from pathlib import Path
import json
import os
import shutil
import sys
import pytest
from ci import ROOT

def selected_versions():
    """Small unit-test representatives, unrelated to publication eligibility."""
    from ci.plan.projects import operator_projects
    versions = {}
    for project in sorted(operator_projects(ROOT), key=lambda p:(p.name,p.version)):
        versions.setdefault(project.name, project.version)
    return versions

SELECTED_VERSIONS = selected_versions()

def selected_project(code):
    return ROOT / 'operators' / code



def sdk_fixture(root, directory):
    """Create a metadata-only wheel and lock for acquisition/context unit tests."""
    import hashlib
    from zipfile import ZipFile
    from ci.build.sdk_wheel import locked_sdk
    pin = locked_sdk(root, directory)
    target = root / 'fixture-download' / pin.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, 'w') as archive:
        archive.writestr(f'striding_operator_sdk-{pin.version}.dist-info/METADATA',
            f'Metadata-Version: 2.4\nName: striding-operator-sdk\nVersion: {pin.version}\n')
    lock = directory / 'uv.lock'
    lock.write_text(lock.read_text().replace(pin.digest, hashlib.sha256(target.read_bytes()).hexdigest()))
    return locked_sdk(root, directory), target
