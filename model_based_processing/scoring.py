"""
License: GNU GPL 3.0

Scoring backends for the Auto-Generate feature.

Copyright (C) 2025 Sean Geles

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

# One ScoringBackend interface, three implementations selected by
# HI_LITE_LLM_BACKEND:
#
#   local  Ollama/llama.cpp-compatible server on the user's own machine.
#   api    Any OpenAI-compatible /v1/chat/completions endpoint, with the
#          user's own base URL + key (OpenAI, Anthropic, OpenRouter, ...).
#   g4f    Development-only example of a manually-integrated third-party
#          provider library. Not shipped, not a dependency, never installed
#          automatically (see CONTRIBUTING.md, "Custom third-party providers").
#
# With HI_LITE_LLM_BACKEND unset, resolution order is: `local` if a server
# responds, else `api` if a key is configured, else scoring fails with an
# explanatory message. There is never an implicit fallback to g4f.

import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from typing import List, Dict, Tuple, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# This script's output is piped into the backend on Windows (cp1252 default):
# force UTF-8 and line buffering so prints can't crash or stall the pipe.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Load .env if available
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


class ScoringError(Exception):
    """
    A scoring failure with a user-facing message. `category` is one of
    auth / rate_limit / timeout / connection / config / parse / api so the
    server can surface distinct messages instead of a blanket failure.
    """

    def __init__(self, message: str, category: str = "api", diagnostics: Optional[Dict] = None):
        super().__init__(message)
        self.category = category
        self.diagnostics = diagnostics


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_LOCAL_URL = "http://127.0.0.1:11434"
DEFAULT_API_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_MODEL = "gpt-4o-mini"
# Provisional default; scoring is a short structured-output task so a small
# instruct model is enough. Documented in README; override with
# HI_LITE_LLM_LOCAL_MODEL or the settings UI.
DEFAULT_LOCAL_MODEL = "qwen2.5:7b-instruct"

CONFIG_FILENAME = "llm_config.json"


def data_dir() -> str:
    """Same resolution as web_interface.py: HI_LITE_DATA_DIR or repo root."""
    configured = os.environ.get("HI_LITE_DATA_DIR")
    if configured:
        return os.path.abspath(configured)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path() -> str:
    return os.path.join(data_dir(), CONFIG_FILENAME)


def load_config_file() -> Dict:
    """Read llm_config.json (written by the settings UI). Missing/invalid -> {}."""
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _setting(env_name: str, file_key: str, default: Optional[str] = None) -> Optional[str]:
    """Env var wins over the settings file, which wins over the default."""
    value = os.environ.get(env_name)
    if value:
        return value
    value = load_config_file().get(file_key)
    if value:
        return str(value)
    return default


def get_llm_settings() -> Dict:
    """
    Effective settings for every backend. NOTE: contains the API key --
    never log or serialize this dict wholesale.
    """
    return {
        "backend": _setting("HI_LITE_LLM_BACKEND", "backend"),
        "base_url": _setting("HI_LITE_LLM_BASE_URL", "base_url", DEFAULT_API_BASE_URL),
        "api_key": _setting("HI_LITE_LLM_API_KEY", "api_key"),
        "model": _setting("HI_LITE_LLM_MODEL", "model", DEFAULT_API_MODEL),
        "local_url": _setting("HI_LITE_LLM_LOCAL_URL", "local_url", DEFAULT_LOCAL_URL),
        "local_model": _setting("HI_LITE_LLM_LOCAL_MODEL", "local_model", DEFAULT_LOCAL_MODEL),
    }


# ---------------------------------------------------------------------------
# Backend-independent prompt / parsing / enrichment
# ---------------------------------------------------------------------------

# Bump this when the scoring prompt/response semantics intentionally change.
SCORING_PROMPT_VERSION = "2-batched"


def _append_endpoint_path(base_url: str, path: str) -> str:
    """Append the request route to the path, keeping credentials in the query."""
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + path,
                       parts.query, ""))


def endpoint_identity(base_url: str) -> str:
    """Nonsecret server/path identity. Untyped URL queries may contain secrets."""
    try:
        parts = urlsplit(base_url)
        if not parts.scheme or not parts.netloc:
            raise ValueError
        # Preserve path spelling/case and internal slashes; only trailing
        # slashes are ignored, as in _append_endpoint_path's actual request.
        return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1],
                           parts.path.rstrip("/"), "", ""))
    except ValueError:
        raise ScoringError("The scoring endpoint URL is invalid.", category="config") from None


def _endpoint_query_reusable(base_url: str) -> bool:
    """Unknown query fields may route requests; never guess or persist their values."""
    credential_fields = {"key", "api_key", "api-key", "apikey", "token", "access_token",
                         "access-token", "api_token", "auth", "authorization", "bearer",
                         "password", "secret", "signature", "sig", "credential", "credentials"}
    return all(name.lower() in credential_fields for name, _ in
               parse_qsl(urlsplit(base_url).query, keep_blank_values=True))


def transcript_digest(segments: List[Dict]) -> str:
    """Hash the ordered authoritative scoring segments, not the clip manifest."""
    try:
        if not isinstance(segments, list) or not segments:
            raise ValueError
        records = []
        for segment in segments:
            sid = canonical_segment_id(segment.get("id"))
            if sid is None or not isinstance(segment["text"], str):
                raise ValueError
            records.append({"id": sid, "text": segment["text"].strip(),
                            "start": float(segment["start"]), "end": float(segment["end"])})
        serialized = json.dumps(records, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        raise ScoringError("Current transcript cannot identify a scoring request.",
                           category="parse") from None


def score_cache_identity(segments, instructions, backend) -> Dict:
    """Use the resolved backend's request fields; never serialize settings/keys."""
    criteria = (instructions or "").strip()
    if not criteria:
        raise ScoringError("Highlight instructions are required.", category="config")
    model = getattr(backend, "model", None)
    provider = getattr(backend, "provider", None)
    base_url = getattr(backend, "base_url", None)
    return {
        "transcript_digest": transcript_digest(segments),
        "criteria": criteria,
        "backend": backend.name,
        "provider": provider if isinstance(provider, str) else None,
        "endpoint": endpoint_identity(base_url) if isinstance(base_url, str) else None,
        "endpoint_query_reusable": (_endpoint_query_reusable(base_url)
                                     if isinstance(base_url, str) else True),
        "model": model if isinstance(model, str) and model else None,
        "prompt_version": SCORING_PROMPT_VERSION,
    }


