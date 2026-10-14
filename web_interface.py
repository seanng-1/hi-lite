# SPDX-License-Identifier: GPL-3.0-or-later
"""
Run a flask back-end server to serve JSON, videos and highlight export API
"""
import os
import re
import sys
import json
import subprocess
import tempfile
import threading
from uuid import uuid4
from datetime import datetime
from flask import Flask, request, jsonify, send_file, send_from_directory, Response
from flask_cors import CORS
from moviepy import VideoFileClip, concatenate_videoclips
from werkzeug.utils import secure_filename
from subtitle_serializer import serialize_subtitles, NoCaptions
from transcript_store import invalidate_transcript, read_current_transcript
from source_preview import SOURCE_LOCK, UNAVAILABLE as SOURCE_UNAVAILABLE, invalidate_source, publish_source, resolve_source

# Safe to import at module level: scoring.py only touches heavy/optional
# deps (requests session, g4f) lazily inside the backends.
from model_based_processing import scoring as llm_scoring

# Windows consoles/pipes default to cp1252, which cannot encode transcript
# text (or anything non-ASCII) and would kill the process with
# UnicodeEncodeError. Force UTF-8 for everything we print.
# line_buffering: when stdout is a pipe (Electron/parent backend reads it),
# Python would otherwise buffer ~8KB before flushing, so progress lines
# arrive in bursts long after the fact.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


# All app data lives under HI_LITE_DATA_DIR (set by the Electron shell to a
# per-user writable folder in production; defaults to the repo root in dev).
DATA_DIR = os.path.abspath(os.getenv("HI_LITE_DATA_DIR", "."))
MANIFEST_DIR = os.path.join(DATA_DIR, "highlights")
SEGMENTS_DIR = os.path.join(MANIFEST_DIR, "segments")          # folder with mp4 files
SEGMENTS_JSON = os.path.join(MANIFEST_DIR, "segments_manifest.json")  # manifest JSON
HIGHLIGHTS_DIR = os.path.join(MANIFEST_DIR, "final")           # output highlight reel
SCORED_SEGMENTS = os.path.join(MANIFEST_DIR, "scored_segments.json")  # scored unsorted segments
os.makedirs(HIGHLIGHTS_DIR, exist_ok=True)

# ffmpeg binaries (Electron ships them and points these env vars at them)
FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_PATH", "ffprobe")

# Add upload configuration
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv', 'webm'}

# Ensure upload folder exists and has proper permissions
try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    # Test write permissions by creating a test file
    test_file = os.path.join(UPLOAD_FOLDER, ".write_test")
    try:
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)
    except Exception as e:
        print(f"WARNING: Upload folder exists but is not writable: {e}")
        print(f"WARNING: Upload folder path: {os.path.abspath(UPLOAD_FOLDER)}")
        print(f"WARNING: Upload folder permissions: {oct(os.stat(UPLOAD_FOLDER).st_mode) if os.path.exists(UPLOAD_FOLDER) else 'does not exist'}")
except Exception as e:
    print(f"ERROR: Could not create upload folder: {e}")
    print(f"ERROR: Attempted path: {os.path.abspath(UPLOAD_FOLDER)}")

# Track processing status
processing_status = {}

# The mutex protects only the owner token; requests never wait for a job.
# Ownership can pass from an HTTP handler to its existing background thread.
_admission_mutex = threading.Lock()
_workspace_owner = None


def acquire_workspace():
    global _workspace_owner
    if not _admission_mutex.acquire(blocking=False):
        return None
    try:
        if _workspace_owner is not None:
            return None
        _workspace_owner = object()
        return _workspace_owner
    finally:
        _admission_mutex.release()


def release_workspace(owner):
    global _workspace_owner
    with _admission_mutex:
        if _workspace_owner is owner:
            _workspace_owner = None


def workspace_busy():
    return jsonify({
        "status": "error",
        "message": "Hi-Lite is busy processing another operation. Please wait for it to finish."
    }), 409


def run_workspace_job(owner, task_id, worker, worker_args):
    """Keep admission until either of the existing workers has returned."""
    try:
        worker(*worker_args)
    except Exception as e:
        app.logger.exception("Workspace job failed")
        processing_status[task_id] = {"status": "error", "message": f"Processing failed: {e}"}
    finally:
        release_workspace(owner)

# Active pipeline subprocess, so the parent watchdog can kill it on shutdown
active_pipeline_process = None


def finish_pipeline_process(process):
    """Reap this direct child before admission is released, including on error."""
    global active_pipeline_process
    if process is not None:
        try:
            if process.poll() is None:
                process.kill()
            process.wait()
        finally:
            active_pipeline_process = None


def start_parent_watchdog():
    """
    Exit when our stdin pipe hits EOF, which means the parent (Electron)
    process died — including crashes and force-kills where it never got to
    run its own cleanup. Enabled only when Electron sets HI_LITE_WATCHDOG=1,
    so running the backend manually from a console is unaffected.
    """
    if os.getenv("HI_LITE_WATCHDOG") != "1" or sys.stdin is None:
        return

    def _watch():
        try:
            sys.stdin.buffer.read()  # blocks until EOF = parent is gone
        except Exception:
            pass
        try:
            if active_pipeline_process and active_pipeline_process.poll() is None:
                active_pipeline_process.kill()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_watch, daemon=True, name="parent-watchdog").start()

app = Flask(__name__)
CORS(app)  # allow requests from Vite dev server (port 5173)

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    import sys

    if getattr(sys, 'frozen', False):
        # Application is frozen (packaged by PyInstaller)
        return os.path.join(sys._MEIPASS, relative_path)
    # Application is running normally (in dev/unfrozen mode)
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), relative_path)

# Serve segments manifest JSON
@app.route("/data/segments_manifest.json")
def get_segments_json():
    if not os.path.exists(SEGMENTS_JSON):
        # Return empty segments list so the app shows the styled upload screen
        return jsonify({"segments": []}), 200
    
    with SOURCE_LOCK:
        try:
            _, source = resolve_source(MANIFEST_DIR, UPLOAD_FOLDER)
        except ValueError:
            # Preserve the existing serving/validation contract for legacy or
            # malformed manifests. The UI handles a missing source explicitly.
            response = send_from_directory(os.path.dirname(SEGMENTS_JSON), os.path.basename(SEGMENTS_JSON))
        else:
            with open(SEGMENTS_JSON, encoding='utf-8') as stream:
                manifest = json.load(stream)
            manifest['source'] = source
            response = jsonify(manifest)
        response.headers['Cache-Control'] = 'no-store'
        return response


