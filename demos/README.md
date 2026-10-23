# Reproducible synthetic export showcase

`render_showcase.py` creates an original animated arcade-racing scene and passes
it through Hi-Lite's **real `media_editor.export_source_ranges` implementation**.
This is a demonstration, not a recording of a real game. Every frame includes
`SYNTHETIC GAMEPLAY DEMO`. Caption text is authored for the demo, not produced by
a speech-recognition model. The quiet audio track is also generated from scratch.
No private recordings, game assets, network calls, model access, API keys, or
browser automation are involved.

## Run

Requirements: Python 3.10+, Pillow, and `ffmpeg`/`ffprobe` on PATH. FFmpeg must
support the libx264 and libass filters used by the production exporter.

```sh
python demos/render_showcase.py --output-dir /tmp/hi-lite-showcase
```

Generated assets must be outside the repository. The default workload is bounded:
12 seconds of 1920 × 1080 source at 24 fps and five 7-second exports. A completed
source can be reused with `--reuse-source` when refining an export demonstration.
Do not commit the resulting media.

## What it demonstrates

- Actual, on-demand source-range export, including a nonchronological reel:
  source seconds **6–9 → 1–3 → 9–11**. The 3–6 second passage is omitted.
- Burned captions in 16:9, 9:16, and 1:1 outputs using the actual ASS renderer.
- Viewport-aware caption sizing, custom text color, outline, and font selection.
- Center-focal-point crop behavior. No auto-tracking or AI reframing is claimed.
- SRT and WebVTT subtitles remapped to edited timeline time. The omitted caption
  is absent, and reordered captions appear in reel order.
- Preserved generated audio, checked by probing the output.
- A matching clean edited export for a before/after caption comparison.

Exports are not upscaled. For this source the actual dimensions are 1920 × 1080
(16:9), 594 × 1056 (9:16), and 1080 × 1080 (1:1). The portrait exporter uses the
largest exact, even-sized 9:16 crop supported by the source dimensions.

## Evidence and limitations

`showcase-manifest.json` records the disclosure, source SHA-256, source caption
cues, selected ranges, remapped captions, output settings, and actual ffprobe
results. The script asserts dimensions, audio presence, duration within a frame
plus mux tolerance, subtitle ordering, and omission of the removed caption.

PNG files are **unaltered FFmpeg frame extractions from the actual MP4 outputs**.
Names ending `drift`, `boost`, and `finish` are output timestamps 1, 4, and 6
seconds, corresponding to source timestamps 7, 2, and 10 seconds. Raw-source
frames at 2, 4, 7, and 10 seconds illustrate the original chronology.

These demonstrations validate the local media export backend. They do not prove
browser UI operation, transcription quality, automatic highlight ranking,
platform upload, live game capture, or distributed packaging. The output naming
is descriptive, not a claim that a particular game or platform is integrated.