def build_prompt(transcript: str, instructions: str) -> str:
    """
    Build the scoring prompt. `instructions` is required free-form user
    text describing what counts as a highlight. The JSON output contract
    is fixed and always appended so the response stays machine-parseable.
    """
    criteria = (instructions or "").strip()
    if not criteria:
        raise ValueError("Highlight instructions are required.")
    example_json = '{"segments": [{"segment_id": "abc123...", "score": 8, "reason": "Great save"}]}'
    return f"""Analyze this video transcript and score each segment for highlight potential.

 Return a JSON array where each object has:
 - segment_id: the exact ID from the transcript
 - score: number 1-10 for highlight value
 - reason: brief explanation

 What counts as a highlight:
 {criteria}

 Transcript with segment IDs:
 {transcript}


 Return format: {example_json}"""


STRICT_JSON_SUFFIX = (
    "\n\nIMPORTANT: Respond with ONLY the JSON object described above. "
    "No prose, no explanation, no markdown code fences."
)


def extract_scored_segments(result_text: str) -> Optional[List[Dict]]:
    """
    Pull the {"segments": [...]} payload out of a model response, tolerating
    surrounding prose/fences. Returns None when no usable JSON is found.
    """
    if not isinstance(result_text, str) or not result_text:
        return None
    json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
    if not json_match:
        return None
    try:
        result = json.loads(json_match.group())
    except (ValueError, RecursionError):
        return None
    segments = result.get("segments") if isinstance(result, dict) else None
    if isinstance(segments, list):
        return segments
    return None


class ScoringResult(list):
    """Keep the backend's list contract while carrying bounded validation diagnostics."""

    def __init__(self, records, validation):
        super().__init__(records)
        self.validation = validation


def canonical_segment_id(value):
    """Exact transcript identity; quoted integer IDs are allowed, guessing is not."""
    if type(value) is int and value >= 0:
        try:
            return str(value)
        except ValueError:  # Python's oversized integer-string limit.
            return None
    if isinstance(value, str) and value and value == value.strip():
        return value
    return None


