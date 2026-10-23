# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure cut/caption contracts plus small, local ffmpeg export regressions."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import media_editor as editor


class EditingContracts(unittest.TestCase):
    def test_large_contiguous_document_coalesces_without_mutation(self):
        ranges = [{"start": index, "end": index + 1} for index in range(10000)]
        original = copy.deepcopy(ranges)
        self.assertEqual(editor.coalesce_source_ranges(ranges),
                         [{"start": 0, "end": 10000, "crop": {"x": .5, "y": .5}}])
        self.assertEqual(ranges, original)
        self.assertEqual(len(editor.validate_source_ranges(ranges * 10)), 100000)
        with self.assertRaisesRegex(ValueError, "100000"):
            editor.validate_source_ranges(ranges * 10 + ranges[:1])

    def test_coalescing_respects_order_gaps_repeats_and_effective_crop(self):
        ranges = [{"start": 2, "end": 3}, {"start": 3, "end": 4, "crop": {"x": .2}},
                  {"start": 4, "end": 5, "crop": {"x": .8}}, {"start": 6, "end": 7},
                  {"start": 0, "end": 1}, {"start": 0, "end": 1},
                  {"start": 1.00000001, "end": 2}]
        result = editor.coalesce_source_ranges(ranges, {"x": .2})
        self.assertEqual([(r["start"], r["end"]) for r in result],
                         [(2, 4), (4, 5), (6, 7), (0, 1), (0, 1), (1.00000001, 2)])
        self.assertEqual(result[0]["crop"], {"x": .2, "y": .5})

    def test_captions_continue_through_adjacent_ranges_even_across_crop_change(self):
        cues = [{"start": 0, "end": 4, "text": "continuous"}]
        cuts = [{"start": 0, "end": 1}, {"start": 1, "end": 2, "crop": {"x": 0}},
                {"start": 3, "end": 4}, {"start": 0, "end": 1}]
        self.assertEqual(editor.remap_captions(cues, cuts),
                         [{"start": 0, "end": 2, "text": "continuous"},
                          {"start": 2, "end": 3, "text": "continuous"},
                          {"start": 3, "end": 4, "text": "continuous"}])

    def test_large_caption_index_preserves_nested_overlaps_and_reordering(self):
        cues = [{"start": index, "end": index + .5, "text": str(index)} for index in range(10000)]
        cues.insert(0, {"start": 0, "end": 10000, "text": "overlap"})
        cuts = [{"start": index, "end": index + .5} for index in range(9999, -1, -1)]
        result = editor.remap_captions(cues, cuts)
        self.assertEqual(len(result), 20000)
        self.assertEqual([cue["text"] for cue in result[:4]], ["overlap", "9999", "overlap", "9998"])
        self.assertEqual(result[-1], {"start": 4999.5, "end": 5000, "text": "0"})

    def test_caption_repeat_expansion_is_bounded(self):
        cues = [{"start": 0, "end": 1, "text": "repeated"}]
        cuts = [{"start": 0, "end": 1}] * 3
        with patch.object(editor, "MAX_REMAPPED_CAPTIONS", 2), self.assertRaisesRegex(ValueError, "smaller groups"):
            editor.remap_captions(cues, cuts)
        with patch.object(editor, "MAX_REMAPPED_TEXT", 10), self.assertRaisesRegex(ValueError, "smaller groups"):
            editor.remap_captions(cues, cuts)

    def test_pipeline_defaults_to_metadata_and_legacy_render_is_explicit(self):
        import main as pipeline
        transcript = {"segments": [{"id": "one", "start": 0, "end": 1, "text": "Hello"}]}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(pipeline, "extract_audio"), patch.object(pipeline, "transcribe_audio", return_value=transcript), \
             patch.object(pipeline, "export_segments_to_excel"), patch.object(pipeline, "save_segment_manifest"), \
             patch.object(pipeline, "build_segment_metadata", return_value=[{"id": 0}]) as metadata, \
             patch.object(pipeline, "extract_all_segments", return_value=[{"id": 0}]) as render:
            pipeline.main("source.mp4", folder)
            metadata.assert_called_once()
            render.assert_not_called()
            pipeline.main("source.mp4", folder, render_segments=True)
            render.assert_called_once()

    def test_range_validation_and_order(self):
        ranges = [{"start": 4, "end": 6, "crop": {"x": 1}}, {"start": 0, "end": 1},
                  {"start": 4, "end": 6}]
        original = copy.deepcopy(ranges)
        result = editor.validate_source_ranges(ranges, 6)
        self.assertEqual([r["start"] for r in result], [4, 0, 4])
        self.assertEqual(result[0]["crop"], {"x": 1, "y": .5})
        self.assertEqual(ranges, original)
        for invalid in (None, [], [{"start": True, "end": 1}], [{"start": 0, "end": float("nan")}],
                        [{"start": -1, "end": 1}], [{"start": 1, "end": 1}],
                        [{"start": 0, "end": 7}], [{"start": "0", "end": 1}],
                        [{"start": 0, "end": 1, "crop": {"x": 2}}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                editor.validate_source_ranges(invalid, 6)

    def test_captions_clip_reorder_and_repeat_through_cuts(self):
        cues = [{"start": 1, "end": 4, "text": "first"}, {"start": 4, "end": 7, "text": "second"},
                {"start": 0, "end": 1, "text": "not selected"}, {"start": 2, "end": 3, "text": " "}]
        cuts = [{"start": 5, "end": 6}, {"start": 2, "end": 5}, {"start": 5, "end": 6}]
        original = copy.deepcopy(cues)
        expected = [{"start": 0, "end": 1, "text": "second"}, {"start": 1, "end": 3, "text": "first"},
                    {"start": 3, "end": 5, "text": "second"}]
        self.assertEqual(editor.remap_captions(cues, cuts), expected)
        self.assertEqual(cues, original)
        self.assertIn("00:00:01,000 --> 00:00:03,000\nfirst", editor.export_subtitles(cues, cuts))
        self.assertTrue(editor.export_subtitles(cues, cuts, "vtt").startswith("WEBVTT"))

    def test_empty_and_invalid_caption_exports(self):
        cuts = [{"start": 2, "end": 3}]
        for cues in ([], [{"start": 0, "end": 1, "text": "before"}]):
            with self.assertRaises(editor.NoCaptions):
                editor.export_subtitles(cues, cuts)
        for cue in ({"start": 0, "end": 1, "text": None}, {"start": 2, "end": 1, "text": "bad"},
                    {"start": False, "end": 1, "text": "bad"}, {"start": 0, "end": 1, "text": "\0"}):
            with self.assertRaises(ValueError):
                editor.validate_captions([cue])

    def test_settings_reject_code_and_nonfinite_values(self):
        for settings in ({"aspect_ratio": "4:3"}, {"burn_captions": "yes"}, {"crop": {"x": float("inf")}},
                         {"caption_style": {"font_family": "Arial,foo"}},
                         {"caption_style": {"font_color": "red:movie=evil"}},
                         {"caption_style": {"font_size": True}}, {"caption_style": {"font_size": 10 ** 400}},
                         {"caption_style": {"position": "left"}}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                editor.validate_export_settings(settings)
        self.assertFalse(editor.validate_export_settings()["burn_captions"])

    def test_crop_is_even_exact_aspect_and_clamped(self):
        for aspect, ratio in (("9:16", 9 / 16), ("16:9", 16 / 9), ("1:1", 1)):
            for x, y in ((0, 0), (.5, .5), (1, 1)):
                crop = editor.crop_geometry(1920, 1080, aspect, {"x": x, "y": y})
                self.assertAlmostEqual(crop["width"] / crop["height"], ratio)
                self.assertTrue(all(value % 2 == 0 for value in crop.values()))
                self.assertLessEqual(crop["x"] + crop["width"], 1920)
                self.assertLessEqual(crop["y"] + crop["height"], 1080)
        self.assertEqual(editor.crop_geometry(161, 91)["width"], 160)
        self.assertLess(editor.crop_geometry(1920, 1080, "9:16", {"x": 0})["x"],
                        editor.crop_geometry(1920, 1080, "9:16", {"x": 1})["x"])

    def test_ass_neutralizes_caption_markup_and_uses_viewport(self):
        style = editor.validate_export_settings()["caption_style"]
        ass = editor._ass_subtitles([{"start": 0, "end": 1, "text": r"{\p1} hello"}], 360, 640, style)
        self.assertIn("PlayResX: 360", ass)
        self.assertIn("PlayResY: 640", ass)
        self.assertNotIn(r"{\p1}", ass)

    def test_metadata_preserves_smart_buffer_and_identity_without_rendering(self):
        from video_processor import build_segment_metadata, get_smart_buffer
        transcript = {"segments": [{"id": "a", "start": 1, "end": 2, "text": "one",
                                    "words": [{"start": 1.2, "end": 1.9}]},
                                   {"id": "b", "start": 2.2, "end": 3, "text": "two"},
                                   {"id": "c", "start": 4, "end": 5, "text": ""}]}
        with patch.dict(os.environ, {"HI_LITE_PRE_BUFFER": ".5", "HI_LITE_POST_BUFFER": ".75"}), \
             patch.object(editor, "probe_source", return_value={"duration": 6}), \
             patch.object(subprocess, "run") as render:
            result = build_segment_metadata("source.mp4", transcript)
            expected = get_smart_buffer(transcript["segments"][0], None, transcript["segments"][1], 6)
        render.assert_not_called()
        self.assertEqual([r["id"] for r in result], [0, 1])
        self.assertEqual([r["transcript_id"] for r in result], ["a", "b"])
        self.assertEqual((result[0]["start"], result[0]["end"]), expected)
        self.assertEqual(result[0]["end"], result[1]["start"])
        self.assertTrue(all(r["path"] is None and r["rendered"] is False for r in result))


class ProcessLifecycle(unittest.TestCase):
    def test_export_renders_10000_contiguous_ranges_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder, "source.mp4"), Path(folder, "out.mp4")
            source.write_bytes(b"source")
            info = {"duration": 10000, "width": 160, "height": 90, "fps": 10, "has_audio": False}

            def render(command, **kwargs):
                target = Path(kwargs.get("cwd", folder)) / command[-1]
                target.write_bytes(b"finished")

            with patch.object(editor, "probe_source", return_value=info), \
                 patch.object(editor, "_run_process", side_effect=render) as process:
                result = editor.export_source_ranges(source,
                    [{"start": index, "end": index + 1} for index in range(10000)], output,
                    captions=[{"start": 0, "end": 10000, "text": "one continuous cue"}])
            self.assertEqual(process.call_count, 2)  # one source render plus final mux
            self.assertEqual(result["duration"], 10000)
            self.assertEqual(result["subtitles"]["srt"].count("one continuous cue"), 1)

    def test_cancellation_reaps_process(self):
        event = threading.Event()
        process = []
        popen = subprocess.Popen

        def record(*args, **kwargs):
            child = popen(*args, **kwargs)
            process.append(child)
            event.set()
            return child

        with patch.object(editor.subprocess, "Popen", side_effect=record), self.assertRaises(editor.ExportCancelled):
            editor._run_process([sys.executable, "-c", "import time; time.sleep(30)"], cancel_event=event)
        self.assertIsNotNone(process[0].poll())

    def test_failure_and_timeout_are_reaped(self):
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            editor._run_process([sys.executable, "-c", "import sys; print('fixture failure', file=sys.stderr); sys.exit(2)"])
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            editor._run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.05)
        self.assertLess(time.monotonic() - start, 5)

    def test_export_error_preserves_old_output_and_cleans_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder, "source.mp4"), Path(folder, "output.mp4")
            source.write_bytes(b"source")
            output.write_bytes(b"old export")
            info = {"duration": 3, "width": 160, "height": 90, "fps": 10, "has_audio": False}
            with patch.object(editor, "probe_source", return_value=info), \
                 patch.object(editor, "_run_process", side_effect=RuntimeError("render failed")), \
                 self.assertRaisesRegex(RuntimeError, "render failed"):
                editor.export_source_ranges(source, [{"start": 0, "end": 1}], output)
            self.assertEqual(output.read_bytes(), b"old export")
            self.assertFalse(list(Path(folder).glob(".hi-lite-export-*")))

    def test_precancelled_export_never_probes(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "source.mp4")
            source.write_bytes(b"source")
            event = threading.Event()
            event.set()
            with patch.object(editor, "probe_source") as probe, self.assertRaises(editor.ExportCancelled):
                editor.export_source_ranges(source, [{"start": 0, "end": 1}], Path(folder, "out.mp4"), cancel_event=event)
            probe.assert_not_called()


@unittest.skipUnless(shutil.which(editor.FFMPEG) and shutil.which(editor.FFPROBE), "ffmpeg and ffprobe required")
class RealMediaExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix="hi-lite-range-tests-")
        cls.addClassCleanup(cls.folder.cleanup)
        cls.source = Path(cls.folder.name, "source.mp4")
        colors = "color=c=red:s=160x90:r=10,drawbox=c=green:t=fill:enable='gte(t,1)*lt(t,2)',drawbox=c=blue:t=fill:enable='gte(t,2)'"
        subprocess.run([editor.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", colors,
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1", "-c:a", "aac", str(cls.source)], check=True)

    def test_reordered_ranges_captions_audio_and_aspects(self):
        cuts = [{"start": 2, "end": 2.5}, {"start": .5, "end": 1.5}]
        captions = [{"start": 0, "end": 1, "text": "first edited"}, {"start": 2, "end": 3, "text": "last edited"}]
        for aspect in editor.ASPECT_RATIOS:
            with self.subTest(aspect=aspect):
                output = Path(self.folder.name, aspect.replace(":", "-") + ".mp4")
                progress = []
                result = editor.export_source_ranges(self.source, cuts, output, captions=captions,
                    settings={"aspect_ratio": aspect, "crop": {"x": 1, "y": 0}}, progress_callback=progress.append)
                info = editor.probe_source(output)
                self.assertAlmostEqual(info["duration"], 1.5, delta=.15)
                self.assertTrue(info["has_audio"])
                self.assertEqual((info["width"], info["height"]), (result["width"], result["height"]))
                self.assertLess(result["subtitles"]["srt"].index("last edited"), result["subtitles"]["srt"].index("first edited"))
                self.assertEqual(progress[-1], 100)
                self.assertFalse(list(Path(self.folder.name).glob(".hi-lite-export-*")))
                # The first selected cut is blue; after its half-second the
                # reordered recording jumps back to red. Verify actual pixels,
                # not only metadata, so an accidental chronological sort fails.
                for moment, channel in ((.1, 2), (.6, 0)):
                    frame = subprocess.check_output([editor.FFMPEG, "-v", "error", "-ss", str(moment),
                        "-i", str(output), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
                    averages = [sum(frame[index::3]) / (len(frame) / 3) for index in range(3)]
                    self.assertGreater(averages[channel], 150, averages)
                    self.assertEqual(averages.index(max(averages)), channel)

    def test_caption_burn_in_and_silent_source(self):
        silent = Path(self.folder.name, "silent.mp4")
        subprocess.run([editor.FFMPEG, "-v", "error", "-y", "-i", str(self.source), "-c:v", "copy", "-an", str(silent)], check=True)
        output = Path(self.folder.name, "captioned.mp4")
        result = editor.export_source_ranges(silent, [{"start": 0, "end": 1}], output,
            captions=[{"start": 0, "end": 1, "text": "Edited captions"}],
            settings={"burn_captions": True, "aspect_ratio": "1:1", "caption_style": {"position": "top", "font_color": "#FFCC00"}})
        self.assertTrue(output.is_file())
        self.assertIn("Edited captions", result["subtitles"]["vtt"])
        self.assertFalse(editor.probe_source(output)["has_audio"])


if __name__ == "__main__":
    unittest.main()
