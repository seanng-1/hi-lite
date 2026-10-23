# SPDX-License-Identifier: GPL-3.0-or-later
"""Non-destructive editing endpoints, shared by legacy and project routes.

Editing is previewed as JSON. The single project autosave channel publishes that
JSON, avoiding server-side edits racing the user's draft in another window.
"""
import json
from pathlib import Path
import threading
from uuid import uuid4
from flask import jsonify, request, Response

from edit_model import initial_edits, validate_edits, apply_edit, cleanup_suggestions, candidate_groups
from media_editor import export_source_ranges, export_subtitles, validate_export_settings, validate_captions
from source_preview import SOURCE_LOCK, resolve_source
from transcript_store import read_current_transcript


def register_editor_api(app, backend):
    b = backend

    def payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('Request body must be a JSON object.')
        return data

    def paths():
        return b['workspace_paths']()

    def inputs():
        p = paths()
        with open(p['SEGMENTS_JSON'], encoding='utf-8') as stream:
            manifest = json.load(stream)
        transcript = read_current_transcript(p['MANIFEST_DIR'])
        project_id = b['_current_project'].get()
        state = b['projects'].get(project_id)['state'] if project_id else {}
        document = state.get('edit_document') or initial_edits(manifest['segments'])
        document = validate_edits(document, manifest['segments'])
        captions = state.get('edited_captions')
        if captions is None:
            captions = [dict(id=cue.get('id', index), start=cue['start'], end=cue['end'], text=cue.get('text', ''))
                        for index, cue in enumerate(transcript)]
        return p, manifest, transcript, state, document, captions

    def failure(error, status=400):
        return jsonify(status='error', message=str(error)), status

    def document_from(data, manifest, current):
        return validate_edits(data.get('document', current), manifest['segments'])

    @app.route('/api/editor')
    def editor_state():
        try:
            p, manifest, transcript, state, document, captions = inputs()
            try:
                with SOURCE_LOCK:
                    _, source = resolve_source(p['MANIFEST_DIR'], p['UPLOAD_FOLDER'])
            except ValueError:
                source = None
            exports = [dict(output_file='/final/' + output.name, size=output.stat().st_size)
                       for output in sorted(Path(p['HIGHLIGHTS_DIR']).glob('*.mp4'), key=lambda output: output.stat().st_mtime, reverse=True)
                       if output.is_file() and not output.is_symlink()]
            return jsonify(document=document, captions=captions, transcript=transcript, source=source, exports=exports,
                           settings=state.get('export_settings', {}), candidates=state.get('candidates', []))
        except (OSError, ValueError, KeyError, TypeError) as error:
            return failure(error)

    @app.route('/api/edit-preview', methods=['POST'])
    def preview_edit():
        try:
            data = payload()
            _, manifest, _, _, current, _ = inputs()
            document = document_from(data, manifest, current)
            return jsonify(document=apply_edit(document, data.get('action', {})))
        except (OSError, ValueError, KeyError, TypeError) as error:
            return failure(error)

    @app.route('/api/cleanup', methods=['POST'])
    def suggest_cleanup():
        try:
            data = payload()
            _, _, transcript, _, _, _ = inputs()
            kwargs = {key: data[key] for key in ('min_pause', 'keep_pause', 'fillers') if key in data}
            return jsonify(suggestions=cleanup_suggestions(transcript, **kwargs))
        except (OSError, ValueError, KeyError, TypeError) as error:
            return failure(error)

    def start_job(kind, worker, owner=None):
        owner = owner or b['acquire_workspace']()
        if owner is None:
            return b['workspace_busy']()
        task_id = f'{kind}_{uuid4().hex}'
        b['_task_projects'][task_id] = b['_current_project'].get()
        b['_cancel_events'][task_id] = threading.Event()
        b['processing_status'][task_id] = dict(status='processing', stage='starting', progress=0, message=f'Starting {kind}...')
        try:
            thread = threading.Thread(target=b['run_workspace_job'], args=(owner, task_id, worker, (task_id,)), daemon=True)
            thread.start()
            return jsonify(status='ok', task_id=task_id)
        except Exception as error:
            b['release_workspace'](owner)
            b['processing_status'][task_id] = dict(status='error', message=str(error))
            return failure(error, 500)

    @app.route('/api/render', methods=['POST'])
    def render_edits():
        owner = b['acquire_workspace']()
        if owner is None:
            return b['workspace_busy']()
        try:
            data = payload()
            p, manifest, _, _, current, captions = inputs()
            document = document_from(data, manifest, current)
            settings = validate_export_settings(data.get('settings', {}))
            captions = data.get('captions', captions)
            with SOURCE_LOCK:
                source, _ = resolve_source(p['MANIFEST_DIR'], p['UPLOAD_FOLDER'])
            ranges = document['ranges']
            if not ranges:
                raise ValueError('Keep at least one range before exporting.')
            # Validate caption text/timing before reserving the renderer.
            captions = validate_captions(captions)
            individual = data.get('individual', False)
            if type(individual) is not bool:
                raise ValueError('Individual export must be true or false.')
            groups = {}
            if individual:
                for row in ranges:
                    groups.setdefault(row['group_id'], []).append(row)
            else:
                groups['reel'] = ranges
        except (OSError, ValueError, KeyError, TypeError) as error:
            b['release_workspace'](owner)
            return failure(error)

        def worker(task):
            outputs = []
            created = []
            try:
                for index, selected in enumerate(groups.values()):
                    if b['_cancel_events'][task].is_set():
                        break
                    name = f'{task}_{index + 1}.mp4'
                    b['processing_status'][task] = dict(status='processing', stage='rendering', progress=int(index / len(groups) * 95),
                                                       message=f'Rendering {index + 1} of {len(groups)}...')
                    created.append(Path(p['HIGHLIGHTS_DIR']) / name)
                    result = export_source_ranges(source, selected, Path(p['HIGHLIGHTS_DIR']) / name,
                                                  captions=captions, settings=settings, cancel_event=b['_cancel_events'][task])
                    outputs.append(dict(output_file=f'/final/{name}', duration=result['duration']))
                if b['_cancel_events'][task].is_set():
                    raise RuntimeError('Export cancelled.')
                b['processing_status'][task] = dict(status='complete', progress=100, message='Export ready.', outputs=outputs,
                                                   output_file=outputs[0]['output_file'] if outputs else None)
            except Exception:
                for output in created:
                    output.unlink(missing_ok=True)
                raise
        return start_job('render', worker, owner)

    @app.route('/api/edited-subtitles', methods=['POST'])
    def edited_subtitles():
        try:
            data = payload()
            _, manifest, _, _, current, captions = inputs()
            document = document_from(data, manifest, current)
            format = data.get('format', 'srt')
            text = export_subtitles(data.get('captions', captions), document['ranges'], format)
            return Response(text, content_type='text/vtt; charset=utf-8' if format == 'vtt' else 'application/x-subrip; charset=utf-8',
                            headers={'Content-Disposition': f'attachment; filename="edited-captions.{format}"', 'Cache-Control': 'no-store'})
        except (OSError, ValueError, KeyError, TypeError) as error:
            return failure(error)

    @app.route('/api/candidates', methods=['POST'])
    def generate_candidates():
        owner = b['acquire_workspace']()
        if owner is None:
            return b['workspace_busy']()
        try:
            data = payload()
            _, _, transcript, _, _, _ = inputs()
            options = {key: data[key] for key in ('desired_count', 'target_duration', 'clip_type') if key in data}
            # Validate options without invoking a configured provider.
            candidate_groups(transcript, **options)
            use_ai = data.get('use_ai', False)
            if type(use_ai) is not bool:
                raise ValueError('AI scoring must be true or false.')
            instructions = data.get('instructions', '')
            if not isinstance(instructions, str) or len(instructions) > 20000:
                raise ValueError('AI instructions must be text of at most 20000 characters.')
            instructions = instructions.strip()
            if use_ai and not instructions:
                raise ValueError('Describe what makes a good clip before using AI scoring.')
            settings = b['llm_scoring'].get_llm_settings() if use_ai else None
            scoring_backend = b['llm_scoring'].get_backend(settings=settings) if use_ai else None
        except (OSError, ValueError, KeyError, TypeError, b['llm_scoring'].ScoringError) as error:
            b['release_workspace'](owner)
            return failure(error)

        def worker(task):
            scored = None
            if use_ai:
                b['processing_status'][task] = dict(status='processing', stage='scoring', progress=10,
                                                   message='Scoring the recording in bounded batches...')
                if not b['run_scoring_subprocess'](task, instructions, settings, scoring_backend.name):
                    return
                identity = b['llm_scoring'].score_cache_identity(transcript, instructions, scoring_backend)
                scored = b['llm_scoring'].load_score_cache(Path(paths()['MANIFEST_DIR']) / 'sorted_segments.json', transcript,
                                                          identity, require_complete=False)
            candidates = candidate_groups(transcript, scored_records=scored, **options)
            b['processing_status'][task] = dict(status='complete', progress=100, message='Candidates ready to review. No edits were applied.',
                                               candidates=candidates, origin='ai' if use_ai else 'local_heuristic')
        return start_job('candidates', worker, owner)
