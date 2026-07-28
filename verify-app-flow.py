# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify the packaged backend end-to-end the way the Electron app uses it:
spawn backend.exe with a stdin pipe + watchdog, upload a video, wait for
processing, check the exact endpoints the UI fetches, then die abruptly
and let a follow-up check confirm backend.exe exited via the watchdog.

Run on Windows against the staging dir produced by build-windows.sh:
    python verify-app-flow.py
The staging dir defaults to %USERPROFILE%\\hi-lite-build; override with
HI_LITE_BUILD_DIR. Expects backend-dist\\backend.exe, bin\\ffmpeg.exe,
bin\\ffprobe.exe, and a test.mp4 inside it."""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get("HI_LITE_BUILD_DIR",
                      os.path.join(os.path.expanduser("~"), "hi-lite-build"))
PORT = "8040"
URL = f"http://127.0.0.1:{PORT}"

env = {**os.environ,
       "HI_LITE_WATCHDOG": "1",
       "HI_LITE_PORT": PORT,
       "HI_LITE_DATA_DIR": os.path.join(BASE, "verifydata"),
       "WHISPER_MODEL": "tiny",
       "FFMPEG_PATH": os.path.join(BASE, "bin", "ffmpeg.exe"),
       "FFPROBE_PATH": os.path.join(BASE, "bin", "ffprobe.exe")}

BACKEND_EXE = os.path.join(BASE, "backend-dist", "backend.exe")

# Build guard: g4f is dev-only and must never ship, so the frozen exe must
# not be able to import it. If a collect_all/hiddenimports sweep ever drags
# it into the bundle, this is what catches it.
no_g4f = subprocess.run([BACKEND_EXE, "--verify-no-g4f"], env=env, cwd=BASE,
                        capture_output=True, text=True, timeout=120)
print((no_g4f.stdout or "").strip() or (no_g4f.stderr or "").strip())
if no_g4f.returncode != 0:
    print("FAIL: g4f is importable from backend.exe - it must not ship")
    sys.exit(1)

backend = subprocess.Popen([BACKEND_EXE],
                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=env, cwd=BASE)
print(f"backend.exe pid {backend.pid}")

for _ in range(60):
    time.sleep(2)
    try:
        urllib.request.urlopen(f"{URL}/data/segments_manifest.json", timeout=3)
        break
    except Exception:
        pass
else:
    print("FAIL: server never came up"); sys.exit(1)
print("server up")

up = subprocess.run(["curl.exe", "-s", "-X", "POST", "-F",
                     f"video=@{os.path.join(BASE, 'test.mp4')}",
                     f"{URL}/api/upload"],
                    capture_output=True, text=True)
task = json.loads(up.stdout)["task_id"]
print(f"upload ok, task {task}")

status = {}
for _ in range(120):
    time.sleep(5)
    with urllib.request.urlopen(f"{URL}/api/processing-status/{task}") as r:
        status = json.load(r)
    if status.get("status") in ("complete", "error"):
        break
print(f"processing: {status.get('status')} - {status.get('message')}")
if status.get("status") != "complete":
    sys.exit(1)

with urllib.request.urlopen(f"{URL}/data/segments_manifest.json") as r:
    manifest = json.load(r)
n = len(manifest["segments"])
print(f"UI manifest endpoint: {n} segments")

first = manifest["segments"][0]["filename"]
with urllib.request.urlopen(f"{URL}/segments/{first}") as r:
    print(f"UI video endpoint /segments/{first}: HTTP {r.status}")

print("APP FLOW OK - now dying abruptly to test the watchdog")
os._exit(1)  # no cleanup, no kill: backend must exit on its own
