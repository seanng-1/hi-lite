# SPDX-License-Identifier: GPL-3.0-or-later
"""Real scoring subprocess + ffmpeg smoke, with deterministic AI and no network.

Run from the repository root: python tests/smoke_score_cache.py
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Inject only into the scoring child. Exercise the real subprocess/config,
# score publication, job admission, HTTP routes, and media tools.
PROVIDER_STUB = '''
import json
import os
from types import SimpleNamespace
import requests

def fixture_completion(*args, **kwargs):
    with open(os.environ["HI_LITE_SMOKE_CALLS"], "a", encoding="utf-8") as f:
        f.write("called\\n")
    records = [{"segment_id": 0, "score": 8}, {"segment_id": 4, "score": 3},
               {"segment_id": 9, "score": 7.5}]
    return SimpleNamespace(status_code=200, json=lambda: {
        "choices": [{"message": {"content": json.dumps({"segments": records})}}]})

requests.post = fixture_completion
'''


def main():
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("This smoke check requires ffmpeg and ffprobe on PATH.")
    with tempfile.TemporaryDirectory(prefix="hi-lite-cache-smoke-") as directory:
        data = Path(directory)
        (data / "sitecustomize.py").write_text(PROVIDER_STUB)
        calls = data / "provider-calls.txt"
        settings = {"HI_LITE_DATA_DIR": directory, "HI_LITE_LLM_BACKEND": "api",
                    "HI_LITE_LLM_BASE_URL": "https://fixture.invalid/v1",
                    "HI_LITE_LLM_MODEL": "fixture-model", "HI_LITE_LLM_API_KEY": "",
                    "HI_LITE_SMOKE_CALLS": str(calls), "PYTHONPATH": directory,
                    "FFMPEG_PATH": ffmpeg, "FFPROBE_PATH": ffprobe}
        with patch.dict(os.environ, settings), redirect_stdout(io.StringIO()):
            import web_interface as web
            from highlight_analyzer import save_segment_manifest

            segments = json.loads((ROOT / "tests/fixtures/segments.json").read_text())["segments"]
            save_segment_manifest([{**s, "transcript_id": s["id"]} for s in segments], web.MANIFEST_DIR)
            Path(web.SEGMENTS_DIR).mkdir(parents=True, exist_ok=True)
            transcript = Path(web.MANIFEST_DIR, "transcription.json")
            transcript.write_text(json.dumps({"segments": segments}))
            for segment, color in zip(segments, ("red", "green", "blue")):
                subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                                f"color=c={color}:s=160x90:r=10", "-t", "1.5",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y",
                                str(Path(web.SEGMENTS_DIR, segment["filename"]))], check=True)

            client = web.app.test_client()
            summary = []

            def generate(label, threshold, expected_calls, expected_clips):
                response = client.post("/api/auto-generate", json={
                    "instructions": "great plays", "min_score": threshold})
                assert response.status_code == 200, response.json
                # The existing worker is asynchronous; wait through its owner.
                deadline = time.monotonic() + 30
                while web._workspace_owner is not None and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert web._workspace_owner is None, "generation did not release admission"
                result = client.get("/api/processing-status/" + response.json["task_id"]).json
                assert result["status"] == "complete", result
                assert result["highlights_used"] == expected_clips, result
                provider_calls = len(calls.read_text().splitlines())
                assert provider_calls == expected_calls, provider_calls
                preview = client.post("/api/preview-auto-export", json={
                    "instructions": "great plays", "min_score": threshold})
                assert preview.status_code == 200, preview.json
                assert preview.json["count"] == expected_clips, preview.json
                path = Path(web.HIGHLIGHTS_DIR, result["output_file"].rsplit("/", 1)[-1])
                duration = float(subprocess.check_output([
                    ffprobe, "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(path)], text=True))
                assert abs(duration - 1.5 * expected_clips) < 0.1, duration
                assert client.get(result["output_file"]).data == path.read_bytes()
                summary.append({"step": label, "provider_calls": provider_calls,
                                "clips": expected_clips, "duration": duration})

            generate("score", 7, 1, 2)
            generate("reuse", 7, 1, 2)
            segments[0]["text"] = "a different transcript at the same filename"
            transcript.write_text(json.dumps({"segments": segments}))
            assert client.post("/api/preview-auto-export", json={
                "instructions": "great plays", "min_score": 7}).status_code == 400
            generate("transcript change", 7, 2, 2)
            generate("threshold change", 8, 2, 1)
        print(json.dumps({"status": "passed", "steps": summary}, indent=2))


if __name__ == "__main__":
    main()
