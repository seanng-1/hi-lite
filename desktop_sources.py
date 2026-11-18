# SPDX-License-Identifier: GPL-3.0-or-later
"""Desktop-only original-file linking. No browser endpoint accepts unauthenticated paths.

Electron main generates a fresh launch token, retains it outside the renderer,
opens a native picker, and sends the chosen file directly to these endpoints.
Standalone/browser servers have no token and cannot use either operation.
"""
import hmac
import os
import threading
from uuid import uuid4

from flask import jsonify, request

from source_preview import (SOURCE_LOCK, prepare_external_source, relink_external_source,
                            invalidate_source, discard_external_preparation)
from transcript_store import invalidate_transcript


def register_desktop_sources(app, backend):
    b = backend
    desktop_token = os.environ.get('HI_LITE_DESKTOP_TOKEN', '')

    def authorize():
        supplied = request.headers.get('X-Hi-Lite-Desktop-Token', '')
        # bytes comparison also rejects malformed/non-ASCII values without a 500.
        return bool(desktop_token) and hmac.compare_digest(supplied.encode('utf-8'), desktop_token.encode('utf-8'))

    def failure(error, status=400):
        return jsonify(status='error', message=str(error)), status

    def handle(link):
        if not authorize():
            return failure('Original-file linking is available only through the desktop file picker.', 403)
        owner = None
        context = None
        started = False
        task_id = None
        folder = None
        try:
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or set(data) != {'project_id', 'path'}:
                raise ValueError('A project and native file selection are required.')
            project_id = data['project_id']
            b['projects'].get(project_id)
            if not isinstance(data['path'], str) or not b['allowed_file'](data['path']):
                raise ValueError('Choose a supported original video file.')
            context = b['_current_project'].set(project_id)
            owner = b['acquire_workspace']()
            if owner is None:
                return b['workspace_busy']()
            paths = b['workspace_paths']()
            folder = paths['MANIFEST_DIR']
            if not link:
                with SOURCE_LOCK:
                    source = relink_external_source(folder, data['path'])
                source['url'] = f'/api/projects/{project_id}' + source['url']
                return jsonify(status='ok', project_id=project_id, source=source)
            if not b['can_import_recording']():
                return failure('This project already contains a recording. Create a new project to link another.', 409)
            task_id = 'task_' + uuid4().hex
            with SOURCE_LOCK:
                invalidate_source(folder)
                invalidate_transcript(folder)
                source = prepare_external_source(folder, data['path'], task_id.removeprefix('task_'))
            b['_task_projects'][task_id] = project_id
            b['_cancel_events'][task_id] = threading.Event()
            b['processing_status'][task_id] = dict(status='processing', stage='starting', progress=0,
                                                  message='Processing linked original recording...')
            b['persist_job'](task_id)

            def worker(video_path, output_folder, task):
                try:
                    b['process_video_async'](video_path, output_folder, task)
                finally:
                    with SOURCE_LOCK:
                        discard_external_preparation(output_folder)

            thread = threading.Thread(target=b['run_workspace_job'],
                                      args=(owner, task_id, worker, (str(source), folder, task_id)))
            thread.daemon = True
            thread.start()
            started = True
            return jsonify(status='ok', project_id=project_id, task_id=task_id)
        except FileNotFoundError:
            return failure('Project or selected original recording was not found.', 404)
        except OSError:
            return failure('Could not read the selected original recording. Check that it is available and readable.')
        except (ValueError, KeyError, TypeError) as error:
            return failure(error)
        except Exception:
            if task_id:
                b['processing_status'][task_id] = dict(status='error', message='Could not start the linked recording import.')
                b['persist_job'](task_id)
            app.logger.exception('Desktop source operation failed')
            return failure('Could not start the linked recording import.', 500)
        finally:
            if owner is not None and not started:
                if link and task_id and folder:
                    with SOURCE_LOCK:
                        discard_external_preparation(folder)
                b['release_workspace'](owner)
            if context is not None:
                b['_current_project'].reset(context)

    app.add_url_rule('/api/desktop/link-project', 'desktop_link_project', lambda: handle(True), methods=['POST'])
    app.add_url_rule('/api/desktop/relink-project', 'desktop_relink_project', lambda: handle(False), methods=['POST'])
