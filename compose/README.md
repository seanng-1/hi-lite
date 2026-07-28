# Docker dev environment

One container running both dev servers (Python 3.12 + Node 20 + ffmpeg baked in):

- Flask backend → http://localhost:8000
- Vite frontend → http://localhost:5173 (use this one; it proxies API calls to Flask)

```bash
cd compose
docker compose up --build     # first run / after backend dep changes
docker compose up             # subsequent runs
docker compose up --build -V  # after changing web/package.json (-V renews the node_modules volume)
```

The repo is bind-mounted, so frontend edits hot-reload via Vite. Backend edits
need a container restart (the Flask reloader is deliberately disabled — it forks
the process and resets global state). Uploads, highlights, and downloaded
Whisper models land in the repo root as in native dev and persist across runs.

Tip: `WHISPER_MODEL=tiny docker compose up` for fast transcription while testing.

Not covered by Docker: the Electron shell and Windows packaging — those are
native/WSL only (see electron/README.md).

## VS Code Dev Containers

`.devcontainer/devcontainer.json` points at this same `docker-compose.yml`, so
"Reopen in Container" in VS Code gets you the identical environment with no
extra setup.
