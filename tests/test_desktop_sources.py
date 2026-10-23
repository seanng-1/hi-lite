# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from source_preview import (EXTERNAL_PREPARATION, MARKER, SOURCE_LOCK, digest, prepare_external_source,
                            publish_source, relink_external_source, resolve_source)
from transcript_store import publish_transcript

ROOT = Path(__file__).resolve().parents[1]


class ExternalSourceIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'highlights'
        self.uploads = self.root / 'uploads'
        self.folder.mkdir()
        self.uploads.mkdir()
        self.original = self.root / 'original.mp4'
        self.original.write_bytes(b'original recording contents')
        self.identity = 'a' * 32
        publish_transcript(self.folder, {'segments': [{'start': 0, 'end': 1, 'text': 'Test.'}]})
        (self.folder / 'segments_manifest.json').write_text('{"segments": []}')

    def publish(self):
        prepare_external_source(self.folder, str(self.original), self.identity)
        publish_source(self.folder, self.uploads, self.original, self.identity)

    def test_explicit_preparation_required_no_copy_and_public_metadata_has_no_path(self):
        with self.assertRaises(ValueError):
            publish_source(self.folder, self.uploads, self.original, self.identity)
        self.publish()
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertFalse((self.folder / EXTERNAL_PREPARATION).exists())
        path, public = resolve_source(self.folder, self.uploads)
        self.assertEqual(path, self.original)
        self.assertEqual(public, {'id': self.identity, 'url': '/api/source/' + self.identity})
        record = json.loads((self.folder / MARKER).read_text())
        self.assertEqual(record['mode'], 'external')
        self.assertEqual(record['content_sha256'], digest(self.original))

    def test_preparation_never_serves_unfinished_import(self):
        prepare_external_source(self.folder, str(self.original), self.identity)
        with self.assertRaises(ValueError):
            resolve_source(self.folder, self.uploads)
        with self.assertRaises(ValueError):
            publish_source(self.folder, self.uploads, self.original, 'b' * 32)
        self.assertFalse((self.folder / MARKER).exists())

    def test_modified_during_import_fails_closed(self):
        prepare_external_source(self.folder, str(self.original), self.identity)
        self.original.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            publish_source(self.folder, self.uploads, self.original, self.identity)
        self.assertFalse((self.folder / MARKER).exists())

    def test_moved_file_relinks_by_content_and_preserves_source_id(self):
        self.publish()
        moved = self.original.rename(self.root / 'moved.mp4')
        with self.assertRaises(ValueError):
            resolve_source(self.folder, self.uploads)
        public = relink_external_source(self.folder, str(moved))
        self.assertEqual(public['id'], self.identity)
        self.assertEqual(resolve_source(self.folder, self.uploads)[0], moved)

    def test_wrong_recording_cannot_relink_even_when_size_matches(self):
        self.publish()
        before = (self.folder / MARKER).read_bytes()
        other = self.root / 'other.mp4'
        other.write_bytes(b'x' * self.original.stat().st_size)
        with self.assertRaisesRegex(ValueError, 'not the original'):
            relink_external_source(self.folder, str(other))
        self.assertEqual((self.folder / MARKER).read_bytes(), before)
        self.assertEqual(resolve_source(self.folder, self.uploads)[0], self.original)

    def test_changed_content_cannot_hide_behind_same_size_and_mtime(self):
        self.publish()
        original_stat = self.original.stat()
        self.original.write_bytes(b'x' * original_stat.st_size)
        os.utime(self.original, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with self.assertRaises(ValueError):
            resolve_source(self.folder, self.uploads)

    def test_replaced_symlink_and_transcript_tampering_fail_closed(self):
        self.publish()
        moved = self.original.rename(self.root / 'moved.mp4')
        self.original.symlink_to(moved)
        with self.assertRaises(ValueError):
            resolve_source(self.folder, self.uploads)
        self.original.unlink()
        moved.rename(self.original)
        relink_external_source(self.folder, str(self.original))
        (self.folder / 'transcription.json').write_text('{}')
        with self.assertRaises(ValueError):
            resolve_source(self.folder, self.uploads)
        with self.assertRaises(ValueError):
            relink_external_source(self.folder, str(self.original))

    def test_preparation_rejects_nonabsolute_and_nonregular_paths(self):
        for path in ('original.mp4', str(self.root), ''):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                prepare_external_source(self.folder, path, self.identity)
        fifo = self.root / 'pipe.mp4'
        if hasattr(os, 'mkfifo'):
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                prepare_external_source(self.folder, str(fifo), self.identity)

    def test_hashing_does_not_load_entire_video(self):
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('Unbounded read')):
            prepare_external_source(self.folder, str(self.original), self.identity)


class DesktopSourceAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / 'original.mp4'
        self.original.write_bytes(b'0123456789')
        self.load_backend('test-private-desktop-token')
        self.project = self.web.projects.create('Linked project')
        self.project_id = self.project['id']
        self.paths = self.web.projects.paths(self.project_id)
        self.headers = {'X-Hi-Lite-Desktop-Token': 'test-private-desktop-token'}

    def load_backend(self, token):
        spec = importlib.util.spec_from_file_location('desktop_backend', ROOT / 'web_interface.py')
        self.web = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, HI_LITE_DATA_DIR=self.temp.name, HI_LITE_DESKTOP_TOKEN=token):
            spec.loader.exec_module(self.web)
            if 'desktop_link_project' not in self.web.app.view_functions:
                from desktop_sources import register_desktop_sources
                register_desktop_sources(self.web.app, vars(self.web))
        self.client = self.web.app.test_client()

    def call(self, relink=False, **changes):
        payload = dict(project_id=self.project_id, path=str(self.original))
        payload.update(changes)
        return self.client.post('/api/desktop/' + ('relink' if relink else 'link') + '-project',
                                json=payload, headers=self.headers)

    def import_now(self):
        class InlineThread:
            def __init__(thread, target, args):
                thread.run = lambda: target(*args)
            def start(thread):
                thread.run()
        def pipeline(source, folder, task):
            self.assertEqual(Path(source), self.original)
            self.assertEqual(self.web._current_project.get(), self.project_id)
            publish_transcript(folder, {'segments': []})
            Path(folder, 'segments_manifest.json').write_text('{"segments": []}')
            with SOURCE_LOCK:
                publish_source(folder, self.paths['UPLOAD_FOLDER'], source, task.removeprefix('task_'))
            self.web.processing_status[task] = dict(status='complete', progress=100)
        with patch('desktop_sources.threading.Thread', InlineThread), patch.object(self.web, 'process_video_async', side_effect=pipeline):
            response = self.call()
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_authentication_required_and_unconfigured_browser_disabled(self):
        for headers in ({}, {'X-Hi-Lite-Desktop-Token': 'wrong'}, {'X-Hi-Lite-Desktop-Token': 'é'}):
            for route in ('link-project', 'relink-project'):
                response = self.client.post('/api/desktop/' + route, headers=headers,
                                            json=dict(project_id=self.project_id, path=str(self.original)))
                self.assertEqual(response.status_code, 403)
        self.load_backend('')
        self.assertEqual(self.call().status_code, 403)
        self.assertFalse(Path(self.paths['MANIFEST_DIR'], EXTERNAL_PREPARATION).exists())

    def test_link_runs_existing_pipeline_without_upload_copy_and_serves_range(self):
        result = self.import_now()
        self.assertEqual(self.web._task_projects[result['task_id']], self.project_id)
        self.assertIsNone(self.web._current_project.get())
        self.assertIsNone(self.web._workspace_owner)
        self.assertEqual(list(Path(self.paths['UPLOAD_FOLDER']).iterdir()), [])
        manifest = self.client.get(f'/api/projects/{self.project_id}/data/segments_manifest.json').json
        self.assertNotIn(str(self.original), json.dumps(manifest))
        response = self.client.get(manifest['source']['url'], headers={'Range': 'bytes=2-5'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b'2345')
        response.close()
        self.assertEqual(self.call().status_code, 409)

    def test_relink_requires_same_content_and_does_not_run_pipeline(self):
        self.import_now()
        original_id = json.loads(Path(self.paths['MANIFEST_DIR'], MARKER).read_text())['id']
        moved = self.original.rename(self.root / 'moved.mp4')
        wrong = self.root / 'wrong.mp4'
        wrong.write_bytes(b'xxxxxxxxxx')
        with patch.object(self.web, 'process_video_async', Mock()) as pipeline:
            self.assertEqual(self.call(relink=True, path=str(wrong)).status_code, 400)
            response = self.call(relink=True, path=str(moved))
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['source']['id'], original_id)
            pipeline.assert_not_called()
        served = self.client.get(response.json['source']['url'])
        self.assertEqual(served.data, b'0123456789')
        served.close()

    def test_validation_and_busy_preserve_existing_workspace(self):
        for changes in (dict(project_id='../secret'), dict(path='relative.mp4'), dict(path=str(self.root / 'bad.txt')), dict(extra=True)):
            self.assertEqual(self.call(**changes).status_code, 400)
        self.assertEqual(self.call(project_id='a' * 32).status_code, 404)
        owner = self.web.acquire_workspace()
        try:
            self.assertEqual(self.call().status_code, 409)
        finally:
            self.web.release_workspace(owner)
        self.assertFalse(Path(self.paths['MANIFEST_DIR'], MARKER).exists())
        self.assertIsNone(self.web._current_project.get())

    def test_failed_partial_import_can_retry_but_completed_original_cannot(self):
        folder = Path(self.paths['MANIFEST_DIR'])
        (folder / 'segments_manifest.json').write_text('{"segments": []}')
        (folder / 'import-status.json').write_text(json.dumps({
            'server_instance': self.web._server_instance, 'result': {'status': 'error'},
        }))
        self.import_now()
        # Even a stale error record cannot authorize replacing a completed source.
        (folder / 'import-status.json').write_text(json.dumps({
            'server_instance': self.web._server_instance, 'result': {'status': 'error'},
        }))
        self.assertEqual(self.call().status_code, 409)

    def test_worker_start_failure_releases_admission_and_pending_reference(self):
        with patch('desktop_sources.threading.Thread', side_effect=RuntimeError('cannot start')):
            self.assertEqual(self.call().status_code, 500)
        self.assertIsNone(self.web._workspace_owner)
        self.assertFalse(Path(self.paths['MANIFEST_DIR'], MARKER).exists())
        self.assertFalse(Path(self.paths['MANIFEST_DIR'], EXTERNAL_PREPARATION).exists())
        persisted = json.loads(Path(self.paths['MANIFEST_DIR'], 'import-status.json').read_text())
        self.assertEqual(persisted['result']['status'], 'error')

    def test_failed_import_discards_preparation_and_never_exposes_source(self):
        class InlineThread:
            def __init__(thread, target, args):
                thread.run = lambda: target(*args)
            def start(thread):
                thread.run()
        with patch('desktop_sources.threading.Thread', InlineThread), patch.object(self.web, 'process_video_async', side_effect=ValueError('bad video')):
            result = self.call()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.web.processing_status[result.json['task_id']]['status'], 'error')
        self.assertFalse(Path(self.paths['MANIFEST_DIR'], MARKER).exists())
        self.assertFalse(Path(self.paths['MANIFEST_DIR'], EXTERNAL_PREPARATION).exists())
        self.assertIsNone(self.web._workspace_owner)


if __name__ == '__main__':
    unittest.main()
