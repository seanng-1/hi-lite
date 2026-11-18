# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned local projects. JSON saves are atomic and revision checked.

Existing single-workspace recordings are adopted by reference, never moved or
copied: source validity fingerprints and old download links remain valid.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4
from transcript_store import _atomic_write

LEGACY_ID = '0' * 32
PROJECT_ID = re.compile(r'^[a-f0-9]{32}$')
MAX_CHECKPOINTS = 50


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

    def _checkpoint_folder(self, project_id):
        self._read(project_id)
        folder = self.directory(project_id) / 'checkpoints'
        if folder.is_symlink() or folder.resolve().parent != self.directory(project_id).resolve():
            raise ValueError('Invalid checkpoint directory.')
        return folder

    def _checkpoint_path(self, project_id, checkpoint_id):
        if not isinstance(checkpoint_id, str) or not PROJECT_ID.fullmatch(checkpoint_id):
            raise ValueError('Invalid checkpoint ID.')
        path = self._checkpoint_folder(project_id) / (checkpoint_id + '.json')
        if path.is_symlink():
            raise ValueError('Invalid checkpoint file.')
        return path

    def _recording_signature(self, project_id):
        """Bind edits to the same transcript, without retaining private source paths."""
        folder = Path(self.paths(project_id)['MANIFEST_DIR'])
        result = {}
        for name in ('segments_manifest.json', 'transcription.json'):
            path = folder / name
            if path.is_file():
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                result[name] = digest.hexdigest()
        if 'segments_manifest.json' not in result:
            raise ValueError('Import a recording before saving checkpoints.')
        return result

    @staticmethod
    def _checkpoint_summary(record):
        return {key: record[key] for key in ('id', 'name', 'created_at', 'project_revision')}

    def _read_checkpoint(self, project_id, checkpoint_id):
        path = self._checkpoint_path(project_id, checkpoint_id)
        try:
            record = json.loads(path.read_bytes())
        except FileNotFoundError:
            raise FileNotFoundError('Checkpoint not found in this project.') from None
        if (not isinstance(record, dict) or record.get('version') != 1 or
                record.get('project_id') != project_id or record.get('id') != checkpoint_id or
                not isinstance(record.get('name'), str) or not isinstance(record.get('created_at'), str) or
                type(record.get('project_revision')) is not int or not isinstance(record.get('recording'), dict)):
            raise ValueError('The checkpoint is invalid.')
        validate_state(record.get('state'))
        return record

    def list_checkpoints(self, project_id):
        with self.lock:
            folder = self._checkpoint_folder(project_id)
            result = []
            for path in folder.glob('*.json'):
                if PROJECT_ID.fullmatch(path.stem) and not path.is_symlink():
                    try:
                        result.append(self._checkpoint_summary(self._read_checkpoint(project_id, path.stem)))
                    except (ValueError, OSError):
                        continue
            return sorted(result, key=lambda record: record['created_at'], reverse=True)

    def create_checkpoint(self, project_id, revision, name):
        with self.lock:
            project = self._read(project_id)
            if type(revision) is not int or revision != project['revision']:
                raise ProjectConflict('This project changed in another window. Reload before saving a checkpoint.')
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
                raise ValueError('Checkpoint name must contain 1 to 120 characters.')
            folder = self._checkpoint_folder(project_id)
            if sum(1 for _ in folder.glob('*.json')) >= MAX_CHECKPOINTS:
                raise ValueError(f'This project already has {MAX_CHECKPOINTS} checkpoints. Delete an unused checkpoint first.')
            record = dict(version=1, id=uuid4().hex, project_id=project_id, name=name.strip(),
                          created_at=utc_now(), project_revision=revision,
                          recording=self._recording_signature(project_id), state=validate_state(project['state']))
            folder.mkdir(parents=True, exist_ok=True)
            _atomic_write(self._checkpoint_path(project_id, record['id']), json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            return self._checkpoint_summary(record)

    def restore_checkpoint(self, project_id, checkpoint_id, revision):
        with self.lock:
            project = self._read(project_id)
            if type(revision) is not int or revision != project['revision']:
                raise ProjectConflict('This project changed in another window. Reload before restoring a checkpoint.')
            checkpoint = self._read_checkpoint(project_id, checkpoint_id)
            if checkpoint['recording'] != self._recording_signature(project_id):
                raise ValueError('The recording or transcript changed. This checkpoint cannot be restored safely.')
            # Publish the recovery point before changing state. A failed save leaves
            # the current project untouched and the recovery point still available.
            recovery = self.create_checkpoint(project_id, revision, ('Before restoring: ' + checkpoint['name'])[:120])
            restored = self.save(project_id, revision, checkpoint['state'])
            return dict(project=restored, recovery_checkpoint=recovery)

    def delete_checkpoint(self, project_id, checkpoint_id):
        with self.lock:
            path = self._checkpoint_path(project_id, checkpoint_id)
            if not path.is_file():
                raise FileNotFoundError('Checkpoint not found in this project.')
            path.unlink()
