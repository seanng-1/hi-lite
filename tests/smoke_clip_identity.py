# SPDX-License-Identifier: GPL-3.0-or-later
"""Real extraction, cached automatic export and full reel decode; no live provider.

Run from the repository root: python tests/smoke_clip_identity.py
Speech recognition uses a deterministic transcript with nonnumeric IDs.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
IDS = ['d9543b316f574230b92dcb5595529a39', 'aae4b0f11997452eba2c8d931d90164c',
       '3409b327d7b2425fbc095543968502cf']


def main():
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise RuntimeError('This smoke check requires ffmpeg and ffprobe on PATH.')
    with tempfile.TemporaryDirectory(prefix='hi-lite-identity-smoke-') as directory:
        settings = {'HI_LITE_DATA_DIR': directory, 'HI_LITE_LLM_BACKEND': 'api',
                    'HI_LITE_LLM_BASE_URL': 'https://fixture.invalid/v1',
                    'HI_LITE_LLM_MODEL': 'fixture', 'HI_LITE_LLM_API_KEY': '',
                    'HI_LITE_PRE_BUFFER': '0.5', 'HI_LITE_POST_BUFFER': '0.75',
                    'FFMPEG_PATH': ffmpeg, 'FFPROBE_PATH': ffprobe}
        with patch.dict(os.environ, settings), redirect_stdout(io.StringIO()):
            import main as pipeline
            from model_based_processing import scoring
            import web_interface as web

            source = Path(directory, 'source.mp4')
            subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=s=160x90:r=10', '-f', 'lavfi', '-i',
                            'sine=frequency=880:sample_rate=48000', '-t', '12',
                            '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
                            '-pix_fmt', 'yuv420p', '-y', str(source)],
                           check=True, capture_output=True, timeout=60)
            segments = [{'id': sid, 'start': i * 3 + 1, 'end': i * 3 + 2,
                         'text': 'An amazing goal!'} for i, sid in enumerate(IDS)]
            with patch.object(pipeline, 'transcribe_audio', return_value={'segments': segments}):
                pipeline.main(str(source), web.MANIFEST_DIR)
            client = web.app.test_client()
            clips = client.get('/data/segments_manifest.json').json['segments']
            assert [c['id'] for c in clips] == [0, 1, 2], clips
            assert [c['transcript_id'] for c in clips] == IDS, clips
            assert [c['filename'] for c in clips] == [f'segment_{i:03d}.mp4' for i in range(3)]
            highlights = scoring.enrich_results([
                {'segment_id': sid, 'score': score} for sid, score in zip(IDS, [9, 6, 3])], segments)
            backend = scoring.get_backend()
            cache = Path(web.MANIFEST_DIR, 'sorted_segments.json')
            cache.write_text(json.dumps({'highlights': highlights, 'cache': {
                'identity': scoring.score_cache_identity(segments, 'great plays', backend),
                'complete': True}}))
            cache_before = cache.read_bytes()

            def decode(url):
                reel = Path(web.HIGHLIGHTS_DIR, url.rsplit('/', 1)[-1])
                assert client.get(url).data == reel.read_bytes()
                subprocess.run([ffmpeg, '-v', 'error', '-xerror', '-i', str(reel),
                                '-map', '0', '-f', 'null', '-'],
                               check=True, capture_output=True, timeout=60)
                return float(subprocess.check_output([
                    ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1', str(reel)], text=True))

            summary = []
            with patch.object(web, 'run_scoring_subprocess',
                              side_effect=AssertionError('Valid cached scores must be reused.')) as provider:
                for threshold, count in [(7, 1), (5, 2), (2, 3)]:
                    qualifying = [h for h in highlights if h['score'] >= threshold]
                    matched = [web.find_manifest_segment(h, clips) for h in qualifying]
                    assert len(qualifying) == count and all(matched)
                    assert [c['filename'] for c in matched] == [c['filename'] for c in clips[:count]]
                    response = client.post('/api/auto-generate', json={
                        'instructions': 'great plays', 'min_score': threshold})
                    assert response.status_code == 200, response.json
                    deadline = time.monotonic() + 30
                    while web._workspace_owner is not None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    assert web._workspace_owner is None, 'generation did not release admission'
                    result = client.get('/api/processing-status/' + response.json['task_id']).json
                    assert result['status'] == 'complete', result
                    assert result['highlights_used'] == count, result
                    assert [s['segment_id'] for s in result['segments_included']] == IDS[:count]
                    metadata = json.loads(max(Path(web.SEGMENTS_DIR).glob(
                        'auto_highlights_metadata_*.json')).read_text())
                    assert [s['segment_id'] for s in metadata['segments']] == IDS[:count]
                    preview = client.post('/api/preview-auto-export', json={
                        'instructions': 'great plays', 'min_score': threshold})
                    assert preview.status_code == 200 and preview.json['count'] == count, preview.json
                    summary.append({'threshold': threshold, 'qualifying': len(qualifying),
                                    'matched': len(matched), 'exported': result['highlights_used'],
                                    'duration': decode(result['output_file']), 'decode': 'passed'})
                provider.assert_not_called()
            assert cache.read_bytes() == cache_before, 'threshold-only reuse changed the score cache'
            manual = client.post('/api/export', json={'selected_ids': [0, 2]})
            assert manual.status_code == 200 and manual.json['selected_count'] == 2, manual.json
            manual_duration = decode(manual.json['output_file'])
        print(json.dumps({'status': 'passed', 'dataset': 'deterministic three-segment transcript',
                          'provider_calls': 0, 'steps': summary,
                          'manual': {'clips': 2, 'duration': manual_duration, 'decode': 'passed'}}, indent=2))


if __name__ == '__main__':
    main()
