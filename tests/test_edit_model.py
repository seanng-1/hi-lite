# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure source-time edits, cleanup review, and whole-recording planning."""
from copy import deepcopy
import json
import unittest

from edit_model import (accept_suggestion, apply_edit, candidate_groups,
                        cleanup_suggestions, initial_edits, validate_edits)

SEGMENTS = [
    {"id": 0, "transcript_id": "a", "start": 0, "end": 5, "text": "First passage"},
    {"id": 4, "transcript_id": "b", "start": 5, "end": 10, "text": "Second passage"},
    {"id": 9, "transcript_id": "c", "start": 12, "end": 18, "text": "Final passage"},
]
TRANSCRIPT = {"duration": 20, "segments": [
    {"id": "a", "start": 1, "end": 4, "text": "Um, this is useful.", "words": [
        {"word": " Um,", "start": 1, "end": 1.3},
        {"word": "this", "start": 1.4, "end": 1.6},
        {"word": "is", "start": 1.7, "end": 1.8},
        {"word": "useful.", "start": 3.7, "end": 4}]},
    {"id": "b", "start": 7, "end": 9, "text": "A second thing.", "words": [
        {"word": "A", "start": 7, "end": 7.2},
        {"word": "second", "start": 7.3, "end": 8},
        {"word": "thing.", "start": 8.1, "end": 9}]},
]}


