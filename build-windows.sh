#!/usr/bin/env bash
# Build the Hi-Lite Windows installer from WSL.
#
# Drives native Windows tools (Python/PyInstaller, portable Node/electron-builder)
# through WSL interop. One-time machine setup is documented in electron/README.md;
# after that, re-run this script whenever you change the app.
#
# Usage:  ./build-windows.sh
# Output: electron/release/Hi-Lite Setup <version>.exe (also copied to the Desktop)
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
# Windows username is auto-detected; override with HI_LITE_WIN_USER if needed.
WIN_USER="${HI_LITE_WIN_USER:-$( (cd /mnt/c && cmd.exe /c 'echo %USERNAME%' 2>/dev/null) | tr -d '\r' )}"
[ -n "$WIN_USER" ] || { echo "ERROR: could not detect Windows username; set HI_LITE_WIN_USER" >&2; exit 1; }
BUILD_WSL="/mnt/c/Users/$WIN_USER/hi-lite-build"     # staging dir, WSL view
BUILD_WIN="C:\\Users\\$WIN_USER\\hi-lite-build"      # same dir, Windows view
# Override with HI_LITE_WIN_PYTHON if Python lives elsewhere on your machine.
WIN_PYTHON="${HI_LITE_WIN_PYTHON:-C:\\Users\\$WIN_USER\\AppData\\Local\\Programs\\Python\\Python312\\python.exe}"

# cmd.exe refuses UNC (\\wsl$) working directories, so run it from /mnt/c
wincmd() { (cd /mnt/c && cmd.exe /c "$@"); }

echo "==> 1/5 Building web UI (Vite)"
(cd "$REPO/web" && npm run build)

echo "==> 2/5 Syncing sources to $BUILD_WIN"
mkdir -p "$BUILD_WSL/web" "$BUILD_WSL/bin" "$BUILD_WSL/electron"
cp "$REPO"/{web_interface,main,subtitle_serializer,transcript_store,video_processor,transcription_client,local_transcriber,highlight_analyzer}.py \
   "$REPO/backend.spec" "$REPO/requirements-backend.txt" "$REPO/verify-app-flow.py" "$BUILD_WSL/"
rm -rf "$BUILD_WSL/model_based_processing"
cp -r "$REPO/model_based_processing" "$BUILD_WSL/model_based_processing"
rm -rf "$BUILD_WSL/model_based_processing/__pycache__"
rm -rf "$BUILD_WSL/web/dist"
cp -r "$REPO/web/dist" "$BUILD_WSL/web/dist"
rm -rf "$BUILD_WSL/electron/src"
cp -r "$REPO/electron/src" "$BUILD_WSL/electron/src"
cp "$REPO/electron"/{package.json,package-lock.json,tsconfig.json} "$BUILD_WSL/electron/"

if [ ! -f "$BUILD_WSL/bin/ffmpeg.exe" ] || [ ! -f "$BUILD_WSL/bin/ffprobe.exe" ]; then
  echo "ERROR: put ffmpeg.exe and ffprobe.exe in $BUILD_WSL/bin/ first" >&2
  echo "       (https://www.gyan.dev/ffmpeg/builds/ 'essentials' zip)" >&2
  exit 1
fi

echo "==> 3/5 Backend venv + dependencies"
if [ ! -f "$BUILD_WSL/.venv-build/Scripts/python.exe" ]; then
  wincmd "cd /d $BUILD_WIN && $WIN_PYTHON -m venv .venv-build"
fi
wincmd "cd /d $BUILD_WIN && .venv-build\\Scripts\\pip.exe install --quiet -r requirements-backend.txt pyinstaller"

echo "==> 4/5 PyInstaller -> backend.exe"
wincmd "cd /d $BUILD_WIN && .venv-build\\Scripts\\pyinstaller.exe --clean --distpath backend-dist --workpath .pyi-build backend.spec"

echo "==> 5/5 Electron: npm ci + tsc + NSIS installer"
NODE_PATH_WIN="$BUILD_WIN\\node"
if [ ! -d "$BUILD_WSL/electron/node_modules" ]; then
  wincmd "cd /d $BUILD_WIN\\electron && set PATH=$NODE_PATH_WIN;%PATH%&& npm.cmd ci --no-audit --no-fund"
fi
wincmd "cd /d $BUILD_WIN\\electron && set PATH=$NODE_PATH_WIN;%PATH%&& set CSC_IDENTITY_AUTO_DISCOVERY=false&& npm.cmd run build && npm.cmd run dist"

INSTALLER="$(ls "$BUILD_WSL"/electron/release/Hi-Lite\ Setup\ *.exe | tail -1)"
cp "$INSTALLER" "/mnt/c/Users/$WIN_USER/Desktop/"
echo
echo "DONE: $INSTALLER"
echo "Copied to C:\\Users\\$WIN_USER\\Desktop\\$(basename "$INSTALLER")"
