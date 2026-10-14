# SPDX-License-Identifier: GPL-3.0-or-later
"""Transcript/config applicability at the real scoring, preview and generation boundaries."""
import copy
from contextlib import redirect_stdout
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

import requests
from model_based_processing import scoring


ROOT = Path(__file__).resolve().parents[1]
SEGMENTS = json.loads((ROOT / "tests/fixtures/segments.json").read_text())["segments"]
SETTINGS = {
    "backend": "api", "base_url": "https://provider.invalid/v1",
    "api_key": "credential-one", "model": "fixture-model",
    "local_url": "http://127.0.0.1:11434", "local_model": "fixture-model",
}


class CacheIdentity(unittest.TestCase):
    def identity(self, segments=None, settings=None, criteria="great plays"):
        return scoring.score_cache_identity(
            segments if segments is not None else SEGMENTS, criteria,
            scoring.get_backend(settings=settings or SETTINGS))

    def test_deterministic_digest_uses_canonical_ids_and_effective_transcript(self):
        equivalent = copy.deepcopy(SEGMENTS)
        for segment in equivalent:
            segment["id"] = str(segment["id"])
            segment["text"] = "  " + segment["text"] + "  "
            segment["start"] = float(segment["start"])
            segment["filename"] = "another-clip.mp4"
        self.assertEqual(self.identity(), self.identity(equivalent))
        for field, value in (("id", 123), ("text", "a different recording"),
                             ("start", 0.125), ("end", 99)):
            with self.subTest(field=field):
                changed = copy.deepcopy(SEGMENTS)
                changed[0][field] = value
                self.assertNotEqual(self.identity(), self.identity(changed))
        reordered = list(reversed(SEGMENTS))
        self.assertNotEqual(self.identity(), self.identity(reordered))

    def test_invalid_transcript_identity_has_bounded_nonsecret_error(self):
        for segments in (None, {}, [], [None], [{"id": "secret-token"}],
                         [{**SEGMENTS[0], "start": float("nan")}],
                         [{**SEGMENTS[0], "id": True}]):
            with self.subTest(segments=segments):
                with self.assertRaises(scoring.ScoringError) as failure:
                    scoring.transcript_digest(segments)
                self.assertNotIn("secret-token", str(failure.exception))

    def test_criteria_strip_matches_prompt_without_changing_case(self):
        self.assertEqual(self.identity(criteria="  great plays\n"), self.identity())
        self.assertNotEqual(self.identity(criteria="Great plays"), self.identity())

    def test_default_and_explicit_equivalent_settings_share_identity(self):
        with patch.dict(os.environ, {"HI_LITE_LLM_BACKEND": " API "}, clear=True), \
             patch.object(scoring, "load_config_file", return_value={}):
            implicit = scoring.get_backend()
            with patch.dict(os.environ, {
                "HI_LITE_LLM_BASE_URL": scoring.DEFAULT_API_BASE_URL + "/",
                "HI_LITE_LLM_MODEL": scoring.DEFAULT_API_MODEL,
                "HI_LITE_LLM_API_KEY": "rotated-credential",
            }):
                explicit = scoring.get_backend()
            self.assertEqual(scoring.score_cache_identity(SEGMENTS, "great plays", implicit),
                             scoring.score_cache_identity(SEGMENTS, "great plays", explicit))
        with patch.dict(os.environ, {"HI_LITE_LLM_BACKEND": "LOCAL"}, clear=True), \
             patch.object(scoring, "load_config_file", return_value={}):
            implicit = scoring.get_backend()
            with patch.dict(os.environ, {
                "HI_LITE_LLM_LOCAL_URL": scoring.DEFAULT_LOCAL_URL + "/",
                "HI_LITE_LLM_LOCAL_MODEL": scoring.DEFAULT_LOCAL_MODEL,
            }):
                explicit = scoring.get_backend()
            self.assertEqual(scoring.score_cache_identity(SEGMENTS, "great plays", implicit),
                             scoring.score_cache_identity(SEGMENTS, "great plays", explicit))

    def test_auto_routing_identity_uses_selected_backend_and_model(self):
        settings = {**SETTINGS, "backend": None}
        with patch.object(scoring.LocalBackend, "is_available", return_value=True):
            auto_local = self.identity(settings=settings)
        with patch.object(scoring.LocalBackend, "is_available", return_value=False):
            auto_api = self.identity(settings=settings)
        self.assertEqual(auto_local, self.identity(settings={**settings, "backend": "local"}))
        self.assertEqual(auto_api, self.identity(settings={**settings, "backend": "api"}))
        self.assertNotEqual(auto_local, auto_api)

    def test_endpoint_preserves_paths_and_excludes_all_url_credentials(self):
        for backend, field in (("api", "base_url"), ("local", "local_url")):
            with self.subTest(backend=backend):
                a = {**SETTINGS, "backend": backend, field:
                     "https://user:password-one@provider.invalid/Tenant//v1/?token=token-one#secret-one"}
                b = {**a, "api_key": "credential-two", field:
                     "https://other:password-two@provider.invalid/Tenant//v1/?token=token-two#secret-two"}
                identity = self.identity(settings=a)
                self.assertEqual(identity, self.identity(settings=b))
                self.assertNotEqual(identity, self.identity(settings={**b, field:
                    "https://provider.invalid/tenant//v1"}))
                self.assertNotEqual(identity, self.identity(settings={**b, field:
                    "https://provider.invalid/Tenant/v1"}))
                self.assertEqual(identity, self.identity(settings={**b, field:
                    "https://provider.invalid/Tenant//v1?token=token-two"}))
                for secret in ("user", "password-one", "token-one", "secret-one", "credential-one"):
                    self.assertNotIn(secret, json.dumps(identity))

    def test_future_backend_uses_existing_request_attributes(self):
        backend = SimpleNamespace(name="developer", model="model-a", provider="provider-a",
                                  base_url="https://custom.invalid/request", api_key="never-cache-this")
        first = scoring.score_cache_identity(SEGMENTS, "great plays", backend)
        backend.provider = "provider-b"
        self.assertNotEqual(first, scoring.score_cache_identity(SEGMENTS, "great plays", backend))
        self.assertNotIn("never-cache-this", json.dumps(first))

    def test_endpoint_request_path_and_identity_agree_with_query_credentials(self):
        for backend_name, field, route in (("api", "base_url", "/v1/chat/completions"),
                                           ("local", "local_url", "/v1/chat/completions")):
            settings = {**SETTINGS, "backend": backend_name, field:
                        "https://user:password@provider.invalid/?token=secret#private"}
            if backend_name == "api":
                settings[field] = settings[field].replace("invalid/", "invalid/v1/")
            backend = scoring.get_backend(settings=settings)
            response = SimpleNamespace(status_code=200, json=lambda: {
                "choices": [{"message": {"content": '{"segments": [{"segment_id": 0, "score": 8}]}'}}]})
            with patch("requests.post", return_value=response) as provider:
                backend.score(SEGMENTS, "fixture", "great plays")
            self.assertEqual(provider.call_args.args[0],
                             "https://user:password@provider.invalid" + route + "?token=secret")
            identity = scoring.score_cache_identity(SEGMENTS, "great plays", backend)
            self.assertEqual(identity["endpoint"], "https://provider.invalid/v1")
            self.assertTrue(identity["endpoint_query_reusable"])

    def test_endpoint_failure_messages_do_not_expose_url_credentials(self):
        url = "https://user:private-password@provider.invalid/v1?token=private-token#private-fragment"
        for failure in (requests.exceptions.Timeout("private-exception"),
                        requests.exceptions.ConnectionError("private-exception"), None):
            with self.subTest(failure=type(failure).__name__):
                with patch("requests.post", side_effect=failure,
                           return_value=SimpleNamespace(status_code=404)):
                    with self.assertRaises(scoring.ScoringError) as error:
                        scoring._chat_completion(url, "fixture", "prompt", api_key="private-key")
                for secret in ("private-password", "private-token", "private-fragment",
                               "private-exception", "private-key"):
                    self.assertNotIn(secret, str(error.exception))