@app.route('/api/source/<source_id>')
def serve_source(source_id):
    # Opening the validated, uniquely named upload happens under the same lock
    # as replacement invalidation. Flask handles conditional HTTP byte ranges.
    with SOURCE_LOCK:
        try:
            path, _ = resolve_source(MANIFEST_DIR, UPLOAD_FOLDER, source_id)
        except ValueError:
            return jsonify(status='error', message=SOURCE_UNAVAILABLE), 404
        response = send_file(path, conditional=True)
        response.headers['Cache-Control'] = 'no-store'
        return response

# Serve individual video segments
@app.route("/segments/<filename>")
def serve_segment(filename):
    if not os.path.exists(os.path.join(SEGMENTS_DIR, filename)):
        return jsonify({"status": "error", "message": f"{filename} not found"}), 404
    return send_from_directory(SEGMENTS_DIR, filename)

# Serve final highlight reel
@app.route("/final/<filename>")
def serve_final(filename):
    if not os.path.exists(os.path.join(HIGHLIGHTS_DIR, filename)):
        return jsonify({"status": "error", "message": f"{filename} not found"}), 404
    return send_from_directory(HIGHLIGHTS_DIR, filename)

# Receive selected segments and generate highlight reel
@app.route("/api/export", methods=["POST"])
def export_segments():
    owner = acquire_workspace()
    if owner is None:
        return workspace_busy()
    try:
        data = request.json
        selected_ids = data.get("selected_ids", [])

        # Load manifest
        if not os.path.exists(SEGMENTS_JSON):
            return jsonify({"status": "error", "message": "segments_manifest.json not found"}), 404
        with open(SEGMENTS_JSON, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        segments_list = manifest.get("segments", [])

        # Filter selected segments
        selected_segments = [s for s in segments_list if s["id"] in selected_ids]

        if not selected_segments:
            return jsonify({"status": "error", "message": "No segments selected"}), 400

        clips = []
        for seg in selected_segments:
            video_path = os.path.join(SEGMENTS_DIR, seg["filename"])
            if os.path.exists(video_path):
                clips.append(VideoFileClip(video_path))
            else:
                print(f"Warning: file not found {video_path}")

        if clips:
            final_clip = concatenate_videoclips(clips, method="compose")
            output_filename = "highlight_reel.mp4"
            output_path = os.path.join(HIGHLIGHTS_DIR, output_filename)
            final_clip.write_videofile(output_path, codec="libx264", audio_codec="aac")
            final_clip.close()
            return jsonify({
                "status": "ok",
                "output_file": f"/final/{output_filename}",
                "selected_count": len(clips)
            })
        else:
            return jsonify({"status": "error", "message": "No valid video clips found"}), 400
    finally:
        release_workspace(owner)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def process_video_async(video_path, output_folder, task_id):
    """
    Process video by calling main.py as a subprocess.
    """
    global processing_status, active_pipeline_process
    process = None

    try:
        # Task should already be registered, but update if not
        if task_id not in processing_status:
            processing_status[task_id] = {
                "status": "processing",
                "stage": "starting",
                "progress": 5,
                "message": "Starting video processing..."
            }
        else:
            # Update existing status
            processing_status[task_id].update({
                "status": "processing",
                "stage": "starting",
                "progress": 5,
                "message": "Starting video processing..."
            })
        
        # Ensure paths are absolute
        video_path = os.path.abspath(video_path)
        output_folder = os.path.abspath(output_folder)

        print(f"DEBUG: Calling main.py with video: {video_path}")
        print(f"DEBUG: Output folder: {output_folder}")

        if not os.path.exists(video_path):
            raise Exception(f"Video file not found: {video_path}")

        # Remove the previous manifest so the UI never shows stale results
        # for a new upload (segment files get overwritten by the pipeline).
        try:
            if os.path.exists(SEGMENTS_JSON):
                os.remove(SEGMENTS_JSON)
        except OSError as e:
            print(f"WARNING: could not remove old manifest: {e}")

        # PyInstaller onefile leaves _PYI_*/_MEIPASS2 bookkeeping vars in our
        # environment; a re-spawned copy of this exe inherits them and its
        # bootloader deadlocks waiting for a launch stage that never comes.
        # Always hand children a cleaned environment.
        child_env = {k: v for k, v in os.environ.items()
                     if not k.startswith("_PYI") and k != "_MEIPASS2"}

        if getattr(sys, 'frozen', False):
            # Frozen (PyInstaller): sys.executable is this backend executable.
            # Re-invoke it with --process-video, handled at the bottom of this file.
            cmd = [sys.executable, "--process-video", video_path, "--output", output_folder]
        else:
            # Dev: run main.py with the current Python interpreter
            script_dir = os.path.dirname(os.path.abspath(__file__))
            main_script = os.path.join(script_dir, "main.py")
            if not os.path.exists(main_script):
                raise Exception(f"main.py not found at {main_script}")
            cmd = [sys.executable, main_script, video_path, "--output", output_folder]
        print(f"DEBUG: Running command: {' '.join(cmd)}")
        
        process = subprocess.Popen(
            cmd,
            env=child_env,
            # stdin must NOT be inherited: our watchdog thread keeps a blocking
            # read pending on it, and on Windows that serializes the child's own
            # startup access to the shared handle — deadlocking it at 0% CPU.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1
        )
        active_pipeline_process = process
        
        # Stream output and update status based on progress indicators
        output_lines = []
        for line in process.stdout:
            line = line.strip()
            if line:
                output_lines.append(line)
                print(line)  # Also log to Flask console
                
                # Update status based on main.py output
                if "Step 1: Extracting audio" in line:
                    processing_status[task_id] = {
                        "status": "processing",
                        "stage": "extracting_audio",
                        "progress": 10,
                        "message": "Extracting audio..."
                    }
                elif "Step 2: Transcribing audio" in line:
                    processing_status[task_id] = {
                        "status": "processing",
                        "stage": "transcribing",
                        "progress": 30,
                        "message": "Transcribing audio..."
                    }
                elif "Loading Whisper model" in line:
                    processing_status[task_id] = {
                        "status": "processing",
                        "stage": "loading_model",
                        "progress": 20,
                        "message": "Loading Whisper model (first run downloads it, ~460 MB)..."
                    }
                elif "Step 3: Saving transcription data" in line:
                    processing_status[task_id] = {
                        "status": "processing",
                        "stage": "saving_transcription",
                        "progress": 50,
                        "message": "Saving transcription..."
                    }
                elif "Step 4: Extracting video segments" in line:
                    processing_status[task_id] = {
                        "status": "processing",
                        "stage": "extracting_segments",
                        "progress": 70,
                        "message": "Extracting video segments..."
                    }
                elif "Step 5: Creating segment manifest" in line:
                    processing_status[task_id] = {
                        "status": "processing",
                        "stage": "creating_manifest",
                        "progress": 90,
                        "message": "Creating segment manifest..."
                    }
                elif "Processing Complete" in line or "Processing complete" in line:
                    # Check if manifest was created successfully
                    manifest_path = os.path.join(output_folder, "segments_manifest.json")
                    if os.path.exists(manifest_path):
                        try:
                            with open(manifest_path, "r") as f:
                                manifest = json.load(f)
                            segment_count = len(manifest.get("segments", []))
                            
                            processing_status[task_id] = {
                                "status": "processing",
                                "progress": 99,
                                "message": "Finalizing recording…",
                                "segment_count": segment_count,
                                "output_folder": output_folder
                            }
                            print(f"DEBUG: Processing complete! {segment_count} segments created")
                        except Exception as e:
                            print(f"WARNING: Could not read manifest: {e}")
        
        # Wait for process to complete
        return_code = process.wait()
        active_pipeline_process = None
        
        # Check output for early return indicators (main.py returns early but with exit code 0)
        error_output = "\n".join(output_lines[-30:])  # Last 30 lines for context
        
        print(f"DEBUG: main.py process finished with return code: {return_code}")
        print(f"DEBUG: Checking for manifest at: {output_folder}")
        print(f"DEBUG: Output folder exists: {os.path.exists(output_folder)}")
        
        # Check for early return conditions in output (main.py returns early but exits with 0)
        error_output_lower = error_output.lower()
        if "transcription failed" in error_output_lower:
            error_msg = "Transcription failed. Check the logs for details."
            print(f"ERROR: {error_msg}")
            processing_status[task_id] = {
                "status": "error",
                "message": error_msg
            }
            return
        
        if "no segments extracted" in error_output_lower:
            error_msg = "No segments were extracted from the video."
            print(f"ERROR: {error_msg}")
            processing_status[task_id] = {
                "status": "error",
                "message": error_msg
            }
            return
        
        if return_code != 0:
            error_msg = f"main.py exited with code {return_code}"
            print(f"ERROR: {error_msg}")
            print(f"ERROR: Last output lines:\n{error_output}")
            processing_status[task_id] = {
                "status": "error",
                "message": error_msg
            }
            return
        
        # Check what files were created
        if os.path.exists(output_folder):
            print(f"DEBUG: Files in output folder:")
            for file in os.listdir(output_folder):
                file_path = os.path.join(output_folder, file)
                print(f"  - {file} ({'dir' if os.path.isdir(file_path) else 'file'})")
        
        # Double-check completion status - manifest should exist if processing completed
        manifest_path = os.path.join(output_folder, "segments_manifest.json")
        print(f"DEBUG: Looking for manifest at: {manifest_path}")
        print(f"DEBUG: Manifest file exists: {os.path.exists(manifest_path)}")
        
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r") as f:
                    manifest = json.load(f)
                segment_count = len(manifest.get("segments", []))
                
                print(f"DEBUG: Manifest found with {segment_count} segments")
                # Source preview is optional: failed validation must never
                # turn a successful legacy import/export into a failed job.
                with SOURCE_LOCK:
                    try:
                        publish_source(output_folder, UPLOAD_FOLDER, video_path, task_id.removeprefix('task_'))
                    except (OSError, ValueError) as error:
                        app.logger.warning('Source preview unavailable: %s', error)
                
                if processing_status[task_id].get("status") != "complete":
                    processing_status[task_id] = {
                        "status": "complete",
                        "progress": 100,
                        "message": "Processing complete!",
                        "segment_count": segment_count,
                        "output_folder": output_folder
                    }
            except Exception as e:
                print(f"ERROR: Could not read manifest: {e}")
                import traceback
                traceback.print_exc()
                processing_status[task_id] = {
                    "status": "error",
                    "message": f"Manifest file exists but could not be read: {str(e)}"
                }
        else:
            # Manifest not found - check if processing actually completed
            transcription_json = os.path.join(output_folder, "transcription.json")
            segments_dir = os.path.join(output_folder, "segments")
            
            if not os.path.exists(transcription_json):
                error_msg = "Processing appears to have failed - transcription.json not found. Check the logs for details."
            elif not os.path.exists(segments_dir) or not os.listdir(segments_dir):
                error_msg = "Transcription completed but no video segments were extracted."
            else:
                error_msg = f"Processing completed but manifest file not found. Check server logs for details."
            
            print(f"ERROR: {error_msg}")
            print(f"ERROR: Output folder contents: {os.listdir(output_folder) if os.path.exists(output_folder) else 'folder does not exist'}")
            processing_status[task_id] = {
                "status": "error",
                "message": error_msg
            }
        
    except Exception as e:
        print(f"ERROR: Unexpected error in process_video_async: {e}")
        import traceback
        traceback.print_exc()
        processing_status[task_id] = {
            "status": "error",
            "message": f"Processing failed: {str(e)}"
        }

    finally:
        finish_pipeline_process(process)

@app.after_request
def subtitle_cache_policy(response):
    if request.path == "/api/subtitles" or request.path.startswith("/api/subtitles/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/subtitles", defaults={"format": None})
@app.route("/api/subtitles/<format>")
def subtitles(format):
    if format is not None and format not in ("srt", "vtt"):
        return jsonify(message="Unsupported subtitle format. Use srt or vtt."), 400
    owner = acquire_workspace()
    if owner is None:
        return jsonify(available=False, message="Subtitles unavailable while processing. Please wait."), 409
    try:
        content = serialize_subtitles(read_current_transcript(MANIFEST_DIR), format or "srt")
        if format is None:
            response = jsonify(available=True, message="Original recording subtitles ready")
        else:
            response = Response(content.encode("utf-8"), content_type=(
                "application/x-subrip" if format == "srt" else "text/vtt") + "; charset=utf-8")
            response.headers["Content-Disposition"] = f'attachment; filename="original-recording.{format}"'
        response.headers["Cache-Control"] = "no-store"
        return response
    except (FileNotFoundError, NoCaptions) as exc:
        return jsonify(available=False, message=str(exc)), 404
    except (ValueError, UnicodeError):
        return jsonify(available=False, message="Invalid transcript timing or content. Import the recording again."), 422
    except OSError:
        return jsonify(available=False, message="Could not read current transcript."), 500
    finally:
        release_workspace(owner)


@app.route("/api/upload", methods=["POST"])
def upload_video():
    """
    Handle video upload and start processing
    """
    owner = None
    worker_started = False
    try:
        if 'video' not in request.files:
            return jsonify({"status": "error", "message": "No video file provided"}), 400
        
        file = request.files['video']
        
        if file.filename == '':
            return jsonify({"status": "error", "message": "No file selected"}), 400
        
        if not allowed_file(file.filename):
            return jsonify({
                "status": "error", 
                "message": f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
            }), 400
        
        owner = acquire_workspace()
        if owner is None:
            return workspace_busy()

        # Fail closed before any replacement work, including upload/worker failures.
        with SOURCE_LOCK:
            invalidate_source(MANIFEST_DIR)
            invalidate_transcript(MANIFEST_DIR)

        # Ensure upload folder exists with proper permissions
        upload_path = os.path.abspath(UPLOAD_FOLDER)
        try:
            os.makedirs(upload_path, mode=0o755, exist_ok=True)
            print(f"DEBUG: Upload folder path: {upload_path}")
            print(f"DEBUG: Upload folder exists: {os.path.exists(upload_path)}")
            print(f"DEBUG: Upload folder is writable: {os.access(upload_path, os.W_OK)}")
        except Exception as e:
            print(f"ERROR: Could not create upload folder: {e}")
            print(f"ERROR: Attempted path: {upload_path}")
            print(f"ERROR: Current working directory: {os.getcwd()}")
            import traceback
            traceback.print_exc()
            return jsonify({
                "status": "error",
                "message": f"Failed to create upload directory: {str(e)}. Path: {upload_path}"
            }), 500
        
        # Save uploaded file
        filename = secure_filename(file.filename)
        task_id = f"task_{uuid4().hex}"
        unique_filename = f"{task_id.removeprefix('task_')}_{filename}"
        video_path = os.path.join(upload_path, unique_filename)
        
        # Ensure we're using absolute path
        video_path = os.path.abspath(video_path)
        print(f"DEBUG: Attempting to save file to: {video_path}")
        print(f"DEBUG: Parent directory exists: {os.path.exists(os.path.dirname(video_path))}")
        print(f"DEBUG: Parent directory writable: {os.access(os.path.dirname(video_path), os.W_OK)}")
        
        try:
            # Try to set permissions on parent directory if needed
            if os.path.exists(upload_path):
                try:
                    os.chmod(upload_path, 0o755)
                except Exception:
                    pass  # Ignore chmod errors, just try to write
            
            file.save(video_path)
            print(f"DEBUG: File saved successfully to {video_path}")
            
            # Verify file was saved and set permissions
            if os.path.exists(video_path):
                try:
                    os.chmod(video_path, 0o644)
                except Exception:
                    pass  # Ignore chmod errors
            else:
                raise Exception("File save appeared to succeed but file does not exist")
                
        except PermissionError as e:
            print(f"ERROR: Permission denied saving file: {e}")
            print(f"ERROR: Upload path: {upload_path}")
            print(f"ERROR: Current user (if available): {os.getenv('USER', 'unknown')}")
            print(f"ERROR: Directory owner info: {os.stat(upload_path).st_uid if os.path.exists(upload_path) else 'N/A'}")
            import traceback
            traceback.print_exc()
            return jsonify({
                "status": "error",
                "message": f"Permission denied saving file. The uploads directory may need proper permissions. Error: {str(e)}"
            }), 500
        except Exception as e:
            print(f"ERROR: Failed to save file: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({
                "status": "error",
                "message": f"Failed to save uploaded file: {str(e)}"
            }), 500
        
        # Verify file was saved
        if not os.path.exists(video_path):
            return jsonify({
                "status": "error",
                "message": "File was not saved successfully"
            }), 500
        
        # Process into the root highlights folder — that's what the UI reads
        # (/data/segments_manifest.json and /segments/<file>). A new upload
        # replaces the previous results.
        output_folder = MANIFEST_DIR
        try:
            os.makedirs(output_folder, exist_ok=True)
            os.makedirs(os.path.join(output_folder, "segments"), exist_ok=True)
            print(f"DEBUG: Created output folder: {output_folder}")
        except Exception as e:
            print(f"ERROR: Failed to create output folder: {e}")
            return jsonify({
                "status": "error",
                "message": f"Failed to create output directory: {str(e)}"
            }), 500
        
        # Register task immediately so status endpoint can find it
        processing_status[task_id] = {
            "status": "processing",
            "stage": "starting",
            "progress": 0,
            "message": "Starting video processing..."
        }
        print(f"DEBUG: Registered task {task_id} in processing_status")
        print(f"DEBUG: Total tasks after registration: {len(processing_status)}")
        print(f"DEBUG: All task_ids: {list(processing_status.keys())}")
        
        # Start processing in background thread
        try:
            processing_thread = threading.Thread(
                target=run_workspace_job,
                args=(owner, task_id, process_video_async, (video_path, output_folder, task_id))
            )
            processing_thread.daemon = True
            processing_thread.start()
            worker_started = True
            print(f"DEBUG: Started processing thread for task {task_id}")
        except Exception as e:
            print(f"ERROR: Failed to start processing thread: {e}")
            import traceback
            traceback.print_exc()
            # Mark task as error
            processing_status[task_id] = {
                "status": "error",
                "message": f"Failed to start processing: {str(e)}"
            }
            return jsonify({
                "status": "error",
                "message": f"Failed to start processing: {str(e)}"
            }), 500
        
        return jsonify({
            "status": "ok",
            "message": "Upload successful. Processing started.",
            "task_id": task_id,
            "filename": unique_filename
        })
    
    except Exception as e:
        print(f"ERROR: Unexpected error in upload_video: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "status": "error",
            "message": f"Upload failed: {str(e)}"
        }), 500

    finally:
        if owner is not None and not worker_started:
            release_workspace(owner)


