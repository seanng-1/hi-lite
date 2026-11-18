# SPDX-License-Identifier: GPL-3.0-or-later
"""Original-source subtitles. Round decimal seconds to nearest ms, ties up.

Blank text is omitted (after validating timing). Blank lines are removed to
protect cue boundaries; CRLF/CR become LF. Overlaps and equal-start input order
are preserved. WebVTT reserves &, < and >, so those are escaped in VTT only.
"""
from decimal import Decimal, ROUND_HALF_UP
from html import escape


class NoCaptions(ValueError):
    pass


def serialize_subtitles(cues, format):
    if format not in ("srt", "vtt"):
        raise ValueError("Unsupported subtitle format. Use srt or vtt.")
    if not isinstance(cues, list):
        raise ValueError("Transcript segments must be a list.")
    validated = []
    for index, cue in enumerate(cues):
        try:
            start, end = cue["start"], cue["end"]
            if any(isinstance(t, bool) or not isinstance(t, (int, float)) for t in (start, end)):
                raise ValueError()
            start, end = Decimal(str(start)), Decimal(str(end))
            if not start.is_finite() or not end.is_finite() or start < 0 or end <= start:
                raise ValueError()
            a, b = (int((t * 1000).to_integral_value(rounding=ROUND_HALF_UP)) for t in (start, end))
            if b <= a:
                raise ValueError()
            text = cue["text"]
            if not isinstance(text, str) or "\x00" in text:
                raise ValueError()
            text = "\n".join(line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip())
            if text:
                validated.append((start, a, b, text))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError(f"Invalid subtitle cue {index + 1}: expected finite nonnegative timing, positive millisecond duration and text.") from exc
    if not validated:
        raise NoCaptions("No captions available")
    validated.sort(key=lambda cue: cue[0])

    def timestamp(ms):
        seconds, millis = divmod(ms, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        separator = ',' if format == 'srt' else '.'
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"

    blocks = []
    for number, (_, start, end, text) in enumerate(validated, 1):
        prefix = f"{number}\n" if format == "srt" else ""
        if format == "vtt":
            text = escape(text, quote=False)
        blocks.append(f"{prefix}{timestamp(start)} --> {timestamp(end)}\n{text}\n\n")
    return ("WEBVTT\n\n" if format == "vtt" else "") + "".join(blocks)
