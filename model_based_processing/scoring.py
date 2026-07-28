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

import json
import os
import re
import sys
import time
from typing import List, Dict, Tuple, Optional

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

    def __init__(self, message: str, category: str = "api"):
        super().__init__(message)
        self.category = category


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
    if not result_text:
        return None
    json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
    if not json_match:
        return None
    try:
        result = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None
    segments = result.get("segments") if isinstance(result, dict) else None
    if isinstance(segments, list) and len(segments) > 0:
        return segments
    return None


def enrich_results(scored_segments: List[Dict], segments: List[Dict]) -> List[Dict]:
    """
    Join model scores back onto the original segment data and sort by score.
    Compare ids as strings: our transcriber uses integer ids and the model
    may echo them back quoted.
    """
    segment_lookup = {str(seg['id']): seg for seg in segments}
    enriched_results = []
    for scored_segment in scored_segments:
        segment_id = str(scored_segment.get('segment_id'))
        if segment_id in segment_lookup:
            original = segment_lookup[segment_id]
            enriched_results.append({
                "segment_id": original['id'],
                "start_time": original['start'],
                "end_time": original['end'],
                "text": original['text'].strip(),
                "score": scored_segment.get('score', 5),
                "reason": scored_segment.get('reason', 'No reason provided')
            })
    enriched_results.sort(key=lambda x: x['score'], reverse=True)
    return enriched_results


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
        text, score, reason), sorted by score descending.
        """
        raise NotImplementedError


def _chat_completion(base_url: str, model: str, prompt: str,
                     api_key: Optional[str] = None, timeout: int = 120) -> str:
    """
    One OpenAI-compatible /chat/completions call. Raises ScoringError with a
    distinct category for auth / rate-limit / timeout / connection failures.
    """
    import requests

    url = base_url.rstrip("/") + "/chat/completions"
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
            f"The model at {base_url} did not respond within {timeout}s.",
            category="timeout")
    except requests.exceptions.ConnectionError:
        raise ScoringError(
            f"Could not connect to {base_url}. Is the server running / the URL correct?",
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
            f"Endpoint or model not found at {url} (model '{model}'). "
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
        print(f"+ Success with {self.model} in {time.time() - t0:.1f}s")
        return enrich_results(scored, segments)


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
        self.base_url = settings["local_url"].rstrip("/") + "/v1"
        self.model = settings["local_model"]

    @staticmethod
    def is_available(settings: Optional[Dict] = None, timeout: float = 2.0) -> bool:
        """True when a server responds on the local URL. Both Ollama and
        llama.cpp answer GET /v1/models."""
        import requests

        settings = settings or get_llm_settings()
        url = settings["local_url"].rstrip("/") + "/v1/models"
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
                    print(f"+ Success with {model} in {elapsed:.1f}s")
                    return enrich_results(scored, segments)

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


def get_backend(name: Optional[str] = None) -> ScoringBackend:
    """
    Resolve the scoring backend. Explicit choice (argument or
    HI_LITE_LLM_BACKEND) wins and never falls back to another backend;
    otherwise: `local` if a server responds, else `api` if a key is
    configured, else ScoringError explaining how to set one up.
    """
    settings = get_llm_settings()
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
        print(f"Using scoring backend: {backend.name}")
        highlights = backend.score(segments, transcript, instructions)
    except ScoringError as e:
        print(f"ERROR: {e}")
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
        "highlights": highlights,
    }
    scored_path = os.path.join(output_dir, "scored_segments.json")
    with open(scored_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    sorted_payload = dict(payload)
    sorted_payload["highlights"] = sorted(highlights, key=lambda h: h["start_time"])
    sorted_path = os.path.join(output_dir, "sorted_segments.json")
    with open(sorted_path, "w", encoding="utf-8") as f:
        json.dump(sorted_payload, f, indent=2)

    print(f"Results saved to {scored_path} and {sorted_path}")
    print(f"Scoring complete: {len(highlights)} highlights")
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