@app.route("/api/processing-status/<task_id>", methods=["GET"])
def get_processing_status(task_id):
    """
    Check processing status for a task
    """
    print(f"DEBUG: Status check requested for task_id: {task_id}")
    print(f"DEBUG: Available task_ids: {list(processing_status.keys())}")
    print(f"DEBUG: Total tasks in memory: {len(processing_status)}")
    
    if task_id not in processing_status:
        print(f"ERROR: Task {task_id} not found in processing_status")
        return jsonify({
            "status": "error",
            "message": f"Task not found. Available tasks: {list(processing_status.keys())}"
        }), 404
    
    return jsonify(processing_status[task_id])


@app.route("/api/available-videos", methods=["GET"])
def get_available_videos():
    """
    List all processed videos available for viewing
    """
    highlights_dir = MANIFEST_DIR
    videos = []
    
    if not os.path.exists(highlights_dir):
        return jsonify({"videos": []})
    
    for folder in os.listdir(highlights_dir):
        folder_path = os.path.join(highlights_dir, folder)
        manifest_path = os.path.join(folder_path, "segments_manifest.json")
        
        if os.path.isdir(folder_path) and os.path.exists(manifest_path):
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
            
            videos.append({
                "id": folder,
                "name": folder.replace("video_", "").replace("_", " "),
                "segment_count": len(manifest.get("segments", [])),
                "created_at": folder.replace("video_", ""),
                "manifest_path": f"/data/{folder}/segments_manifest.json"
            })
    
    return jsonify({"videos": videos})


