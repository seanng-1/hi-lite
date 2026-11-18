# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subtitle_serializer import serialize_subtitles, NoCaptions
from transcript_store import publish_transcript, MARKER

ROOT = Path(__file__).resolve().parents[1]


def cue(start=0, end=1, text='Hello'):
    return dict(start=start, end=end, text=text)


class Serializer(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(serialize_subtitles([cue()], 'srt'), '1\n00:00:00,000 --> 00:00:01,000\nHello\n\n')
        self.assertEqual(serialize_subtitles([cue()], 'vtt'), 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n\n')

    def test_rounding(self):
        for seconds, expected in [(1.123, '00:00:01.123'), (1.9995, '00:00:02.000'),
                                  (59.9995, '00:01:00.000'), (3599.9995, '01:00:00.000'),
                                  (360001.001, '100:00:01.001')]:
            for fmt in ('srt', 'vtt'):
                self.assertIn(expected.replace('.', ',' if fmt == 'srt' else '.'),
                              serialize_subtitles([cue(seconds, seconds+1)], fmt))

    def test_invalid(self):
        for start, end in [(-1, 1), (0, -1), (1, 1), (2, 1), (float('nan'), 1),
                           (0, float('nan')), (float('inf'), 2), (0, float('inf')),
                           (0, .0004), (True, 2), ('0', 2)]:
            for fmt in ('srt', 'vtt'):
                with self.subTest(start=start, end=end, fmt=fmt), self.assertRaises(ValueError):
                    serialize_subtitles([cue(start, end)], fmt)

    def test_order_overlap_text(self):
        cues = [cue(2, 4, 'later'), cue(0, 3, 'é 日本 & "quotes" <tag>\r\n\r\nsecond'), cue(0, 1, 'equal')]
        for fmt in ('srt', 'vtt'):
            result = serialize_subtitles(cues, fmt)
            self.assertLess(result.index('second'), result.index('equal'))
            self.assertLess(result.index('equal'), result.index('later'))
            self.assertIn('00:00:03' + (',' if fmt == 'srt' else '.') + '000', result)
            self.assertIn('é 日本', result)
            self.assertNotIn('\r', result)
            self.assertIn('&amp; "quotes" &lt;tag&gt;\nsecond' if fmt == 'vtt' else '& "quotes" <tag>\nsecond', result)
        self.assertIn('\n\n3\n', serialize_subtitles(cues, 'srt'))

    def test_empty(self):
        for fmt in ('srt', 'vtt'):
            for cues in ([], [cue(text=' \r\n ')]):
                with self.assertRaises(NoCaptions):
                    serialize_subtitles(cues, fmt)


class Downloads(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.load_backend()

    def load_backend(self):
        spec = importlib.util.spec_from_file_location('subtitle_backend', ROOT / 'web_interface.py')
        self.backend = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, HI_LITE_DATA_DIR=self.temp.name):
            spec.loader.exec_module(self.backend)
        self.client = self.backend.app.test_client()
        self.folder = Path(self.backend.MANIFEST_DIR)

    def publish(self, text):
        publish_transcript(self.folder, {'segments': [cue(text=text)]})

    def download(self, fmt='srt'):
        return self.client.get('/api/subtitles/' + fmt)

    def admit(self):
        self.jobs = []
        class HeldThread:
            def __init__(thread, target, args):
                self.jobs.append(lambda: target(*args))
            def start(thread):
                pass
        with patch.object(self.backend.threading, 'Thread', HeldThread):
            result = self.client.post('/api/upload', data={'video': (io.BytesIO(b'video'), 'source.mp4')})
        self.assertEqual(result.status_code, 200)

    def test_replacement_failure_restart_retry(self):
        self.publish('recording A')
        self.assertIn(b'recording A', self.download().data)
        self.admit()
        self.assertEqual(self.download().status_code, 409)
        self.assertFalse((self.folder / MARKER).exists())
        self.assertIn('recording A', (self.folder / 'transcription.json').read_text())
        # A terminated parent has no in-memory ownership after restart.
        self.load_backend()
        self.assertEqual(self.download().status_code, 404)
        with patch.object(self.backend, 'process_video_async', side_effect=RuntimeError('before publication')):
            self.admit()
            self.jobs[0]()
        self.assertEqual(self.download().status_code, 404)
        self.load_backend()
        self.assertEqual(self.download().status_code, 404)
        with patch.object(self.backend, 'process_video_async', side_effect=lambda *args: self.publish('recording B')):
            self.admit()
            self.jobs[0]()
        self.assertIn(b'recording B', self.download().data)
        self.assertNotIn(b'recording A', self.download().data)

    def test_success_replacement(self):
        for name in ('A', 'B'):
            with patch.object(self.backend, 'process_video_async', side_effect=lambda *args: self.publish(name)):
                self.admit()
                self.jobs[0]()
            self.assertIn(('\n'+name+'\n').encode(), self.download().data)

    def test_api_and_independence(self):
        self.assertEqual(self.download().status_code, 404)
        self.assertEqual(self.download('txt').status_code, 400)
        self.publish('日本 & text')
        for fmt, mime in [('srt', 'application/x-subrip'), ('vtt', 'text/vtt')]:
            before = self.download(fmt)
            self.assertEqual(before.status_code, 200)
            self.assertEqual(before.content_type, mime + '; charset=utf-8')
            self.assertIn(f'original-recording.{fmt}', before.headers['Content-Disposition'])
            self.assertEqual(before.headers['Cache-Control'], 'no-store')
            for filename in ('segments_manifest.json', 'scored_segments.json', 'selection.json'):
                (self.folder / filename).write_text('{}')
            self.assertEqual(before.data, self.client.get(f'/api/subtitles/{fmt}?threshold=10&selected_ids=99').data)
        self.assertTrue(self.client.get('/api/subtitles').json['available'])
        publish_transcript(self.folder, {'segments': []})
        self.assertEqual(self.download().json['message'], 'No captions available')
        publish_transcript(self.folder, {'segments': [cue(-1, 2)]})
        self.assertEqual(self.download().status_code, 422)
        (self.folder / 'transcription.json').write_text('{}')
        self.assertEqual(self.download().status_code, 422)

    def test_export_holds_admission_for_snapshot(self):
        self.publish('A')
        original = self.backend.serialize_subtitles
        def serialize(*args):
            rejected = self.client.post('/api/upload', data={'video': (io.BytesIO(b'B'), 'b.mp4')})
            self.assertEqual(rejected.status_code, 409)
            return original(*args)
        with patch.object(self.backend, 'serialize_subtitles', side_effect=serialize):
            self.assertIn(b'\nA\n', self.download().data)
        self.assertIsNone(self.backend._workspace_owner)

    def test_real_pipeline_publication_and_prepublication_failure(self):
        import main as pipeline
        source = Path(self.temp.name, 'source.mp4')
        source.write_bytes(b'fixture')
        with patch.object(pipeline, 'extract_audio'), \
             patch.object(pipeline, 'transcribe_audio', return_value={'segments': [cue(text='B')]}), \
             patch.object(pipeline, 'export_segments_to_excel'), \
             patch.object(pipeline, 'build_segment_metadata', return_value=[]):
            self.publish('A')
            pipeline.main(str(source), str(self.folder))
            # Valid publication survives a later clip/manifest failure.
            self.assertIn(b'\nB\n', self.download().data)
            with patch.object(pipeline, 'transcribe_audio', side_effect=RuntimeError('transcription failed')):
                with self.assertRaises(RuntimeError):
                    pipeline.main(str(source), str(self.folder))
            self.load_backend()
            self.assertEqual(self.download().status_code, 404)

    def test_interrupted_publication(self):
        self.publish('A')
        with patch('transcript_store._atomic_write', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.publish('B')
        self.load_backend()
        self.assertEqual(self.download().status_code, 404)


if __name__ == '__main__':
    unittest.main()
