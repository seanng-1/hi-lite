# Contributing to Hi-Lite

Thanks for your interest! This doc covers the architecture, dev setups, and the
Windows-packaging landmines that have each caused a real release failure.

## Architecture (3 processes in production)

1. **Electron shell** (`electron/src/main.ts`) — spawns `backend.exe`, loads
   `http://127.0.0.1:8000/`, kills the backend tree on quit.
2. **Backend** (`web_interface.py`, packaged with PyInstaller) — Flask serving the
   built frontend, upload/status/export APIs, and clip files.
3. **Pipeline child** — on upload, the server re-invokes *its own executable* as
   `backend.exe --process-video ...` (in dev: `python main.py`). Pipeline:
   extract audio → transcribe (faster-whisper, word timestamps) → create buffered
   source-range metadata → write `segments_manifest.json`. No per-segment render
   is required; `--render-segments` preserves the old CLI behavior.

The frontend (`web/src/selector.ts`, `projects.ts`, `transcript-editor.ts`) uses
`/api/projects/<id>` for revision-checked project state and prefixes existing
media/API paths with that project scope. `project_store.py` publishes atomic
JSON with compare-and-swap revisions; `editor_api.py` validates edits and launches
jobs. Existing unscoped routes retain the original single-workspace layout.
Legacy adoption references that layout without copying/moving source files.
Never mutate module-level workspace paths: request ContextVars and explicitly
inherited worker context are what isolate projects.

`edit_model.py` owns immutable source-time edit snapshots and reviewable suggestions.
`media_editor.py` is the shared source-range renderer for manual, AI and editor
exports. It validates cut order/timing, crops, remaps captions and stages outputs
atomically; cancellation must reap the subprocess before releasing admission.
`desktop_sources.py` permits external-file linking only through the token-protected
Electron main-process native picker, never renderer-supplied arbitrary paths.

