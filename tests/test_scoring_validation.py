# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual backend/consumer paths, without live or paid providers."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

from model_based_processing import scoring


ROOT = Path(__file__).resolve().parents[1]
SEGMENTS = json.loads((ROOT / "tests/fixtures/segments.json").read_text())["segments"]
SETTINGS = {
    "base_url": "https://provider.invalid/v1", "api_key": "secret-test-key",
    "model": "fixture-model", "local_url": "http://127.0.0.1:11434",
    "local_model": "fixture-local",
}


def mixed_records():
    return [
        {"segment_id": 0, "score": 4, "reason": "first"},
        {"segment_id": "4", "score": "9"},  # The public prompt requires numbers.
        {"segment_id": 4, "score": None},
        {"segment_id": 999, "score": 10, "start_time": 0},
        {"segment_id": "0", "score": 8, "reason": "winner"},
        None, ["bad record"],
        {"segment_id": 9, "score": 7.5},
        {"segment_id": 0, "score": 6},
        {"segment_id": 0, "score": 8, "reason": "later tie"},
    ]


class ScoreValidation(unittest.TestCase):
    def test_numeric_bounds_and_decimals_normalize_to_float(self):
        result = scoring.enrich_results([
            {"segment_id": 0, "score": 1}, {"segment_id": "4", "score": 10},
            {"segment_id": 9, "score": 6.25},
        ], SEGMENTS)
        self.assertEqual([r["score"] for r in result], [10.0, 6.25, 1.0])
        self.assertTrue(all(type(r["score"]) is float for r in result))
        self.assertEqual(result.validation["status"], "complete")
        self.assertEqual(result.validation["missing_segment_ids"], [])
        self.assertEqual(result[0]["segment_id"], 4)

    def test_invalid_scores_do_not_reach_sorting(self):
        invalid = [None, True, False, "", "bad", "8", "8.5", " 8 ", "1e0",
                   float("nan"), float("inf"), -float("inf"), -3, 0, 0.99,
                   10.5, 999, 10**400, [], {}, [8]]
        for value in invalid:
            with self.subTest(value=value):
                result = scoring.enrich_results([
                    {"segment_id": 0, "score": value}, {"segment_id": 4, "score": 7},
                ], SEGMENTS)
                self.assertEqual([r["segment_id"] for r in result], [4])
                self.assertEqual(result.validation["rejected_records"], 1)
                self.assertEqual(result.validation["status"], "partial")
                with self.assertRaisesRegex(scoring.ScoringError, "No usable AI scores"):
                    scoring.enrich_results([{"segment_id": 0, "score": value}], SEGMENTS)

    def test_missing_fields_and_malformed_records(self):
        invalid = [{"segment_id": 0}, {"score": 8}, None, 4, "record", [], True]
        result = scoring.enrich_results(invalid + [{"segment_id": 9, "score": 6}], SEGMENTS)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.validation["rejected_records"], len(invalid))
        self.assertIn("missing_score", result.validation["rejection_counts"])
        self.assertIn("missing_id", result.validation["rejection_counts"])

    def test_only_exact_ids_resolve_and_no_guessing(self):
        for index, sid in enumerate((None, True, False, 0.0, -1, [], {}, "", " 0", "0 ", "00",
                                     "unknown", 999, 10**5000)):
            with self.subTest(case=index):
                with self.assertRaises(scoring.ScoringError):
                    scoring.enrich_results([{"segment_id": sid, "score": 8,
                                             "start_time": 0, "text": SEGMENTS[0]["text"]}], SEGMENTS)
        string_segments = [{"id": "abc123", "start": 0, "end": 1, "text": "fixture"}]
        self.assertEqual(scoring.enrich_results([
            {"segment_id": "abc123", "score": 8}], string_segments)[0]["segment_id"], "abc123")

    def test_ambiguous_transcript_ids_are_rejected(self):
        for duplicate_id in (0, "0"):
            with self.subTest(duplicate_id=duplicate_id):
                transcript = SEGMENTS + [{**SEGMENTS[0], "id": duplicate_id, "text": "other"}]
                result = scoring.enrich_results([
                    {"segment_id": 0, "score": 10}, {"segment_id": 4, "score": 6}], transcript)
                self.assertEqual([r["segment_id"] for r in result], [4])
                self.assertEqual(result.validation["rejection_counts"], {"ambiguous_id": 1})
                self.assertEqual(result.validation["status"], "partial")
        transcript = [SEGMENTS[0], {**SEGMENTS[1], "id": True}]
        result = scoring.enrich_results([{"segment_id": 0, "score": 8}], transcript)
        self.assertEqual(result.validation["invalid_transcript_id_count"], 1)
        self.assertEqual(result.validation["status"], "partial")

    def test_highest_duplicate_and_first_tie_keep_stable_order(self):
        records = [
            {"segment_id": 0, "score": 4}, {"segment_id": 4, "score": 8},
            {"segment_id": "0", "score": 8, "reason": "winner"},
            {"segment_id": 0, "score": 6}, {"segment_id": 0, "score": 8, "reason": "tie"},
            {"segment_id": 0, "score": 999},
        ]
        original = copy.deepcopy(records)
        result = scoring.enrich_results(records, SEGMENTS)
        self.assertEqual([r["segment_id"] for r in result], [0, 4])
        self.assertEqual(result[0]["score"], 8)
        self.assertEqual(result[0]["reason"], "winner")
        self.assertEqual(result.validation["duplicate_records"], 3)
        self.assertEqual(result.validation["rejected_records"], 1)
        self.assertEqual(records, original)

    def test_provider_fields_cannot_override_transcript_data(self):
        result = scoring.enrich_results([{"segment_id": 0, "score": 8,
                                         "start_time": "bad", "end_time": float("inf"),
                                         "text": "wrong", "reason": {"bad": True}}], SEGMENTS)
        self.assertEqual(result[0]["start_time"], SEGMENTS[0]["start"])
        self.assertEqual(result[0]["end_time"], SEGMENTS[0]["end"])
        self.assertEqual(result[0]["text"], SEGMENTS[0]["text"])
        self.assertEqual(result[0]["reason"], "No reason provided")

    def test_partial_and_unusable_have_bounded_diagnostics(self):
        segments = [{"id": i, "start": i, "end": i+1, "text": "fixture"} for i in range(30)]
        result = scoring.enrich_results([None]*30 + [{"segment_id": 0, "score": 8}], segments)
        self.assertEqual(result.validation["status"], "partial")
        self.assertEqual(result.validation["missing_segment_count"], 29)
        self.assertEqual(result.validation["missing_segment_ids"], list(range(1, 11)))
        self.assertEqual(len(result.validation["rejection_examples"]), 10)
        with self.assertRaises(scoring.ScoringError) as failure:
            scoring.enrich_results([{"score": "secret-test-key"}]*30, segments)
        self.assertEqual(failure.exception.category, "parse")
        self.assertEqual(failure.exception.diagnostics["status"], "unusable")
        self.assertNotIn("secret-test-key", str(failure.exception))
        self.assertNotIn("secret-test-key", json.dumps(failure.exception.diagnostics))

    def test_invalid_list_envelopes_and_empty_results(self):
        for records in (None, {}, "bad", 8, True):
            with self.subTest(records=records):
                with self.assertRaisesRegex(scoring.ScoringError, "list"):
                    scoring.enrich_results(records, SEGMENTS)
        with self.assertRaisesRegex(scoring.ScoringError, "No usable AI scores"):
            scoring.enrich_results([], SEGMENTS)