def validate_scored_segments(scored_segments, segments) -> ScoringResult:
    """
    Provider-neutral boundary, also used for disk payloads before enrichment.
    Scores are JSON numbers (not booleans or strings), finite and within 1-10.
    Keep the highest valid score per exact ID; first occurrence fixes order and
    the first record wins equal-score ties. Never invent scores for omissions.
    """
    if not isinstance(scored_segments, list):
        raise ScoringError("Score response must contain a list of segment records.", category="parse")
    if not isinstance(segments, list) or not segments:
        raise ScoringError("Current transcript has no segment identities to validate scores against.",
                           category="parse")

    lookup = {}
    ambiguous = set()
    invalid_transcript_ids = 0
    for segment in segments:
        sid = canonical_segment_id(segment.get("id")) if isinstance(segment, dict) else None
        if sid is None:
            invalid_transcript_ids += 1
        elif sid in lookup:
            ambiguous.add(sid)
        else:
            lookup[sid] = segment

    accepted = {}
    rejection_counts = {}
    rejection_examples = []
    duplicate_records = 0
    for index, record in enumerate(scored_segments):
        problem = None
        sid = None
        if not isinstance(record, dict):
            problem = "malformed_record"
        elif "segment_id" not in record:
            problem = "missing_id"
        else:
            sid = canonical_segment_id(record["segment_id"])
            if sid is None:
                problem = "malformed_id"
            elif sid in ambiguous:
                problem = "ambiguous_id"
            elif sid not in lookup:
                problem = "unknown_id"
            elif "score" not in record:
                problem = "missing_score"
            elif type(record["score"]) not in (int, float):
                problem = "invalid_score_type"
            elif type(record["score"]) is float and not math.isfinite(record["score"]):
                problem = "nonfinite_score"
            elif not 1 <= record["score"] <= 10:
                # Range check before normalization avoids huge-integer overflow.
                problem = "invalid_score_range"

        if problem:
            rejection_counts[problem] = rejection_counts.get(problem, 0) + 1
            if len(rejection_examples) < 10:
                # No raw provider values, reasons, or secret configuration.
                rejection_examples.append({"record_index": index, "problem": problem})
            continue

        normalized = {
            "segment_id": lookup[sid]["id"],
            "score": float(record["score"]),
            "reason": record.get("reason") if isinstance(record.get("reason"), str)
                      else "No reason provided",
        }
        if sid in accepted:
            duplicate_records += 1
            if normalized["score"] <= accepted[sid]["score"]:
                continue
        accepted[sid] = normalized

    missing = [sid for sid in lookup if sid not in accepted]
    validation = {
        "status": "complete" if len(accepted) == len(segments) else "partial",
        "total_segments": len(segments),
        "scored_segments": len(accepted),
        "missing_segment_count": len(missing),
        "missing_segment_ids": [lookup[sid]["id"] for sid in missing[:10]],
        "ambiguous_transcript_id_count": len(ambiguous),
        "invalid_transcript_id_count": invalid_transcript_ids,
        "rejected_records": sum(rejection_counts.values()),
        "rejection_counts": rejection_counts,
        "rejection_examples": rejection_examples,
        "duplicate_records": duplicate_records,
    }
    if not accepted:
        validation["status"] = "unusable"
        raise ScoringError("No usable AI scores remain after validation "
                           f"({validation['rejected_records']} records rejected).",
                           category="parse", diagnostics=validation)
    return ScoringResult(accepted.values(), validation)


def enrich_results(scored_segments: List[Dict], segments: List[Dict]) -> List[Dict]:
    """
    Validate before joining onto transcript data or sorting. All providers
    and cached-score readers share this boundary; provider times/text are ignored.
    """
    validated = validate_scored_segments(scored_segments, segments)
    segment_lookup = {canonical_segment_id(seg['id']): seg for seg in segments
                      if isinstance(seg, dict) and canonical_segment_id(seg.get('id')) is not None}
    enriched_results = ScoringResult([], validated.validation)
    for scored_segment in validated:
        original = segment_lookup[canonical_segment_id(scored_segment['segment_id'])]
        enriched_results.append({
            **scored_segment,
            "start_time": original['start'],
            "end_time": original['end'],
            "text": original['text'].strip(),
        })
    enriched_results.sort(key=lambda x: x['score'], reverse=True)
    return enriched_results


def _read_scored_payload(file_path: str) -> Dict:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError, RecursionError):
        raise ScoringError("Cached AI scores could not be read as JSON. Run AI scoring again.",
                           category="parse")
    if not isinstance(payload, dict) or not isinstance(payload.get("highlights"), list):
        raise ScoringError("Cached AI scores must contain a highlights list. Run AI scoring again.",
                           category="parse")
    return payload


def load_scored_results(file_path: str, segments: List[Dict]) -> ScoringResult:
    """Structural HL-A3a boundary; this alone does not establish cache applicability."""
    return enrich_results(_read_scored_payload(file_path)["highlights"], segments)