class EditSnapshots(unittest.TestCase):
    def setUp(self):
        self.edits = initial_edits(SEGMENTS)

    def test_initial_keeps_manifest_ids_order_and_buffered_source_times(self):
        self.assertEqual(self.edits["source_duration"], 18)
        self.assertEqual([r["clip_id"] for r in self.edits["ranges"]], [0, 4, 9])
        self.assertEqual([(r["start"], r["end"]) for r in self.edits["ranges"]], [(0, 5), (5, 10), (12, 18)])
        self.assertEqual(json.loads(json.dumps(self.edits)), self.edits)
        self.assertEqual(initial_edits([], 20)["ranges"], [])

    def test_validate_independent_copy_and_authoritative_clip_bounds(self):
        result = validate_edits(self.edits, SEGMENTS, 20)
        result["ranges"][0]["end"] = 2
        self.assertEqual(self.edits["ranges"][0]["end"], 5)
        for updates in ({"clip_id": "a"}, {"end": 6}, {"start": -1}, {"id": ""}):
            with self.subTest(updates=updates):
                broken = deepcopy(self.edits)
                broken["ranges"][0].update(updates)
                with self.assertRaises(ValueError):
                    validate_edits(broken, SEGMENTS)
        with self.assertRaises(ValueError):
            validate_edits(self.edits, SEGMENTS, 17)

    def test_invalid_times_versions_and_ids_fail_closed(self):
        for bad in (True, "1", float("nan"), float("inf"), -1, {}, 10 ** 400):
            with self.subTest(bad=repr(bad)[:30]):
                broken = deepcopy(self.edits)
                broken["ranges"][0]["start"] = bad
                with self.assertRaises(ValueError):
                    validate_edits(broken)
        for bad in (True, "1", 2, None):
            with self.assertRaises(ValueError):
                validate_edits({**self.edits, "version": bad})
        for field in ("id", "clip_id", "group_id"):
            broken = deepcopy(self.edits)
            broken["ranges"][0][field] = True
            with self.assertRaises(ValueError):
                validate_edits(broken)
        broken = deepcopy(self.edits)
        broken["ranges"][1]["id"] = broken["ranges"][0]["id"]
        with self.assertRaises(ValueError):
            validate_edits(broken)

    def test_source_validation_does_not_coerce_identity_or_time(self):
        for segments in (SEGMENTS + [SEGMENTS[0]], [{**SEGMENTS[0], "start": "0"}],
                         [{**SEGMENTS[0], "id": False}], [{**SEGMENTS[0], "end": 0}], [None]):
            with self.assertRaises(ValueError):
                initial_edits(segments)
        with self.assertRaises(ValueError):
            initial_edits(SEGMENTS, 17)
        bad = deepcopy(self.edits)
        bad["ranges"][0]["clip_id"] = "0"
        with self.assertRaises(ValueError):
            validate_edits(bad, SEGMENTS)

    def test_remove_passage_splits_without_losing_ids_or_mutating_history(self):
        before = deepcopy(self.edits)
        result = apply_edit(self.edits, {"type": "remove", "start": 1, "end": 3})
        self.assertEqual([(r["start"], r["end"]) for r in result["ranges"]], [(0, 1), (3, 5), (5, 10), (12, 18)])
        self.assertEqual([r["clip_id"] for r in result["ranges"]], [0, 0, 4, 9])
        self.assertEqual(result["ranges"][0]["group_id"], result["ranges"][1]["group_id"])
        self.assertEqual(self.edits, before)
        self.assertEqual(validate_edits(result, SEGMENTS), result)

    def test_repeated_split_ids_are_unique_and_reaccept_is_idempotent(self):
        result = apply_edit(self.edits, {"type": "remove", "start": 1, "end": 2})
        result = apply_edit(result, {"type": "remove", "start": 3, "end": 4})
        self.assertEqual(len({r["id"] for r in result["ranges"]}), 5)
        self.assertEqual(apply_edit(result, {"type": "remove", "start": 3, "end": 4}), result)

    def test_keep_interval_intersects_current_selection_without_restoration(self):
        removed = apply_edit(self.edits, {"type": "remove", "start": 1, "end": 3})
        kept = apply_edit(removed, {"type": "keep", "start": 0.5, "end": 4})
        self.assertEqual([(r["start"], r["end"]) for r in kept["ranges"]], [(0.5, 1), (3, 4)])

    def test_clip_and_range_selection_are_exact(self):
        kept = apply_edit(self.edits, {"type": "keep", "clip_ids": [9, 0]})
        self.assertEqual([r["clip_id"] for r in kept["ranges"]], [0, 9])
        removed = apply_edit(self.edits, {"type": "remove", "range_ids": ["range-2"]})
        self.assertEqual(removed, kept)
        for action in ({"type": "remove", "clip_ids": ["0"]},
                       {"type": "remove", "clip_ids": [0, 0]},
                       {"type": "remove", "clip_ids": []},
                       {"type": "keep", "clip_ids": [0], "start": 0, "end": 1},
                       {"type": "remove", "start": 1}):
            with self.assertRaises(ValueError):
                apply_edit(self.edits, action)

    def test_trim_split_and_combine_preserve_disjoint_cuts(self):
        split = apply_edit(self.edits, {"type": "split", "range_id": "range-1", "at": 2})
        self.assertNotEqual(split["ranges"][0]["group_id"], split["ranges"][1]["group_id"])
        trimmed = apply_edit(split, {"type": "trim", "range_id": "range-1-2", "start": 3, "end": 4})
        combined = apply_edit(trimmed, {"type": "combine", "range_ids": ["range-1", "range-1-2"]})
        self.assertEqual([(r["start"], r["end"]) for r in combined["ranges"][:2]], [(0, 2), (3, 4)])
        self.assertEqual(combined["ranges"][0]["group_id"], combined["ranges"][1]["group_id"])
        self.assertEqual(validate_edits(combined, SEGMENTS), combined)

    def test_trim_cannot_expand_split_cannot_be_zero_and_combine_is_consecutive(self):
        for action in ({"type": "trim", "range_id": "range-1", "start": 0, "end": 6},
                       {"type": "split", "range_id": "range-1", "at": 5},
                       {"type": "split", "range_id": "range-1", "at": 0},
                       {"type": "trim", "range_id": "missing", "start": 0, "end": 1},
                       {"type": "combine", "range_ids": ["range-1", "range-3"]},
                       {"type": "combine", "range_ids": ["range-1"]}):
            with self.assertRaises(ValueError):
                apply_edit(self.edits, action)

    def test_reorder_is_complete_permutation_and_history_is_json_safe(self):
        result = apply_edit(self.edits, {"type": "reorder", "range_ids": ["range-3", "range-1", "range-2"]})
        self.assertEqual([r["clip_id"] for r in result["ranges"]], [9, 0, 4])
        history = json.loads(json.dumps([self.edits, result]))
        self.assertEqual(validate_edits(history[0], SEGMENTS), self.edits)
        self.assertEqual(validate_edits(history[1], SEGMENTS), result)
        with self.assertRaises(ValueError):
            apply_edit(self.edits, {"type": "reorder", "range_ids": ["range-1"]})
        with self.assertRaises(ValueError):
            apply_edit(self.edits, {"type": "delete-source"})

    def test_remove_all_creates_valid_empty_document(self):
        result = apply_edit(self.edits, {"type": "remove", "start": 0, "end": 18})
        self.assertEqual(result["ranges"], [])
        self.assertEqual(validate_edits(result, SEGMENTS), result)


