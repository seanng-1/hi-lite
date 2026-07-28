# Hi-Lite Electron shell

Packages the Flask backend (PyInstaller `backend.exe`) + built web UI + ffmpeg
into a single Windows installer. **No Docker, Python, or Whisper install is
required on end-user machines** — transcription runs inside the backend via
faster-whisper, and the model is downloaded automatically on first use into the
per-user data folder.

## How it fits together (production)

- Electron spawns `resources/backend.exe`, which serves the API **and** the
  built frontend on `http://127.0.0.1:8000`.
- When a video is uploaded, the backend re-invokes itself as
  `backend.exe --process-video <video> --output <folder>` to run the pipeline
  (there is no Python interpreter on the user's machine, so `sys.executable`
  *is* backend.exe).
- Electron passes these env vars to the backend:
  - `HI_LITE_DATA_DIR` = Electron `userData` folder (uploads, highlights and
    whisper models live there, never the install dir)
  - `FFMPEG_PATH` / `FFPROBE_PATH` / `FFMPEG_BINARY` = shipped binaries in
    `resources/bin`
- Optional backend env vars: `WHISPER_MODEL` (default `small`),
  `HI_LITE_PRE_BUFFER` / `HI_LITE_POST_BUFFER` (clip padding seconds),
  `HI_LITE_PORT` (default 8000), `USE_WHISHPER=1` to use an external Whishper
  server instead of the embedded transcriber.

## Dev workflow

Linux prerequisites for Electron:

```
sudo apt install -y \
  libnss3 libatk-bridge2.0-0t64 libx11-xcb1 libxcomposite1 libxcursor1 \
  libxdamage1 libxi6 libxtst6 libcups2t64 libdrm2 libgbm1 libasound2t64 \
  libpangocairo-1.0-0 libpango-1.0-0 libgtk-3-0t64
```

- Terminal 1 (UI): `cd web && npm install && npm run dev` (Vite on :5173)
- Terminal 2 (Electron): `cd electron && npm install && npm run dev`
  (spawns `python3 ../web_interface.py` for the backend on :8000)

## Building the Windows app

The backend must be built by a **Windows** Python (PyInstaller output is
platform-specific), but the whole build is driven from WSL via interop.
After the one-time setup below, rebuilding is a single command from the
repo root in WSL:

```bash
./build-windows.sh
```

That builds the web UI, syncs sources to `%USERPROFILE%\hi-lite-build`,
rebuilds `backend.exe` with PyInstaller, packages the NSIS installer with
electron-builder, and copies `Hi-Lite Setup <version>.exe` to the Desktop.
Takes ~5 minutes. Installer output also stays in
`%USERPROFILE%\hi-lite-build\electron\release\`.

### One-time machine setup

The script expects a staging dir at `%USERPROFILE%\hi-lite-build` with:

1. **Windows Python 3.12** (per-user, no admin):
   `winget install --id Python.Python.3.12 --scope user`
   → `%USERPROFILE%\AppData\Local\Programs\Python\Python312\python.exe`
   (auto-detected; override via `HI_LITE_WIN_USER` / `HI_LITE_WIN_PYTHON`
   env vars when running `build-windows.sh`)
2. **Portable Node.js** unzipped to `hi-lite-build\node\`
   (node-v22.x-win-x64.zip from https://nodejs.org/dist/ — no installer)
3. **ffmpeg binaries** at `hi-lite-build\bin\ffmpeg.exe` and `ffprobe.exe`
   (https://www.gyan.dev/ffmpeg/builds/, "essentials" zip)

The venv (`.venv-build`) and `electron\node_modules` are created by the
script on first run and reused afterwards. If a dependency ever seems
stale, delete those two folders and re-run the script.

### WSL → Windows interop gotchas (if you script anything yourself)

- Always `cmd.exe /c "cd /d C:\..."` — cmd can't use `\\wsl$` paths as cwd.
- `timeout /t` fails from WSL (no console stdin); use `ping -n <secs+1> 127.0.0.1 > nul`.
- Quote escaping through WSL→cmd is unreliable; write a `.bat` file for
  anything non-trivial.

### Sanity-checking backend.exe before packaging

```
backend-dist\backend.exe
```
should serve the UI at http://127.0.0.1:8000. Then upload a short mp4 through
the UI — the first run downloads the whisper model (~460 MB for `small`), so
give it a few minutes. Set `WHISPER_MODEL=tiny` for a quicker smoke test.

### Notes

- `win.signAndEditExecutable` is disabled in package.json: the build is
  unsigned (fine for beta), and enabling it requires Windows Developer Mode or
  admin rights because electron-builder's winCodeSign extraction creates
  symlinks. Re-enable it when we start code-signing releases.

- The first transcription needs internet access to fetch the model from
  Hugging Face; after that the app works fully offline.
- Models/uploads/highlights live in `%APPDATA%\Hi-Lite` (Electron userData).
- The backend binds to 127.0.0.1 in production, which avoids the Windows
  Firewall prompt.
