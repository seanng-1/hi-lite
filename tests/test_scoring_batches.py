# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded whole-recording scoring with no provider/network access."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from model_based_processing import scoring


def records(count=10, text="An important passage."):
    return [{"id": f"segment-{i}", "start": i * 3, "end": i * 3 + 2,
             "text": text} for i in range(count)]


def scores(segments, transcript, instructions):
    return [{"segment_id": s["id"], "score": 8, "reason": "Fixture"} for s in segments]


class BoundedScoring(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        context = redirect_stdout(self.output)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.backend = SimpleNamespace(name="fixture", model="fixture", score=Mock(side_effect=scores))

    def test_small_transcript_remains_one_call_with_exact_format_and_ids(self):
        segments = records(3)
        result = scoring.score_in_batches(self.backend, segments, "important moments")
        self.backend.score.assert_called_once()
        batch, transcript, instructions = self.backend.score.call_args.args
        self.assertEqual(batch, segments)
        self.assertEqual(transcript, "\n".join(f"ID:{s['id']} [{s['start']:.2f}s-{s['end']:.2f}s] {s['text']}" for s in segments))
        self.assertEqual(instructions, "important moments")
        self.assertEqual(result.validation["status"], "complete")
        self.assertEqual(result.validation["batch_count"], 1)

    def test_entire_long_recording_is_scored_in_bounded_batches(self):
        segments = records(247)
        original = deepcopy(segments)
        result = scoring.score_in_batches(self.backend, segments, "moments")
        self.assertEqual(self.backend.score.call_count, 4)
        visited = []
        for call in self.backend.score.call_args_list:
            batch, transcript, _ = call.args
            self.assertLessEqual(len(batch), 80)
            self.assertLessEqual(len(transcript), 16000)
            visited.extend(s["id"] for s in batch)
        self.assertEqual(visited, [s["id"] for s in segments])
        self.assertEqual(len(result), 247)
        self.assertEqual(result.validation["status"], "complete")
        self.assertEqual(segments, original)

    def test_character_bound_is_independent_of_segment_count(self):
        segments = records(12, "Here is a somewhat longer paragraph " * 2)
        result = scoring.score_in_batches(self.backend, segments, "moments", max_segments=80, max_chars=250)
        self.assertGreater(self.backend.score.call_count, 1)
        self.assertEqual(len(result), 12)
        self.assertTrue(all(len(call.args[1]) <= 250 for call in self.backend.score.call_args_list))

    def test_oversized_passage_is_fully_scored_with_unchanged_id_and_timing(self):
        text = " ".join(f"word{i}" for i in range(120))
        segments = records(1, text)
        result = scoring.score_in_batches(self.backend, segments, "moments", max_chars=140)
        seen = []
        for call in self.backend.score.call_args_list:
            batch, transcript, _ = call.args
            self.assertEqual(len(batch), 1)
            self.assertEqual((batch[0]["id"], batch[0]["start"], batch[0]["end"]), ("segment-0", 0, 2))
            self.assertLessEqual(len(transcript), 140)
            seen.extend(batch[0]["text"].split())
        self.assertEqual(seen, text.split())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], text)
        self.assertEqual(result.validation["status"], "complete")
        self.assertGreater(result.validation["fragment_count"], 1)

    def test_single_word_oversize_is_not_dropped_or_infinite(self):
        text = "x" * 300
        scoring.score_in_batches(self.backend, records(1, text), "moments", max_chars=64)
        seen = "".join(call.args[0][0]["text"] for call in self.backend.score.call_args_list)
        self.assertEqual(seen, text)
        self.assertTrue(all(len(call.args[1]) <= 64 for call in self.backend.score.call_args_list))

    def test_max_fragment_score_wins_without_duplicate_output(self):
        def varying(batch, transcript, instructions):
            return [{"segment_id": s["id"], "score": min(10, self.backend.score.call_count), "reason": "Fixture"} for s in batch]
        self.backend.score.side_effect = varying
        result = scoring.score_in_batches(self.backend, records(1, "long passage " * 80), "moments", max_chars=100)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["score"], 10)

    def test_cross_batch_identity_is_rejected_before_global_aggregation(self):
        def mixed(batch, transcript, instructions):
            own = scores(batch, transcript, instructions)
            if batch[0]["id"] == "segment-0":
                own.append({"segment_id": "segment-2", "score": 10})
            return own
        self.backend.score.side_effect = mixed
        result = scoring.score_in_batches(self.backend, records(3), "moments", max_segments=1)
        self.assertEqual([r["score"] for r in result], [8, 8, 8])
        self.assertEqual(result.validation["rejection_counts"], {"unknown_id": 1})
        self.assertEqual(result.validation["rejection_examples"][0]["batch_index"], 0)

    def test_partial_batch_coverage_is_preserved_across_entire_transcript(self):
        self.backend.score.side_effect = lambda batch, transcript, instructions: scores(batch[:1], transcript, instructions)
        result = scoring.score_in_batches(self.backend, records(8), "moments", max_segments=3)
        self.assertEqual(result.validation["status"], "partial")
        self.assertEqual(result.validation["missing_segment_count"], 5)
        self.assertEqual(result.validation["missing_segment_ids"], ["segment-1", "segment-2", "segment-4", "segment-5", "segment-7"])
        self.assertEqual([r["segment_id"] for r in result], ["segment-0", "segment-3", "segment-6"])

    def test_parse_failure_does_not_discard_other_batches_or_expose_error_text(self):
        def intermittent(batch, transcript, instructions):
            if batch[0]["id"] == "segment-2":
                raise scoring.ScoringError("secret response must not be printed", category="parse")
            return scores(batch, transcript, instructions)
        self.backend.score.side_effect = intermittent
        result = scoring.score_in_batches(self.backend, records(6), "moments", max_segments=2)
        self.assertEqual(result.validation["status"], "partial")
        self.assertEqual(result.validation["failed_batch_count"], 1)
        self.assertEqual(result.validation["missing_segment_ids"], ["segment-2", "segment-3"])
        self.assertNotIn("secret response", self.output.getvalue())

    def test_fragment_failure_excludes_the_entire_affected_passage(self):
        def intermittent(batch, transcript, instructions):
            if self.backend.score.call_count == 2:
                raise scoring.ScoringError("parse", category="parse")
            return scores(batch, transcript, instructions)
        self.backend.score.side_effect = intermittent
        segments = records(1, "long passage " * 40) + [{"id": "other", "start": 3, "end": 5, "text": "Other"}]
        result = scoring.score_in_batches(self.backend, segments, "moments", max_chars=100)
        self.assertEqual([r["segment_id"] for r in result], ["other"])
        self.assertEqual(result.validation["status"], "partial")
        self.assertEqual(result.validation["missing_segment_ids"], ["segment-0"])

    def test_non_parse_provider_failure_stops_without_extra_requests(self):
        for category in ("auth", "rate_limit", "timeout", "connection", "api"):
            self.backend.score.reset_mock()
            self.backend.score.side_effect = scoring.ScoringError("Provider failure", category=category)
            with self.assertRaises(scoring.ScoringError):
                scoring.score_in_batches(self.backend, records(20), "moments", max_segments=2)
            self.backend.score.assert_called_once()

    def test_all_parse_failures_do_not_fabricate_scores(self):
        self.backend.score.side_effect = scoring.ScoringError("No result", category="parse")
        with self.assertRaisesRegex(scoring.ScoringError, "No usable AI scores"):
            scoring.score_in_batches(self.backend, records(3), "moments", max_segments=1)
        self.assertEqual(self.backend.score.call_count, 3)

    def test_built_in_diagnostics_survive_revalidation(self):
        def provider(batch, transcript, instructions):
            return scoring.enrich_results(scores(batch, transcript, instructions) + [None], batch)
        self.backend.score.side_effect = provider
        result = scoring.score_in_batches(self.backend, records(4), "moments", max_segments=2)
        self.assertEqual(result.validation["rejected_records"], 2)
        self.assertEqual(result.validation["rejection_counts"], {"malformed_record": 2})
        self.assertEqual([e["batch_index"] for e in result.validation["rejection_examples"]], [0, 1])

    def test_invalid_inputs_fail_before_provider_invocation(self):
        for kwargs in ({"max_segments": True}, {"max_segments": 0}, {"max_chars": 1}, {"max_chars": "100"}):
            with self.assertRaises(ValueError):
                scoring.score_in_batches(self.backend, records(1), "moments", **kwargs)
        for source in ([], [{**records(1)[0], "start": "0"}], records(1) * 2,
                       [{**records(1)[0], "end": float("nan")}], [{**records(1)[0], "id": True}]):
            with self.assertRaises(scoring.ScoringError):
                scoring.score_in_batches(self.backend, source, "moments")
        self.backend.score.assert_not_called()

    def test_entry_point_writes_full_recording_and_batch_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "transcription.json")
            path.write_text(json.dumps({"segments": records(170)}))
            with patch.object(scoring, "get_backend", return_value=self.backend):
                self.assertTrue(scoring.run_scoring(str(path), directory, "moments"))
            payload = json.loads(Path(directory, "sorted_segments.json").read_text())
            self.assertEqual(len(payload["highlights"]), 170)
            self.assertEqual(payload["validation"]["batch_count"], 3)
            self.assertTrue(payload["cache"]["complete"])
            self.assertEqual(payload["cache"]["identity"]["prompt_version"], "2-batched")


if __name__ == "__main__":
    unittest.main()
