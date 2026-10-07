# SPDX-License-Identifier: GPL-3.0-or-later
"""API/producer checks; expensive video processing is stubbed."""
import copy
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


FIXTURE = Path(__file__).parent / "fixtures" / "segments.json"


class ManifestContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.TemporaryDirectory(prefix="hi-lite-a1-")
        cls.addClassCleanup(cls.data.cleanup)
        with patch.dict(os.environ, {"HI_LITE_DATA_DIR": cls.data.name}):
            cls.backend = importlib.import_module("web_interface")
        cls.analyzer = importlib.import_module("highlight_analyzer")
        cls.manifest = Path(cls.backend.SEGMENTS_JSON)
        cls.client = cls.backend.app.test_client()

    def setUp(self):
        self.manifest.unlink(missing_ok=True)
        self.backend.processing_status.clear()
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.fixture["segments"] = [{**s, "transcript_id": s["id"]}
                                    for s in self.fixture["segments"]]

    def test_missing_manifest_is_normal_empty_state(self):
        response = self.client.get("/data/segments_manifest.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"segments": []})

    def test_manifest_endpoint_delivers_data_for_frontend_validation(self):
        # The serving boundary intentionally delivers bytes; the UI validates
        # their structure before rendering. Include an empty file and bad JSON.
        for contents in ("", "{broken", "[]", "null", '{"segments": []}',
                         '{"segments": [null]}', json.dumps(self.fixture)):
            with self.subTest(contents=contents):
                self.manifest.write_text(contents, encoding="utf-8")
                response = self.client.get("/data/segments_manifest.json")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_data(as_text=True), contents)

    def test_producer_sets_boolean_flags_by_id_without_mutating_input(self):
        segments = self.fixture["segments"]
        # Stale input flags must not override the existing analyzer's matches.
        segments[1]["is_keyword"] = True
        original = copy.deepcopy(segments)
        self.analyzer.save_segment_manifest(segments, self.backend.MANIFEST_DIR)
        manifest = self.client.get("/data/segments_manifest.json").json
        self.assertEqual(segments, original)
        self.assertEqual([s["id"] for s in manifest["keyword_highlights"]], [0, 9])
        self.assertEqual([s["is_keyword"] for s in manifest["segments"]], [True, False, True])
        self.assertTrue(all(type(s["is_keyword"]) is bool for s in manifest["segments"]))
        self.assertEqual(manifest["total_segments"], 3)
        self.assertEqual(manifest["total_duration"], 4.5)
        self.assertEqual([s["text"] for s in manifest["segments"]], [s["text"] for s in original])
        self.assertEqual([s["transcript_id"] for s in manifest["segments"]], [0, 4, 9])
        self.assertEqual([s["transcript_id"] for s in manifest["keyword_highlights"]], [0, 9])

    def test_empty_producer_manifest(self):
        self.analyzer.save_segment_manifest([], self.backend.MANIFEST_DIR)
        manifest = self.client.get("/data/segments_manifest.json").json
        self.assertEqual(manifest["segments"], [])
        self.assertEqual(manifest["keyword_highlights"], [])
        self.assertEqual(manifest["total_duration"], 0)

    def test_upload_status_and_ui_endpoints(self):
        def process_fixture(video_path, output_folder, task_id):
            self.assertEqual(output_folder, self.backend.MANIFEST_DIR)
            self.assertEqual(Path(video_path).read_bytes(), b"short fixture")
            self.analyzer.save_segment_manifest(self.fixture["segments"], output_folder)
            (Path(self.backend.SEGMENTS_DIR) / "segment_0.mp4").write_bytes(b"fixture clip")
            self.backend.processing_status[task_id] = {"status": "complete", "progress": 100}

        # Exercise the real upload route, with only the background worker stubbed.
        with patch.object(self.backend, "process_video_async", process_fixture), \
             patch.object(self.backend.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"](
                *thread.call_args.kwargs["args"])
            response = self.client.post("/api/upload", data={
                "video": (io.BytesIO(b"short fixture"), "fixture.mp4")
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")
        task_id = response.json["task_id"]
        self.assertEqual(self.client.get(f"/api/processing-status/{task_id}").json["status"], "complete")
        manifest = self.client.get("/data/segments_manifest.json").json
        self.assertEqual([s["id"] for s in manifest["segments"] if s["is_keyword"]], [0, 9])
        self.assertEqual(self.client.get("/segments/segment_0.mp4").data, b"fixture clip")


if __name__ == "__main__":
    unittest.main()