def load_score_cache(file_path, segments, identity, *, require_complete=True) -> ScoringResult:
    """One applicability decision for generation and preview; a miss raises ScoringError.

    Only the just-completed scoring job may consume partial/non-reusable results
    with require_complete=False. Automatic reuse always requires an effective model.
    """
    payload = _read_scored_payload(file_path)
    cache = payload.get("cache")
    if (not isinstance(cache, dict) or set(cache) != {"identity", "complete"}
            or not isinstance(cache.get("identity"), dict)
            or cache["identity"] != identity or type(cache.get("complete")) is not bool):
        raise ScoringError("Cached AI scores do not match this scoring request. Run AI scoring again.",
                           category="parse")
    if require_complete and (not cache["complete"] or not identity["model"]
                             or not identity["endpoint_query_reusable"]):
        raise ScoringError("Cached AI scores are not a complete reusable result. Run AI scoring again.",
                           category="parse")
    result = enrich_results(payload["highlights"], segments)
    if (cache["complete"] != (result.validation["status"] == "complete")
            or result.validation["rejected_records"]):
        raise ScoringError("Cached AI scores failed validation. Run AI scoring again.",
                           category="parse", diagnostics=result.validation)
    return result


def _publish_score_payload(file_path, payload):
    """Publish one finished JSON file; readers see either old or completed scores."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                         dir=os.path.dirname(file_path),
                                         prefix=".scores-", suffix=".tmp") as f:
            temporary_path = f.name
            json.dump(payload, f, indent=2, allow_nan=False)
        os.replace(temporary_path, file_path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def report_validation(result: ScoringResult):
    """Counts and bounded record indexes only; never dump a model response."""
    print("Score validation: " + json.dumps(result.validation))


def _run_with_timeout(func, timeout_seconds: int):
    """
    Run a blocking function with a timeout, raising TimeoutError on expiry.
    Uses a daemon thread rather than ThreadPoolExecutor: the executor's
    context manager waits for the worker to finish even after the timeout
    fires, so one hung provider would block the whole run forever.
    """
    import threading
    import queue

    result_q: "queue.Queue" = queue.Queue()

    def _runner():
        try:
            result_q.put(("ok", func()))
        except Exception as e:
            result_q.put(("err", e))

    threading.Thread(target=_runner, daemon=True).start()
    try:
        kind, value = result_q.get(timeout=timeout_seconds)
    except queue.Empty:
        raise TimeoutError(f"provider call timed out after {timeout_seconds}s")
    if kind == "err":
        raise value
    return value


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class ScoringBackend:
    """Scores transcript segments. Implementations raise ScoringError on failure."""

    name = "base"

    def score(self, segments: List[Dict], transcript: str, instructions: str) -> List[Dict]:
        """
        Return the enriched-result list (segment_id, start_time, end_time,
        text, score, reason), sorted by score descending. Implementations must
        use enrich_results() before consuming scores, preserving its validation
        diagnostics (ScoringResult remains compatible with list callers).
        """
        raise NotImplementedError


def _chat_completion(base_url: str, model: str, prompt: str,
                     api_key: Optional[str] = None, timeout: int = 120) -> str:
    """
    One OpenAI-compatible /chat/completions call. Raises ScoringError with a
    distinct category for auth / rate-limit / timeout / connection failures.
    """
    import requests

    url = _append_endpoint_path(base_url, "/chat/completions")
    safe_url = endpoint_identity(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = requests.post(
            url,
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        raise ScoringError(
            f"The model at {safe_url} did not respond within {timeout}s.",
            category="timeout")
    except requests.exceptions.ConnectionError:
        raise ScoringError(
            f"Could not connect to {safe_url}. Is the server running / the URL correct?",
            category="connection")

    if response.status_code in (401, 403):
        raise ScoringError(
            "Authentication failed - check your API key.", category="auth")
    if response.status_code == 429:
        raise ScoringError(
            "Rate limited by the provider - wait a moment and try again.",
            category="rate_limit")
    if response.status_code == 404:
        raise ScoringError(
            f"Endpoint or model not found at {safe_url}/chat/completions (model '{model}'). "
            "Check the base URL and model name.", category="config")
    if response.status_code >= 400:
        # Provider error bodies are useful but can be huge; keep the head.
        detail = response.text[:300]
        raise ScoringError(
            f"Provider returned HTTP {response.status_code}: {detail}",
            category="api")

    try:
        return response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        raise ScoringError(
            "Provider response was not in OpenAI chat-completions format.",
            category="api")


class _OpenAICompatibleBackend(ScoringBackend):
    """Shared scoring flow for `local` and `api` (both speak /v1/chat/completions)."""

    base_url: str
    model: str
    api_key: Optional[str] = None
    timeout: int = 120

    def score(self, segments, transcript, instructions):
        prompt = build_prompt(transcript, instructions)
        print(f"Trying {self.model} via {self.name} backend...")
        t0 = time.time()
        result_text = _chat_completion(self.base_url, self.model, prompt,
                                       api_key=self.api_key, timeout=self.timeout)
        scored = extract_scored_segments(result_text)
        if scored is None:
            # Small models routinely wrap or malform the JSON on the first
            # try; one strict retry recovers most of them.
            print("Response was not parseable JSON, retrying with strict format...")
            result_text = _chat_completion(
                self.base_url, self.model, prompt + STRICT_JSON_SUFFIX,
                api_key=self.api_key, timeout=self.timeout)
            scored = extract_scored_segments(result_text)
        if scored is None:
            raise ScoringError(
                f"The model ({self.model}) did not return usable JSON scores. "
                "A larger or more instruction-following model may be needed.",
                category="parse")
        result = enrich_results(scored, segments)
        report_validation(result)
        print(f"+ Scoring {result.validation['status']} with {self.model} in {time.time() - t0:.1f}s")
        return result


class APIBackend(_OpenAICompatibleBackend):
    """User-supplied OpenAI-compatible endpoint: their base URL, key, and terms."""

    name = "api"

    def __init__(self, settings: Optional[Dict] = None):
        settings = settings or get_llm_settings()
        self.base_url = settings["base_url"]
        self.api_key = settings["api_key"]
        self.model = settings["model"]

    @staticmethod
    def is_configured(settings: Optional[Dict] = None) -> bool:
        """A key makes `api` eligible for auto-selection. A keyless gateway
        still works, but only when HI_LITE_LLM_BACKEND=api is explicit."""
        settings = settings or get_llm_settings()
        return bool(settings["api_key"])


class LocalBackend(_OpenAICompatibleBackend):
    """
    Model server on the user's machine (Ollama, llama.cpp server, or anything
    exposing OpenAI-compatible /v1 routes). Nothing leaves the machine.
    """

    name = "local"
    timeout = 600  # local models on modest hardware are slow; don't give up early

    def __init__(self, settings: Optional[Dict] = None):
        settings = settings or get_llm_settings()
        self.base_url = _append_endpoint_path(settings["local_url"], "/v1")
        self.model = settings["local_model"]

    @staticmethod
    def is_available(settings: Optional[Dict] = None, timeout: float = 2.0) -> bool:
        """True when a server responds on the local URL. Both Ollama and
        llama.cpp answer GET /v1/models."""
        import requests

        settings = settings or get_llm_settings()
        url = _append_endpoint_path(settings["local_url"], "/v1/models")
        try:
            return requests.get(url, timeout=timeout).status_code < 500
        except Exception:
            return False


class G4FBackend(ScoringBackend):
    """
    Development-only example of integrating a third-party provider library
    (g4f). g4f is NOT a dependency, is never shipped in the installer, and is
    never installed automatically; a developer opting in with
    HI_LITE_LLM_BACKEND=g4f installs it manually and is responsible for
    complying with the terms of whatever endpoints it routes to.
    """

    name = "g4f"

    def __init__(self):
        try:
            import g4f  # noqa: F401  (deliberately lazy: never shipped)
            from g4f.client import Client
        except ImportError:
            raise ScoringError(
                "g4f is not installed. It is not a dependency of Hi-Lite and "
                "is never shipped; a developer who wants this backend must "
                "install it manually in their own environment (see "
                "CONTRIBUTING.md, 'Custom third-party providers'). Configure "
                "the local or api backend instead.",
                category="config")
        self._g4f = g4f
        self.client = Client()
        # Optional token some g4f providers accept (dev convenience only)
        token = os.environ.get("g4f_token")
        if token:
            os.environ.setdefault("OPENAI_API_KEY", token)
            os.environ.setdefault("G4F_API_KEY", token)

    def _resolve_provider(self, name: Optional[str]):
        if not name:
            return None
        try:
            return getattr(self._g4f.Provider, name)
        except Exception:
            return None

    def score(self, segments, transcript, instructions):
        prompt = build_prompt(transcript, instructions)

        # (provider_name_or_None, model): None = let g4f auto-route across its
        # own maintained provider list, which survives provider rot far better
        # than pinning. Names that no longer exist in g4f are skipped.
        providers_to_try = [
            (None, "gpt-4o-mini"),
            (None, "gpt-4o"),
            (None, "deepseek-v3"),
            (None, "llama-3.3-70b"),
            ("DeepInfra", "deepseek-ai/DeepSeek-V4-Flash"),
        ]

        for provider_name, model in providers_to_try:
            provider = self._resolve_provider(provider_name)
            if provider_name and provider is None:
                print(f"Provider {provider_name} not available in this g4f version, skipping...")
                continue
            try:
                print(f"Trying {model}" + (f" via {provider_name}..." if provider_name else " (auto provider)..."))

                def _call():
                    kwargs = {
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                    }
                    if provider is not None:
                        kwargs["provider"] = provider
                    return self.client.chat.completions.create(**kwargs)

                t0 = time.time()
                response = _run_with_timeout(_call, timeout_seconds=60)
                elapsed = time.time() - t0

                scored = extract_scored_segments(response.choices[0].message.content)
                if scored is not None:
                    result = enrich_results(scored, segments)
                    report_validation(result)
                    print(f"+ Scoring {result.validation['status']} with {model} in {elapsed:.1f}s")
                    return result

            except Exception as e:
                print(f"Failed with {model}" + (f" via {provider_name}" if provider_name else "") + f": {e}")
                continue

        raise ScoringError(
            "All g4f providers failed. g4f failures are routine and not "
            "supported; configure the local or api backend for reliable scoring.",
            category="api")


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

NO_BACKEND_MESSAGE = (
    "No scoring backend is configured. Either run a local model server "
    "(Ollama at 127.0.0.1:11434) or add an API key in the AI settings. "
    "Manual clip selection works without any of this.")


def get_backend(name: Optional[str] = None, settings: Optional[Dict] = None) -> ScoringBackend:
    """
    Resolve the scoring backend. Explicit choice (argument or
    HI_LITE_LLM_BACKEND) wins and never falls back to another backend;
    otherwise: `local` if a server responds, else `api` if a key is
    configured, else ScoringError explaining how to set one up.
    """
    settings = settings if settings is not None else get_llm_settings()
    explicit = (name or settings["backend"] or "").strip().lower()

    if explicit:
        if explicit == "local":
            return LocalBackend(settings)
        if explicit == "api":
            return APIBackend(settings)
        if explicit == "g4f":
            return G4FBackend()
        raise ScoringError(
            f"Unknown HI_LITE_LLM_BACKEND '{explicit}' (expected local, api, or g4f).",
            category="config")

    if LocalBackend.is_available(settings):
        return LocalBackend(settings)
    if APIBackend.is_configured(settings):
        return APIBackend(settings)
    raise ScoringError(NO_BACKEND_MESSAGE, category="config")


def resolve_backend_name() -> Optional[str]:
    """
    Which backend get_backend() would pick right now, or None if scoring is
    unavailable. Used by the settings API; never raises.
    """
    try:
        return get_backend().name
    except ScoringError:
        return None


# ---------------------------------------------------------------------------
# Subprocess entry point (unchanged contract with web_interface.py)
# ---------------------------------------------------------------------------

def load_transcription_json(file_path: str) -> Tuple[List[Dict], str]:
    """
    Load JSON transcription and return both segments and formatted text

    Args:
        file_path: Path to the JSON transcription file

    Returns:
        Tuple of (segments_list, formatted_transcript_string)
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        segments = data.get('segments', [])
        transcript_lines = []

        # Create formatted transcript with segment IDs for analysis
        for segment in segments:
            segment_id = segment.get('id', 'unknown')
            start_time = segment.get('start', 0)
            end_time = segment.get('end', 0)
            text = segment.get('text', '').strip()

            if text:  # Only add non-empty segments
                transcript_lines.append(f"ID:{segment_id} [{start_time:.2f}s-{end_time:.2f}s] {text}")

        transcript = '\n'.join(transcript_lines)

        # Log some stats
        duration = data.get('duration', 0)
        language = data.get('language', 'unknown')
        num_segments = len(segments)

        print(f"Loaded transcription:")
        print(f"   Duration: {duration:.1f}s ({int(duration//60)}m {int(duration%60)}s)")
        print(f"   Language: {language}")
        print(f"   Segments: {num_segments}")
        print()

        return segments, transcript

    except FileNotFoundError:
        print(f"ERROR: Error: Could not find transcription file at {file_path}")
        print("Please check the file path and try again.")
        return [], ""
    except json.JSONDecodeError as e:
        print(f"ERROR: Error: Invalid JSON format in {file_path}")
        print(f"JSON Error: {e}")
        return [], ""
    except Exception as e:
        print(f"ERROR: Error loading transcription: {e}")
        return [], ""


