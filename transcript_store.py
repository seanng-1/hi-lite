# SPDX-License-Identifier: GPL-3.0-or-later
"""Tiny persisted subtitle validity marker, scoped to the single workspace.

Callers hold existing workspace admission. Publication in the pipeline child
happens while its parent owns admission. Missing markers fail closed, including
legacy transcripts and imports interrupted before publication.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

MARKER = 'subtitle-transcript.sha256'


def invalidate_transcript(folder):
    Path(folder, MARKER).unlink(missing_ok=True)


def _atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish_transcript(folder, transcript):
    invalidate_transcript(folder)
    data = json.dumps(transcript, ensure_ascii=False, indent=4).encode('utf-8')
    _atomic_write(Path(folder, 'transcription.json'), data)
    _atomic_write(Path(folder, MARKER), hashlib.sha256(data).hexdigest().encode('ascii'))


def read_current_transcript(folder):
    try:
        marker = Path(folder, MARKER).read_bytes()
        data = Path(folder, 'transcription.json').read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError('No current recording transcript. Import a recording to enable subtitles.') from None
    if marker != hashlib.sha256(data).hexdigest().encode('ascii'):
        raise ValueError('Current transcript is invalid. Import the recording again.')
    try:
        transcript = json.loads(data)
        return transcript['segments']
    except (ValueError, KeyError, TypeError):
        raise ValueError('Current transcript is invalid. Import the recording again.') from None