# Update the segments manifest endpoint to support different videos
@app.route("/data/<video_id>/segments_manifest.json")
def get_video_segments_json(video_id):
    manifest_path = os.path.join(MANIFEST_DIR, video_id, "segments_manifest.json")
    if not os.path.exists(manifest_path):
        return jsonify({"status": "error", "message": "Manifest not found"}), 404
    
    directory = os.path.dirname(manifest_path)
    filename = os.path.basename(manifest_path)
    return send_from_directory(directory, filename)


# Update segments serving to support different videos
@app.route("/segments/<video_id>/<filename>")
def serve_video_segment(video_id, filename):
    segments_path = os.path.join(MANIFEST_DIR, video_id, "segments", filename)
    if not os.path.exists(segments_path):
        return jsonify({"status": "error", "message": "Segment not found"}), 404
    
    directory = os.path.dirname(segments_path)
    return send_from_directory(directory, filename)


def find_manifest_segment(highlight, segments_list):
    """Resolve only an unambiguous exact canonical ID; never guess from timestamps."""
    sid = llm_scoring.canonical_segment_id(highlight.get("segment_id"))
    if sid is None:
        return None
    matches = [seg for seg in segments_list if isinstance(seg, dict)
               and llm_scoring.canonical_segment_id(seg.get("transcript_id")) == sid]
    return matches[0] if len(matches) == 1 else None