class PublicBackendPath(unittest.TestCase):
    def api_response(self, content):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": content}}]}
        return response

    def test_api_http_parse_validate_enrich_sort_mixed_response(self):
        raw = 'Response:\n```json\n' + json.dumps({"segments": mixed_records()}) + '\n```'
        with patch("requests.post", return_value=self.api_response(raw)) as network:
            result = scoring.APIBackend(SETTINGS).score(SEGMENTS, "fixture", "great plays")
        network.assert_called_once()
        self.assertEqual(network.call_args.args[0], SETTINGS["base_url"] + "/chat/completions")
        self.assertEqual([r["segment_id"] for r in result], [0, 9])
        self.assertEqual([r["score"] for r in result], [8.0, 7.5])
        self.assertEqual(result[0]["reason"], "winner")
        self.assertEqual(result.validation["missing_segment_ids"], [4])
        self.assertEqual(result.validation["status"], "partial")
        self.assertEqual(result.validation["rejected_records"], 5)

    def test_local_uses_the_same_validation_boundary(self):
        raw = json.dumps({"segments": mixed_records()})
        with patch("requests.post", return_value=self.api_response(raw)) as network:
            result = scoring.LocalBackend(SETTINGS).score(SEGMENTS, "fixture", "great plays")
        self.assertEqual(network.call_args.args[0], SETTINGS["local_url"] + "/v1/chat/completions")
        self.assertEqual(network.call_args.kwargs["timeout"], 600)
        self.assertEqual([r["score"] for r in result], [8.0, 7.5])

    def test_complete_public_response_has_complete_coverage(self):
        raw = json.dumps({"segments": [{"segment_id": sid, "score": value}
                                       for sid, value in ((0, 1), (4, 10), (9, 6.25))]})
        with patch("requests.post", return_value=self.api_response(raw)):
            result = scoring.APIBackend(SETTINGS).score(SEGMENTS, "fixture", "great plays")
        self.assertEqual(result.validation["status"], "complete")
        self.assertEqual(result.validation["missing_segment_count"], 0)
        self.assertEqual([r["score"] for r in result], [10.0, 6.25, 1.0])

    def test_public_path_rejects_wholly_invalid_records_without_retry(self):
        for records in ([], [{"segment_id": 0, "score": None}], [{"segment_id": 999, "score": 8}],
                        [None, {"segment_id": 0, "score": "8"}]):
            with self.subTest(records=records):
                with patch("requests.post", return_value=self.api_response(json.dumps({"segments": records}))) as network:
                    with self.assertRaisesRegex(scoring.ScoringError, "No usable AI scores"):
                        scoring.APIBackend(SETTINGS).score(SEGMENTS, "fixture", "great plays")
                network.assert_called_once()

    def test_malformed_model_envelopes_fail_clearly_after_existing_format_retry(self):
        for raw in (None, [], {}, 8, "", "secret-test-key", "{broken", "null", "[]",
                    '{"segments": null}', '{"segments": {}}', '{}',
                    '{"segments": {"nested": ' + '['*5000 + 'null' + ']'*5000 + '}}',
                    '{"segments": [{"segment_id": ' + '9'*5000 + ', "score": 8}]}'):
            with self.subTest(raw_type=type(raw).__name__, length=len(raw) if isinstance(raw, str) else 0):
                with patch("requests.post", return_value=self.api_response(raw)) as network:
                    with self.assertRaises(scoring.ScoringError) as failure:
                        scoring.APIBackend(SETTINGS).score(SEGMENTS, "fixture", "great plays")
                self.assertEqual(failure.exception.category, "parse")
                self.assertNotIn("secret-test-key", str(failure.exception))
                self.assertEqual(network.call_count, 2)

    def test_malformed_http_response_envelope(self):
        for raw in (None, [], {}, {"choices": []}, {"choices": [{"message": None}]}):
            with self.subTest(raw=raw):
                response = Mock(status_code=200)
                response.json.return_value = raw
                with patch("requests.post", return_value=response):
                    with self.assertRaisesRegex(scoring.ScoringError, "chat-completions format"):
                        scoring.APIBackend(SETTINGS).score(SEGMENTS, "fixture", "great plays")

    def test_g4f_extension_reuses_boundary_without_installing_library(self):
        backend = object.__new__(scoring.G4FBackend)
        backend._g4f = SimpleNamespace(Provider=SimpleNamespace())
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"segments": mixed_records()})))])
        backend.client = Mock()
        backend.client.chat.completions.create.return_value = response
        result = backend.score(SEGMENTS, "fixture", "great plays")
        self.assertEqual([r["score"] for r in result], [8.0, 7.5])
        self.assertEqual(result.validation["status"], "partial")

    def test_scoring_entry_point_persists_partial_coverage_and_only_valid_scores(self):
        with tempfile.TemporaryDirectory(prefix="hi-lite-scoring-") as directory:
            transcript = Path(directory, "transcription.json")
            transcript.write_text(json.dumps({"segments": SEGMENTS}))
            backend = scoring.APIBackend(SETTINGS)
            with patch.object(scoring, "get_backend", return_value=backend), \
                 patch("requests.post", return_value=self.api_response(json.dumps({"segments": mixed_records()}))):
                self.assertTrue(scoring.run_scoring(str(transcript), directory, "great plays"))
            for filename in ("scored_segments.json", "sorted_segments.json"):
                payload = json.loads(Path(directory, filename).read_text())
                self.assertEqual(payload["validation"]["status"], "partial")
                self.assertEqual(payload["validation"]["missing_segment_ids"], [4])
                self.assertEqual([r["segment_id"] for r in payload["highlights"]], [0, 9])
                self.assertEqual(len(payload["highlights"]), 2)

    def test_scoring_entry_point_clear_failure_without_persisting_unusable_scores(self):
        with tempfile.TemporaryDirectory(prefix="hi-lite-scoring-") as directory:
            transcript = Path(directory, "transcription.json")
            transcript.write_text(json.dumps({"segments": SEGMENTS}))
            output = io.StringIO()
            with patch.object(scoring, "get_backend", return_value=scoring.APIBackend(SETTINGS)), \
                 patch("requests.post", return_value=self.api_response('{"segments": [null]}')), \
                 redirect_stdout(output):
                self.assertFalse(scoring.run_scoring(str(transcript), directory, "great plays"))
            self.assertIn("ERROR: No usable AI scores", output.getvalue())
            self.assertFalse(Path(directory, "sorted_segments.json").exists())

    def test_custom_backend_plain_list_is_validated_before_entry_point_consumers(self):
        with tempfile.TemporaryDirectory(prefix="hi-lite-scoring-") as directory:
            transcript = Path(directory, "transcription.json")
            transcript.write_text(json.dumps({"segments": SEGMENTS}))
            backend = Mock(name="custom")
            backend.name = "custom"
            backend.score.return_value = mixed_records()
            with patch.object(scoring, "get_backend", return_value=backend):
                self.assertTrue(scoring.run_scoring(str(transcript), directory, "great plays"))
            payload = json.loads(Path(directory, "sorted_segments.json").read_text())
            self.assertEqual(len(payload["highlights"]), 2)
            self.assertEqual(payload["validation"]["status"], "partial")