**Auto-Generate** POSTs `/api/auto-generate`; the server runs the LLM scorer
(`model_based_processing/scoring.py`) as a subprocess, then stitches clips whose score
clears the threshold, matched to manifest segments **by id** (never by start time — clip
starts are buffered). Scoring goes through a `ScoringBackend` selected by
`HI_LITE_LLM_BACKEND` / the in-app AI Settings: `local` (Ollama/llama.cpp-compatible
server, preferred when one responds) or `api` (any OpenAI-compatible endpoint with the
user's own key). With neither configured, Auto-Generate is disabled with an explanatory
message — manual selection never needs the network.

**Clip timing invariant:** adjacent clips split the silence gap at its midpoint
(`video_processor.get_smart_buffer`), computed identically from both sides — this is
what guarantees clips never overlap. Don't clamp against raw segment boundaries.

## Dev environments

### Native (Linux / WSL / macOS)
Python 3.12+, Node 20, ffmpeg on PATH.
```bash
python3 web_interface.py              # backend :8000
cd web && npm install && npm run dev  # frontend :5173
```
Or run the pipeline directly: `python3 main.py video.mp4 --output highlights`
(tip: `WHISPER_MODEL=tiny` for fast iteration).

### Docker
```bash
cd compose && docker compose up --build
```
Runs backend + frontend with all deps included (see `compose/README.md`).
Electron and Windows packaging are native-only.

### Electron shell
```bash
cd electron && npm install && npm run dev   # expects backend+frontend running
```

## Windows packaging

`./build-windows.sh` from WSL builds the full installer (see `electron/README.md`
for one-time setup). CI builds the installer on every PR to main.

**Landmines — read before touching packaging or pipeline prints:**
- **Frozen self-spawn:** in the onefile exe `sys.executable` *is* backend.exe.
  Children must be spawned with `_PYI_*`/`_MEIPASS2` stripped from env and
  `stdin=subprocess.DEVNULL`, or they never start / deadlock.
- **ASCII-only prints** in the pipeline. Windows pipes default to cp1252; one emoji
  kills the child with `UnicodeEncodeError`.
- **Status parsing is string-matching** on stdout ("Step 1: Extracting audio", …).
  If you reword prints in `main.py`/`local_transcriber.py`, update the matchers in
  `process_video_async`.
- **Packaging venv must contain only `requirements-backend.txt`** — the full
  requirements drag gigabytes into the exe. Keep runtime deps in *both* files.
- **Shutdown:** backend must die with Electron (taskkill + stdin-EOF watchdog).
  Orphaned backend.exe processes break the installer and steal port 8000.
- **g4f must never ship.** It is not a dependency and must not appear in any
  requirements file or in `backend.spec`'s `collect_all` (it is in `excludes`).
  `verify-app-flow.py` runs `backend.exe --verify-no-g4f` and fails the build
  if it is importable from the frozen exe.

After any packaging change, run the end-to-end check on the packaged app:
`verify-app-flow.py` (frozen server + upload + UI endpoints + shutdown watchdog).

## Custom third-party providers

The shipped scoring backends are `local` and `api`. Because the `api` backend speaks the
standard OpenAI `/v1/chat/completions` shape, most "custom provider" needs are met with no
code at all: point `HI_LITE_LLM_BASE_URL` (or the in-app AI Settings) at any compatible
endpoint — a commercial provider, an aggregator, or a self-hosted gateway.

If a provider needs its own client library instead, implement a `ScoringBackend` subclass
in `model_based_processing/scoring.py` (one `score(segments, transcript, instructions)`
method; reuse `build_prompt` / `extract_scored_segments` / `enrich_results`) and register
it in `get_backend()`. Call `enrich_results()` on the parsed records before any
sorting or selection. It validates exact transcript IDs, accepts finite JSON
numbers in the inclusive 1-10 range (strings and booleans are rejected), and
keeps the highest valid score per ID. The first occurrence fixes ordering;
the first record wins equal-score ties. Its `ScoringResult` is list-compatible
and carries bounded `validation` diagnostics: complete/partial coverage,
missing IDs (up to ten, with a total count), rejection counts/record indexes,
and duplicate counts. No usable records raises `ScoringError`.

The existing score files keep these diagnostics in a `validation` field.
They also keep a small `cache` envelope with an identity and completeness flag.
`score_cache_identity()` uses the ordered `transcription.json` segments (canonical
IDs, stripped request text, source start/end times), stripped criteria, resolved
backend/provider, effective model, nonsecret endpoint, and `SCORING_PROMPT_VERSION`.
Bump that constant beside `build_prompt()` whenever scoring semantics change.
Threshold, ordering, export controls, and credentials are outside the identity.

`load_score_cache()` is the shared applicability check for preview and generation;
it revalidates highlights through `enrich_results()` and verifies actual completeness.
Legacy, mismatching, corrupt, and incomplete caches miss without migration or deletion.
Fresh partial scores remain usable for the admitted generation job, with omitted
segments excluded, but cannot be automatically reused by a later job or preview.
Explicit rescore bypasses the cache. Score files publish through temporary files and
atomic replacement; `sorted_segments.json` is the authoritative cache published last.

Identity uses the backend's existing `name`, `model`, optional string `provider`, and
optional `base_url` request attributes after defaults and routing resolve. A backend
without a stable effective model can still score, but its results cannot be reused
automatically (including dynamically routed development extensions). The subprocess
receives the resolved settings and backend choice so a second auto-detection probe or
settings edit cannot change the admitted request. Endpoint identity preserves path
case and internal slashes, removes embedded credentials, and omits URL queries and
fragments because they can contain untyped secrets. Known credential-only query fields
allow reuse; any unknown query fields disable automatic reuse, since their values may
alter routing and cannot safely be persisted. This eligibility flag is checked against
the current endpoint, not trusted from disk. Never serialize the full settings
dictionary into score metadata or diagnostic output.
API/local request paths are appended before query parameters, keeping credential-only
URL variations consistent with the nonsecret endpoint identity. Preview requests must
send the current `instructions`, just as generation requests do.

Two rules for any such integration:

1. **Its library must never be installed automatically.** Do not add it to
   `requirements-backend.txt`, `requirements.txt`, the Docker images, or
   `backend.spec` — import it lazily and fail with a clear message when absent, the
   way `G4FBackend` does. Anything a requirements file pulls in is part of what
   Hi-Lite ships, and that is a decision for maintainers, not a side effect.
2. **You are responsible for the terms.** If you manually install and use a
   third-party provider or library, complying with the terms of service of that
   library and of every endpoint it talks to is on you, not on Hi-Lite. Do not
   enable such a backend in anything you distribute to other people.

`G4FBackend` (`HI_LITE_LLM_BACKEND=g4f`) is the in-tree example of this pattern: a
backend for a manually-installed library, for development and educational purposes only.
It relays to endpoints you cannot predict or control, so never point it at a transcript
containing anything private, and don't file issues for its `All providers failed`
errors — they are routine and unsupported.

Setup, in a development checkout only:

```bash
# 1. Install manually into your own dev environment -- g4f appears in no
#    requirements file, on purpose. (0.6.x is broken; use >=7.)
pip install "g4f>=7"

# 2. Opt in explicitly. No default ever selects this backend.
export HI_LITE_LLM_BACKEND=g4f

# 3. Run the dev server as normal.
python3 web_interface.py
```

Packaged builds exclude it entirely (`backend.spec` lists it in `excludes`, and
`verify-app-flow.py` fails the build if `backend.exe` can import it); if it is set but
not installed, Auto-Generate fails with an explanatory message rather than falling back
to another backend.

## PRs

- Branch from `main`; CI must pass (it builds the Windows installer).
- Run the API/manifest checks with `python verify-app-flow.py --contracts`.
  Run backend checks with
  `python -m unittest discover -s tests -v`; scoring provider calls are stubbed.
  Run `python tests/smoke_score_cache.py` for real scoring-subprocess and ffmpeg
  coverage of scoring, reuse, transcript invalidation, and threshold-only reuse.
  Run project-state checks with `cd web && npm run test:projects`, deterministic
  DOM/API checks with `npm run test:dom` (uses the test-only jsdom dependency),
  and the full built UI browser suite with `npm test`; install Chromium or set
  `CHROME_BIN` to its executable. The Chromium suite uses no browser automation test dependency. DOM tests stub
  media/layout and do not replace real browser playback or visual checks.
  Packaging changes also need the Windows `verify-app-flow.py` run.
- Keep pipeline prints ASCII; don't break the status-parser strings.
- Contributions are accepted under the project license (GPL-3.0-or-later).

## Releases

Bump the version in `electron/package.json` (the single source of version truth),
tag `vX.Y.Z`, publish a GitHub Release — CI attaches the Windows installer
automatically.
