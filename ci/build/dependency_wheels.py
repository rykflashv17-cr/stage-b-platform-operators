"""Stage exact historical package wheels from Nexus without retaining their source."""
from dataclasses import dataclass
from email.parser import BytesParser
import hashlib
from pathlib import Path
import re
import shutil
import tempfile
import tomllib
from zipfile import ZipFile

from ci.build.sdk_wheel import download_from_nexus
from ci.plan.projects import normalized_name


@dataclass(frozen=True)
class WheelPin:
    name: str
    version: str
    filename: str
    digest: str


def locked_wheels(root, directory):
    root, directory = Path(root), Path(directory)
    metadata = tomllib.loads((directory / 'pyproject.toml').read_text())
    sources = metadata.get('tool', {}).get('uv', {}).get('sources', {})
    if not sources:
        return []
    lock = tomllib.loads((directory / 'uv.lock').read_text())
    pins = []
    for name, source in sources.items():
        path = source.get('path', '') if isinstance(source, dict) else ''
        if not path.endswith('.whl'):
            continue
        filename = Path(path).name
        expected = (root / 'build/wheels' / filename).resolve()
        if (directory / path).resolve() != expected:
            raise ValueError(f'{directory}: wheel staging path mismatch: {name}')
        rows = [p for p in lock['package'] if normalized_name(p['name']) == normalized_name(name)]
        if len(rows) != 1 or rows[0].get('source') != source:
            raise ValueError(f'{directory}: missing exact wheel lock: {name}')
        row = rows[0]
        hashes = [w['hash'] for w in row.get('wheels', []) if w.get('filename') == filename]
        canonical = f'{normalized_name(name).replace("-", "_")}-{row["version"]}-py3-none-any.whl'
        if filename != canonical or len(hashes) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', hashes[0]):
            raise ValueError(f'{directory}: invalid wheel identity or digest: {name}')
        pins.append(WheelPin(normalized_name(name), row['version'], filename, hashes[0][7:]))
    return pins


def verify_wheel(path, pin):
    path = Path(path)
    if path.name != pin.filename or path.is_symlink() or not path.is_file():
        raise ValueError('wheel filename or file type differs from lock')
    if hashlib.sha256(path.read_bytes()).hexdigest() != pin.digest:
        raise ValueError('wheel digest differs from lock')
    with ZipFile(path) as archive:
        entries = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
        if len(entries) != 1:
            raise ValueError('invalid wheel metadata')
        metadata = BytesParser().parsebytes(archive.read(entries[0]))
    if normalized_name(metadata['Name']) != pin.name or metadata['Version'] != pin.version:
        raise ValueError('wheel metadata differs from lock')


def prepare_wheel(root, pin, *, wheel=None, download=download_from_nexus):
    directory = Path(root) / 'build/wheels'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / pin.filename
    if wheel is not None:
        verify_wheel(wheel, pin)
    if target.exists() or target.is_symlink():
        verify_wheel(target, pin)
        return target
    with tempfile.TemporaryDirectory(dir=directory) as scratch:
        staged = Path(scratch) / pin.filename
        if wheel is None:
            download(pin, Path(scratch))
        else:
            shutil.copyfile(wheel, staged)
        verify_wheel(staged, pin)
        staged.replace(target)
    return target


def prepare_external(root, directories, owners, *, offline=False):
    """Local owners are rebuilt; every other locked path wheel comes from Nexus."""
    local = {p.wheel for p in owners}
    pins = {}
    for directory in directories:
        for pin in locked_wheels(root, directory):
            if pin.filename in pins and pins[pin.filename] != pin:
                raise ValueError('conflicting wheel hashes for one version: ' + pin.filename)
            pins[pin.filename] = pin
    result = []
    for filename, pin in sorted(pins.items()):
        if filename in local:
            continue
        if offline and not (Path(root) / 'build/wheels' / filename).exists():
            raise ValueError('offline wheel missing: ' + filename)
        result.append(prepare_wheel(root, pin))
    return result
