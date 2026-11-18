# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated real-upload/FFmpeg smoke server for capture-workspace.mjs.

Explicit source + transcript inputs are test assets, never a production source
resolver. Only speech recognition is stubbed; import, extraction, publication,
HTTP source serving, native playback and exports use the actual application.
"""
import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--transcript', required=True)
    args = parser.parse_args()
    source = Path(args.source).resolve(strict=True)
    transcript = Path(args.transcript).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='hi-lite-source-smoke-') as directory:
        os.environ['HI_LITE_DATA_DIR'] = directory
        os.environ['PYTHONPATH'] = directory + os.pathsep + str(ROOT)
        # Stub only recognition in the real pipeline child, without providers,
        # model downloads, or modifications to the supplied test recording.
        Path(directory, 'sitecustomize.py').write_text(
            'import sys, types, json\n'
            'module = types.ModuleType("transcription_client")\n'
            f'module.transcribe_audio = lambda **kwargs: json.loads({transcript.read_text()!r})\n'
            'sys.modules["transcription_client"] = module\n')
        import web_interface as web
        client = web.app.test_client()
        with source.open('rb') as stream:
            response = client.post('/api/upload', data={'video': (stream, source.name)})
        assert response.status_code == 200, response.json
        task = response.json['task_id']
        deadline = time.monotonic() + 240
        while web._workspace_owner is not None and time.monotonic() < deadline:
            time.sleep(.1)
        assert web._workspace_owner is None, 'Real import timed out'
        result = client.get('/api/processing-status/' + task).json
        assert result['status'] == 'complete', result
        manifest = client.get('/data/segments_manifest.json').json
        source_url = manifest['source']['url']
        response = client.get(source_url, headers={'Range': 'bytes=0-1023'})
        assert response.status_code == 206 and len(response.data) == 1024
        response.close()
        duration = float(subprocess.check_output([web.FFPROBE, '-v', 'error', '-show_entries',
            'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(source)], text=True))
        from werkzeug.serving import make_server
        server = make_server('127.0.0.1', 0, web.app, threaded=True)
        print('SOURCE_SMOKE_READY ' + json.dumps(dict(url=f'http://127.0.0.1:{server.server_port}/',
              duration=duration, source_url=source_url, segments=len(manifest['segments']), pid=os.getpid())), flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