class CleanupReview(unittest.TestCase):
    def test_word_alignment_finds_internal_and_between_segment_silence(self):
        original = deepcopy(TRANSCRIPT)
        suggestions = cleanup_suggestions(TRANSCRIPT)
        pauses = [s for s in suggestions if s["type"] == "silence"]
        self.assertEqual(len(pauses), 2)
        self.assertAlmostEqual(pauses[0]["start"], 1.95)
        self.assertAlmostEqual(pauses[0]["end"], 3.55)
        self.assertAlmostEqual(pauses[1]["start"], 4.15)
        self.assertAlmostEqual(pauses[1]["end"], 6.85)
        self.assertEqual(TRANSCRIPT, original)
        self.assertEqual(cleanup_suggestions(TRANSCRIPT), suggestions)

    def test_filler_match_is_whole_word_punctuation_insensitive(self):
        suggestions = cleanup_suggestions(TRANSCRIPT)
        fillers = [s for s in suggestions if s["type"] == "filler"]
        self.assertEqual(len(fillers), 1)
        self.assertEqual((fillers[0]["start"], fillers[0]["end"], fillers[0]["text"]), (1, 1.3, "Um,"))
        transcript = [{"id": 0, "start": 0, "end": 2, "text": "umbrella", "words": [{"word": "umbrella", "start": 0, "end": 2}]}]
        self.assertEqual(cleanup_suggestions(transcript), [])

    def test_missing_or_bad_words_never_infer_internal_silence_or_filler_cuts(self):
        for words in (None, [], [{"word": "um", "start": 1, "end": 11}],
                      [{"word": "um", "start": True, "end": 2}],
                      [{"word": "um", "start": 1, "end": 2}, None]):
            transcript = [{"id": 0, "start": 0, "end": 10, "text": "um", "words": words}]
            self.assertEqual(cleanup_suggestions(transcript), [])

    def test_overlapping_speech_does_not_generate_false_silence(self):
        transcript = [
            {"id": 0, "start": 0, "end": 3, "text": "first"},
            {"id": 1, "start": 2, "end": 8, "text": "second"},
            {"id": 2, "start": 4, "end": 5, "text": "third"},
            {"id": 3, "start": 8, "end": 9, "text": "fourth"},
        ]
        self.assertEqual(cleanup_suggestions(transcript), [])

    def test_multiword_fillers_are_opt_in_and_longest_first(self):
        transcript = [{"id": "x", "start": 0, "end": 1, "words": [
            {"word": "You", "start": 0, "end": .2},
            {"word": "know,", "start": .3, "end": .5},
            {"word": "right?", "start": .6, "end": 1}]}]
        result = cleanup_suggestions(transcript, fillers=["you", "you know"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "You know,")
        self.assertEqual(result[0]["end"], .5)

    def test_accept_is_explicit_non_destructive_and_idempotent(self):
        edits = initial_edits(SEGMENTS)
        before = deepcopy(edits)
        suggestion = cleanup_suggestions(TRANSCRIPT)[0]
        edited = accept_suggestion(edits, suggestion)
        self.assertEqual(edits, before)
        self.assertLess(sum(r["end"] - r["start"] for r in edited["ranges"]), 16)
        self.assertEqual(accept_suggestion(edited, suggestion), edited)
        with self.assertRaises(ValueError):
            accept_suggestion(edits, {"type": "other", "start": 1, "end": 2})

    def test_invalid_cleanup_options_are_rejected(self):
        for kwargs in ({"min_pause": True}, {"min_pause": 0}, {"keep_pause": -1},
                       {"min_pause": .3, "keep_pause": .3}, {"fillers": "um"}, {"fillers": [None]}):
            with self.assertRaises(ValueError):
                cleanup_suggestions(TRANSCRIPT, **kwargs)


class CandidatePlanning(unittest.TestCase):
    def fixture(self, count=120, interval=30):
        return [{"id": f"speech-{i}", "start": i * interval, "end": i * interval + interval - 1,
                 "text": f"Passage {i}. Here is an important example."} for i in range(count)]

    def test_hour_recording_covers_beginning_middle_and_end(self):
        transcript = self.fixture()
        original = deepcopy(transcript)
        candidates = candidate_groups(transcript, 3, 60)
        self.assertEqual(len(candidates), 3)
        self.assertLess(candidates[0]["start"], 1200)
        self.assertTrue(1200 <= candidates[1]["start"] < 2400)
        self.assertGreaterEqual(candidates[-1]["end"], 2400)
        self.assertEqual(transcript, original)
        self.assertEqual(candidate_groups(transcript, 3, 60), candidates)
        self.assertTrue(all(c["scoring_source"] == "local_heuristic" for c in candidates))
        self.assertTrue(all("not an AI" in c["reason"] for c in candidates))
        self.assertTrue(all(c["source_span"] <= 60 for c in candidates))
        self.assertEqual(json.loads(json.dumps(candidates)), candidates)

    def test_ids_are_authoritative_not_similar_times_or_positions(self):
        transcript = self.fixture(8)
        scores = [{"segment_id": "speech-7", "score": 10, "start_time": 99999},
                  {"segment_id": 0, "score": 10}, {"segment_id": "unknown", "score": 10}]
        candidates = candidate_groups(transcript, 1, 30, scored_records=scores)
        self.assertEqual(candidates[0]["segment_ids"], ["speech-7"])
        self.assertEqual(candidates[0]["start"], 210)
        self.assertEqual(candidates[0]["scoring_source"], "ai_scores")

    def test_partial_bad_scores_never_coerce_or_break_planning(self):
        transcript = self.fixture(8)
        scores = [{"segment_id": "speech-0", "score": True},
                  {"segment_id": "speech-1", "score": "10"},
                  {"segment_id": "speech-2", "score": float("nan")},
                  {"segment_id": "speech-3", "score": 11}, None,
                  {"segment_id": "speech-7", "score": 9}]
        candidates = candidate_groups(transcript, 1, 30, scored_records=scores)
        self.assertEqual(candidates[0]["segment_ids"], ["speech-7"])
        with self.assertRaises(ValueError):
            candidate_groups(transcript, scored_records="scores")

    def test_transcription_confidence_is_not_treated_as_highlight_score(self):
        transcript = [{**s, "score": 10 if i == 7 else 1} for i, s in enumerate(self.fixture(8))]
        result = candidate_groups(transcript, 1, 30)
        self.assertEqual(result[0]["scoring_source"], "local_heuristic")
        self.assertEqual(result[0]["segment_ids"], ["speech-0"])

    def test_enriched_records_are_supported_without_guessing_identity(self):
        enriched = [{"segment_id": "x", "start_time": 2, "end_time": 8, "text": "First", "score": 5},
                    {"segment_id": "y", "start_time": 12, "end_time": 18, "text": "Second", "score": 9}]
        result = candidate_groups(enriched, 1, 10)
        self.assertEqual(result[0]["segment_ids"], ["y"])
        self.assertEqual(result[0]["ranges"][0]["transcript_id"], "y")
        self.assertEqual(result[0]["scoring_source"], "ai_scores")

    def test_oversized_segment_is_split_and_count_never_fabricates_duplicates(self):
        transcript = [{"id": "long", "start": 0, "end": 120, "text": "A long passage"}]
        result = candidate_groups(transcript, 4, 30)
        self.assertEqual(len(result), 4)
        self.assertEqual([c["start"] for c in result], [0, 30, 60, 90])
        self.assertEqual(len({c["id"] for c in result}), 4)
        short = candidate_groups([{**transcript[0], "end": 2}], 4, 30)
        self.assertEqual(len(short), 1)

    def test_long_segment_prefers_word_end_boundaries(self):
        transcript = [{"id": "long", "start": 0, "end": 12, "text": "one two three", "words": [
            {"word": "one", "start": 0, "end": 3}, {"word": "two", "start": 4, "end": 7},
            {"word": "three", "start": 8, "end": 12}]}]
        result = candidate_groups(transcript, 3, 5)
        self.assertEqual([c["end"] for c in result], [3, 7, 12])
        self.assertEqual([c["text"] for c in result], ["one", "two", "three"])

    def test_type_changes_heuristic_and_options_are_bounded(self):
        transcript = [{"id": 0, "start": 0, "end": 5, "text": "A funny joke, everyone laughs."},
                      {"id": 1, "start": 10, "end": 15, "text": "How to learn: first step, next example."}]
        self.assertEqual(candidate_groups(transcript, 1, 5, "funny")[0]["segment_ids"], [0])
        self.assertEqual(candidate_groups(transcript, 1, 5, "tutorial")[0]["segment_ids"], [1])
        for kwargs in ({"desired_count": True}, {"desired_count": 0}, {"desired_count": 51},
                       {"target_duration": "30"}, {"target_duration": 0}, {"target_duration": 601},
                       {"clip_type": ""}, {"clip_type": []}):
            with self.assertRaises(ValueError):
                candidate_groups(transcript, **kwargs)
        self.assertEqual(candidate_groups([]), [])

    def test_overlapping_transcript_does_not_duplicate_source_within_candidate(self):
        transcript = [{"id": 0, "start": 0, "end": 5, "text": "First"},
                      {"id": 1, "start": 4, "end": 7, "text": "Overlap"},
                      {"id": 2, "start": 5, "end": 6, "text": "Contained"}]
        result = candidate_groups(transcript, 1, 10)[0]
        self.assertEqual(result["duration"], 7)
        self.assertEqual(result["ranges"], [{"start": 0, "end": 5, "transcript_id": 0},
                                            {"start": 5, "end": 7, "transcript_id": 1}])

    def test_ambiguous_identity_is_rejected(self):
        transcript = [{"id": 0, "start": 0, "end": 1}, {"id": "0", "start": 1, "end": 2}]
        with self.assertRaises(ValueError):
            candidate_groups(transcript)


if __name__ == "__main__":
    unittest.main()
