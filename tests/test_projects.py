# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project_store import ProjectStore, ProjectConflict, LEGACY_ID
from transcript_store import publish_transcript
from source_preview import publish_source

ROOT = Path(__file__).resolve().parents[1]


class Projects(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProjectStore(self.root)

    def test_round_trip_revision_and_independent_directories(self):
        one = self.store.create('First')
        two = self.store.create('Second')
        state = {'selected_ids': [2], 'edits': [{'start': 1, 'end': 2}], 'captions': {'2': 'Changed'}}
        saved = self.store.save(one['id'], 0, state)
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(ProjectStore(self.root).get(one['id'])['state'], state)
        self.assertEqual(self.store.get(two['id'])['state']['selected_ids'], [])
        self.assertNotEqual(self.store.paths(one['id']), self.store.paths(two['id']))
        with self.assertRaises(ProjectConflict):
            self.store.save(one['id'], 0, {'selected_ids': []})
        self.assertEqual(self.store.get(one['id'])['state'], state)

    def test_legacy_adoption_never_moves_or_copies_media(self):
        (self.root / 'highlights').mkdir()
        manifest = self.root / 'highlights' / 'segments_manifest.json'
        manifest.write_text('{"segments": []}')
        before = manifest.stat()
        result = self.store.list()
        self.assertEqual(result[0]['id'], LEGACY_ID)
        self.assertEqual(Path(self.store.paths(LEGACY_ID)['MANIFEST_DIR']), self.root / 'highlights')
        self.assertEqual(before.st_ino, manifest.stat().st_ino)
        self.assertEqual(manifest.read_text(), '{"segments": []}')
        self.assertEqual(len(self.store.list()), 1)

    def test_validation_atomic_failure_and_traversal(self):
        project = self.store.create()
        for state in ({'selected_ids': [True]}, {'selected_ids': [1, 1]}, {'selected_ids': [-1]}, {'bad': float('nan')}):
            with self.assertRaises(ValueError): self.store.save(project['id'], 0, state)
        with self.assertRaises(ValueError): self.store.get('../outside')
        with patch('project_store._atomic_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.store.save(project['id'], 0, {'selected_ids': [1]})
        self.assertEqual(self.store.get(project['id'])['revision'], 0)
        self.assertEqual(self.store.get(project['id'])['state']['selected_ids'], [])


class ProjectAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        spec = importlib.util.spec_from_file_location('project_test_backend', ROOT / 'web_interface.py')
        self.backend = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'HI_LITE_DATA_DIR': self.temp.name}):
            spec.loader.exec_module(self.backend)
        self.client = self.backend.app.test_client()
        self.one = self.client.post('/api/projects', json={'name': 'One'}).json
        self.two = self.client.post('/api/projects', json={'name': 'Two'}).json

    def url(self, project, suffix=''):
        return '/api/projects/' + project['id'] + suffix

    def seed(self, project):
        paths = self.backend.projects.paths(project['id'])
        segments = [{'id': 0, 'start': 0, 'end': 1, 'duration': 1, 'filename': 'segment_0.mp4', 'text': 'Hello'}]
        Path(paths['SEGMENTS_JSON']).write_text(json.dumps({'segments': segments}))
        publish_transcript(paths['MANIFEST_DIR'], {'segments': segments})
        source_id = 'a' * 32
        source = Path(paths['UPLOAD_FOLDER']) / 'source.mp4'
        source.write_bytes(b'fixture source')
        publish_source(paths['MANIFEST_DIR'], paths['UPLOAD_FOLDER'], source, source_id)
        return paths, source_id

    def test_reopen_scoped_source_and_subtitles_without_pipeline(self):
        paths, source_id = self.seed(self.one)
        with patch.object(self.backend.subprocess, 'Popen', side_effect=AssertionError('retranscription')):
            manifest = self.client.get(self.url(self.one, '/data/segments_manifest.json'))
            self.assertEqual(manifest.status_code, 200)
            self.assertEqual(manifest.json['source']['url'], self.url(self.one, '/api/source/' + source_id))
            self.assertEqual(self.client.get(manifest.json['source']['url']).data, b'fixture source')
            self.assertEqual(self.client.get(self.url(self.two, '/api/source/' + source_id)).status_code, 404)
            self.assertEqual(self.client.get(self.url(self.two, '/data/segments_manifest.json')).json, {'segments': []})
            self.assertIn(b'Hello', self.client.get(self.url(self.one, '/api/subtitles/srt')).data)
            self.assertEqual(self.client.get('/data/segments_manifest.json').json, {'segments': []})

    def test_project_save_conflict_and_existing_source_not_overwritten(self):
        paths, source_id = self.seed(self.one)
        saved = self.client.put(self.url(self.one), json={'revision': 0, 'state': {'selected_ids': [0]}})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.client.put(self.url(self.one), json={'revision': 0, 'state': {}}).status_code, 409)
        before = Path(paths['SEGMENTS_JSON']).read_bytes()
        response = self.client.post(self.url(self.one, '/api/upload'), data={'video': (io.BytesIO(b'new'), 'new.mp4')})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Path(paths['SEGMENTS_JSON']).read_bytes(), before)
        self.assertIsNone(self.backend._workspace_owner)

    def test_background_worker_inherits_project_and_task_status_is_scoped(self):
        held = []
        class Thread:
            def __init__(self, target, args): self.target, self.args = target, args; held.append(self)
            def start(self): pass
        visited = []
        def worker(path, folder, task):
            visited.append(self.backend.workspace_paths())
            self.backend.processing_status[task] = {'status': 'complete'}
        with patch.object(self.backend.threading, 'Thread', Thread), patch.object(self.backend, 'process_video_async', worker):
            response = self.client.post(self.url(self.one, '/api/upload'), data={'video': (io.BytesIO(b'new'), 'new.mp4')})
            self.assertEqual(response.status_code, 200)
            task = response.json['task_id']
            self.assertEqual(self.client.get(self.url(self.two, '/api/processing-status/' + task)).status_code, 404)
            held[0].target(*held[0].args)
        self.assertEqual(visited[0], self.backend.projects.paths(self.one['id']))
        self.assertEqual(self.client.get(self.url(self.one, '/api/processing-status/' + task)).json['status'], 'complete')
        self.assertIsNone(self.backend._workspace_owner)
        self.assertIsNone(self.backend._current_project.get())


if __name__ == '__main__': unittest.main()