def _scoring_batches(segments, max_segments, max_chars):
    """Bound transcript text without dropping a long passage or changing its id."""
    if type(max_segments) is not int or max_segments < 1:
        raise ValueError("Maximum batch segments must be a positive integer.")
    if type(max_chars) is not int or max_chars < 64:
        raise ValueError("Maximum batch characters must be an integer of at least 64.")
    transcript_digest(segments)  # Validate identities, text, and finite timings first.
    identities = [canonical_segment_id(s["id"]) for s in segments]
    if len(set(identities)) != len(identities):
        raise ScoringError("Transcript ids are ambiguous; import the recording again.", category="parse")
    batches, batch, lines, size, seen = [], [], [], 0, set()
    for segment in segments:
        sid = canonical_segment_id(segment["id"])
        if (any(type(segment[key]) not in (int, float) for key in ("start", "end"))
                or segment["start"] < 0 or segment["end"] <= segment["start"]):
            raise ScoringError("Transcript timing is invalid; import the recording again.", category="parse")
        header = f"ID:{segment['id']} [{segment['start']:.2f}s-{segment['end']:.2f}s] "
        budget = max_chars - len(header)
        if budget < 1:
            raise ScoringError("A transcript identifier exceeds the scoring batch size.", category="parse")
        remaining = segment["text"].strip()
        fragments = []
        if not remaining:
            fragments.append("")
        while remaining:
            cut = min(len(remaining), budget)
            if cut < len(remaining):
                # Keep words whole when possible; a single oversized word must split.
                boundary = remaining.rfind(" ", 0, cut + 1)
                if boundary > 0:
                    cut = boundary
            fragments.append(remaining[:cut].strip())
            remaining = remaining[cut:].lstrip()
        for fragment in fragments:
            line = header + fragment if fragment else ""
            extra = len(line) + (1 if lines and line else 0)
            if batch and (len(batch) >= max_segments or size + extra > max_chars or sid in seen):
                batches.append((batch, "\n".join(lines)))
                batch, lines, size, seen = [], [], 0, set()
                extra = len(line)
            batch.append({**segment, "text": fragment})
            seen.add(sid)
            if line:
                lines.append(line)
            size += extra
    if batch:
        batches.append((batch, "\n".join(lines)))
    return batches


