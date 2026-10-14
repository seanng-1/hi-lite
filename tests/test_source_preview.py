# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from source_preview import MARKER, publish_source
from transcript_store import publish_transcript

ROOT = Path(__file__).resolve().parents[1]


class SourcePreview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.load_backend()
        self.folder = Path(self.web.MANIFEST_DIR)
        self.uploads = Path(self.web.UPLOAD_FOLDER)
        self.seed('a' * 32, 'source.mp4', b'0123456789')

    def load_backend(self):
        spec = importlib.util.spec_from_file_location('source_backend', ROOT / 'web_interface.py')
        self.web = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, HI_LITE_DATA_DIR=self.temp.name):
            spec.loader.exec_module(self.web)
        self.client = self.web.app.test_client()

    def seed(self, identity, filename, content):
        self.source = self.uploads / filename
        self.source.write_bytes(content)
        publish_transcript(self.folder, {'segments': [{'start': 2, 'end': 3, 'text': 'speech'}]})
        (self.folder / 'segments_manifest.json').write_text(json.dumps({'segments': []}))
        publish_source(self.folder, self.uploads, self.source, identity)
        self.url = '/api/source/' + identity

    def upload(self, filename='replacement.mp4'):
        return self.client.post('/api/upload', data={'video': (io.BytesIO(b'replacement'), filename)})

    def assert_unavailable(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)
        self.assertIn('Reimport', response.json['message'])
        self.assertNotIn('source', self.client.get('/data/segments_manifest.json').json)

    def test_bound_source_manifest_range_head_and_restart(self):
        self.load_backend()
        manifest = self.client.get('/data/segments_manifest.json')
        self.assertEqual(manifest.json['source'], {'id': 'a' * 32, 'url': self.url})
        self.assertEqual(manifest.headers['Cache-Control'], 'no-store')
        response = self.client.get(self.url, headers={'Range': 'bytes=2-5'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b'2345')
        self.assertEqual(response.headers['Content-Range'], 'bytes 2-5/10')
        self.assertEqual(response.headers['Accept-Ranges'], 'bytes')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        response.close()
        response = self.client.get(self.url, headers={'Range': 'bytes=-3'})
        self.assertEqual(response.data, b'789')
        response.close()
        response = self.client.head(self.url)
        self.assertEqual(response.content_length, 10)
        response.close()
        self.assertEqual(self.client.get(self.url, headers={'Range': 'bytes=100-200'}).status_code, 416)
        self.assertEqual(self.client.get('/api/source/' + 'b' * 32).status_code, 404)

    def test_missing_modified_manifest_transcript_and_source_fail_closed(self):
        for name in ('segments_manifest.json', 'transcription.json', 'subtitle-transcript.sha256', 'source'):
            with self.subTest(name=name):
                self.seed('a' * 32, 'source.mp4', b'0123456789')
                path = self.source if name == 'source' else self.folder / name
                path.write_bytes(b'changed')
                self.assertEqual(self.client.get(self.url).status_code, 404)
                path.unlink()
                self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_legacy_corrupt_and_traversal_markers_never_guess(self):
        for content in ('null', '[]', '{broken', '{}', json.dumps({'id': 'a'*32, 'filename': '../secret.mp4'})):
            with self.subTest(content=content):
                (self.folder / MARKER).write_text(content)
                self.assert_unavailable()
        (self.folder / MARKER).unlink()
        self.assert_unavailable()
        for path in ('../source.mp4', '..%2Fsource.mp4', 'source.mp4', 'a'*32 + '?path=/etc/passwd'):
            self.assertEqual(self.client.get('/api/source/' + path).status_code, 404)

    def test_symlink_and_replaced_file_fail_closed(self):
        outside = Path(self.temp.name, 'outside.mp4')
        outside.write_bytes(b'0123456789')
        self.source.unlink()
        self.source.symlink_to(outside)
        self.assert_unavailable()
        self.source.unlink()
        self.source.write_bytes(b'0123456789')
        self.assert_unavailable()

    def test_rejected_upload_keeps_source(self):
        self.assertEqual(self.client.post('/api/upload', data={'video': (io.BytesIO(b'x'), 'bad.txt')}).status_code, 400)
        owner = self.web.acquire_workspace()
        try:
            self.assertEqual(self.upload().status_code, 409)
            response = self.client.get(self.url)
            self.assertEqual(response.data, b'0123456789')
            response.close()
        finally:
            self.web.release_workspace(owner)

    def test_replacement_failure_invalidates_before_save_and_survives_restart(self):
        def fail_save(*args, **kwargs):
            self.assert_unavailable()
            raise OSError('save failed')
        with patch('werkzeug.datastructures.FileStorage.save', side_effect=fail_save):
            self.assertEqual(self.upload().status_code, 500)
        self.load_backend()
        self.assert_unavailable()

    def test_successful_import_publishes_only_after_child_success(self):
        uploads = []
        def pipeline(command, **kwargs):
            uploads.append(command[2])
            self.assertEqual(self.client.get(self.url).status_code, 404)
            publish_transcript(self.folder, {'segments': []})
            (self.folder / 'segments_manifest.json').write_text('{"segments": []}')
            process = unittest.mock.Mock(stdout=io.StringIO('Processing Complete\n'))
            process.wait.return_value = 0
            process.poll.return_value = 0
            return process
        class InlineThread:
            def __init__(thread, target, args):
                thread.run = lambda: target(*args)
            def start(thread):
                thread.run()
        with patch.object(self.web.threading, 'Thread', InlineThread), patch.object(self.web.subprocess, 'Popen', side_effect=pipeline):
            first = self.upload('same.mp4')
            self.assertEqual(first.status_code, 200)
            current = self.client.get('/data/segments_manifest.json').json['source']
            self.assertEqual(current['id'], first.json['task_id'].removeprefix('task_'))
            response = self.client.get(current['url'])
            self.assertEqual(response.data, b'replacement')
            response.close()
            second = self.upload('same.mp4')
            self.assertNotEqual(first.json['filename'], second.json['filename'])
            self.assertNotEqual(uploads[0], uploads[1])
            self.assertEqual(self.client.get(current['url']).status_code, 404)
            self.assertEqual(self.client.get(self.url).status_code, 404)
            self.assertEqual(self.client.get('/api/processing-status/' + second.json['task_id']).json['status'], 'complete')

    def test_worker_start_failure_cannot_restore_previous_source(self):
        with patch.object(self.web.threading, 'Thread', side_effect=RuntimeError('start failed')):
            self.assertEqual(self.upload().status_code, 500)
        self.assert_unavailable()


if __name__ == '__main__':
    unittest.main()
