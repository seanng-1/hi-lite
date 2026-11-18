# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated source ownership for imports and explicitly linked desktop files.

Only a completed import may publish current-source.json. Browser requests never
choose filesystem paths. Desktop preparation requires an authenticated, native
picker request; the marker binds that path to the completed transcript/manifest.
Callers hold SOURCE_LOCK when reading, publishing or invalidating markers.
"""
import hashlib
import json
from pathlib import Path
import re
import stat
from threading import RLock

from transcript_store import _atomic_write, read_current_transcript

SOURCE_LOCK = RLock()
MARKER = 'current-source.json'
EXTERNAL_PREPARATION = 'external-source.json'
UNAVAILABLE = 'Original source unavailable. Reimport this recording to enable source preview.'
EXTERNAL_UNAVAILABLE = 'Linked original unavailable or changed. Use Relink original to select the same recording.'


def invalidate_source(folder):
    Path(folder, MARKER).unlink(missing_ok=True)
    discard_external_preparation(folder)


def discard_external_preparation(folder):
    Path(folder, EXTERNAL_PREPARATION).unlink(missing_ok=True)


def fingerprint(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def external_fingerprint(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def digest(path):
    """Hash large original recordings in bounded memory, never read_bytes()."""
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def managed_source(uploads, filename):
    if not isinstance(filename, str) or not filename or '/' in filename or '\\' in filename or filename in ('.', '..'):
        raise ValueError(UNAVAILABLE)
    root = Path(uploads).resolve()
    path = root / filename
    if path.is_symlink() or path.resolve().parent != root or not path.is_file() or path.stat().st_size == 0:
        raise ValueError(UNAVAILABLE)
    return path


def external_source(path):
    """Accept only the canonical absolute regular-file path stored by preparation."""
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError(EXTERNAL_UNAVAILABLE)
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink() or candidate.resolve(strict=True) != candidate:
        raise ValueError(EXTERNAL_UNAVAILABLE)
    info = candidate.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise ValueError(EXTERNAL_UNAVAILABLE)
    return candidate


def inspect_external_source(path):
    """Resolve a picker selection once, then detect changes while hashing it."""
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError('Choose an original video file using the desktop picker.')
    candidate = external_source(str(Path(path).resolve(strict=True)))
    before = external_fingerprint(candidate)
    content = digest(candidate)
    if before != external_fingerprint(external_source(str(candidate))):
        raise ValueError('The original recording changed while it was being checked. Try again.')
    return dict(path=str(candidate), filename=candidate.name, fingerprint=before, content_sha256=content)


def prepare_external_source(folder, path, source_id):
    if not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_id):
        raise ValueError(EXTERNAL_UNAVAILABLE)
    record = dict(id=source_id, mode='external', **inspect_external_source(path))
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _atomic_write(folder / EXTERNAL_PREPARATION, json.dumps(record).encode('utf-8'))
    return Path(record['path'])


def _validate_external_record(record):
    if (record.get('mode') != 'external' or
            not isinstance(record.get('content_sha256'), str) or
            not re.fullmatch(r'[a-f0-9]{64}', record['content_sha256'])):
        raise ValueError(EXTERNAL_UNAVAILABLE)
    path = external_source(record.get('path'))
    if record.get('fingerprint') != external_fingerprint(path):
        raise ValueError(EXTERNAL_UNAVAILABLE)
    return path


def publish_source(folder, uploads, source_path, source_id):
    folder = Path(folder)
    if not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_id):
        raise ValueError(UNAVAILABLE)
    prepared = folder / EXTERNAL_PREPARATION
    if prepared.is_file():
        record = json.loads(prepared.read_bytes())
        if not isinstance(record, dict) or record.get('id') != source_id:
            raise ValueError(EXTERNAL_UNAVAILABLE)
        path = _validate_external_record(record)
        if path != Path(source_path).absolute() or digest(path) != record['content_sha256']:
            raise ValueError(EXTERNAL_UNAVAILABLE)
        _validate_external_record(record)
    else:
        path = managed_source(uploads, Path(source_path).name)
        if path != Path(source_path).absolute():
            raise ValueError(UNAVAILABLE)
        record = dict(id=source_id, filename=path.name, fingerprint=fingerprint(path))
    read_current_transcript(folder)
    record.update(manifest=digest(folder / 'segments_manifest.json'), transcript=digest(folder / 'transcription.json'))
    _atomic_write(folder / MARKER, json.dumps(record).encode('utf-8'))
    discard_external_preparation(folder)


def _read_bound_record(folder, source_id=None):
    record = json.loads((folder / MARKER).read_bytes())
    if (not isinstance(record, dict) or not isinstance(record.get('id'), str) or
            not re.fullmatch(r'[a-f0-9]{32}', record['id']) or
            source_id is not None and source_id != record['id']):
        raise ValueError(UNAVAILABLE)
    if (record['manifest'] != digest(folder / 'segments_manifest.json') or
            record['transcript'] != digest(folder / 'transcription.json')):
        raise ValueError(UNAVAILABLE)
    read_current_transcript(folder)
    return record


def relink_external_source(folder, selected_path):
    """Only an identical original can replace a moved/missing external file."""
    folder = Path(folder)
    record = _read_bound_record(folder)
    if record.get('mode') != 'external' or not isinstance(record.get('content_sha256'), str):
        raise ValueError('This project has no linked desktop source. Import it into a new project instead.')
    candidate = inspect_external_source(selected_path)
    if candidate['content_sha256'] != record['content_sha256']:
        raise ValueError('That file is not the original recording. Select an unchanged copy of the linked file.')
    record.update(candidate)
    _atomic_write(folder / MARKER, json.dumps(record).encode('utf-8'))
    return dict(id=record['id'], url=f"/api/source/{record['id']}")


def resolve_source(folder, uploads, source_id=None):
    try:
        folder = Path(folder)
        record = _read_bound_record(folder, source_id)
        if record.get('mode') == 'external':
            path = _validate_external_record(record)
        elif record.get('mode', 'managed') == 'managed':
            path = managed_source(uploads, record['filename'])
            if record['fingerprint'] != fingerprint(path):
                raise ValueError(UNAVAILABLE)
        else:
            raise ValueError(UNAVAILABLE)
        return path, dict(id=record['id'], url=f"/api/source/{record['id']}")
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError(UNAVAILABLE) from None
