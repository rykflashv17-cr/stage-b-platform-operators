"""Compare runtime metadata with the author-owned Python project declaration."""
import hashlib
from pathlib import Path
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version


def requirement_identity(value, extra=None):
    requirement = Requirement(value)
    if extra is not None:
        marker = f'extra == "{canonicalize_name(extra)}"'
        if requirement.marker:
            marker = f"({requirement.marker}) and {marker}"
        requirement.marker = Marker(marker)
    return (
        canonicalize_name(requirement.name),
        tuple(sorted(canonicalize_name(e) for e in requirement.extras)),
        str(requirement.specifier),
        str(requirement.marker) if requirement.marker else None,
        requirement.url,
    )


def metadata_errors(project, installed):
    errors = []
    owner = project["name"]
    if canonicalize_name(installed["name"]) != canonicalize_name(owner):
        errors.append(f"{owner}: metadata Name differs from project")
    if Version(installed["version"]) != Version(project["version"]):
        errors.append(f"{owner}: metadata Version differs from project")
    if SpecifierSet(installed.get("requires_python") or "") != SpecifierSet(project.get("requires-python") or ""):
        errors.append(f"{owner}: metadata Requires-Python differs from project")
    expected = {requirement_identity(r) for r in project.get("dependencies", [])}
    optional = project.get("optional-dependencies", {})
    for extra, requirements in optional.items():
        expected.update(requirement_identity(r, extra) for r in requirements)
    actual = {requirement_identity(r) for r in installed.get("requires_dist", [])}
    if expected != actual:
        errors.append(f"{owner}: metadata Requires-Dist differs from project")
    if {canonicalize_name(e) for e in optional} != {canonicalize_name(e) for e in installed.get("provides_extra", [])}:
        errors.append(f"{owner}: metadata Provides-Extra differs from project")
    return errors


def verify_wheel_lock(wheel, lock, name, version):
    matches = [
        p for p in lock["package"] if p["name"] == name and p["version"] == version
    ]
    expected = "sha256:" + hashlib.sha256(Path(wheel).read_bytes()).hexdigest()
    if len(matches) != 1 or not any(
        record.get("filename") == Path(wheel).name and record.get("hash") == expected
        for record in matches[0].get("wheels", [])
    ):
        raise ValueError(f"wheel does not match lock: {name}")
