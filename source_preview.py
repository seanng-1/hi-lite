# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated source ownership for the existing single import workspace.

This is a validity marker, not a project contract. Only a completed import may
publish it. Legacy workspaces fail closed; never infer ownership from filenames.
Callers hold SOURCE_LOCK when reading, publishing or invalidating the marker.
"""
import hashlib
import json
from pathlib import Path
import re
from threading import RLock

from transcript_store import _atomic_write, read_current_transcript

SOURCE_LOCK = RLock()
MARKER = 'current-source.json'
UNAVAILABLE = 'Original source unavailable. Reimport this recording to enable source preview.'


def invalidate_source(folder):
    Path(folder, MARKER).unlink(missing_ok=True)


def fingerprint(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def managed_source(uploads, filename):
    if not isinstance(filename, str) or not filename or '/' in filename or '\\' in filename or filename in ('.', '..'):
        raise ValueError(UNAVAILABLE)
    root = Path(uploads).resolve()
    path = root / filename
    if path.is_symlink() or path.resolve().parent != root or not path.is_file() or path.stat().st_size == 0:
        raise ValueError(UNAVAILABLE)
    return path


def publish_source(folder, uploads, source_path, source_id):
    folder = Path(folder)
    path = managed_source(uploads, Path(source_path).name)
    if path != Path(source_path).absolute() or not re.fullmatch(r'[a-f0-9]{32}', source_id):
        raise ValueError(UNAVAILABLE)
    read_current_transcript(folder)
    record = dict(id=source_id, filename=path.name, fingerprint=fingerprint(path),
                  manifest=digest(folder / 'segments_manifest.json'),
                  transcript=digest(folder / 'transcription.json'))
    _atomic_write(folder / MARKER, json.dumps(record).encode('utf-8'))


def resolve_source(folder, uploads, source_id=None):
    try:
        folder = Path(folder)
        record = json.loads((folder / MARKER).read_bytes())
        if not re.fullmatch(r'[a-f0-9]{32}', record['id']) or source_id is not None and source_id != record['id']:
            raise ValueError(UNAVAILABLE)
        path = managed_source(uploads, record['filename'])
        if (record['fingerprint'] != fingerprint(path) or
                record['manifest'] != digest(folder / 'segments_manifest.json') or
                record['transcript'] != digest(folder / 'transcription.json')):
            raise ValueError(UNAVAILABLE)
        read_current_transcript(folder)
        return path, dict(id=record['id'], url=f"/api/source/{record['id']}")
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError(UNAVAILABLE) from None