def automatic_export_segments(manifest):
    """Legacy clip indexes cannot establish transcript identity; require reimport."""
    segments = manifest.get("segments", [])
    if any(not isinstance(seg, dict) or
           llm_scoring.canonical_segment_id(seg.get("transcript_id")) is None
           for seg in segments):
        raise ValueError("Clip manifest lacks canonical transcript identity. Import the recording again.")
    return segments


def current_scoring_segments():
    """The scoring transcript is authoritative; the clip manifest is only for export."""
    try:
        with open(os.path.join(MANIFEST_DIR, "transcription.json"), "r", encoding="utf-8") as f:
            transcript = json.load(f)
    except (OSError, ValueError, RecursionError):
        raise llm_scoring.ScoringError("Current transcription.json could not be read. Process a video first.",
                                       category="parse")
    if not isinstance(transcript, dict):
        raise llm_scoring.ScoringError("Current transcription.json must contain transcript segments.",
                                       category="parse")
    return transcript.get("segments")


def load_validated_highlights(scored_path, instructions):
    """All automatic disk consumers use the same identity and HL-A3a validation."""
    segments = current_scoring_segments()
    backend = llm_scoring.get_backend()
    identity = llm_scoring.score_cache_identity(segments, instructions, backend)
    return llm_scoring.load_score_cache(scored_path, segments, identity)


