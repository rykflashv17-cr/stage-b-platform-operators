"""B04 business archive and its portable, generic file manifest."""
import hashlib
import json
from pathlib import PurePosixPath
from zipfile import ZipFile, ZipInfo, ZIP_STORED

from pretraining_data_cleaning_common.handoff import require


def _chunks(stream, check):
    while True:
        check()
        block = stream.read(1024 * 1024)
        if not block:
            return
        yield block


def build_archive(ctx, selected, paths):
    require(len(selected) == len(paths), 'download count differs from selected files')
    limit = ctx.storage.limits.max_upload_bytes
    require(len(selected) <= 100000, 'archive member limit exceeded')
    files = []
    rows = []
    for item, path in zip(selected, paths, strict=True):
        index = item['episode_index']
        relative = f'episode_{index:03d}/' + PurePosixPath(item['source_ref']['logical_key']).name
        rows.append((relative, item, path))
    rows.sort(key=lambda row: row[0].encode('utf-8'))
    require(len({row[0] for row in rows}) == len(rows), 'duplicate delivery path')
    target = ctx.workspace.path('accepted_originals.zip')
    try:
        with ZipFile(target, 'x', compression=ZIP_STORED, allowZip64=True) as archive:
            for relative, item, path in rows:
                checksum, size = hashlib.sha256(), 0
                info = ZipInfo(relative)
                info.compress_type = ZIP_STORED
                info.external_attr = 0o100644 << 16
                with path.open('rb') as source, archive.open(info, 'w', force_zip64=True) as destination:
                    for block in _chunks(source, ctx.check_cancelled):
                        require(archive.fp.tell() + len(block) <= limit, "archive byte budget exceeded")
                        destination.write(block)
                        checksum.update(block)
                        size += len(block)
                require(checksum.hexdigest() == item['source_ref']['sha256'], 'original MCAP digest mismatch')
                files.append(dict(delivery_path=relative, size_bytes=size, sha256=checksum.hexdigest(),
                                  business=dict(episode_index=item['episode_index'], source_ref=item['source_ref'])))
            manifest = dict(format='delivery_manifest_v1', files=files)
            raw = json.dumps(manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
            require(len(raw) <= 16 * 1024**2, 'manifest byte limit exceeded')
            require(archive.fp.tell() + len(raw) <= limit, 'archive byte budget exceeded')
            archive.writestr('delivery_manifest.json', raw)
        require(target.stat().st_size <= limit, 'archive byte budget exceeded')
        with ZipFile(target) as archive:
            require(set(archive.namelist()) == {'delivery_manifest.json', *(f['delivery_path'] for f in files)}
                    and len(archive.namelist()) == len(files) + 1, 'archive member mismatch')
            require(json.loads(archive.read('delivery_manifest.json')) == manifest, 'archive manifest mismatch')
            for item in files:
                checksum, size = hashlib.sha256(), 0
                with archive.open(item['delivery_path']) as source:
                    for block in _chunks(source, ctx.check_cancelled):
                        checksum.update(block)
                        size += len(block)
                require(size == item['size_bytes'] and checksum.hexdigest() == item['sha256'], 'archive digest mismatch')
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise
