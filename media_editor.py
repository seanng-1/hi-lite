# SPDX-License-Identifier: GPL-3.0-or-later
"""On-demand exports from original source ranges, never pre-rendered segments.

Public API:
    export_source_ranges(source, [{"start": 1, "end": 3}], destination,
                         captions=[{"start": 1, "end": 2, "text": "Hello"}],
                         settings={"aspect_ratio": "9:16", "burn_captions": True})

Ranges remain in the supplied order (including repeats). All timestamps passed
in are original-source seconds. ``remap_captions`` and ``export_subtitles`` map
edited captions onto the cut timeline. A range can override the global
``crop: {x: 0.5, y: 0.5}`` focal point, normalized to [0, 1]. Output dimensions
use the largest even-sized crop that fits the source, without upscaling.

Only requested ranges are rendered, in a private staging directory. Processes
are reaped on failure/cancellation, and the destination is replaced only after
the entire render succeeds. A threading.Event-compatible ``cancel_event`` and
``progress_callback(percent)`` are optional. The result includes the output
path, dimensions, duration, and SRT/VTT strings (not public download URLs).
"""
import json
import math
import os
from bisect import bisect_left
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from subtitle_serializer import NoCaptions, serialize_subtitles

FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_PATH", "ffprobe")
ASPECT_RATIOS = {"original": None, "9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1)}
MAX_SOURCE_RANGES = 100000
MAX_SOURCE_CAPTIONS = 50000
MAX_REMAPPED_CAPTIONS = 250000
MAX_REMAPPED_TEXT = 16 * 1024 * 1024


class ExportCancelled(RuntimeError):
    """The caller cancelled a render; no partial export was published."""


def _number(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number.")
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number.") from None
    if (not math.isfinite(value) or value < minimum or
            maximum is not None and value > maximum):
        raise ValueError(f"{name} must be a finite number between {minimum} and {maximum}.")
    return value


def _crop(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"x", "y"}:
        raise ValueError("Crop must contain normalized x and y focal points.")
    return {axis: _number(value.get(axis, 0.5), f"Crop {axis}", 0, 1) for axis in ("x", "y")}


def validate_source_ranges(ranges, duration=None):
    """Return normalized ranges, rejecting bad timings rather than guessing."""
    if not isinstance(ranges, list) or not ranges or len(ranges) > MAX_SOURCE_RANGES:
        raise ValueError(f"Choose between 1 and {MAX_SOURCE_RANGES} source ranges.")
    if duration is not None:
        duration = _number(duration, "Source duration", 0.001)
    result = []
    for index, item in enumerate(ranges):
        if not isinstance(item, dict):
            raise ValueError(f"Invalid source range {index + 1}.")
        start = _number(item.get("start"), "Range start")
        end = _number(item.get("end"), "Range end")
        if end - start < 0.001 or duration is not None and end > duration + 0.000001:
            raise ValueError(f"Source range {index + 1} is empty or outside the recording.")
        result.append({"start": start, "end": end, **({"crop": _crop(item["crop"])} if "crop" in item else {})})
    return result


def coalesce_source_ranges(ranges, default_crop=None):
    """Collapse exactly contiguous, identically framed ranges in O(range count).

    No sorting, tolerance or gap filling is used. Repeats, backwards jumps and
    crop changes remain separate render operations. Missing crop values use the
    export's default crop, so an explicit equivalent default also coalesces.
    The caller's logical edit document is never changed.
    """
    fallback = _crop(default_crop)
    result = []
    for item in validate_source_ranges(ranges):
        current = {**item, "crop": item.get("crop", fallback).copy()}
        if result and result[-1]["end"] == current["start"] and result[-1]["crop"] == current["crop"]:
            result[-1]["end"] = current["end"]
        else:
            result.append(current)
    return result


def validate_captions(captions, duration=None):
    """Validate editable source-time cues; empty text intentionally hides a cue."""
    if not isinstance(captions, list) or len(captions) > MAX_SOURCE_CAPTIONS:
        raise ValueError(f"Captions must be a list of at most {MAX_SOURCE_CAPTIONS} cues.")
    result = []
    for index, cue in enumerate(captions):
        if not isinstance(cue, dict):
            raise ValueError(f"Invalid caption {index + 1}.")
        start = _number(cue.get("start"), "Caption start")
        end = _number(cue.get("end"), "Caption end")
        text = cue.get("text")
        if (end <= start or duration is not None and end > duration + 0.000001 or
                not isinstance(text, str) or "\x00" in text or len(text) > 20000):
            raise ValueError(f"Invalid caption {index + 1}: check timing and text.")
        text = "\n".join(line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip())
        result.append({"start": start, "end": end, "text": text})
    return sorted(result, key=lambda cue: cue["start"])


def validate_export_settings(settings=None):
    settings = {} if settings is None else settings
    if not isinstance(settings, dict):
        raise ValueError("Export settings must be an object.")
    aspect = settings.get("aspect_ratio", "original")
    if not isinstance(aspect, str) or aspect not in ASPECT_RATIOS:
        raise ValueError("Aspect ratio must be original, 9:16, 16:9 or 1:1.")
    burn = settings.get("burn_captions", False)
    if not isinstance(burn, bool):
        raise ValueError("burn_captions must be a boolean.")
    style = settings.get("caption_style", {})
    if not isinstance(style, dict):
        raise ValueError("Caption style must be an object.")
    position = style.get("position", "bottom")
    if position not in ("bottom", "center", "top"):
        raise ValueError("Caption position must be bottom, center or top.")
    font = style.get("font_family", "Arial")
    if not isinstance(font, str) or not re.fullmatch(r"[A-Za-z0-9 _-]{1,64}", font):
        raise ValueError("Invalid caption font family.")
    colors = {}
    for key, default in (("font_color", "#FFFFFF"), ("outline_color", "#000000")):
        color = style.get(key, default)
        if not isinstance(color, str) or not re.fullmatch(r"#[a-fA-F0-9]{6}", color):
            raise ValueError(f"{key} must be a #RRGGBB color.")
        colors[key] = color.upper()
    return {"aspect_ratio": aspect, "crop": _crop(settings.get("crop")), "burn_captions": burn,
            "caption_style": {"font_family": font, "font_size": _number(style.get("font_size", 42), "Font size", 8, 120),
                              "outline_width": _number(style.get("outline_width", 2), "Outline width", 0, 10),
                              "position": position, **colors}}


def remap_captions(captions, ranges):
    """Clip/shift cues to reel time without splitting at continuous source seams.

    An interval index avoids a full transcript scan for each of up to 100000
    logical cuts. Repeated footage legitimately duplicates cues, but expansion
    is bounded to 250000 output cues and 16 Mi characters of output cue text.
    """
    cues = [cue for cue in validate_captions(captions) if cue["text"]]
    selected = validate_source_ranges(ranges)
    # Framing does not change the caption timeline. Adjacent source time is
    # continuous even when two video ranges need distinct crop settings.
    selected = coalesce_source_ranges([{"start": item["start"], "end": item["end"]} for item in selected])
    if not cues:
        return []
    starts = [cue["start"] for cue in cues]
    # A max-end interval tree handles overlapping/nested cues as well as the
    # usual disjoint transcript. Traversal preserves equal-start input order.
    size = 1 << (len(cues) - 1).bit_length()
    max_ends = [0.0] * (2 * size)
    max_ends[size:size + len(cues)] = [cue["end"] for cue in cues]
    for node in range(size - 1, 0, -1):
        max_ends[node] = max(max_ends[node * 2], max_ends[node * 2 + 1])

    def overlaps(start, end):
        last = bisect_left(starts, end)
        stack = [(1, 0, size)]
        while stack:
            node, left, right = stack.pop()
            if left >= last or max_ends[node] <= start:
                continue
            if right - left == 1:
                yield cues[left]
            else:
                middle = (left + right) // 2
                stack.append((node * 2 + 1, middle, right))
                stack.append((node * 2, left, middle))

    result, offset, text_size = [], 0.0, 0
    for selected_range in selected:
        start, end = selected_range["start"], selected_range["end"]
        for cue in overlaps(start, end):
            left, right = max(start, cue["start"]), min(end, cue["end"])
            # Tiny clipped slivers cannot be represented by millisecond subtitles.
            if right - left >= 0.001:
                text_size += len(cue["text"])
                if len(result) >= MAX_REMAPPED_CAPTIONS or text_size > MAX_REMAPPED_TEXT:
                    raise ValueError("Selected repeats produce too many captions; export smaller groups.")
                result.append({"start": round(offset + left - start, 9),
                               "end": round(offset + right - start, 9), "text": cue["text"]})
        offset += end - start
    return sorted(result, key=lambda cue: cue["start"])


def export_subtitles(captions, ranges, format="srt"):
    """Return SRT/WebVTT on the final cut timeline; raise NoCaptions when empty."""
    return serialize_subtitles(remap_captions(captions, ranges), format)


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise ExportCancelled("Export cancelled.")


def _terminate(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()
    except ProcessLookupError:
        process.wait()


def _run_process(command, *, cancel_event=None, cwd=None, timeout=None):
    """Drain to files, not pipes, so verbose ffmpeg cannot deadlock or exhaust RAM."""
    _check_cancel(cancel_event)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        options = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   cwd=cwd, **options)
        try:
            deadline = time.monotonic() + timeout if timeout else None
            while process.poll() is None:
                _check_cancel(cancel_event)
                if deadline and time.monotonic() >= deadline:
                    raise TimeoutError("Media probe timed out.")
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            _check_cancel(cancel_event)
            if process.returncode:
                stderr.seek(max(0, stderr.tell() - 16000))
                detail = stderr.read().decode("utf-8", "replace").strip()
                raise RuntimeError(f"Media processing failed ({process.returncode}): {detail[-3000:]}")
            stdout.seek(0)
            return stdout.read().decode("utf-8", "replace")
        finally:
            _terminate(process)


def probe_source(source_path, *, cancel_event=None):
    """Probe real display dimensions (including SAR/rotation), duration and audio."""
    result = _run_process([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json",
                           str(source_path)], cancel_event=cancel_event, timeout=30)
    try:
        data = json.loads(result)
        stream = next(s for s in data["streams"] if s.get("codec_type") == "video" and
                      not s.get("disposition", {}).get("attached_pic"))
        duration = float(data.get("format", {}).get("duration") or stream["duration"])
        width, height = int(stream["width"]), int(stream["height"])
        sar = stream.get("sample_aspect_ratio", "1:1").split(":")
        if len(sar) == 2 and float(sar[0]) > 0 and float(sar[1]) > 0:
            width = round(width * float(sar[0]) / float(sar[1]))
        rotation = float(stream.get("tags", {}).get("rotate", 0))
        for side in stream.get("side_data_list", []):
            if "rotation" in side:
                rotation = float(side["rotation"])
        if round(rotation) % 180 == 90:
            width, height = height, width
        rate = stream.get("avg_frame_rate", "30/1").split("/")
        fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else 30.0
        if not math.isfinite(fps) or fps <= 0:
            fps = 30.0
        if not math.isfinite(duration) or duration <= 0 or min(width, height) < 2:
            raise ValueError()
        return {"duration": duration, "width": width, "height": height, "fps": min(fps, 120),
                "has_audio": any(s.get("codec_type") == "audio" for s in data["streams"])}
    except (KeyError, ValueError, TypeError, StopIteration, ZeroDivisionError) as exc:
        raise ValueError("Source is not a readable video with a known duration.") from exc


def crop_geometry(width, height, aspect_ratio="original", crop=None):
    """Return even output width/height and bounded pixel crop origin."""
    focal = _crop(crop)
    if aspect_ratio not in ASPECT_RATIOS or min(width, height) < 2:
        raise ValueError("Invalid crop dimensions or aspect ratio.")
    ratio = ASPECT_RATIOS[aspect_ratio]
    if ratio is None:
        out_width, out_height = int(width) // 2 * 2, int(height) // 2 * 2
    else:
        unit = int(min(width / ratio[0], height / ratio[1])) // 2 * 2
        if unit < 2:
            raise ValueError("Source dimensions are too small for this aspect ratio.")
        out_width, out_height = ratio[0] * unit, ratio[1] * unit
    # A normalized focal point is centered in the viewport, clamped at source edges.
    x = max(0, min(width - out_width, focal["x"] * width - out_width / 2))
    y = max(0, min(height - out_height, focal["y"] * height - out_height / 2))
    return {"width": out_width, "height": out_height, "x": int(x) // 2 * 2, "y": int(y) // 2 * 2}


def _ass_subtitles(cues, width, height, style):
    def color(value):
        return "&H00" + value[5:7] + value[3:5] + value[1:3]

    def timestamp(value):
        total = int(value * 100 + 0.5)
        seconds, fraction = divmod(total, 100)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"

    # Font size is expressed against a 1080px short edge, preserving legibility
    # relative to the viewport when the user switches portrait/square/landscape.
    scale = min(width, height) / 1080
    size = max(8, style["font_size"] * scale)
    outline = max(0, style["outline_width"] * scale)
    margin_x, margin_y = max(2, round(width * .05)), max(2, round(height * .06))
    alignment = {"bottom": 2, "center": 5, "top": 8}[style["position"]]
    header = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n"
              "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
              "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
              "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
              f"Style: Default,{style['font_family']},{size:.2f},{color(style['font_color'])},&H000000FF,"
              f"{color(style['outline_color'])},&H80000000,0,0,0,0,100,100,0,0,1,{outline:.2f},0,"
              f"{alignment},{margin_x},{margin_x},{margin_y},1\n\n[Events]\n"
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    for cue in cues:
        # Neutralize ASS override markup: user-edited text is never filter code.
        text = cue["text"].replace("\\", "\uFE68").replace("{", "\uFF5B").replace("}", "\uFF5D").replace("\n", "\\N")
        if timestamp(cue["start"]) != timestamp(cue["end"]):
            lines.append(f"Dialogue: 0,{timestamp(cue['start'])},{timestamp(cue['end'])},Default,,0,0,0,,{text}\n")
    return header + "".join(lines)


def export_source_ranges(source_path, ranges, output_path, *, captions=None, settings=None,
                         cancel_event=None, progress_callback=None):
    """Render ordered source ranges and atomically publish one MP4.

    ``subtitles`` in the returned dict contains SRT/VTT text, ready for a caller
    to publish within its own project/job namespace. No global files are used.
    Cancellation raises ExportCancelled and leaves an existing output intact.
    """
    source, output = Path(source_path).resolve(), Path(output_path).resolve()
    if not source.is_file() or source == output:
        raise ValueError("Choose an existing source and a different output path.")
    if output.suffix.lower() != ".mp4":
        raise ValueError("Video exports must use an .mp4 destination.")
    selected = validate_source_ranges(ranges)
    options = validate_export_settings(settings)
    source_captions = validate_captions([] if captions is None else captions)
    _check_cancel(cancel_event)
    info = probe_source(source, cancel_event=cancel_event)
    selected = validate_source_ranges(selected, info["duration"])
    # Render physical continuity once, not once per transcript sentence. Work
    # stays sequential (one ffmpeg process at a time), even for gapped reels.
    selected = coalesce_source_ranges(selected, options["crop"])
    source_captions = validate_captions(source_captions, info["duration"])
    cues = remap_captions(source_captions, selected)
    geometry = crop_geometry(info["width"], info["height"], options["aspect_ratio"], options["crop"])
    subtitles = {}
    if cues:
        subtitles = {fmt: serialize_subtitles(cues, fmt) for fmt in ("srt", "vtt")}
    elif options["burn_captions"]:
        raise NoCaptions("No captions overlap the selected source ranges.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".hi-lite-export-", dir=output.parent) as temporary:
        stage = Path(temporary)
        parts = []
        for index, item in enumerate(selected):
            _check_cancel(cancel_event)
            crop = crop_geometry(info["width"], info["height"], options["aspect_ratio"],
                                 item.get("crop", options["crop"]))
            name = f"range-{index:05d}.mp4"
            filters = (f"scale={info['width']}:{info['height']},setsar=1,"
                       f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']},setpts=PTS-STARTPTS")
            command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-ss", f"{item['start']:.9f}", "-i", str(source),
                       "-t", f"{item['end'] - item['start']:.9f}", "-map", "0:v:0", "-vf", filters,
                       "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                       "-threads", "2", "-r", f"{info['fps']:.9f}"]
            if info["has_audio"]:
                command += ["-map", "0:a:0", "-af", "asetpts=PTS-STARTPTS,apad", "-c:a", "aac",
                            "-ar", "48000", "-ac", "2"]
            else:
                command += ["-an"]
            command += ["-map_metadata", "-1", "-movflags", "+faststart", str(stage / name)]
            _run_process(command, cancel_event=cancel_event)
            parts.append(f"file '{name}'\n")
            if progress_callback:
                progress_callback(round((index + 1) / len(selected) * 85))
        (stage / "ranges.txt").write_text("".join(parts), encoding="utf-8")
        total_duration = sum(item["end"] - item["start"] for item in selected)
        command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat",
                   "-safe", "1", "-i", "ranges.txt", "-t", f"{total_duration:.9f}"]
        if options["burn_captions"]:
            (stage / "captions.ass").write_text(_ass_subtitles(cues, geometry["width"], geometry["height"],
                                                               options["caption_style"]), encoding="utf-8")
            command += ["-vf", "ass=filename=captions.ass", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-threads", "2", "-c:a", "copy"]
        else:
            command += ["-c", "copy"]
        command += ["-movflags", "+faststart", "complete.mp4"]
        _run_process(command, cancel_event=cancel_event, cwd=str(stage))
        completed = stage / "complete.mp4"
        if not completed.is_file() or completed.stat().st_size == 0:
            raise RuntimeError("Media processing produced an empty export.")
        _check_cancel(cancel_event)
        os.replace(completed, output)
    if progress_callback:
        progress_callback(100)
    return {"path": str(output), "duration": round(total_duration, 9), "width": geometry["width"],
            "height": geometry["height"], "subtitles": subtitles}