def run_scoring_subprocess(task_id, instructions, settings=None, backend_name=None):
    """
    Run AI highlight scoring (model_based_processing/scoring.py) as a
    subprocess, mirroring how the video pipeline is spawned. Writes
    scored_segments.json + sorted_segments.json into the highlights folder.
    Returns True on success.
    """
    global active_pipeline_process

    transcript_path = os.path.join(MANIFEST_DIR, "transcription.json")
    if not os.path.exists(transcript_path):
        processing_status[task_id] = {
            "status": "error",
            "message": "transcription.json not found - process a video first."
        }
        return False

    args = ["--transcript", transcript_path, "--output-dir", MANIFEST_DIR,
            "--instructions", instructions]

    # Same self-spawn rules as the video pipeline: cleaned env, no stdin.
    child_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("_PYI") and k != "_MEIPASS2"}
    if settings is not None:
        # Pin the resolved request across the process boundary. Settings edits
        # and a changed auto-detection probe cannot silently reroute this job.
        for key in ("base_url", "api_key", "model", "local_url", "local_model"):
            child_env["HI_LITE_LLM_" + key.upper()] = settings[key] or ""
        child_env["HI_LITE_LLM_BACKEND"] = backend_name

    if getattr(sys, 'frozen', False):
        cmd = [sys.executable, "--score-highlights"] + args
    else:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "model_based_processing", "scoring.py")
        cmd = [sys.executable, script] + args

    print(f"DEBUG: Running scoring command: {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )
    active_pipeline_process = process

    try:
        scoring_ok = False
        last_error = None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                print(f"[scoring] {line}")
                if line.startswith("Trying "):
                    processing_status[task_id] = {
                        "status": "processing", "stage": "scoring", "progress": 30,
                        "message": f"Scoring with AI ({line[len('Trying '):]})..."
                    }
                elif line.startswith("ERROR: "):
                    # scoring.py prints distinct, user-facing failure reasons
                    # (auth / rate limit / timeout / no backend configured / ...)
                    last_error = line[len("ERROR: "):]
                elif "Scoring complete" in line:
                    scoring_ok = True

        return_code = process.wait()
        active_pipeline_process = None

        if return_code != 0 or not scoring_ok:
            processing_status[task_id] = {
                "status": "error",
                "message": last_error or "AI scoring failed. Check the app logs."
            }
            return False
        return True
    finally:
        finish_pipeline_process(process)


def build_highlight_reel(min_score, sort_chronological, instructions, highlights=None):
    """
    Build the highlight reel mp4 from sorted_segments.json.
    Returns the success payload dict; raises ValueError with a user-facing
    message on expected failures.
    """
    scored_segments_path = os.path.join(MANIFEST_DIR, "sorted_segments.json")
    print(f"DEBUG: Looking for sorted_segments.json at: {scored_segments_path}")

    if not os.path.exists(scored_segments_path):
        raise ValueError("sorted_segments.json not found. Run AI scoring first.")

    if highlights is None:
        highlights = load_validated_highlights(scored_segments_path, instructions)
    print(f"DEBUG: Total highlights loaded: {len(highlights)}")

    # Filter by score threshold
    filtered_highlights = [h for h in highlights if h["score"] >= min_score]
    print(f"DEBUG: Filtered highlights (score >= {min_score}): {len(filtered_highlights)}")

    if not filtered_highlights:
        raise ValueError(f"No highlights with score >= {min_score} found. Try a lower threshold.")

    # Load segments manifest to match segment IDs to video files
    if not os.path.exists(SEGMENTS_JSON):
        raise ValueError("segments_manifest.json not found")

    with open(SEGMENTS_JSON, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    segments_list = automatic_export_segments(manifest)
    print(f"DEBUG: Total segments in manifest: {len(segments_list)}")

    # Scores are already validated and deduplicated. Manifest matching is exact
    # because buffered clip timestamps are not transcript identities.
    pairs = []
    for highlight in filtered_highlights:
        matching_segment = find_manifest_segment(highlight, segments_list)
        if not matching_segment:
            print(f"WARNING: No unique manifest segment for ID {highlight['segment_id']}")
            continue
        pairs.append((highlight, matching_segment))
    if sort_chronological:
        # Sort by the manifest clip's actual start, not the LLM-reported time
        pairs.sort(key=lambda p: p[1].get("start", 0))
    else:
        # Sort by score (highest first)
        pairs.sort(key=lambda p: p[0]["score"], reverse=True)

    # Build list of video file paths
    video_paths = []
    used_segments = []

    for highlight, matching_segment in pairs:
        video_path = os.path.join(SEGMENTS_DIR, matching_segment["filename"])
        # Convert to absolute path for ffmpeg
        video_path = os.path.abspath(video_path)

        if os.path.exists(video_path):
            video_paths.append(video_path)
            used_segments.append({
                "segment_id": highlight["segment_id"],
                "text": highlight.get("text"),
                "score": highlight.get("score"),
                "reason": highlight.get("reason"),
                "start_time": highlight.get("start_time")
            })
            print(f"DEBUG: Added clip {len(video_paths)}: {video_path}")
        else:
            print(f"WARNING: Video file not found: {video_path}")
    
    print(f"DEBUG: Total video clips to concatenate: {len(video_paths)}")

    if not video_paths:
        raise ValueError("No valid video clips found for the filtered highlights")

    # Generate output filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"auto_highlights_score{min_score}_{timestamp}.mp4"
    # Use absolute path for output too
    output_path = os.path.abspath(os.path.join(HIGHLIGHTS_DIR, output_filename))

    # Create a temporary file list for ffmpeg
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
        concat_file = f.name
        for video_path in video_paths:
            # Escape single quotes and write in ffmpeg concat format
            escaped_path = video_path.replace("'", "'\\''")
            f.write(f"file '{escaped_path}'\n")

    print(f"DEBUG: Created concat file: {concat_file}")
    print(f"DEBUG: Output path: {output_path}")

    # Use ffmpeg to concatenate videos
    # -safe 0: allow absolute paths
    # -f concat: use concat demuxer
    # -c copy: copy streams without re-encoding (fast!)
    cmd = [
        FFMPEG,
        '-f', 'concat',
        '-safe', '0',
        '-i', concat_file,
        '-c', 'copy',
        '-y',  # Overwrite output file if exists
        output_path
    ]

    print(f"DEBUG: Running ffmpeg command...")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    # Clean up temp file
    os.unlink(concat_file)

    if result.returncode != 0:
        print(f"FFMPEG ERROR: {result.stderr}")
        raise RuntimeError(f"FFmpeg failed: {result.stderr[-500:]}")

    print(f"DEBUG: FFmpeg completed successfully")

    # Get file size
    file_size_bytes = os.path.getsize(output_path)
    file_size_mb = round(file_size_bytes / (1024 * 1024), 2)

    # Save metadata about what was included
    metadata = {
        "created_at": timestamp,
        "min_score": min_score,
        "total_highlights_available": len(highlights),
        "highlights_used": len(used_segments),
        "score_validation": highlights.validation,
        "segments": used_segments
    }

    metadata_path = os.path.join(SEGMENTS_DIR, f"auto_highlights_metadata_{timestamp}.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    print(f"DEBUG: Success! Video saved to {output_path} ({file_size_mb} MB)")

    return {
        "status": "ok",
        "output_file": f"/final/{output_filename}",
        "highlights_used": len(used_segments),
        "total_available": len(highlights),
        "score_validation": highlights.validation,
        "min_score": min_score,
        "file_size_mb": file_size_mb,
        "segments_included": used_segments
    }


def auto_generate_async(task_id, min_score, sort_chronological, instructions, rescore):
    """
    Background task: optionally (re)run AI scoring, then build the reel.
    Progress/results land in processing_status[task_id] for the UI to poll.
    """
    try:
        segments = current_scoring_segments()
        settings = llm_scoring.get_llm_settings()
        backend = llm_scoring.get_backend(settings=settings)
        identity = llm_scoring.score_cache_identity(segments, instructions, backend)
        sorted_path = os.path.join(MANIFEST_DIR, "sorted_segments.json")
        highlights = None
        if not rescore:
            try:
                highlights = llm_scoring.load_score_cache(sorted_path, segments, identity)
            except llm_scoring.ScoringError:
                pass

        if highlights is None:
            processing_status[task_id] = {
                "status": "processing", "stage": "scoring", "progress": 10,
                "message": "Scoring segments with AI (this can take a minute)..."
            }
            if not run_scoring_subprocess(task_id, instructions, settings, backend.name):
                return  # status already set to error
            # Fresh partial results remain usable under HL-A3a. They will miss
            # on subsequent automatic reuse; identity and payload still validate.
            highlights = llm_scoring.load_score_cache(sorted_path, segments, identity,
                                                       require_complete=False)

        processing_status[task_id] = {
            "status": "processing", "stage": "building_reel", "progress": 80,
            "message": "Building highlight reel..."
        }
        payload = build_highlight_reel(min_score, sort_chronological, instructions, highlights)
        payload.update({
            "status": "complete", "progress": 100,
            "message": ("Highlight reel ready (partial AI scores)."
                        if payload["score_validation"]["status"] == "partial"
                        else "Highlight reel ready!")
        })
        processing_status[task_id] = payload

    except (ValueError, RuntimeError, llm_scoring.ScoringError) as e:
        processing_status[task_id] = {"status": "error", "message": str(e)}
    except Exception as e:
        import traceback
        traceback.print_exc()
        processing_status[task_id] = {"status": "error", "message": f"Auto-generate failed: {e}"}


@app.route("/api/auto-generate", methods=["POST"])
def auto_export_highlights():
    """
    Kick off AI scoring (subprocess running model_based_processing/scoring.py
    with the required user instructions merged into the prompt) followed by
    reel generation. The scoring backend is resolved by the subprocess
    (HI_LITE_LLM_BACKEND / llm_config.json; see /api/llm-settings).
    Returns a task_id; poll /api/processing-status/<task_id> for progress.
    """
    data = request.json or {}
    min_score = data.get("min_score", 7)
    sort_chronological = data.get("sort_chronological", True)
    instructions = (data.get("instructions") or "").strip()
    rescore = bool(data.get("rescore", False))

    if not instructions:
        return jsonify({
            "status": "error",
            "message": "Instructions are required - describe what counts as a highlight."
        }), 400

    owner = acquire_workspace()
    if owner is None:
        return workspace_busy()

    task_id = None
    worker_started = False
    try:
        task_id = f"autogen_{uuid4().hex}"
        processing_status[task_id] = {
            "status": "processing", "stage": "starting", "progress": 0,
            "message": "Starting auto-generation..."
        }
        thread = threading.Thread(
            target=run_workspace_job,
            args=(owner, task_id, auto_generate_async,
                  (task_id, min_score, sort_chronological, instructions, rescore))
        )
        thread.daemon = True
        thread.start()
        worker_started = True
        return jsonify({"status": "ok", "task_id": task_id})
    except Exception as e:
        message = f"Failed to start auto-generation: {e}"
        if task_id is not None:
            processing_status[task_id] = {"status": "error", "message": message}
        return jsonify({"status": "error", "message": message}), 500
    finally:
        if not worker_started:
            release_workspace(owner)


def _resolve_llm_state():
    """
    Effective LLM settings plus which backend Auto-Generate would use right
    now (None = unavailable). Mirrors scoring.get_backend() without
    constructing a backend, so the local probe runs exactly once.
    """
    settings = llm_scoring.get_llm_settings()
    local_available = llm_scoring.LocalBackend.is_available(settings)
    explicit = (settings["backend"] or "").strip().lower()
    if explicit in ("local", "api", "g4f"):
        resolved = explicit
    elif local_available:
        resolved = "local"
    elif llm_scoring.APIBackend.is_configured(settings):
        resolved = "api"
    else:
        resolved = None
    return settings, resolved, local_available


@app.route("/api/llm-settings", methods=["GET"])
def get_llm_settings():
    """
    Current Auto-Generate backend configuration for the settings UI.
    The API key itself is never returned, only whether one is set.
    """
    settings, resolved, local_available = _resolve_llm_state()
    return jsonify({
        "backend_override": settings["backend"],
        "resolved_backend": resolved,
        "base_url": settings["base_url"],
        "model": settings["model"],
        "has_api_key": bool(settings["api_key"]),
        "local_url": settings["local_url"],
        "local_model": settings["local_model"],
        "local_available": local_available,
    })


@app.route("/api/llm-settings", methods=["POST"])
def save_llm_settings():
    """
    Persist Auto-Generate backend settings to llm_config.json in the data
    dir (server-side on purpose: the API key must not live in localStorage).
    Omitted fields are left unchanged; empty strings reset a field to its
    default; {"clear_api_key": true} removes the stored key.
    """
    data = request.json or {}
    config = llm_scoring.load_config_file()

    for key in ("base_url", "model", "api_key", "local_url", "local_model", "backend"):
        if key in data:
            value = (data.get(key) or "").strip()
            if value:
                config[key] = value
            else:
                config.pop(key, None)

    if data.get("clear_api_key"):
        config.pop("api_key", None)

    backend = config.get("backend")
    if backend and backend not in ("local", "api", "g4f"):
        return jsonify({"status": "error",
                        "message": f"Unknown backend '{backend}'."}), 400

    path = llm_scoring.config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    try:
        os.chmod(path, 0o600)  # best effort; no-op on Windows
    except OSError:
        pass

    settings, resolved, local_available = _resolve_llm_state()
    return jsonify({
        "status": "ok",
        "resolved_backend": resolved,
        "has_api_key": bool(settings["api_key"]),
        "local_available": local_available,
    })


@app.route("/api/llm-test", methods=["POST"])
def test_llm_connection():
    """
    One tiny chat completion against the local or api backend so the user
    can verify their settings before spending a real scoring run on them.
    """
    data = request.json or {}
    settings, resolved, _ = _resolve_llm_state()
    target = (data.get("target") or resolved or "").strip().lower()

    if target not in ("local", "api"):
        return jsonify({
            "status": "error",
            "message": llm_scoring.NO_BACKEND_MESSAGE,
        }), 400

    if target == "local":
        backend = llm_scoring.LocalBackend(settings)
        timeout = 60  # first request may cold-load the model
    else:
        backend = llm_scoring.APIBackend(settings)
        timeout = 30

    try:
        reply = llm_scoring._chat_completion(
            backend.base_url, backend.model,
            "Reply with only the word OK.",
            api_key=backend.api_key, timeout=timeout)
        return jsonify({
            "status": "ok",
            "backend": target,
            "model": backend.model,
            "reply": (reply or "").strip()[:80],
        })
    except llm_scoring.ScoringError as e:
        return jsonify({
            "status": "error",
            "backend": target,
            "category": e.category,
            "message": str(e),
        }), 502


@app.route("/api/discard-reel", methods=["POST"])
def discard_reel():
    """
    Delete a generated highlight reel so the user can retry with a new
    prompt or go back to manual selection.
    Expects {"output_file": "/final/<name>.mp4"} (as returned by the
    export/auto-generate endpoints).
    """
    owner = acquire_workspace()
    if owner is None:
        return workspace_busy()
    try:
        data = request.json or {}
        filename = os.path.basename(data.get("output_file") or "")
        if not filename.endswith(".mp4"):
            return jsonify({"status": "error", "message": "Invalid reel filename."}), 400

        reel_path = os.path.join(HIGHLIGHTS_DIR, filename)
        if not os.path.exists(reel_path):
            return jsonify({"status": "error", "message": f"{filename} not found."}), 404

        try:
            os.remove(reel_path)
        except OSError as e:
            return jsonify({"status": "error", "message": f"Could not delete reel: {e}"}), 500

        # Auto reels also wrote a metadata JSON keyed by the same timestamp.
        match = re.fullmatch(r"auto_highlights_score\d+_(\d{8}_\d{6})\.mp4", filename)
        if match:
            metadata_path = os.path.join(
                SEGMENTS_DIR, f"auto_highlights_metadata_{match.group(1)}.json"
            )
            try:
                if os.path.exists(metadata_path):
                    os.remove(metadata_path)
            except OSError:
                pass  # the reel itself is gone, which is what matters

        return jsonify({"status": "ok"})
    finally:
        release_workspace(owner)



# Optional: Add an endpoint to preview what would be included

@app.route("/api/preview-auto-export", methods=["POST"])
def preview_auto_export():
    """
    Preview what will be included in auto-generated highlight reel
    Returns count and estimated duration without actually creating the video
    """
    data = request.json or {}
    min_score = data.get("min_score", 7)
    
    # Path to your AI-scored highlights
    highlights_base = os.path.dirname(SEGMENTS_DIR)
    scored_segments_path = os.path.join(highlights_base, "sorted_segments.json")
    
    if not os.path.exists(scored_segments_path):
        return jsonify({
            "status": "error", 
            "message": "sorted_segments.json not found. Run AI scoring first."
        }), 404
    
    # Disk contents must pass the same boundary as new provider output.
    try:
        highlights = load_validated_highlights(scored_segments_path, data.get("instructions"))
    except llm_scoring.ScoringError as e:
        return jsonify({"status": "error", "message": str(e), "category": e.category,
                        "score_validation": e.diagnostics}), 400
    
    # Filter by score threshold
    filtered_highlights = [h for h in highlights if h["score"] >= min_score]
    
    if not filtered_highlights:
        return jsonify({
            "status": "error", 
            "message": f"No highlights with score >= {min_score} found"
        }), 400
    
    # Load segments manifest
    if not os.path.exists(SEGMENTS_JSON):
        return jsonify({
            "status": "error", 
            "message": "segments_manifest.json not found"
        }), 404
    
    with open(SEGMENTS_JSON, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    try:
        segments_list = automatic_export_segments(manifest)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    # Calculate estimated duration using ffprobe
    total_duration = 0
    valid_clips = 0
    
    for highlight in filtered_highlights:
        matching_segment = find_manifest_segment(highlight, segments_list)

        if matching_segment:
            video_path = os.path.join(SEGMENTS_DIR, matching_segment["filename"])
            # Convert to absolute path
            video_path = os.path.abspath(video_path)
            
            if os.path.exists(video_path):
                # Use ffprobe to get video duration
                try:
                    cmd = [
                        FFPROBE,
                        '-v', 'error',
                        '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1',
                        video_path
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    
                    if result.returncode == 0 and result.stdout.strip():
                        duration = float(result.stdout.strip())
                        total_duration += duration
                        valid_clips += 1
                    else:
                        # Fallback: use segment duration from manifest
                        duration = matching_segment.get("duration", 0)
                        total_duration += duration
                        valid_clips += 1
                        
                except Exception as e:
                    print(f"Warning: Could not get duration for {video_path}: {e}")
                    # Fallback to manifest duration
                    duration = matching_segment.get("duration", 0)
                    total_duration += duration
                    valid_clips += 1
    
    if valid_clips == 0:
        return jsonify({
            "status": "error",
            "message": "No valid video clips found for preview"
        }), 400
    
    return jsonify({
        "status": "ok",
        "count": valid_clips,
        "total_available": len(highlights),
        "score_validation": highlights.validation,
        "estimated_duration_seconds": round(total_duration, 1),
        "min_score": min_score
    })


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    """Serve the frontend (index.html for root, static files for assets)"""
    import sys
    
    # Check if running as PyInstaller bundle
    is_frozen = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')
    
    if not is_frozen:
        # Development fallback
        if path == '':
            return jsonify({
                "status": "ok",
                "message": "Backend is running. Frontend should be at localhost:5173 in development."
            })
        return jsonify({"error": "Not found"}), 404
    
    # Production: serve from bundled frontend
    frontend_dir = os.path.join(sys._MEIPASS, 'web', 'dist')
    
    # If path is empty, serve index.html
    if path == '':
        file_path = os.path.join(frontend_dir, 'index.html')
    else:
        file_path = os.path.join(frontend_dir, path)
    
    # Security: ensure the file is within frontend_dir
    real_frontend = os.path.realpath(frontend_dir)
    real_file = os.path.realpath(file_path)
    
    if not real_file.startswith(real_frontend):
        return jsonify({"error": "Invalid path"}), 403
    
    # If it's a file, serve it
    if os.path.isfile(file_path):
        directory = os.path.dirname(file_path)
        filename = os.path.basename(file_path)
        return send_from_directory(directory, filename)
    
    # If path doesn't exist and it's not an API route, serve index.html (for client-side routing)
    if not path.startswith('api/') and not path.startswith('data/') and not path.startswith('segments/') and not path.startswith('final/'):
        index_path = os.path.join(frontend_dir, 'index.html')
        if os.path.exists(index_path):
            return send_from_directory(frontend_dir, 'index.html')
    
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    import sys
    import multiprocessing
    multiprocessing.freeze_support()

    # Processing mode: the upload handler re-invokes this same executable with
    # --process-video so the frozen backend.exe can run the pipeline (there is
    # no separate Python interpreter on end-user machines).
    if "--process-video" in sys.argv:
        import main as pipeline
        idx = sys.argv.index("--process-video")
        video_arg = sys.argv[idx + 1]
        output_arg = os.path.join(DATA_DIR, "highlights")
        if "--output" in sys.argv:
            output_arg = sys.argv[sys.argv.index("--output") + 1]
        pipeline.main(video_arg, output_arg)
        sys.exit(0)

    # Scoring mode: run AI highlight scoring instead of serving HTTP.
    # Spawned by /api/auto-generate the same way --process-video is.
    if "--score-highlights" in sys.argv:
        import argparse
        from model_based_processing import scoring

        parser = argparse.ArgumentParser()
        parser.add_argument("--score-highlights", action="store_true")
        parser.add_argument("--transcript", default=os.path.join(MANIFEST_DIR, "transcription.json"))
        parser.add_argument("--output-dir", default=MANIFEST_DIR)
        parser.add_argument("--instructions", required=True)
        score_args = parser.parse_args()

        ok = scoring.run_scoring(
            score_args.transcript, score_args.output_dir, score_args.instructions
        )
        sys.exit(0 if ok else 1)

    # Build-verification mode: succeed only when g4f is NOT importable.
    # verify-app-flow.py runs this against the frozen backend.exe to
    # guarantee the installer ships without g4f (dev-only, never bundled).
    if "--verify-no-g4f" in sys.argv:
        try:
            import g4f  # noqa: F401
        except ImportError:
            print("OK: g4f is not bundled")
            sys.exit(0)
        print("FAIL: g4f is importable from this executable")
        sys.exit(1)

    # Bind to 0.0.0.0 to accept connections from outside Docker container
    # Use use_reloader=False in Docker to prevent process forking that resets global state

    # Disable reloader when running as PyInstaller bundle
    is_frozen = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')
    
    # Die with the parent (Electron) even if it never runs its quit handlers
    start_parent_watchdog()

    port = int(os.getenv('HI_LITE_PORT', 8000))

    # In production (frozen), disable debug and reloader
    if is_frozen:
        app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
    else:
        # Development mode
        use_reloader = os.getenv('FLASK_USE_RELOADER', 'true').lower() == 'true'
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=use_reloader)
