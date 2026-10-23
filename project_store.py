# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned local projects. JSON saves are atomic and revision checked.

Existing single-workspace recordings are adopted by reference, never moved or
copied: source validity fingerprints and old download links remain valid.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4
from transcript_store import _atomic_write

LEGACY_ID = '0' * 32
PROJECT_ID = re.compile(r'^[a-f0-9]{32}$')


class ProjectConflict(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_state(state):
    if not isinstance(state, dict):
        raise ValueError('Project state must be an object.')
    try:
        encoded = json.dumps(state, allow_nan=False, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError):
        raise ValueError('Project state must contain finite JSON values.') from None
    if len(encoded) > 16 * 1024 * 1024:
        raise ValueError('Project state exceeds the 16 MB limit.')
    ids = state.get('selected_ids', [])
    if (not isinstance(ids, list) or any(type(v) is not int or v < 0 for v in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Selected clips must be unique nonnegative integer IDs.')
    return deepcopy(state)


class ProjectStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.projects = self.root / 'projects'
        self.lock = RLock()

    def directory(self, project_id):
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise ValueError('Invalid project ID.')
        path = self.projects / project_id
        if path.is_symlink() or path.resolve().parent != self.projects.resolve():
            raise ValueError('Invalid project directory.')
        return path

    def _read(self, project_id):
        try:
            record = json.loads((self.directory(project_id) / 'project.json').read_bytes())
        except FileNotFoundError:
            raise FileNotFoundError('Project not found.') from None
        if not isinstance(record, dict) or record.get('id') != project_id or record.get('version') != 1:
            raise ValueError('Project file is invalid or uses an unsupported version.')
        if type(record.get('revision')) is not int or record['revision'] < 0:
            raise ValueError('Project revision is invalid.')
        validate_state(record.get('state'))
        return record

    def paths(self, project_id):
        record = self._read(project_id)
        base = self.root if project_id == LEGACY_ID and record.get('storage') == 'legacy' else self.directory(project_id)
        manifest = base / 'highlights'
        return dict(MANIFEST_DIR=str(manifest), SEGMENTS_DIR=str(manifest / 'segments'),
                    SEGMENTS_JSON=str(manifest / 'segments_manifest.json'),
                    HIGHLIGHTS_DIR=str(manifest / 'final'), SCORED_SEGMENTS=str(manifest / 'scored_segments.json'),
                    UPLOAD_FOLDER=str(base / 'uploads'))

    def _public(self, record):
        result = {key: deepcopy(value) for key, value in record.items() if key != 'storage'}
        result['has_media'] = Path(self.paths(record['id'])['SEGMENTS_JSON']).is_file()
        return result

    def get(self, project_id):
        with self.lock:
            return self._public(self._read(project_id))

    def _write(self, record):
        folder = self.directory(record['id'])
        folder.mkdir(parents=True, exist_ok=True)
        _atomic_write(folder / 'project.json', json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))

    def create(self, name='Untitled project', *, project_id=None, storage=None):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError('Project name must contain 1 to 120 characters.')
        with self.lock:
            project_id = project_id or uuid4().hex
            if self.directory(project_id).exists():
                raise ProjectConflict('Project already exists.')
            now = utc_now()
            record = dict(version=1, id=project_id, name=name.strip(), created_at=now, updated_at=now,
                          revision=0, state={'selected_ids': [], 'edits': []})
            if storage:
                record['storage'] = storage
            self._write(record)
            paths = self.paths(project_id)
            for key in ('UPLOAD_FOLDER', 'SEGMENTS_DIR', 'HIGHLIGHTS_DIR'):
                Path(paths[key]).mkdir(parents=True, exist_ok=True)
            return self._public(record)

    def list(self):
        with self.lock:
            # Adoption is non-destructive and only occurs when a real workspace
            # exists. Empty application startup does not create a fake recording.
            if (self.root / 'highlights' / 'segments_manifest.json').is_file() and not self.directory(LEGACY_ID).exists():
                self.create('Previous recording', project_id=LEGACY_ID, storage='legacy')
            records = []
            if self.projects.exists():
                for folder in self.projects.iterdir():
                    if PROJECT_ID.fullmatch(folder.name) and not folder.is_symlink():
                        try:
                            records.append(self.get(folder.name))
                        except (OSError, ValueError):
                            # One corrupt project must not hide all other work.
                            continue
            return sorted(records, key=lambda record: record['updated_at'], reverse=True)

    def save(self, project_id, revision, state, name=None):
        with self.lock:
            record = self._read(project_id)
            if type(revision) is not int or revision != record['revision']:
                raise ProjectConflict('This project changed in another window. Reopen it before saving again.')
            record['state'] = validate_state(state)
            if name is not None:
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
                    raise ValueError('Project name must contain 1 to 120 characters.')
                record['name'] = name.strip()
            record['revision'] += 1
            record['updated_at'] = utc_now()
            self._write(record)
            return self._public(record)