CLIPS = [{**s, "transcript_id": s["id"]} for s in SEGMENTS]


class CacheConsumers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.TemporaryDirectory(prefix="hi-lite-a3b-")
        cls.addClassCleanup(cls.data.cleanup)
        spec = importlib.util.spec_from_file_location("score_cache_backend", ROOT / "web_interface.py")
        cls.web = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"HI_LITE_DATA_DIR": cls.data.name}):
            spec.loader.exec_module(cls.web)

    def setUp(self):
        self.settings = dict(SETTINGS)
        self.criteria = "great plays"
        self.scores = [8, 3, 7.5]
        self.client = self.web.app.test_client()
        self.web.processing_status.clear()
        self.assertIsNone(self.web._workspace_owner)
        self.transcript = Path(self.web.MANIFEST_DIR, "transcription.json")
        self.cache = Path(self.web.MANIFEST_DIR, "sorted_segments.json")
        for name in ("sorted_segments.json", "scored_segments.json"):
            Path(self.web.MANIFEST_DIR, name).unlink(missing_ok=True)
        self.transcript.write_text(json.dumps({"segments": SEGMENTS}))
        Path(self.web.SEGMENTS_JSON).write_text(json.dumps({"segments": CLIPS}))
        Path(self.web.SEGMENTS_DIR).mkdir(parents=True, exist_ok=True)
        for segment in SEGMENTS:
            Path(self.web.SEGMENTS_DIR, segment["filename"]).write_bytes(b"fixture clip")
        self.provider = Mock(side_effect=self.provider_response)
        self.real_worker = self.web.run_scoring_subprocess
        self.worker = Mock(side_effect=self.score_in_process)
        for patcher in (
            patch.object(scoring, "get_llm_settings", side_effect=lambda: dict(self.settings)),
            patch("requests.post", self.provider),
            patch.object(self.web, "run_scoring_subprocess", self.worker),
            patch.object(self.web.subprocess, "run", side_effect=self.media_tool),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def provider_response(self, *args, **kwargs):
        segments = json.loads(self.transcript.read_text())["segments"]
        records = [{"segment_id": s["id"], "score": score}
                   for s, score in zip(segments, self.scores)]
        return SimpleNamespace(status_code=200, json=lambda: {
            "choices": [{"message": {"content": json.dumps({"segments": records})}}]})

    def score_in_process(self, task_id, instructions, settings, backend_name):
        # Run the actual entry point with the request's pinned configuration.
        with patch.object(scoring, "get_llm_settings", return_value={**settings, "backend": backend_name}):
            ok = scoring.run_scoring(str(self.transcript), self.web.MANIFEST_DIR, instructions)
        if not ok:
            self.web.processing_status[task_id] = {"status": "error", "message": "AI scoring failed."}
        return ok

    def media_tool(self, cmd, **kwargs):
        if cmd[0] == self.web.FFPROBE:
            return subprocess.CompletedProcess(cmd, 0, "1.5", "")
        Path(cmd[-1]).write_bytes(b"generated reel")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def generate(self, threshold=7, rescore=False):
        self.web.auto_generate_async("fixture-task", threshold, True, self.criteria, rescore)
        return self.web.processing_status["fixture-task"]

    def preview(self, threshold=7):
        return self.client.post("/api/preview-auto-export", json={
            "min_score": threshold, "instructions": self.criteria})

    def seed(self):
        self.assertEqual(self.generate()["status"], "complete")
        self.provider.assert_called_once()
        self.worker.assert_called_once()

    def assert_miss_then_rescore(self):
        calls = self.provider.call_count
        with patch.object(self.web.subprocess, "run") as media:
            self.assertEqual(self.preview().status_code, 400)
            with self.assertRaises(scoring.ScoringError):
                self.web.build_highlight_reel(7, True, self.criteria)
            media.assert_not_called()
        self.assertEqual(self.generate()["status"], "complete")
        self.assertEqual(self.provider.call_count, calls + 1)
        self.assertEqual(self.preview().status_code, 200)
        self.assertEqual(self.generate()["status"], "complete")
        self.assertEqual(self.provider.call_count, calls + 1)

    def test_matching_cache_generation_and_preview_hit_without_provider(self):
        self.seed()
        preview = self.preview()
        result = self.generate()
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json["count"], result["highlights_used"])
        self.assertEqual(preview.json["score_validation"], result["score_validation"])
        self.provider.assert_called_once()
        self.worker.assert_called_once()

    def test_each_transcript_input_change_misses_at_same_filename_and_count(self):
        for field, value in (("text", "different content"), ("id", 123),
                             ("start", 0.125), ("end", 99)):
            with self.subTest(field=field):
                self.set_transcript(SEGMENTS)
                self.generate(rescore=True)
                changed = copy.deepcopy(SEGMENTS)
                changed[0][field] = value
                self.set_transcript(changed)
                self.assertEqual(len(changed), len(SEGMENTS))
                self.assert_miss_then_rescore()

    def set_transcript(self, segments):
        self.transcript.write_text(json.dumps({"segments": segments}))
        Path(self.web.SEGMENTS_JSON).write_text(json.dumps({"segments": [
            {**s, "transcript_id": s["id"]} for s in segments]}))

    def test_each_effective_configuration_change_misses(self):
        for key, value in (("backend", "local"), ("model", "another-model"),
                           ("base_url", "https://other.invalid/v1"),
                           ("base_url", "https://provider.invalid/another/v1")):
            with self.subTest(key=key, value=value):
                self.settings = dict(SETTINGS)
                self.generate(rescore=True)
                self.settings[key] = value
                self.assert_miss_then_rescore()
        self.criteria = "Great plays"
        self.assert_miss_then_rescore()
        with patch.object(scoring, "SCORING_PROMPT_VERSION", "2"):
            self.assert_miss_then_rescore()

    def test_local_endpoint_only_change_misses(self):
        self.settings["backend"] = "local"
        self.seed()
        for url in ("http://127.0.0.1:9999", "http://127.0.0.1:9999/tenant"):
            self.settings["local_url"] = url
            self.assert_miss_then_rescore()

    def test_untyped_routing_queries_never_reuse_or_persist_possible_secrets(self):
        self.settings["base_url"] += "?tenant=possibly-secret-one"
        self.seed()
        self.assertEqual(self.preview().status_code, 400)
        self.assertEqual(self.generate()["status"], "complete")
        self.assertEqual(self.provider.call_count, 2)
        self.settings["base_url"] = SETTINGS["base_url"] + "?tenant=possibly-secret-two"
        self.assertEqual(self.generate()["status"], "complete")
        self.assertEqual(self.provider.call_count, 3)
        persisted = self.cache.read_text()
        self.assertNotIn("possibly-secret-one", persisted)
        self.assertNotIn("possibly-secret-two", persisted)

    def test_credential_rotation_reuses_and_never_persists_key_material(self):
        self.settings["base_url"] = "https://user:password-one@provider.invalid/v1?token=token-one"
        with redirect_stdout(io.StringIO()) as output:
            self.seed()
            self.settings["api_key"] = "credential-two"
            self.settings["base_url"] = "https://other:password-two@provider.invalid/v1?token=token-two"
            self.assertEqual(self.preview().status_code, 200)
            self.assertEqual(self.generate()["status"], "complete")
        self.provider.assert_called_once()
        persisted = "".join(Path(self.web.MANIFEST_DIR, name).read_text()
                            for name in ("sorted_segments.json", "scored_segments.json"))
        exposed = persisted + output.getvalue()
        for secret in ("credential-one", "credential-two", "password-one", "password-two",
                       "token-one", "token-two", "user:", "other:"):
            self.assertNotIn(secret, exposed)
        self.cache.write_text('{broken')
        self.assertNotIn("password-two", self.preview().get_data(as_text=True))

    def test_threshold_and_order_changes_reuse_scores_and_update_selection(self):
        self.seed()
        self.assertEqual(self.preview(8).json["count"], 1)
        high = self.generate(8)
        self.assertEqual(high["highlights_used"], 1)
        self.assertEqual(high["min_score"], 8)
        self.assertEqual(self.preview(3).json["count"], 3)
        low = self.generate(3)
        self.assertEqual(low["highlights_used"], 3)
        self.web.auto_generate_async("fixture-task", 7, False, self.criteria, False)
        self.assertEqual(self.web.processing_status["fixture-task"]["status"], "complete")
        self.provider.assert_called_once()
        self.worker.assert_called_once()

    def test_explicit_rescore_calls_provider_then_replaces_reusable_result(self):
        self.seed()
        self.scores = [3, 9, 4]
        result = self.generate(rescore=True)
        self.assertEqual([s["segment_id"] for s in result["segments_included"]], [4])
        self.assertEqual(self.provider.call_count, 2)
        self.assertEqual(self.generate()["highlights_used"], 1)
        self.assertEqual(self.preview().json["count"], 1)
        self.assertEqual(self.provider.call_count, 2)

    def test_legacy_corrupt_and_malformed_metadata_caches_all_miss(self):
        self.seed()
        valid = json.loads(self.cache.read_text())
        bad_payloads = ["{broken", "null", "[]"]
        legacy = copy.deepcopy(valid)
        del legacy["cache"]
        bad_payloads.append(json.dumps(legacy))
        for cache in (None, {}, [], {"identity": None, "complete": True},
                      {"identity": valid["cache"]["identity"], "complete": "true"},
                      {"identity": {}, "complete": True}):
            bad_payloads.append(json.dumps({**valid, "cache": cache}))
        for contents in bad_payloads:
            with self.subTest(contents=contents[:40]):
                self.cache.write_text(contents)
                self.assert_miss_then_rescore()

    def test_matching_metadata_never_bypasses_shared_score_validation(self):
        self.seed()
        valid = json.loads(self.cache.read_text())
        for records in ([], [None], [{"segment_id": 999, "score": 10}],
                        [{"segment_id": 0, "score": "8"}],
                        valid["highlights"] + [{"segment_id": 999, "score": 8}],
                        [{**valid["highlights"][0], "score": float("nan")}]
                        + valid["highlights"][1:]):
            with self.subTest(records=records):
                self.cache.write_text(json.dumps({**valid, "highlights": records}))
                self.assert_miss_then_rescore()

    def test_incomplete_and_forged_complete_results_cannot_be_reused(self):
        self.seed()
        valid = json.loads(self.cache.read_text())
        for complete in (False, True):
            self.cache.write_text(json.dumps({**valid, "highlights": valid["highlights"][:1],
                                             "cache": {**valid["cache"], "complete": complete}}))
            self.assert_miss_then_rescore()
        self.cache.write_text(json.dumps({**valid, "cache": {**valid["cache"], "complete": False}}))
        self.assert_miss_then_rescore()

    def test_preview_requires_current_criteria(self):
        self.seed()
        with patch.object(self.web.subprocess, "run") as media:
            response = self.client.post("/api/preview-auto-export", json={"min_score": 7})
        self.assertEqual(response.status_code, 400)
        media.assert_not_called()

    def test_fresh_partial_scores_work_but_are_not_automatically_reused(self):
        self.scores = [8]
        self.seed()
        self.assertEqual(self.web.processing_status["fixture-task"]["score_validation"]["status"], "partial")
        self.assertEqual(self.preview().status_code, 400)
        self.assertEqual(self.generate()["status"], "complete")
        self.assertEqual(self.provider.call_count, 2)

    def test_failed_rescore_does_not_publish_or_export_old_scores(self):
        self.seed()
        before = self.cache.read_bytes()
        self.provider.side_effect = requests.exceptions.Timeout("private-credential")
        with patch.object(self.web.subprocess, "run") as media, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.generate(rescore=True)["status"], "error")
        self.assertEqual(self.cache.read_bytes(), before)
        media.assert_not_called()
        self.assertNotIn("private-credential", output.getvalue())
        self.criteria = "new criteria"
        with patch.object(self.web.subprocess, "run") as media:
            self.assertEqual(self.generate()["status"], "error")
            self.assertEqual(self.preview().status_code, 400)
        self.assertEqual(self.cache.read_bytes(), before)
        media.assert_not_called()

    def test_subprocess_gets_pinned_configuration_without_logging_secrets(self):
        process = Mock(stdout=io.StringIO("Scoring complete\n"))
        process.wait.return_value = 0
        with patch.object(self.web.subprocess, "Popen", return_value=process) as spawn, \
             redirect_stdout(io.StringIO()) as output:
            self.assertTrue(self.real_worker("fixture-task", self.criteria, self.settings, "api"))
        environment = spawn.call_args.kwargs["env"]
        for key in ("base_url", "api_key", "model", "local_url", "local_model"):
            self.assertEqual(environment["HI_LITE_LLM_" + key.upper()], self.settings[key])
        self.assertEqual(environment["HI_LITE_LLM_BACKEND"], "api")
        self.assertNotIn(self.settings["api_key"], output.getvalue())
        self.assertNotIn(self.settings["api_key"], " ".join(spawn.call_args.args[0]))

    def test_failed_publication_does_not_replace_authoritative_cache_or_export(self):
        self.seed()
        before = self.cache.read_bytes()
        replace = os.replace
        def fail_authoritative_replace(source, target):
            if Path(target) == self.cache:
                raise OSError("private-credential")
            replace(source, target)
        self.criteria = "new criteria"
        with patch.object(scoring.os, "replace", side_effect=fail_authoritative_replace), \
             patch.object(self.web.subprocess, "run") as media, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.generate()["status"], "error")
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(self.preview().status_code, 400)
        self.assertEqual(list(Path(self.web.MANIFEST_DIR).glob(".scores-*.tmp")), [])
        self.assertNotIn("private-credential", output.getvalue())
        self.assertNotIn("Scoring complete:", output.getvalue())
        media.assert_not_called()


