# PyInstaller spec for packaging Flask backend (entry: web_interface.py)
# ONE-FILE mode: produces a single standalone .exe
#
# Build from a CLEAN venv containing only requirements-backend.txt, otherwise
# stray heavyweight deps (torch, spacy, ...) get pulled into the bundle:
#
#   python -m venv .venv-build
#   .venv-build\Scripts\pip install -r requirements-backend.txt pyinstaller
#   .venv-build\Scripts\pyinstaller --clean --onefile --name backend \
#       --distpath backend-dist --workpath .pyi-build --specpath . backend.spec

from PyInstaller.utils.hooks import copy_metadata, collect_all
import os

block_cipher = None

hiddenimports = [
    'flask', 'flask_cors', 'moviepy',
    'imageio_ffmpeg', 'numpy', 'PIL', 'decorator', 'proglog',
    # Pipeline modules invoked via `backend.exe --process-video`
    'main', 'video_processor', 'transcription_client',
    'local_transcriber', 'highlight_analyzer',
    # AI scoring invoked via `backend.exe --score-highlights`
    'model_based_processing', 'model_based_processing.scoring',
]

datas = []
binaries = []

# Include package metadata
datas += copy_metadata('imageio')
datas += copy_metadata('imageio-ffmpeg')

# faster-whisper stack: ctranslate2 ships native libs, faster_whisper ships
# VAD model assets, onnxruntime is needed for vad_filter=True.
for pkg in ('faster_whisper', 'ctranslate2', 'onnxruntime', 'tokenizers', 'huggingface_hub', 'av'):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Add frontend files (built web UI served by Flask in production)
web_dist_path = os.path.join('.', 'web', 'dist')
if os.path.exists(web_dist_path):
    datas.append((web_dist_path, 'web/dist'))
    print(f"Bundling frontend from {os.path.abspath(web_dist_path)}")
else:
    raise SystemExit("web/dist NOT FOUND - run `npm run build` in web/ first")

a = Analysis(
    ['web_interface.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # g4f is deliberately excluded: scoring goes through the user's own
    # local or API backend, and g4f is a dev-only, manually-installed option
    # that must never ship in the installer. `backend.exe --verify-no-g4f`
    # (run by verify-app-flow.py) fails the build if it becomes importable.
    excludes=['g4f', 'torch', 'spacy', 'transformers', 'nodriver', 'matplotlib'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
