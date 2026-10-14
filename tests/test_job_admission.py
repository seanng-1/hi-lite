# SPDX-License-Identifier: GPL-3.0-or-later
"""API/job boundaries, with controlled workers and no live providers."""
import copy
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from werkzeug.datastructures import FileStorage


ROOT = Path(__file__).resolve().parents[1]
BUSY = {
    "status": "error",
    "message": "Hi-Lite is busy processing another operation. Please wait for it to finish."
}


class HeldThread:
    """start succeeds, but the test decides when the admitted worker runs."""
    def __init__(self, target, args):
        self.target, self.args = target, args

    def start(self):
        pass

    def finish(self):
        self.target(*self.args)


class FakeProcess:
    def __init__(self, output="", code=0):
        self.stdout = io.StringIO(output)
        self.code = code
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self):
        self.returncode = self.code
        return self.code


class JobAdmission(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.TemporaryDirectory(prefix="hi-lite-a2-")
        cls.addClassCleanup(cls.data.cleanup)
        spec = importlib.util.spec_from_file_location("admission_backend", ROOT / "web_interface.py")
        cls.backend = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"HI_LITE_DATA_DIR": cls.data.name}):
            spec.loader.exec_module(cls.backend)

    def setUp(self):
        self.root = Path(self.data.name)
        self.client = self.backend.app.test_client()
        self.backend.processing_status.clear()
        settings = {"backend": "api", "base_url": "https://fixture.invalid/v1",
                    "api_key": None, "model": "fixture-model",
                    "local_url": "http://127.0.0.1:11434", "local_model": "fixture-local"}
        self.scorer = self.backend.llm_scoring.APIBackend(settings)
        for method, value in (("get_backend", self.scorer), ("get_llm_settings", settings)):
            resolver = patch.object(self.backend.llm_scoring, method, return_value=value)
            resolver.start()
            self.addCleanup(resolver.stop)
        self.assertIsNone(self.backend._workspace_owner)
        self.seed_workspace()

    def tearDown(self):
        self.assertIsNone(self.backend._workspace_owner, "admission leaked")

    def seed_workspace(self):
        b = self.backend
        Path(b.SEGMENTS_DIR).mkdir(parents=True, exist_ok=True)
        self.manifest = {"segments": [{"id": 0, "transcript_id": 0, "filename": "segment_0.mp4", "start": 0,
                                      "end": 1, "duration": 1, "text": "fixture"}]}
        Path(b.SEGMENTS_JSON).write_text(json.dumps(self.manifest))
        Path(b.SEGMENTS_DIR, "segment_0.mp4").write_bytes(b"original clip")
        Path(b.MANIFEST_DIR, "transcription.json").write_text(json.dumps({"segments": [
            {"id": 0, "start": 0, "end": 1, "text": "fixture"}]}))
        self.scores = {"instructions": "fixture", "highlights": [
            {"segment_id": 0, "score": 8, "text": "fixture", "start_time": 0}],
            "cache": {"identity": b.llm_scoring.score_cache_identity(
                self.manifest["segments"], "fixture", self.scorer), "complete": True}}
        for name in ("sorted_segments.json", "scored_segments.json"):
            Path(b.MANIFEST_DIR, name).write_text(json.dumps(self.scores))
        self.reel = Path(b.HIGHLIGHTS_DIR, "auto_highlights_score7_20261004_120000.mp4")
        self.reel.write_bytes(b"original reel")
        self.metadata = Path(b.SEGMENTS_DIR, "auto_highlights_metadata_20261004_120000.json")
        self.metadata.write_text('{"original": true}')

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() else None,
                                                p.stat().st_mtime_ns, p.stat().st_mode)
                for p in self.root.rglob("*")}

    def post(self, operation, client=None):
        client = client or self.client
        if operation == "upload":
            return client.post("/api/upload", data={"video": (io.BytesIO(b"source"), "fixture.mp4")})
        payloads = {"auto-generate": {"instructions": "fixture", "rescore": True},
                    "export": {"selected_ids": [0]},
                    "discard-reel": {"output_file": f"/final/{self.reel.name}"}}
        return client.post(f"/api/{operation}", json=payloads[operation])

    def thread_factory(self, **kwargs):
        thread = HeldThread(**kwargs)
        self.threads.append(thread)
        return thread

    def complete_import(self, video_path, output_folder, task_id):
        self.backend.processing_status[task_id] = {"status": "complete", "message": task_id}

    def complete_auto(self, task_id, *args):
        self.backend.processing_status[task_id] = {"status": "complete", "message": task_id}

    def recovery(self):
        self.threads = []
        with patch.object(self.backend.threading, "Thread", side_effect=self.thread_factory), \
             patch.object(self.backend, "process_video_async", self.complete_import):
            response = self.post("upload")
            self.assertEqual(response.status_code, 200)
            self.threads[-1].finish()
            self.assertEqual(self.status(response)["status"], "complete")
        self.assertIsNone(self.backend._workspace_owner)

    def status(self, response):
        return self.client.get(f"/api/processing-status/{response.json['task_id']}").json

    def assert_conflicts(self):
        before = self.snapshot()
        statuses = copy.deepcopy(self.backend.processing_status)
        owner = self.backend._workspace_owner
        for rejected in ("upload", "auto-generate", "export", "discard-reel"):
            with self.subTest(rejected=rejected):
                response = self.post(rejected)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json, BUSY)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.backend.processing_status, statuses)
                self.assertIs(self.backend._workspace_owner, owner)

    def test_async_conflicts_include_worker_startup_interval(self):
        for admitted in ("upload", "auto-generate"):
            with self.subTest(admitted=admitted):
                self.threads = []
                with patch.object(self.backend.threading, "Thread", side_effect=self.thread_factory), \
                     patch.object(self.backend, "process_video_async", self.complete_import), \
                     patch.object(self.backend, "auto_generate_async", self.complete_auto):
                    response = self.post(admitted)
                    self.assertEqual(response.status_code, 200)
                    self.assert_conflicts()
                    self.assertEqual(len(self.threads), 1)
                    self.threads[0].finish()
                    self.assertEqual(self.status(response)["status"], "complete")
                self.recovery()

    def test_synchronous_export_and_discard_conflicts(self):
        for admitted in ("export", "discard-reel"):
            with self.subTest(admitted=admitted):
                self.seed_workspace()
                entered, proceed = Event(), Event()
                results = []
                original_remove = os.remove

                def write(path, **kwargs):
                    Path(path).write_bytes(b"export in progress")
                    entered.set()
                    self.assertTrue(proceed.wait(5), "export gate timed out")

                def remove(path):
                    if Path(path) == self.reel:
                        entered.set()
                        self.assertTrue(proceed.wait(5), "discard gate timed out")
                    original_remove(path)

                final_clip = Mock()
                final_clip.write_videofile.side_effect = write
                with patch.object(self.backend, "VideoFileClip"), \
                     patch.object(self.backend, "concatenate_videoclips", return_value=final_clip), \
                     patch.object(self.backend.os, "remove", side_effect=remove):
                    thread = Thread(target=lambda: results.append(self.post(admitted, self.backend.app.test_client())))
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(5), "request never reached its mutation")
                        self.assert_conflicts()
                    finally:
                        proceed.set()
                        thread.join(5)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(results[0].status_code, 200)
                if admitted == "discard-reel":
                    self.assertFalse(self.reel.exists())
                    self.assertFalse(self.metadata.exists())
                self.recovery()

    def test_import_acquires_before_saving_the_source(self):
        entered, proceed = Event(), Event()
        results = []
        self.threads = []
        original_save = FileStorage.save

        def save(file, path, *args, **kwargs):
            entered.set()
            self.assertTrue(proceed.wait(5))
            original_save(file, path, *args, **kwargs)

        with patch.object(FileStorage, "save", save), \
             patch.object(self.backend.threading, "Thread", side_effect=self.thread_factory), \
             patch.object(self.backend, "process_video_async", self.complete_import):
            thread = Thread(target=lambda: results.append(self.post("upload", self.backend.app.test_client())))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assert_conflicts()
            finally:
                proceed.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0].status_code, 200)
            self.threads[0].finish()
        self.recovery()

    def test_thread_construction_and_start_failures_release(self):
        for operation in ("upload", "auto-generate"):
            for point in ("construct", "start"):
                with self.subTest(operation=operation, point=point):
                    with patch.object(self.backend.threading, "Thread") as factory:
                        if point == "construct":
                            factory.side_effect = RuntimeError("thread construction failed")
                        else:
                            factory.return_value.start.side_effect = RuntimeError("thread start failed")
                        response = self.post(operation)
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(response.json["status"], "error")
                    self.assertEqual(list(self.backend.processing_status.values())[-1]["status"], "error")
                    self.recovery()

    def test_upload_preworker_mutation_failures_release(self):
        for point in ("save", "upload-directory", "output-directory"):
            with self.subTest(point=point):
                original_makedirs = os.makedirs

                def makedirs(path, *args, **kwargs):
                    failed = self.backend.UPLOAD_FOLDER if point == "upload-directory" else self.backend.MANIFEST_DIR
                    if str(path) == failed:
                        raise OSError("directory failure")
                    return original_makedirs(path, *args, **kwargs)

                if point == "save":
                    failure = patch.object(FileStorage, "save", side_effect=PermissionError("save failed"))
                else:
                    failure = patch.object(os, "makedirs", side_effect=makedirs)
                with failure:
                    response = self.post("upload")
                self.assertEqual(response.status_code, 500)
                self.recovery()

    def test_uncaught_worker_exceptions_release(self):
        for operation, worker in (("upload", "process_video_async"), ("auto-generate", "auto_generate_async")):
            with self.subTest(operation=operation):
                self.threads = []
                with patch.object(self.backend.threading, "Thread", side_effect=self.thread_factory), \
                     patch.object(self.backend, worker, side_effect=RuntimeError("worker failed")):
                    response = self.post(operation)
                    self.threads[0].finish()
                self.assertEqual(self.status(response)["status"], "error")
                self.recovery()

    def test_import_subprocess_and_processing_failures_release(self):
        cases = [("nonzero", "failure", 1), ("transcription", "Transcription failed", 0),
                 ("segments", "No segments extracted", 0), ("manifest", "", 0),
                 ("spawn", "", 0), ("stream", "", 0)]
        for name, output, code in cases:
            with self.subTest(name=name):
                self.seed_workspace()
                self.threads = []
                process = FakeProcess(output, code)
                if name == "stream":
                    def broken_stream():
                        raise OSError("stdout failed")
                        yield
                    process.stdout = broken_stream()
                with patch.object(self.backend.threading, "Thread", side_effect=self.thread_factory), \
                     patch.object(self.backend.subprocess, "Popen", return_value=process) as popen:
                    if name == "spawn":
                        popen.side_effect = OSError("spawn failed")
                    response = self.post("upload")
                    self.threads[0].finish()
                self.assertEqual(self.status(response)["status"], "error")
                if name == "stream":
                    self.assertTrue(process.killed)
                    self.assertIsNotNone(process.returncode)
                self.assertIsNone(self.backend.active_pipeline_process)
                self.recovery()

    def test_auto_processing_and_subprocess_failures_release(self):
        for name in ("transcript", "scoring", "no-success", "spawn", "stream", "ffmpeg", "ffmpeg-spawn", "build"):
            with self.subTest(name=name):
                self.seed_workspace()
                self.threads = []
                output = "ERROR: scoring failed\n" if name == "scoring" else "Scoring complete\n"
                process = FakeProcess(output, 1 if name == "scoring" else 0)
                if name == "no-success":
                    process.stdout = io.StringIO("")
                if name == "stream":
                    def broken_stream():
                        raise OSError("stdout failed")
                        yield
                    process.stdout = broken_stream()
                if name == "transcript":
                    Path(self.backend.MANIFEST_DIR, "transcription.json").unlink()
                if name == "build":
                    Path(self.backend.MANIFEST_DIR, "sorted_segments.json").unlink()
                with patch.object(self.backend.threading, "Thread", side_effect=self.thread_factory), \
                     patch.object(self.backend.subprocess, "Popen", return_value=process) as popen, \
                     patch.object(self.backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "ffmpeg failed")) as run:
                    if name == "spawn":
                        popen.side_effect = OSError("spawn failed")
                    if name == "ffmpeg-spawn":
                        run.side_effect = OSError("ffmpeg launch failed")
                    response = self.post("auto-generate")
                    self.threads[0].finish()
                self.assertEqual(self.status(response)["status"], "error")
                if name == "stream":
                    self.assertTrue(process.killed)
                    self.assertIsNotNone(process.returncode)
                self.assertIsNone(self.backend.active_pipeline_process)
                self.recovery()

    def test_sync_error_returns_and_exceptions_release(self):
        response = self.client.post("/api/export", json={"selected_ids": []})
        self.assertEqual(response.status_code, 400)
        self.recovery()
        Path(self.backend.SEGMENTS_JSON).unlink()
        self.assertEqual(self.post("export").status_code, 404)
        self.recovery()
        self.seed_workspace()
        Path(self.backend.SEGMENTS_DIR, "segment_0.mp4").unlink()
        self.assertEqual(self.post("export").status_code, 400)
        self.recovery()
        self.seed_workspace()
        with patch.object(self.backend, "VideoFileClip", side_effect=RuntimeError("clip failed")):
            self.assertEqual(self.post("export").status_code, 500)
        self.recovery()
        final_clip = Mock()
        final_clip.write_videofile.side_effect = RuntimeError("export failed")
        with patch.object(self.backend, "VideoFileClip"), \
             patch.object(self.backend, "concatenate_videoclips", return_value=final_clip):
            self.assertEqual(self.post("export").status_code, 500)
        self.recovery()
        self.assertEqual(self.client.post("/api/discard-reel", json={}).status_code, 400)
        self.recovery()
        self.reel.unlink()
        self.assertEqual(self.post("discard-reel").status_code, 404)
        self.recovery()
        self.seed_workspace()
        with patch.object(self.backend.os, "remove", side_effect=OSError("delete failed")):
            self.assertEqual(self.post("discard-reel").status_code, 500)
        self.recovery()

    def test_uuid_ids_and_independent_status_with_frozen_time(self):
        self.threads = []
        seen = {}
        with patch.object(self.backend.threading, "Thread", side_effect=self.thread_factory), \
             patch.object(self.backend, "process_video_async", self.complete_import), \
             patch.object(self.backend, "auto_generate_async", self.complete_auto), \
             patch.object(self.backend, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 4, 12, 0, 0)
            for operation in ("upload", "auto-generate") * 4:
                response = self.post(operation)
                self.assertEqual(response.status_code, 200)
                task = response.json["task_id"]
                self.assertNotIn(task, seen)
                self.assertEqual(UUID(task.split("_", 1)[1]).version, 4)
                self.threads[-1].finish()
                seen[task] = self.status(response)
                for old_task, status in seen.items():
                    self.assertEqual(self.client.get(f"/api/processing-status/{old_task}").json, status)
        self.assertEqual(len(seen), 8)
        self.assertEqual(self.client.get("/api/processing-status/unknown").status_code, 404)

    def test_old_completion_cannot_release_or_overwrite_new_task(self):
        self.threads = []
        with patch.object(self.backend.threading, "Thread", side_effect=self.thread_factory), \
             patch.object(self.backend, "process_video_async", self.complete_import):
            old = self.post("upload")
            old_owner = self.threads[-1].args[0]
            self.threads[-1].finish()
            new = self.post("upload")
            owner = self.backend._workspace_owner
            status = self.status(new)
            self.backend.run_workspace_job(old_owner, old.json["task_id"], self.complete_import,
                                           ("unused", "unused", old.json["task_id"]))
            self.assertIs(self.backend._workspace_owner, owner)
            self.assertEqual(self.status(new), status)
            self.assert_conflicts()
            self.threads[-1].finish()
        self.recovery()

    def test_actual_scoring_to_export_holds_one_owner(self):
        scoring, continue_scoring, exporting, continue_export = Event(), Event(), Event(), Event()
        threads = []

        def factory(**kwargs):
            thread = Thread(**kwargs)
            threads.append(thread)
            return thread

        def score_output():
            scoring.set()
            self.assertTrue(continue_scoring.wait(5))
            Path(self.backend.MANIFEST_DIR, "sorted_segments.json").write_text(json.dumps(self.scores))
            yield "Scoring complete\n"

        process = FakeProcess()
        process.stdout = score_output()

        def export(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"generated reel")
            exporting.set()
            self.assertTrue(continue_export.wait(5))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(self.backend.threading, "Thread", side_effect=factory), \
             patch.object(self.backend.subprocess, "Popen", return_value=process), \
             patch.object(self.backend.subprocess, "run", side_effect=export):
            response = self.post("auto-generate")
            try:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(scoring.wait(5))
                owner = self.backend._workspace_owner
                self.assert_conflicts()
                continue_scoring.set()
                self.assertTrue(exporting.wait(5))
                self.assertIs(self.backend._workspace_owner, owner)
                self.assert_conflicts()
            finally:
                continue_scoring.set()
                continue_export.set()
                for thread in threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
        result = self.status(response)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["highlights_used"], 1)
        self.assertEqual(self.client.get(result["output_file"]).data, b"generated reel")
        self.recovery()

    def test_cached_generation_still_owns_export(self):
        self.threads = []

        def export(cmd, **kwargs):
            self.assert_conflicts()
            Path(cmd[-1]).write_bytes(b"cached generation")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(self.backend.threading, "Thread", side_effect=self.thread_factory), \
             patch.object(self.backend.subprocess, "Popen") as scoring, \
             patch.object(self.backend.subprocess, "run", side_effect=export):
            response = self.client.post("/api/auto-generate", json={"instructions": "fixture"})
            self.assertEqual(response.status_code, 200)
            self.threads[0].finish()
            scoring.assert_not_called()
        self.assertEqual(self.status(response)["status"], "complete")
        self.recovery()


if __name__ == "__main__":
    unittest.main()
