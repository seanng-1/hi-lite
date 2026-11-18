# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure, non-destructive source-time editing and reviewable local planning.

A version-1 snapshot contains source_duration and an ordered list of ranges:
{id, clip_id, start, end, group_id}. Times are absolute source seconds, not
buffer-relative clip times. clip_id identifies the original manifest clip;
group_id links consecutive ranges into one edited clip without filling cuts.
Every public editor operation returns a fresh JSON-safe snapshot. Keep prior
snapshots for undo/redo; this module never mutates a transcript or media file.

Candidate planning is an offline heuristic, not an AI/provider invocation.
Optional previously validated AI scores can inform its suggestions, but review
and acceptance remain separate from generation.
"""
from bisect import bisect_right
from copy import deepcopy
import hashlib
import math
import re

VERSION = 1
DEFAULT_FILLERS = ("um", "uh", "erm", "hmm", "ah")


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number.")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be a finite number.") from None
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'} and finite.")
    return result


def _identifier(value, name):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.strip() and len(value) <= 256 and "\x00" not in value:
        return value
    raise ValueError(f"{name} must be a nonnegative integer or nonempty string.")


def _key(value):
    return type(value), value


def _records(value):
    if isinstance(value, dict):
        value = value.get("segments")
    if not isinstance(value, list):
        raise ValueError("Segments must be a list.")
    return value


def _source_segments(value):
    records, seen = [], set()
    for index, raw in enumerate(_records(value)):
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid segment {index + 1}.")
        sid = _identifier(raw.get("id"), "Segment id")
        if _key(sid) in seen:
            raise ValueError("Segment ids must be unique.")
        seen.add(_key(sid))
        start = _number(raw.get("start"), "Segment start")
        end = _number(raw.get("end"), "Segment end", positive=True)
        if end <= start:
            raise ValueError("Segment end must be after its start.")
        records.append({**raw, "id": sid, "start": start, "end": end})
    return records


def initial_edits(segments, source_duration=None):
    """Select all manifest clips, retaining their existing buffered boundaries."""
    segments = _source_segments(segments)
    duration = max((s["end"] for s in segments), default=0.0)
    if source_duration is not None:
        duration = _number(source_duration, "Source duration")
    snapshot = {"version": VERSION, "source_duration": duration, "ranges": [
        {"id": f"range-{i + 1}", "clip_id": s["id"], "start": s["start"],
         "end": s["end"], "group_id": f"group-{i + 1}"}
        for i, s in enumerate(segments)]}
    return validate_edits(snapshot, segments=segments, source_duration=duration)


def validate_edits(edits, segments=None, source_duration=None):
    """Validate untrusted snapshots and return a normalized independent copy.

    Supply authoritative segments/duration at persistence and export boundaries.
    Without them, the snapshot still validates structurally but cannot establish
    its relationship to the current recording. Source overlap is allowed: ranges
    can intentionally repeat footage or arrive from overlapping source segments.
    """
    if not isinstance(edits, dict) or type(edits.get("version")) is not int or edits["version"] != VERSION:
        raise ValueError("Unsupported edit snapshot version.")
    duration = _number(edits.get("source_duration"), "Source duration")
    if source_duration is not None:
        actual = _number(source_duration, "Source duration")
        if duration > actual:
            raise ValueError("Edit duration exceeds the current source duration.")
    if not isinstance(edits.get("ranges"), list):
        raise ValueError("Edit ranges must be a list.")
    lookup = None
    if segments is not None:
        lookup = {_key(s["id"]): s for s in _source_segments(segments)}
    ranges, ids = [], set()
    for raw in edits["ranges"]:
        if not isinstance(raw, dict):
            raise ValueError("Each edit range must be an object.")
        rid = raw.get("id")
        if not isinstance(rid, str):
            raise ValueError("Range id must be a string.")
        _identifier(rid, "Range id")
        if rid in ids:
            raise ValueError("Range ids must be unique.")
        ids.add(rid)
        clip_id = _identifier(raw.get("clip_id"), "Clip id")
        group_id = raw.get("group_id", rid)
        if not isinstance(group_id, str):
            raise ValueError("Group id must be a string.")
        _identifier(group_id, "Group id")
        start = _number(raw.get("start"), "Range start")
        end = _number(raw.get("end"), "Range end", positive=True)
        if end <= start or end > duration:
            raise ValueError("Range must have positive duration within the source.")
        if lookup is not None:
            source = lookup.get(_key(clip_id))
            if source is None:
                raise ValueError("Edit refers to an unknown source clip.")
            if start < source["start"] or end > source["end"]:
                raise ValueError("Edit range exceeds its source clip boundaries.")
        ranges.append({"id": rid, "clip_id": clip_id, "start": start,
                       "end": end, "group_id": group_id})
    return {"version": VERSION, "source_duration": duration, "ranges": ranges}


def _fresh_id(existing, base):
    number = 2
    result = f"{base}-{number}"
    while result in existing:
        number += 1
        result = f"{base}-{number}"
    existing.add(result)
    return result


def _selection(action, ranges, field):
    values = action.get(field)
    if not isinstance(values, list) or not values:
        raise ValueError(f"{field} must be a nonempty list.")
    seen = set()
    known = {_key(r["id" if field == "range_ids" else "clip_id"]) for r in ranges}
    for value in values:
        _identifier(value, field)
        key = _key(value)
        if key in seen or key not in known:
            raise ValueError(f"{field} contains a duplicate or unknown id.")
        seen.add(key)
    return seen


def _interval(action, duration):
    start = _number(action.get("start"), "Selection start")
    end = _number(action.get("end"), "Selection end", positive=True)
    if end <= start or end > duration:
        raise ValueError("Selection must be within the source with positive duration.")
    return start, end


def apply_edit(edits, action):
    """Return a new snapshot for remove, keep, trim, split, combine or reorder.

    remove/keep: exactly one of range_ids, clip_ids, or start/end. Time-based
    operations affect every occurrence of that source passage. keep intersects
    the current selection; it never silently restores previously removed media.
    trim: range_id/start/end, restricted to the existing range.
    split: range_id/at, strictly inside the range.
    combine: consecutive range_ids; links them, retaining every source cut.
    reorder: every range_id exactly once, in desired output order.
    """
    snapshot = validate_edits(edits)
    if not isinstance(action, dict):
        raise ValueError("Edit action must be an object.")
    operation = action.get("type", action.get("op"))
    ranges = snapshot["ranges"]
    if operation in ("remove", "keep"):
        selectors = [name for name in ("range_ids", "clip_ids") if name in action]
        interval = "start" in action or "end" in action
        if len(selectors) + int(interval) != 1:
            raise ValueError("Choose exactly one range, clip, or source-time selection.")
        if selectors:
            field = selectors[0]
            selected = _selection(action, ranges, field)
            key = "id" if field == "range_ids" else "clip_id"
            snapshot["ranges"] = [r for r in ranges if
                                  (_key(r[key]) in selected) == (operation == "keep")]
        else:
            start, end = _interval(action, snapshot["source_duration"])
            result, ids = [], {r["id"] for r in ranges}
            for r in ranges:
                overlap_start, overlap_end = max(start, r["start"]), min(end, r["end"])
                if overlap_start >= overlap_end:
                    if operation == "remove":
                        result.append(r)
                    continue
                if operation == "keep":
                    result.append({**r, "start": overlap_start, "end": overlap_end})
                    continue
                pieces = []
                if r["start"] < overlap_start:
                    pieces.append({**r, "end": overlap_start})
                if overlap_end < r["end"]:
                    pieces.append({**r, "start": overlap_end})
                if len(pieces) == 2:
                    pieces[1]["id"] = _fresh_id(ids, r["id"])
                result.extend(pieces)
            snapshot["ranges"] = result
    elif operation in ("trim", "split"):
        target = next((r for r in ranges if r["id"] == action.get("range_id")), None)
        if target is None:
            raise ValueError("Unknown range id.")
        index = ranges.index(target)
        if operation == "trim":
            start, end = _interval(action, snapshot["source_duration"])
            if start < target["start"] or end > target["end"]:
                raise ValueError("Trim cannot extend an existing range. Undo to restore it.")
            ranges[index] = {**target, "start": start, "end": end}
        else:
            at = _number(action.get("at"), "Split point")
            if not target["start"] < at < target["end"]:
                raise ValueError("Split point must be strictly inside the range.")
            right = {**target, "id": _fresh_id({r["id"] for r in ranges}, target["id"]), "start": at}
            # Splitting creates an independently selectable edited clip.
            right["group_id"] = _fresh_id({r["group_id"] for r in ranges}, target["group_id"])
            ranges[index:index + 1] = [{**target, "end": at}, right]
    elif operation == "combine":
        selected = _selection(action, ranges, "range_ids")
        indices = [i for i, r in enumerate(ranges) if _key(r["id"]) in selected]
        if len(indices) < 2 or indices != list(range(indices[0], indices[-1] + 1)):
            raise ValueError("Combine needs at least two consecutive edit ranges.")
        group = _fresh_id({r["group_id"] for r in ranges}, "combined")
        for index in indices:
            ranges[index]["group_id"] = group
    elif operation == "reorder":
        selected = _selection(action, ranges, "range_ids")
        if len(selected) != len(ranges):
            raise ValueError("Reorder must include every edit range exactly once.")
        lookup = {r["id"]: r for r in ranges}
        snapshot["ranges"] = [lookup[rid] for rid in action["range_ids"]]
    else:
        raise ValueError("Unsupported edit action.")
    return validate_edits(snapshot)


def _words(segment):
    """All-or-nothing validated words; damaged alignment cannot prove silence."""
    words = segment.get("words")
    if not isinstance(words, list) or not words:
        return []
    validated = []
    for word in words:
        try:
            if not isinstance(word, dict) or not isinstance(word.get("word"), str):
                return []
            start, end = _number(word.get("start"), "Word start"), _number(word.get("end"), "Word end")
            if end <= start or start < segment["start"] or end > segment["end"]:
                return []
            validated.append({"word": word["word"], "start": start, "end": end})
        except ValueError:
            return []
    return sorted(validated, key=lambda w: (w["start"], w["end"]))


def _tokens(text):
    return re.findall(r"[^\W_]+(?:['\u2019][^\W_]+)?", text.casefold())


def _suggestion(kind, start, end, text, reason, **metadata):
    identity = hashlib.sha256(f"{kind}:{start:.9f}:{end:.9f}:{text}".encode("utf-8")).hexdigest()[:16]
    return {"id": f"{kind}-{identity}", "type": kind, "start": start, "end": end,
            "text": text, "reason": reason, "removed_duration": end - start, **metadata}


def cleanup_suggestions(transcript, min_pause=1.0, fillers=None, keep_pause=0.3):
    """Suggest, but do not apply, silence shortening and exact filler removals.

    min_pause is the detection threshold; keep_pause is the silence retained
    around both sides combined. Without valid word timestamps a segment is
    conservatively treated as continuous speech and no filler cut is proposed.
    No leading/trailing recording silence is inferred from transcript absence.
    """
    min_pause = _number(min_pause, "Minimum pause", positive=True)
    keep_pause = _number(keep_pause, "Retained pause")
    if keep_pause >= min_pause:
        raise ValueError("Retained pause must be shorter than the minimum pause.")
    if fillers is None:
        fillers = DEFAULT_FILLERS
    if not isinstance(fillers, (list, tuple)) or any(not isinstance(f, str) for f in fillers):
        raise ValueError("Fillers must be a list of words or phrases.")
    phrases = sorted({tuple(_tokens(f)) for f in fillers if _tokens(f)}, key=lambda p: (-len(p), p))
    segments = _source_segments(transcript)
    speech, suggestions = [], []
    for segment in segments:
        words = _words(segment)
        if words:
            speech.extend((w["start"], w["end"]) for w in words)
        else:
            speech.append((segment["start"], segment["end"]))
        i = 0
        while i < len(words):
            match = None
            for phrase in phrases:
                chunk = words[i:i + len(phrase)]
                if len(chunk) != len(phrase):
                    continue
                if any(_tokens(word["word"]) != [token] for word, token in zip(chunk, phrase)):
                    continue
                if any(b["start"] - a["end"] > 0.5 for a, b in zip(chunk, chunk[1:])):
                    continue
                match = chunk
                break
            if match:
                text = " ".join(w["word"].strip() for w in match)
                suggestions.append(_suggestion("filler", match[0]["start"], match[-1]["end"], text,
                    "Possible filler; review in context before removing.", transcript_id=segment["id"]))
                i += len(match)
            else:
                i += 1
    merged = []
    for start, end in sorted(speech):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for left, right in zip(merged, merged[1:]):
        gap = right[0] - left[1]
        if gap >= min_pause:
            start, end = left[1] + keep_pause / 2, right[0] - keep_pause / 2
            suggestions.append(_suggestion("silence", start, end, "",
                f"Shorten a {gap:.2f}s pause to {keep_pause:.2f}s; review the transition.",
                original_start=left[1], original_end=right[0], retained_pause=keep_pause))
    # Overlapping transcript segments can emit the same aligned filler twice.
    return sorted({s["id"]: s for s in suggestions}.values(), key=lambda s: (s["start"], s["end"], s["type"]))


def accept_suggestion(edits, suggestion):
    """Apply one reviewed cleanup cut. Re-accepting it does not restore media."""
    if not isinstance(suggestion, dict) or suggestion.get("type") not in ("silence", "filler"):
        raise ValueError("Unsupported cleanup suggestion.")
    return apply_edit(edits, {"type": "remove", "start": suggestion.get("start"), "end": suggestion.get("end")})


def _planner_segments(transcript):
    normalized = []
    for raw in _records(transcript):
        if not isinstance(raw, dict):
            raise ValueError("Invalid transcript segment.")
        normalized.append({**raw, "id": raw.get("id", raw.get("segment_id")),
                           "start": raw.get("start", raw.get("start_time")),
                           "end": raw.get("end", raw.get("end_time"))})
    result = _source_segments(normalized)
    canonical_ids = [str(s["id"]) for s in result]
    if len(set(canonical_ids)) != len(canonical_ids):
        raise ValueError("Ambiguous transcript ids cannot identify candidate scores.")
    for segment in result:
        if not isinstance(segment.get("text", ""), str):
            raise ValueError("Transcript text must be a string.")
    return sorted(result, key=lambda s: (s["start"], s["end"]))


def _scores(scored_records, segments):
    if scored_records is None:
        return {}
    if isinstance(scored_records, dict):
        scored_records = scored_records.get("highlights", scored_records.get("segments"))
    if not isinstance(scored_records, list):
        raise ValueError("Scored records must be a list.")
    known = {str(s["id"]) for s in segments}
    result = {}
    for record in scored_records:
        if not isinstance(record, dict):
            continue
        try:
            sid = str(_identifier(record.get("segment_id", record.get("id")), "Score segment id"))
            score = _number(record.get("score"), "Candidate score", positive=True)
        except ValueError:
            continue
        if sid in known and 1 <= score <= 10:
            result[sid] = max(score, result.get(sid, 0))
    return result


def _heuristic(text, clip_type):
    text = text.casefold()
    terms = {
        "tutorial": ("how", "step", "first", "next", "because", "example", "learn"),
        "educational": ("how", "why", "because", "example", "learn", "means"),
        "funny": ("funny", "laugh", "joke", "hilarious", "oops"),
        "story": ("then", "when", "finally", "realized", "remember"),
        "quote": ("believe", "never", "always", "life", "important"),
        "hook": ("secret", "why", "imagine", "never", "mistake", "what if"),
        "highlights": ("amazing", "incredible", "winner", "important", "best", "goal"),
        "summary": ("finally", "overall", "key", "important", "conclusion"),
    }.get(clip_type, ("important", "because", "best", "remember"))
    return min(10.0, 4.0 + sum(bool(re.search(r"\b" + re.escape(term) + r"\b", text)) for term in terms))


def candidate_groups(transcript, desired_count=3, target_duration=30, clip_type="highlights", scored_records=None):
    """Plan editable, non-overlapping candidates across the whole recording.

    Returns up to desired_count candidates, never fabricated duplicates to meet
    the count. Each has ordered source ranges with exact transcript identities;
    map those identities to manifest clip ids before applying them as edits.
    target_duration is a maximum source span (seconds), not a word count. Long
    segments are split at word boundaries where possible. The planner uses all
    supplied segments, not a context-window-limited prefix, and chooses a useful
    window from each temporal region before filling remaining slots by score.
    """
    if type(desired_count) is not int or not 1 <= desired_count <= 50:
        raise ValueError("Desired count must be an integer from 1 to 50.")
    target_duration = _number(target_duration, "Target duration", positive=True)
    if not 1 <= target_duration <= 600:
        raise ValueError("Target duration must be from 1 to 600 seconds.")
    if not isinstance(clip_type, str) or not clip_type.strip() or len(clip_type) > 64 or "\x00" in clip_type:
        raise ValueError("Clip type must be a short nonempty string.")
    clip_type = clip_type.strip().casefold()
    segments = _planner_segments(transcript)
    if not segments:
        return []
    if scored_records is None:
        # Enriched score records are distinguishable from transcription confidence.
        enriched = [s for s in _records(transcript) if "segment_id" in s and "start_time" in s]
        scored_records = enriched or None
    scores = _scores(scored_records, segments)
    if sum(math.ceil((s["end"] - s["start"]) / target_duration) for s in segments) > 100000:
        raise ValueError("Recording is too large for this target duration. Increase the target duration.")
    units = []
    for segment in segments:
        words = _words(segment)
        ends = sorted(word["end"] for word in words)
        start, final = segment["start"], segment["end"]
        while start < final:
            limit = min(final, start + target_duration)
            end = limit
            if limit < final and ends:
                index = bisect_right(ends, limit) - 1
                if index >= 0 and ends[index] > start:
                    end = ends[index]
            if end <= start:  # Defensive against precision collapse in huge times.
                raise ValueError("Transcript timing is too large for the target duration.")
            unit_text = segment.get("text", "").strip()
            if words and (start != segment["start"] or end != final):
                unit_text = " ".join(w["word"].strip() for w in words if w["end"] > start and w["start"] < end)
            units.append({"start": start, "end": end, "transcript_id": segment["id"],
                          "text": unit_text, "score": scores.get(str(segment["id"]), _heuristic(unit_text, clip_type)),
                          "scored": str(segment["id"]) in scores})
            start = end
    units.sort(key=lambda u: (u["start"], u["end"]))
    windows = []
    for index, first in enumerate(units):
        limit, ranges, texts, weighted, weight = first["start"] + target_duration, [], [], 0.0, 0.0
        cursor, uses_scores = first["start"], False
        for unit_index in range(index, len(units)):
            unit = units[unit_index]
            if unit["start"] >= limit:
                break
            # Prefer whole passages; do not append a clipped tail of a new one.
            if unit["end"] > limit:
                break
            start, end = max(cursor, unit["start"]), unit["end"]
            if start >= end:
                continue
            ranges.append({"start": start, "end": end, "transcript_id": unit["transcript_id"]})
            texts.append(unit["text"])
            weighted += unit["score"] * (end - start)
            weight += end - start
            uses_scores = uses_scores or unit["scored"]
            cursor = end
        if not ranges:
            continue
        start, end = ranges[0]["start"], ranges[-1]["end"]
        score = weighted / weight
        windows.append({"start": start, "end": end, "duration": weight, "source_span": end - start,
                        "ranges": ranges, "text": " ".join(t for t in texts if t), "score": score,
                        "scoring_source": "ai_scores" if uses_scores else "local_heuristic",
                        "quality": score + min(1.0, weight / target_duration)})
    if not windows:
        return []
    lower, upper = segments[0]["start"], max(s["end"] for s in segments)
    selected = []

    def overlaps(candidate):
        return any(candidate["start"] < other["end"] and candidate["end"] > other["start"] for other in selected)

    # Temporal coverage, including the final region of hour-long recordings.
    for bucket in range(desired_count):
        a = lower + (upper - lower) * bucket / desired_count
        b = lower + (upper - lower) * (bucket + 1) / desired_count
        available = [w for w in windows if a <= (w["start"] + w["end"]) / 2 < b and not overlaps(w)]
        if available:
            selected.append(max(available, key=lambda w: (w["quality"], -w["start"])))
    for candidate in sorted(windows, key=lambda w: (-w["quality"], w["start"])):
        if len(selected) >= desired_count:
            break
        if not overlaps(candidate):
            selected.append(candidate)
    result = []
    for candidate in sorted(selected, key=lambda w: w["start"]):
        ids = list(dict.fromkeys(r["transcript_id"] for r in candidate["ranges"]))
        identity = hashlib.sha256(repr(candidate["ranges"]).encode("utf-8")).hexdigest()[:16]
        text = candidate["text"]
        reason = ("Grouped using available AI scores and source-time coverage." if candidate["scoring_source"] == "ai_scores"
                  else "Offline transcript heuristic with source-time coverage; not an AI quality judgment.")
        result.append({"id": f"candidate-{identity}", "title": text[:77] + "..." if len(text) > 80 else text or "Untitled passage",
                       "clip_type": clip_type, "start": candidate["start"], "end": candidate["end"],
                       "duration": candidate["duration"], "source_span": candidate["source_span"],
                       "segment_ids": ids, "ranges": deepcopy(candidate["ranges"]),
                       "score": round(candidate["score"], 3), "scoring_source": candidate["scoring_source"],
                       "reason": reason + " Review and edit before export.", "text": text})
    return result