def score_in_batches(backend, segments, instructions, max_segments=80, max_chars=16000):
    """Score the entire recording using bounded provider requests.

    Limits apply to transcript characters, not the instructions/prompt envelope.
    Each batch is independently validated against its exact source identities.
    In a multi-batch run a parse failure permits useful, explicitly partial
    results; authentication, rate-limit and transport failures stop immediately.
    A segment split into text fragments is admitted only if every fragment was
    scored, then receives its highest fragment score. This preserves honest
    segment coverage and the existing complete-only automatic cache contract.
    """
    instructions = (instructions or "").strip()
    if not instructions:
        raise ScoringError("Highlight instructions are required.", category="config")
    batches = _scoring_batches(segments, max_segments, max_chars)
    combined, diagnostics, failed, incomplete = [], [], [], set()
    for index, (batch, transcript) in enumerate(batches):
        if len(batches) > 1:
            print(f"Scoring batch {index + 1}/{len(batches)} ({len(batch)} passages)...")
        try:
            returned = backend.score(batch, transcript, instructions)
            validated = enrich_results(returned, batch)
            # Built-in providers already rejected malformed raw records. Preserve
            # their diagnostics rather than counting only the normalized result.
            prior = returned.validation if isinstance(returned, ScoringResult) else None
            diagnostics.append((index, prior or validated.validation))
            scored_ids = {canonical_segment_id(r["segment_id"]) for r in validated}
            incomplete.update(canonical_segment_id(s["id"]) for s in batch
                              if canonical_segment_id(s["id"]) not in scored_ids)
            combined.extend(validated)
        except ScoringError as error:
            if len(batches) == 1 or error.category != "parse":
                raise
            incomplete.update(canonical_segment_id(s["id"]) for s in batch)
            failed.append({"batch_index": index, "category": error.category})
            if error.diagnostics:
                diagnostics.append((index, error.diagnostics))
            print(f"Batch {index + 1} had no usable scores; continuing with remaining passages.")
    # Do not use one successful fragment to conceal an unscored part of a passage.
    combined = [record for record in combined
                if canonical_segment_id(record["segment_id"]) not in incomplete]
    result = enrich_results(combined, segments)
    validation = result.validation
    rejection_counts, examples = {}, []
    for batch_index, detail in diagnostics:
        for key, count in detail.get("rejection_counts", {}).items():
            rejection_counts[key] = rejection_counts.get(key, 0) + count
        for example in detail.get("rejection_examples", []):
            if len(examples) < 10:
                examples.append({**example, **({"batch_index": batch_index} if len(batches) > 1 else {})})
    validation["rejection_counts"] = rejection_counts
    validation["rejected_records"] = sum(rejection_counts.values())
    validation["rejection_examples"] = examples
    validation["duplicate_records"] += sum(d.get("duplicate_records", 0) for _, d in diagnostics)
    validation["batch_count"] = len(batches)
    validation["completed_batches"] = len(batches) - len(failed)
    validation["failed_batch_count"] = len(failed)
    validation["failed_batches"] = failed[:10]
    validation["fragment_count"] = sum(len(batch) for batch, _ in batches)
    report_validation(result)
    return result


