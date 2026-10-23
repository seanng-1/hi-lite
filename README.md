# Hi-Lite

Turn long gameplay, podcasts or sports videos into a highlight reel. No account, no sign-up,
and your video files never leave your computer.

Hi-Lite transcribes your video's commentary on-device (faster-whisper), saves it in
a persistent project, and lets you edit transcript passages without changing the
original recording. Source ranges are rendered only when you export. There is also an optional **Auto-Generate** feature that has an LLM score
the clips for you, using a backend *you* choose: a model running on your own machine
(fully offline), or an OpenAI-compatible API with your own key. See
[What leaves your machine](#what-leaves-your-machine).

<img width="1914" height="948" alt="Upload screen" src="https://github.com/user-attachments/assets/37dc3d62-dcc6-478a-b714-e511e22fbb68" />


## Install (Windows)

Download the latest installer from [Releases](https://github.com/seanng-1/hi-lite/releases).
No Python, Docker, or Whisper install needed — everything is bundled. On first use the
Whisper model (~500 MB) downloads automatically.

## How it works

1. **Upload** a video — audio is extracted and transcribed locally with word-level timestamps
2. **Review** — transcript passages previewed directly from the original recording
   <img width="1909" height="943" alt="Clip selection" src="https://github.com/user-attachments/assets/366961f6-7b86-49f6-8ba5-76ceb036768d" />
3. **Select** clips manually — fully offline — or hit **Auto-Generate** to have an LLM
   score every segment against your own criteria ("clutch rounds and funny moments
   only"). Scoring runs on a local model or your own API account; see
   [Auto-Generate setup](#auto-generate-setup)
   <img width="1914" height="947" alt="Auto-generate" src="https://github.com/user-attachments/assets/9022393a-3fa0-4346-a47a-f9ce59609478" />
4. **Edit and export** — open the transcript editor to trim, split, combine or reorder
   passages; undo/redo; edit captions; review cleanup and clip suggestions; then
   export a reel or individual clips in original, 9:16, 16:9 or 1:1 framing
   <img width="1911" height="945" alt="Export" src="https://github.com/user-attachments/assets/981f5132-50ee-49eb-a5fe-800cbf3921d0" />

## Persistent projects and transcript editing

- **Projects** creates named recordings and opens recent work without retranscription.
  Selections, transcript edits, caption text and export settings autosave locally.
  Save conflicts do not overwrite changes from another window; retry failed saves
  or explicitly reopen the saved version. Closing with unsaved changes warns you.
- Each project has its own source, transcript, manifests and exports under
  `HI_LITE_DATA_DIR/projects/<id>/`. An existing single-workspace recording appears
  as **Previous recording** and is adopted in place, without moving media.
- New imports create metadata, not thousands of intermediate video files. Older
  clip-based workspaces remain readable. Command-line users can opt into eager
  segment rendering with `python main.py video.mp4 --render-segments`.
- **Open transcript editor** edits source-time ranges. Cuts never modify the source.
  Split/trim/combine/reorder, caption edits and accepted cleanup suggestions support
  undo/redo while the editor is open. Saved edits survive reopening the app.
- Captions can be edited, styled, burned into video, or downloaded as SRT/VTT after
  remapping through cuts. Crop controls set horizontal/vertical framing; individual
  exports follow the edited clip groups.
- Candidate generation lets you request a count, target length and category.
  Offline suggestions are explicitly labeled **local heuristics**, not AI.
  Opting into AI uses only your configured provider and scores the complete
  transcript in bounded batches. Every result remains reviewable before replacing
  your edit. The app may return fewer candidates when distinct passages are limited.
- Cleanup proposes silence shortening and aligned filler-word cuts. Nothing is
  removed until you accept a suggestion. Review transitions before exporting.
- Background transcription, scoring and editor rendering can be cancelled. The
  worker is stopped before another operation is admitted; incomplete exports are
  removed rather than offered as finished files.
- The Electron desktop app can **link an original** using its native picker instead
  of copying the video. Keep it unchanged; **Relink original** can locate an unchanged
  moved copy using content verification, without retranscribing. Browser uploads
  continue to use managed copies.

Source preview depends on the codecs supported by the browser; use H.264/AAC
MP4 for the broadest compatibility. FFmpeg export can still read supported source
formats that the browser cannot play. Rendering uses temporary media, including
lossless audio, so allow free disk space for the selected footage.

The original source is still required for source preview and new exports. Keep
project folders and linked originals backed up together. Do not manually edit the
project metadata or replace an original with different footage.

## What leaves your machine

| What you do | What goes over the network |
|---|---|
| Upload, transcribe, cut clips, review, select manually, export | Nothing |
| First transcription after install | One-time Whisper model download from Hugging Face (~500 MB) |
| Auto-Generate with a **local model** | Nothing — scoring runs on your own machine |
| Auto-Generate with an **API backend** | The transcript text of your video, sent to the provider **you** configured with your own key |

Your video and audio files are never uploaded, under any feature. Transcription happens
on your own CPU/GPU via faster-whisper. The in-app AI Settings panel always shows which
backend is active and what (if anything) it sends.

## Auto-Generate setup

Auto-Generate needs an LLM to score your clips. Two supported ways to provide one — if
neither is configured the button is disabled and everything else keeps working:

**Local model (nothing leaves your machine).** Run [Ollama](https://ollama.com) (or any
server exposing OpenAI-compatible `/v1` routes, e.g. llama.cpp) and pull a small instruct
model:

```bash
ollama pull qwen2.5:7b-instruct   # current default; configurable in AI Settings
```

Hi-Lite auto-detects a server at `127.0.0.1:11434` and prefers it over any API.

**OpenAI-compatible API (your provider, your account, your terms).** Open **AI Settings**
in the app, enter a base URL, model, and API key — works with OpenAI, OpenRouter,
Together, a self-hosted gateway, or anything else speaking `/v1/chat/completions`. The key
is stored on your machine in the app's data directory, never in the browser, and is only
ever sent to the base URL you configured.

These two backends — a local model and an OpenAI-compatible API — are the ones Hi-Lite
ships with built-in support for. 

## Testing
**Other providers** can be wired up for **development** or
**educational** purposes; see ["Custom third-party providers" in
CONTRIBUTING.md](CONTRIBUTING.md#custom-third-party-providers) for the walkthrough. You
are responsible for complying with the terms of service of anything you integrate
manually.

## Quick start for developers

```bash
# Backend (Flask, :8000)
pip install -r requirements.txt
python3 web_interface.py

# Frontend (Vite, :5173 — proxies API calls to :8000)
cd web && npm install && npm run dev
```

Requires Python 3.12+, Node 20, and ffmpeg on PATH. A Docker dev environment is also
available — see [CONTRIBUTING.md](CONTRIBUTING.md) for that, the Electron shell, and
Windows packaging.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `WHISPER_MODEL` | `small` | Whisper model size (`tiny` for quick tests) |
| `HI_LITE_PRE_BUFFER` / `HI_LITE_POST_BUFFER` | `0.5` / `0.75` | Seconds of padding around each clip |
| `HI_LITE_PORT` | `8000` | Backend port |
| `HI_LITE_DATA_DIR` | repo root | Where projects, uploads, exports, and models live |
| `HI_LITE_LLM_BACKEND` | auto | Force the Auto-Generate backend: `local` or `api` |
| `HI_LITE_LLM_BASE_URL` / `HI_LITE_LLM_API_KEY` / `HI_LITE_LLM_MODEL` | OpenAI / — / `gpt-4o-mini` | API backend settings (also editable in-app) |
| `HI_LITE_LLM_LOCAL_URL` / `HI_LITE_LLM_LOCAL_MODEL` | `http://127.0.0.1:11434` / `qwen2.5:7b-instruct` | Local backend settings |

## Contributing

Issues and PRs welcome — [CONTRIBUTING.md](CONTRIBUTING.md) has the architecture tour,
dev environments, and the Windows-packaging gotchas that have bitten us before.

## License

Copyright (C) 2025 seanng-1

Hi-Lite is free software licensed under [GPL-3.0-or-later](LICENSE). It comes with
ABSOLUTELY NO WARRANTY, to the extent permitted by law.

The Windows installer bundles ffmpeg and other third-party software — see
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md) for their licenses and for the
written offer of corresponding source.