class CachePublication(unittest.TestCase):
    def test_atomic_replace_keeps_old_complete_cache_during_write_and_cleans_temp(self):
        with tempfile.TemporaryDirectory(prefix="hi-lite-publish-") as directory:
            path = Path(directory, "sorted_segments.json")
            path.write_text('{"old": true}')
            replace = os.replace
            def inspect_then_replace(source, target):
                self.assertEqual(json.loads(path.read_text()), {"old": True})
                self.assertEqual(json.loads(Path(source).read_text()), {"complete": True})
                replace(source, target)
            with patch.object(scoring.os, "replace", side_effect=inspect_then_replace):
                scoring._publish_score_payload(str(path), {"complete": True})
            self.assertEqual(json.loads(path.read_text()), {"complete": True})
            self.assertEqual(list(Path(directory).glob(".scores-*.tmp")), [])

    def test_failed_write_and_replace_leave_previous_cache_intact(self):
        with tempfile.TemporaryDirectory(prefix="hi-lite-publish-") as directory:
            path = Path(directory, "sorted_segments.json")
            path.write_text('{"old": true}')
            with self.assertRaises(ValueError):
                scoring._publish_score_payload(str(path), {"score": float("nan")})
            with patch.object(scoring.os, "replace", side_effect=OSError("private-failure")):
                with self.assertRaises(OSError):
                    scoring._publish_score_payload(str(path), {"new": True})
            self.assertEqual(json.loads(path.read_text()), {"old": True})
            self.assertEqual(list(Path(directory).glob(".scores-*.tmp")), [])

    def test_dynamic_backend_without_effective_model_stays_available_but_uncacheable(self):
        with tempfile.TemporaryDirectory(prefix="hi-lite-dynamic-") as directory:
            path = Path(directory, "transcription.json")
            path.write_text(json.dumps({"segments": SEGMENTS}))
            backend = SimpleNamespace(name="developer", score=Mock(return_value=[
                {"segment_id": s["id"], "score": 8} for s in SEGMENTS]))
            identity = scoring.score_cache_identity(SEGMENTS, "great plays", backend)
            with patch.object(scoring, "get_backend", return_value=backend):
                self.assertTrue(scoring.run_scoring(str(path), directory, "great plays"))
            cached = str(Path(directory, "sorted_segments.json"))
            with self.assertRaises(scoring.ScoringError):
                scoring.load_score_cache(cached, SEGMENTS, identity)
            fresh = scoring.load_score_cache(cached, SEGMENTS, identity, require_complete=False)
            self.assertEqual(fresh.validation["status"], "complete")


if __name__ == "__main__":
    unittest.main()