def run_scoring(transcript_path: str, output_dir: str, instructions: str) -> bool:
    """
    Score a transcription's segments with the configured backend and write
    scored_segments.json (by score) and sorted_segments.json (chronological,
    the file /api/auto-generate consumes) into output_dir.
    Returns True on success. Progress prints are parsed by web_interface.
    """
    instructions = (instructions or "").strip()
    if not instructions:
        print("ERROR: Highlight instructions are required.")
        return False

    segments, transcript = load_transcription_json(transcript_path)
    if not transcript or not segments:
        print("ERROR: Failed to load transcript.")
        return False

    print(f"Using highlight instructions: {instructions}")
    print("Scoring segments with AI...")
    print("=" * 60)

    try:
        backend = get_backend()
        identity = score_cache_identity(segments, instructions, backend)
        print(f"Using scoring backend: {backend.name}")
        highlights = score_in_batches(backend, segments, instructions)
    except ScoringError as e:
        print(f"ERROR: {e}")
        if e.diagnostics:
            print("Score validation: " + json.dumps(e.diagnostics))
        return False
    except ValueError as e:
        print(f"ERROR: {e}")
        return False

    if not highlights:
        print("ERROR: Scoring returned no highlights - none of the returned "
              "segment ids matched the transcript.")
        return False

    print(f"\nTop {min(5, len(highlights))} highlights:")
    for i, h in enumerate(highlights[:5], 1):
        print(f"{i}. Segment {h['segment_id']} | score {h['score']}/10 | "
              f"{h['start_time']:.2f}s-{h['end_time']:.2f}s | {h['reason']}")

    os.makedirs(output_dir, exist_ok=True)
    payload = {
        "analysis_timestamp": time.time(),
        "total_segments": len(segments),
        "instructions": instructions,
        "backend": backend.name,
        "cache": {"identity": identity, "complete": highlights.validation["status"] == "complete"},
        "validation": highlights.validation,
        "highlights": highlights,
    }
    scored_path = os.path.join(output_dir, "scored_segments.json")
    sorted_payload = dict(payload)
    sorted_payload["highlights"] = sorted(highlights, key=lambda h: h["start_time"])
    sorted_path = os.path.join(output_dir, "sorted_segments.json")
    try:
        # The chronological file is the authoritative cache, published last.
        _publish_score_payload(scored_path, payload)
        _publish_score_payload(sorted_path, sorted_payload)
    except (OSError, ValueError):
        print("ERROR: Could not publish completed AI scores.")
        return False

    print(f"Results saved to {scored_path} and {sorted_path}")
    # Keep the completion marker parsed by web_interface; report coverage separately.
    print(f"Scoring complete: {len(highlights)} highlights ({highlights.validation['status']} coverage)")
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Score transcript segments for highlight potential")
    parser.add_argument("--transcript", default="highlights/transcription.json",
                        help="Path to transcription.json")
    parser.add_argument("--output-dir", default="highlights",
                        help="Directory for scored_segments.json / sorted_segments.json")
    parser.add_argument("--instructions", required=True,
                        help="Description of what counts as a highlight (required)")
    args = parser.parse_args()

    ok = run_scoring(args.transcript, args.output_dir, args.instructions)
    sys.exit(0 if ok else 1)
