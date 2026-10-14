# SPDX-License-Identifier: GPL-3.0-or-later
"""Extraction -> manifest -> validated cache -> real automatic export regression."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from highlight_analyzer import save_segment_manifest
from model_based_processing import scoring
import video_processor

ROOT = Path(__file__).resolve().parents[1]
IDS = ['d9543b316f574230b92dcb5595529a39', 'aae4b0f11997452eba2c8d931d90164c',
       '3409b327d7b2425fbc095543968502cf']


class ClipIdentityExport(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory(prefix='hi-lite-clip-identity-')
        self.addCleanup(self.data.cleanup)
        spec = importlib.util.spec_from_file_location('clip_identity_web', ROOT / 'web_interface.py')
        self.web = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'HI_LITE_DATA_DIR': self.data.name}):
            spec.loader.exec_module(self.web)
        self.client = self.web.app.test_client()
        self.segments = [{'id': sid, 'start': i * 3 + 1, 'end': i * 3 + 2,
                          'text': 'An amazing goal!'} for i, sid in enumerate(IDS)]
        self.transcript = {'segments': self.segments}
        Path(self.web.MANIFEST_DIR, 'transcription.json').write_text(json.dumps(self.transcript))
        backend = scoring.APIBackend({'base_url': 'https://fixture.invalid/v1', 'api_key': '',
                                      'model': 'fixture'})
        resolver = patch.object(scoring, 'get_backend', return_value=backend)
        resolver.start()
        self.addCleanup(resolver.stop)
        self.result = scoring.enrich_results([
            {'segment_id': sid, 'score': score} for sid, score in zip(IDS, [9, 6, 3])], self.segments)
        Path(self.web.MANIFEST_DIR, 'sorted_segments.json').write_text(json.dumps({
            'highlights': self.result, 'cache': {'identity': scoring.score_cache_identity(
                self.segments, 'great plays', backend), 'complete': True}}))
        self.paths = []
        with patch.object(video_processor.subprocess, 'run', side_effect=self.media):
            self.clips = video_processor.extract_all_segments('source.mp4', self.transcript,
                                                             self.web.MANIFEST_DIR)
        save_segment_manifest(self.clips, self.web.MANIFEST_DIR)

    def media(self, cmd, **kwargs):
        if cmd[0] == self.web.FFPROBE:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'format': {'duration': 12}})
                                               if '-show_format' in cmd else '2.25', '')
        if '-f' in cmd and cmd[cmd.index('-f') + 1] == 'concat':
            self.paths = Path(cmd[cmd.index('-i') + 1]).read_text().splitlines()
        Path(cmd[-1]).write_bytes(b'fixture media')
        return subprocess.CompletedProcess(cmd, 0, '', '')

    def test_extraction_manifest_cached_scores_thresholds_and_export(self):
        self.assertEqual([c['id'] for c in self.clips], [0, 1, 2])
        self.assertEqual([c['filename'] for c in self.clips],
                         ['segment_000.mp4', 'segment_001.mp4', 'segment_002.mp4'])
        # Assert exported paths first: pre-fix fails because qualifying scores join no clips.
        for threshold, count in [(7, 1), (5, 2), (2, 3)]:
            with self.subTest(threshold=threshold), \
                 patch.object(self.web.subprocess, 'run', side_effect=self.media), \
                 patch.object(self.web, 'run_scoring_subprocess') as provider:
                self.web.auto_generate_async('cached', threshold, True, 'great plays', False)
                result = self.web.processing_status['cached']
                self.assertEqual(result['status'], 'complete', result)
                self.assertEqual(result['highlights_used'], count)
                self.assertEqual([s['segment_id'] for s in result['segments_included']], IDS[:count])
                self.assertEqual(self.paths, ["file '" + str(Path(self.web.SEGMENTS_DIR,
                    c['filename'])) + "'" for c in self.clips[:count]])
                metadata = json.loads(max(Path(self.web.SEGMENTS_DIR).glob(
                    'auto_highlights_metadata_*.json')).read_text())
                self.assertEqual([s['segment_id'] for s in metadata['segments']], IDS[:count])
                self.assertEqual(self.client.get(result['output_file']).data, b'fixture media')
                preview = self.client.post('/api/preview-auto-export', json={
                    'instructions': 'great plays', 'min_score': threshold})
                self.assertEqual(preview.status_code, 200, preview.json)
                self.assertEqual(preview.json['count'], count)
                provider.assert_not_called()
        self.assertEqual([c['transcript_id'] for c in self.clips], IDS)
        manifest = self.client.get('/data/segments_manifest.json').json
        self.assertEqual([c['transcript_id'] for c in manifest['segments']], IDS)
        self.assertEqual([c['transcript_id'] for c in manifest['keyword_highlights']], IDS)

    def test_unknown_clip_and_duplicate_identity_do_not_guess(self):
        clips = [self.clips[1], self.clips[2], {**self.clips[1], 'id': 99}]
        # All times agree with A, but A has no clip and B has ambiguous identity.
        clips = [{**c, 'start': self.clips[0]['start']} for c in clips]
        save_segment_manifest(clips, self.web.MANIFEST_DIR)
        with patch.object(self.web.subprocess, 'run', side_effect=self.media):
            result = self.web.build_highlight_reel(2, True, 'great plays')
            preview = self.client.post('/api/preview-auto-export', json={
                'instructions': 'great plays', 'min_score': 2})
        self.assertEqual([s['segment_id'] for s in result['segments_included']], [IDS[2]])
        self.assertEqual(preview.status_code, 200, preview.json)
        self.assertEqual(preview.json['count'], 1)

    def test_numeric_clip_id_cannot_substitute_for_semantic_identity(self):
        clip = {**self.clips[0], 'id': IDS[0], 'transcript_id': IDS[1]}
        self.assertIsNone(self.web.find_manifest_segment(self.result[0], [clip]))
        save_segment_manifest([clip], self.web.MANIFEST_DIR)
        with patch.object(self.web.subprocess, 'run') as media:
            with self.assertRaisesRegex(ValueError, 'No valid video clips'):
                self.web.build_highlight_reel(7, True, 'great plays')
            preview = self.client.post('/api/preview-auto-export', json={
                'instructions': 'great plays', 'min_score': 7})
        self.assertEqual(preview.status_code, 400)
        self.assertIn('No valid video clips', preview.json['message'])
        media.assert_not_called()

    def test_only_ambiguous_matches_fail_without_exporting_arbitrarily(self):
        save_segment_manifest([self.clips[0], {**self.clips[0], 'id': 99}],
                              self.web.MANIFEST_DIR)
        with patch.object(self.web.subprocess, 'run') as media:
            with self.assertRaisesRegex(ValueError, 'No valid video clips'):
                self.web.build_highlight_reel(7, True, 'great plays')
            preview = self.client.post('/api/preview-auto-export', json={
                'instructions': 'great plays', 'min_score': 7})
        self.assertEqual(preview.status_code, 400)
        self.assertIn('No valid video clips', preview.json['message'])
        media.assert_not_called()

    def test_chronological_and_score_order_remain_distinct(self):
        self.result = scoring.enrich_results([
            {'segment_id': sid, 'score': score} for sid, score in zip(IDS, [3, 6, 9])], self.segments)
        with patch.object(self.web.subprocess, 'run', side_effect=self.media):
            chronological = self.web.build_highlight_reel(2, True, 'great plays', self.result)
            ranked = self.web.build_highlight_reel(2, False, 'great plays', self.result)
        self.assertEqual([s['segment_id'] for s in chronological['segments_included']], IDS)
        self.assertEqual([s['segment_id'] for s in ranked['segments_included']], IDS[::-1])

    def test_old_manifest_requires_reimport_but_manual_numeric_export_works(self):
        old = [{k: v for k, v in c.items() if k != 'transcript_id'} for c in self.clips]
        save_segment_manifest(old, self.web.MANIFEST_DIR)
        original = Path(self.web.SEGMENTS_JSON).read_bytes()
        with patch.object(self.web.subprocess, 'run') as media:
            with self.assertRaisesRegex(ValueError, 'Import the recording again'):
                self.web.build_highlight_reel(7, True, 'great plays')
            preview = self.client.post('/api/preview-auto-export', json={
                'instructions': 'great plays', 'min_score': 7})
            self.assertEqual(preview.status_code, 400)
            self.assertIn('Import the recording again', preview.json['message'])
            media.assert_not_called()
        self.assertEqual(Path(self.web.SEGMENTS_JSON).read_bytes(), original)
        for clips in (old, self.clips):
            save_segment_manifest(clips, self.web.MANIFEST_DIR)
            final = Mock()
            final.write_videofile.side_effect = lambda path, **kw: Path(path).write_bytes(b'manual reel')
            with patch.object(self.web, 'VideoFileClip'), \
                 patch.object(self.web, 'concatenate_videoclips', return_value=final) as concat:
                response = self.client.post('/api/export', json={'selected_ids': [0, 2]})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['selected_count'], 2)
            self.assertEqual(len(concat.call_args.args[0]), 2)

    def test_moviepy_fallback_also_preserves_identity(self):
        video = Mock(duration=12)
        video.__enter__ = Mock(return_value=video)
        video.__exit__ = Mock(return_value=False)
        video.subclipped.return_value.write_videofile.side_effect = (
            lambda path, **kw: Path(path).write_bytes(b'fallback clip'))
        with patch.object(video_processor, 'extract_all_segments_alternative', side_effect=FileNotFoundError), \
             patch.object(video_processor, 'VideoFileClip', return_value=video):
            clips = video_processor.extract_all_segments('source.mp4', self.transcript, self.web.MANIFEST_DIR)
        self.assertEqual([c['transcript_id'] for c in clips], IDS)
        self.assertEqual([c['id'] for c in clips], [0, 1, 2])


if __name__ == '__main__':
    unittest.main()