CLIPS = [{**s, "transcript_id": s["id"]} for s in SEGMENTS]


class CachedScoreConsumers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.TemporaryDirectory(prefix="hi-lite-score-consumers-")
        cls.addClassCleanup(cls.data.cleanup)
        spec = importlib.util.spec_from_file_location("score_validation_backend", ROOT / "web_interface.py")
        cls.backend = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"HI_LITE_DATA_DIR": cls.data.name}):
            spec.loader.exec_module(cls.backend)

    def setUp(self):
        b = self.backend
        self.client = b.app.test_client()
        self.assertIsNone(b._workspace_owner)
        b.processing_status.clear()
        self.scorer = scoring.APIBackend(SETTINGS)
        resolver = patch.object(scoring, "get_backend", return_value=self.scorer)
        resolver.start()
        self.addCleanup(resolver.stop)
        Path(b.SEGMENTS_DIR).mkdir(parents=True, exist_ok=True)
        self.score_path = Path(b.MANIFEST_DIR, "sorted_segments.json")
        self.transcript_path = Path(b.MANIFEST_DIR, "transcription.json")
        self.transcript_path.write_text(json.dumps({"segments": SEGMENTS}))
        Path(b.SEGMENTS_JSON).write_text(json.dumps({"segments": CLIPS}))
        for segment in SEGMENTS:
            Path(b.SEGMENTS_DIR, segment["filename"]).write_bytes(b"fixture clip")
        # Publish the validated records, including complete coverage at a low score.
        self.write_scores(scoring.enrich_results(
            mixed_records() + [{"segment_id": 4, "score": 3}], SEGMENTS))

    def tearDown(self):
        self.assertIsNone(self.backend._workspace_owner)

    def write_scores(self, records, complete=True):
        self.score_path.write_text(json.dumps({"instructions": "fixture", "highlights": records,
                                               "validation": {"status": "complete"},
                                               "cache": {"identity": scoring.score_cache_identity(
                                                   SEGMENTS, "fixture", self.scorer),
                                                   "complete": complete}}))

    def fake_media_tool(self, cmd, **kwargs):
        if cmd[0] == self.backend.FFPROBE:
            return subprocess.CompletedProcess(cmd, 0, "1.5", "")
        self.concat_paths = Path(cmd[cmd.index("-i")+1]).read_text().splitlines()
        Path(cmd[-1]).write_bytes(b"generated reel")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def test_cache_revalidated_before_preview_stats_thresholding_and_export(self):
        with patch.object(self.backend.subprocess, "run", side_effect=self.fake_media_tool):
            preview = self.client.post("/api/preview-auto-export", json={"min_score": 7, "instructions": "fixture"})
            reel = self.backend.build_highlight_reel(7, False, "fixture")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json["count"], 2)
        self.assertEqual(preview.json["total_available"], 3)
        self.assertEqual(preview.json["estimated_duration_seconds"], 3)
        self.assertEqual(preview.json["score_validation"]["status"], "complete")
        self.assertEqual(reel["highlights_used"], 2)
        self.assertEqual([r["segment_id"] for r in reel["segments_included"]], [0, 9])
        self.assertEqual([r["score"] for r in reel["segments_included"]], [8.0, 7.5])
        self.assertEqual(len(self.concat_paths), 2)
        self.assertNotEqual(self.concat_paths[0], self.concat_paths[1])
        self.assertEqual(reel["score_validation"], preview.json["score_validation"])
        self.assertEqual(self.client.get(reel["output_file"]).data, b"generated reel")

    def test_threshold_uses_highest_validated_duplicate_and_excludes_low_scores(self):
        with patch.object(self.backend.subprocess, "run", side_effect=self.fake_media_tool):
            preview = self.client.post("/api/preview-auto-export", json={"min_score": 8, "instructions": "fixture"})
            reel = self.backend.build_highlight_reel(8, True, "fixture")
        self.assertEqual(preview.json["count"], 1)
        self.assertEqual([r["segment_id"] for r in reel["segments_included"]], [0])

    def test_old_caches_with_invalid_numbers_cannot_bypass_validation(self):
        for value in (None, "8", True, float("nan"), float("inf"), -float("inf"), 0, 10.5, [], {}):
            with self.subTest(value=value):
                self.write_scores([{"segment_id": 0, "score": value}, {"segment_id": 9, "score": 8}])
                with patch.object(self.backend.subprocess, "run", side_effect=self.fake_media_tool):
                    preview = self.client.post("/api/preview-auto-export", json={"min_score": 7, "instructions": "fixture"})
                    # The structural boundary still drops bad scores, but this
                    # incomplete/invalid disk payload is not an applicable cache.
                    structural = scoring.load_scored_results(str(self.score_path), SEGMENTS)
                    with self.assertRaises(scoring.ScoringError):
                        self.backend.build_highlight_reel(7, True, "fixture")
                self.assertEqual([r["segment_id"] for r in structural], [9])
                self.assertEqual(preview.status_code, 400)

    def test_malformed_cache_envelopes_and_unusable_records_fail_before_media_tools(self):
        for contents in ("{broken", "null", "[]", '{}', '{"highlights": null}',
                         '{"highlights": {}}', '{"highlights": []}',
                         '{"highlights": [null, {"segment_id": 999, "score": 8, "start_time": 0}]}'):
            with self.subTest(contents=contents):
                self.score_path.write_text(contents)
                with patch.object(self.backend.subprocess, "run") as media:
                    preview = self.client.post("/api/preview-auto-export", json={"min_score": 7, "instructions": "fixture"})
                    with self.assertRaises(scoring.ScoringError):
                        self.backend.build_highlight_reel(7, True, "fixture")
                media.assert_not_called()
                self.assertEqual(preview.status_code, 400)
                self.assertEqual(preview.json["status"], "error")
                self.assertEqual(preview.json["category"], "parse")

    def test_missing_or_malformed_transcript_cannot_fall_back_to_manifest_identity(self):
        for contents in (None, "null", "{broken", '{"segments": []}', '{"segments": {}}'):
            with self.subTest(contents=contents):
                if contents is None:
                    self.transcript_path.unlink()
                else:
                    self.transcript_path.write_text(contents)
                with patch.object(self.backend.subprocess, "run") as media:
                    response = self.client.post("/api/preview-auto-export", json={"min_score": 7, "instructions": "fixture"})
                self.assertEqual(response.status_code, 400)
                media.assert_not_called()

    def test_manifest_matching_rejects_unknown_malformed_and_ambiguous_ids(self):
        for sid in (None, 999, True, 0.0, {}, "00"):
            with self.subTest(sid=sid):
                self.assertIsNone(self.backend.find_manifest_segment(
                    {"segment_id": sid, "start_time": 0}, CLIPS))
        self.assertEqual(self.backend.find_manifest_segment({"segment_id": "0"}, CLIPS)["id"], 0)
        ambiguous = CLIPS + [{**CLIPS[0], "id": "0", "transcript_id": "0"}]
        self.assertIsNone(self.backend.find_manifest_segment({"segment_id": 0}, ambiguous))

    def test_unknown_manifest_id_never_falls_back_to_nearby_clip(self):
        Path(self.backend.SEGMENTS_JSON).write_text(json.dumps({"segments": [
            {**CLIPS[1], "start": 0.5}]}))
        with patch.object(self.backend.subprocess, "run") as media:
            response = self.client.post("/api/preview-auto-export", json={"min_score": 7, "instructions": "fixture"})
            with self.assertRaisesRegex(ValueError, "No valid video clips"):
                self.backend.build_highlight_reel(7, True, "fixture")
        self.assertEqual(response.status_code, 400)
        media.assert_not_called()

    def test_ambiguous_manifest_id_is_excluded_from_preview_and_export(self):
        Path(self.backend.SEGMENTS_JSON).write_text(json.dumps({"segments": CLIPS + [
            {**CLIPS[0], "id": "0", "transcript_id": "0"}]}))
        with patch.object(self.backend.subprocess, "run", side_effect=self.fake_media_tool):
            preview = self.client.post("/api/preview-auto-export", json={"min_score": 7, "instructions": "fixture"})
            reel = self.backend.build_highlight_reel(7, True, "fixture")
        self.assertEqual(preview.json["count"], 1)
        self.assertEqual([r["segment_id"] for r in reel["segments_included"]], [9])

    def test_auto_generation_rescores_partial_cache_and_keeps_fresh_coverage(self):
        partial = scoring.enrich_results(mixed_records(), SEGMENTS)
        self.write_scores(partial, complete=False)
        def score_again(*args):
            self.write_scores(partial, complete=False)
            return True
        with patch.object(self.backend, "run_scoring_subprocess", side_effect=score_again) as provider, \
             patch.object(self.backend.subprocess, "run", side_effect=self.fake_media_tool):
            self.backend.auto_generate_async("fixture-task", 7, True, "fixture", False)
        provider.assert_called_once()
        result = self.backend.processing_status["fixture-task"]
        self.assertEqual(result["status"], "complete")  # The reel job completed.
        self.assertEqual(result["score_validation"]["status"], "partial")
        self.assertIn("partial AI scores", result["message"])
        self.assertEqual(result["highlights_used"], 2)

    def test_auto_generation_wholly_unusable_cache_is_a_clear_error(self):
        self.write_scores([{"segment_id": 999, "score": 10, "start_time": 0}])
        with patch.object(self.backend, "run_scoring_subprocess") as provider, \
             patch.object(self.backend.subprocess, "run") as media:
            self.backend.auto_generate_async("fixture-task", 7, True, "fixture", False)
        provider.assert_called_once()
        media.assert_not_called()
        result = self.backend.processing_status["fixture-task"]
        self.assertEqual(result["status"], "error")
        self.assertIn("No usable AI scores", result["message"])


if __name__ == "__main__":
    unittest.main()
