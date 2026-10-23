# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from transcript_store import publish_transcript
from source_preview import publish_source

ROOT = Path(__file__).resolve().parents[1]


class EditorAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        spec = importlib.util.spec_from_file_location('editor_test_backend', ROOT / 'web_interface.py')
        self.backend = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'HI_LITE_DATA_DIR': self.temp.name}):
            spec.loader.exec_module(self.backend)
        self.client = self.backend.app.test_client()
        self.project = self.client.post('/api/projects', json={'name': 'Editor'}).json
        self.prefix = '/api/projects/' + self.project['id']
        self.paths = self.backend.projects.paths(self.project['id'])
        self.segments = [
            {'id': 0, 'transcript_id': 10, 'start': 0, 'end': 1, 'duration': 1, 'text': 'Um hello', 'filename': '0.mp4', 'rendered': False},
            {'id': 1, 'transcript_id': 20, 'start': 1, 'end': 3, 'duration': 2, 'text': 'Important story', 'filename': '1.mp4', 'rendered': False}]
        self.transcript = [
            {'id': 10, 'start': 0, 'end': .8, 'text': 'Um hello', 'words': [
                {'start': 0, 'end': .2, 'word': 'Um'}, {'start': .3, 'end': .8, 'word': 'hello'}]},
            {'id': 20, 'start': 2, 'end': 3, 'text': 'Important story'}]
        Path(self.paths['SEGMENTS_JSON']).write_text(json.dumps({'segments': self.segments}))
        publish_transcript(self.paths['MANIFEST_DIR'], {'segments': self.transcript})
        self.source = Path(self.paths['UPLOAD_FOLDER']) / 'source.mp4'
        self.source.write_bytes(b'fixture video')
        publish_source(self.paths['MANIFEST_DIR'], self.paths['UPLOAD_FOLDER'], self.source, 'b' * 32)

    def post(self, route, data):
        return self.client.post(self.prefix + '/api/' + route, json=data)

    def document(self):
        result = self.client.get(self.prefix + '/api/editor')
        self.assertEqual(result.status_code, 200, result.json)
        return result.json['document']

    def wait(self, task):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = self.client.get(self.prefix + '/api/processing-status/' + task)
            if response.json['status'] in ('complete', 'error', 'cancelled'):
                return response.json
            time.sleep(.02)
        self.fail('Job did not finish')

    def test_edit_preview_does_not_mutate_transcript_and_saved_state_reopens(self):
        document = self.document()
        original = Path(self.paths['MANIFEST_DIR'], 'transcription.json').read_bytes()
        response = self.post('edit-preview', {'document': document, 'action': {'type': 'split', 'range_id': 'range-1', 'at': .5}})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(response.json['document']['ranges']), 3)
        saved = self.client.put(self.prefix, json={'revision': 0, 'state': {'selected_ids': [0], 'edit_document': response.json['document']}})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.document(), response.json['document'])
        self.assertEqual(Path(self.paths['MANIFEST_DIR'], 'transcription.json').read_bytes(), original)

    def test_cleanup_and_candidates_are_review_only(self):
        before = self.document()
        response = self.post('cleanup', {'min_pause': 1, 'keep_pause': .2})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual({row['type'] for row in response.json['suggestions']}, {'filler', 'silence'})
        response = self.post('candidates', {'desired_count': 2, 'target_duration': 1, 'clip_type': 'story'})
        self.assertEqual(response.status_code, 200, response.json)
        result = self.wait(response.json['task_id'])
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['origin'], 'local_heuristic')
        self.assertTrue(result['candidates'])
        self.assertEqual(self.document(), before)

    def test_reordered_edited_subtitles_and_validation(self):
        document = self.document()
        document['ranges'].reverse()
        captions = [{'start': 0, 'end': 1, 'text': 'Changed first'}, {'start': 2, 'end': 3, 'text': 'Changed second'}]
        response = self.post('edited-subtitles', {'document': document, 'captions': captions, 'format': 'srt'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertLess(response.data.index(b'Changed second'), response.data.index(b'Changed first'))
        self.assertIn(b'00:00:01,000 --> 00:00:02,000', response.data)
        for route in ('render', 'edit-preview', 'cleanup', 'candidates', 'edited-subtitles'):
            self.assertEqual(self.post(route, []).status_code, 400)
        bad = self.document(); bad['ranges'][0]['end'] = 50
        self.assertEqual(self.post('render', {'document': bad}).status_code, 400)

    def test_queued_cancel_does_not_run_worker_and_is_project_scoped(self):
        held = []
        class HeldThread:
            def __init__(self, target, args, **kwargs): self.target, self.args = target, args; held.append(self)
            def start(self): pass
        with patch('editor_api.threading.Thread', HeldThread):
            response = self.post('candidates', {})
        task = response.json['task_id']
        other = self.client.post('/api/projects', json={'name': 'Other'}).json['id']
        self.assertEqual(self.client.post('/api/projects/' + other + '/api/cancel/' + task).status_code, 404)
        self.assertEqual(self.post('cancel/' + task, {}).status_code, 200)
        held[0].target(*held[0].args)
        self.assertEqual(self.wait(task)['status'], 'cancelled')
        self.assertIsNone(self.backend._workspace_owner)
        self.assertEqual(self.post('cancel/' + task, {}).status_code, 409)

    def test_individual_render_failure_cleans_entire_batch(self):
        count = 0
        def export(source, ranges, output, **kwargs):
            nonlocal count
            count += 1
            Path(output).write_bytes(b'partial fixture')
            if count == 2:
                raise RuntimeError('fixture renderer failure')
            return {'duration': 1}
        with patch('editor_api.export_source_ranges', side_effect=export):
            response = self.post('render', {'document': self.document(), 'individual': True, 'captions': []})
            self.assertEqual(response.status_code, 200, response.json)
            result = self.wait(response.json['task_id'])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(list(Path(self.paths['HIGHLIGHTS_DIR']).glob('*.mp4')), [])
        self.assertIsNone(self.backend._workspace_owner)
        self.assertEqual(self.post('candidates', {'instructions': 123}).status_code, 400)
        self.assertIsNone(self.backend._workspace_owner)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg is required')
    def test_real_project_render_individual_and_legacy_selection_export(self):
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=blue:size=160x90:rate=24',
                        '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '3', '-c:v', 'libx264', '-threads', '2',
                        '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(self.source)], check=True)
        publish_source(self.paths['MANIFEST_DIR'], self.paths['UPLOAD_FOLDER'], self.source, 'b' * 32)
        response = self.post('render', {'document': self.document(), 'individual': True,
                                       'settings': {'aspect_ratio': '9:16', 'burn_captions': True}})
        self.assertEqual(response.status_code, 200, response.json)
        result = self.wait(response.json['task_id'])
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(len(result['outputs']), 2)
        for output in result['outputs']:
            self.assertTrue(output['output_file'].startswith(self.prefix + '/final/'))
            with self.client.get(output['output_file']) as download:
                self.assertEqual(download.status_code, 200)
        selection = self.post('export', {'selected_ids': [1]})
        self.assertEqual(selection.status_code, 200, selection.json)
        self.assertEqual(selection.json['selected_count'], 1)
        with self.client.get(selection.json['output_file']) as download:
            self.assertEqual(download.status_code, 200)
        self.assertIsNone(self.backend._workspace_owner)


if __name__ == '__main__': unittest.main()
