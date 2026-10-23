#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render honest, reproducible Hi-Lite examples from original synthetic footage.

This creates a procedural arcade scene, not a recording of a real game. Caption
text is deliberately authored, not speech recognition output. All cut, crop,
caption burn-in and subtitle operations use the real media_editor public API.
Generated media stays outside the repository and contains no private footage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_editor import export_source_ranges, probe_source, remap_captions

WIDTH, HEIGHT, FPS, DURATION = 1920, 1080, 24, 12
CAPTIONS = [
    {"start": 0.4, "end": 3.0, "text": "Hold the boost until the straight."},
    {"start": 3.0, "end": 5.8, "text": "Now! Take the inside line."},
    {"start": 6.0, "end": 9.0, "text": "That drift saved the run."},
    {"start": 9.0, "end": 11.8, "text": "Clean line. Perfect finish."},
]
# Reorder three source sections. The 3–6s passage is deliberately omitted.
CUTS = [{"start": 6, "end": 9}, {"start": 1, "end": 3}, {"start": 9, "end": 11}]


def font(size: int, bold: bool = False):
    for root in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"):
        path = Path(root) / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


FONTS = {n: font(n, n in (20, 28, 34, 52, 64, 76)) for n in (18, 20, 24, 28, 34, 40, 52, 64, 76)}


def text(draw, xy, value, size=24, fill="#edf5ff", anchor=None):
    draw.text(xy, value, font=FONTS[size], fill=fill, anchor=anchor)


def panel(draw, box, outline="#294361", fill="#0b1629", radius=18):
    draw.rounded_rectangle(box, radius, fill=fill, outline=outline, width=2)


def scene_background():
    img = Image.new("RGB", (WIDTH, HEIGHT))
    d = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        q = y / HEIGHT
        # Purple night above a blue-black track.
        c = (int(10 + 32 * max(0, 1 - abs(q - .34) * 3)),
             int(14 + 12 * q), int(37 + 36 * max(0, 1 - abs(q - .34) * 3)))
        d.line((0, y, WIDTH, y), fill=c)
    rng = random.Random(2026)
    for _ in range(165):
        x, y = rng.randrange(WIDTH), rng.randrange(400)
        radius = rng.choice((1, 1, 2))
        d.ellipse((x, y, x + radius, y + radius), fill=rng.choice(("#53648f", "#8096be", "#d7e8ff")))
    # Ringed planet, original vector art.
    d.ellipse((1400, 140, 1560, 300), fill="#372d67", outline="#62578e", width=2)
    d.arc((1340, 190, 1610, 260), 0, 360, fill="#8172a5", width=7)
    # Distant angular skyline.
    for side in (0, 1):
        for i in range(17):
            x = (i * 60 if side == 0 else 1100 + i * 55)
            top = 380 - rng.randrange(30, 155)
            w = rng.randrange(24, 52)
            d.rectangle((x, top, x + w, 465), fill="#0d1731", outline="#243655")
            for y in range(top + 13, 450, 21):
                for xx in range(x + 7, x + w - 5, 12):
                    if rng.random() > .3:
                        d.rectangle((xx, y, xx + 3, y + 7), fill=rng.choice(("#31597c", "#544477", "#6c4a85")))
    d.polygon([(0, 490), (150, 427), (300, 470), (420, 382), (590, 470), (760, 398), (960, 460),
               (1100, 385), (1310, 440), (1440, 362), (1650, 440), (1920, 375), (1920, 620), (0, 620)], fill="#112039")
    return img


BACKGROUND = scene_background()


def scene_frame(t):
    img = BACKGROUND.copy()
    d = ImageDraw.Draw(img)
    horizon = 400
    road_center = 960 + 35 * math.sin(t * .55)
    # Track deck, rail lighting, perspective lane markings.
    d.polygon([(road_center - 75, horizon), (road_center + 75, horizon), (1810, HEIGHT), (110, HEIGHT)], fill="#10233a")
    d.polygon([(road_center - 75, horizon), (road_center - 94, horizon), (30, HEIGHT), (110, HEIGHT)], fill="#23304a")
    d.polygon([(road_center + 75, horizon), (road_center + 94, horizon), (1890, HEIGHT), (1810, HEIGHT)], fill="#23304a")
    for frac in (-1, -.34, .34, 1):
        x0 = road_center + frac * 75
        x1 = 960 + frac * 850
        col = "#67eaf3" if abs(frac) == 1 else "#315779"
        d.line((x0, horizon, x1, HEIGHT), fill=col, width=5 if abs(frac) == 1 else 2)
    for n in range(25):
        p = ((n / 25 + t * .18) % 1) ** 2
        y = horizon + p * (HEIGHT - horizon)
        left, right = road_center - 75 - p * 775, road_center + 75 + p * 775
        d.line((left, y, right, y), fill="#23425d", width=max(1, int(p * 3)))
        for frac in (-.34, .34):
            x = road_center + frac * (75 + p * 775)
            length = 6 + p * 65
            d.line((x, y, x + frac * length * .9, y + length), fill="#52778e", width=max(1, int(p * 5)))
    # Repeating checkpoint arch sweeping toward the racer.
    p = (.1 + (t * .11) % .85)
    y = horizon + p * p * 430
    half = 100 + p * 430
    top = y - 100 - p * 150
    for width, col in ((24, "#1e5f70"), (10, "#49dbe8"), (3, "#b2fbff")):
        d.line((road_center - half, y + 150 * p, road_center - half, top + 45,
                road_center - half + 45, top, road_center + half - 45, top,
                road_center + half, top + 45, road_center + half, y + 150 * p), fill=col, width=width, joint="curve")
    text(d, (road_center, top - 40), "CHECKPOINT  03", 20, "#87e7f0", "mm")
    # Roadside floating lights and geometric sculptures.
    for side in (-1, 1):
        for n in range(7):
            p = ((n / 7 + t * .12) % 1) ** 2
            x = road_center + side * (120 + p * 850)
            y = horizon + p * 640
            h = 15 + p * 120
            d.line((x, y, x, y - h), fill="#516886", width=max(2, int(5 * p)))
            d.ellipse((x - 4 - p * 5, y - h - 4, x + 4 + p * 5, y - h + 4), fill="#ff6aa8")
    # Original hovering craft, movement deliberately contained by center crop.
    cx = 960 + math.sin(t * 1.4) * 55
    cy = 790 + math.sin(t * 2.2) * 8
    d.ellipse((cx - 165, cy + 34, cx + 165, cy + 82), fill="#081827")
    for dx in (-69, 69):
        flame = 45 + 22 * math.sin(t * 39 + dx)
        d.polygon([(cx + dx - 16, cy + 23), (cx + dx + 16, cy + 23), (cx + dx, cy + 23 + flame)], fill="#5bd8fc")
        d.polygon([(cx + dx - 7, cy + 23), (cx + dx + 7, cy + 23), (cx + dx, cy + flame)], fill="#e0fbff")
    d.polygon([(cx, cy - 150), (cx + 56, cy - 68), (cx + 130, cy + 10), (cx + 108, cy + 41),
               (cx + 40, cy + 21), (cx, cy + 47), (cx - 40, cy + 21), (cx - 108, cy + 41),
               (cx - 130, cy + 10), (cx - 56, cy - 68)], fill="#d3e2ef", outline="#fcfdff", width=3)
    d.polygon([(cx, cy - 142), (cx + 34, cy - 56), (cx + 23, cy + 12), (cx - 23, cy + 12), (cx - 34, cy - 56)], fill="#1a3e62", outline="#76f4f6", width=3)
    d.polygon([(cx - 110, cy + 12), (cx - 48, cy - 47), (cx - 55, cy + 18)], fill="#5363a0")
    d.polygon([(cx + 110, cy + 12), (cx + 48, cy - 47), (cx + 55, cy + 18)], fill="#5363a0")
    d.line((cx - 110, cy + 21, cx - 57, cy + 25), fill="#ff73b5", width=6)
    d.line((cx + 110, cy + 21, cx + 57, cy + 25), fill="#ff73b5", width=6)
    # HUD. The synthetic disclosure is centered so every demonstrated crop keeps it.
    panel(d, (WIDTH / 2 - 216, 24, WIDTH / 2 + 216, 67), outline="#5a527f", fill="#191831")
    text(d, (WIDTH / 2, 45), "SYNTHETIC GAMEPLAY DEMO", 20, "#b9b0df", "mm")
    text(d, (70, 65), "NEON / DRIFT", 40, "#f0f8ff")
    text(d, (73, 118), "ORIGINAL PROCEDURAL ARCADE SCENE", 18, "#7d98b6")
    panel(d, (70, 173, 302, 284))
    text(d, (92, 191), "POSITION", 18, "#8da3bc")
    text(d, (90, 220), "01", 52)
    text(d, (175, 245), "/ 08", 24, "#6f91ae")
    text(d, (WIDTH - 74, 68), "LAP  02 / 03", 28, "#a1d8e6", "ra")
    text(d, (WIDTH - 74, 114), f"01:{int(23+t):02d}.{int((t%1)*100):02d}", 34, "#f4f6ff", "ra")
    panel(d, (72, 826, 351, 1008))
    text(d, (95, 847), "VELOCITY", 18, "#8da3bc")
    text(d, (92, 880), str(272 + int(22 * math.sin(t * .9))), 76, "#f2f6ff")
    text(d, (271, 937), "KM/H", 18, "#8da3bc")
    d.rounded_rectangle((95, 978, 326, 985), 4, fill="#20415d")
    d.rounded_rectangle((95, 978, 283, 985), 4, fill="#61e5f2")
    panel(d, (1570, 826, 1848, 1008))
    text(d, (1594, 847), "BOOST", 18, "#8da3bc")
    for n in range(8):
        d.rounded_rectangle((1595 + n * 29, 894, 1615 + n * 29, 944), 3, fill="#b18dff" if n < 6 else "#29334d")
    text(d, (1594, 968), "HOLD  /  RELEASE", 18, "#b29ace")
    if 6 <= t < 9:
        text(d, (960, 167), "+ 1,250", 52, "#84f4df", "mm")
        text(d, (960, 212), "PERFECT DRIFT", 20, "#c4f8ee", "mm")
    elif t >= 9:
        text(d, (960, 167), "NEW BEST", 52, "#b5a0ff", "mm")
        text(d, (960, 212), "CLEAN RUN  /  + 2,500", 20, "#ddd2ff", "mm")
    # No spoken lines are drawn here. Hi-Lite adds them during real export.
    return img


def make_source(destination):
    with tempfile.TemporaryDirectory(prefix="hi-lite-synthetic-") as temporary:
        raw = Path(temporary) / "scene.mp4"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264",
                   "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p", "-threads", "2", str(raw)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        try:
            for n in range(FPS * DURATION):
                process.stdin.write(scene_frame(n / FPS).tobytes())
            process.stdin.close()
            if process.wait(timeout=120):
                raise RuntimeError("Synthetic source encoding failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        # A quiet original procedural pulse track. No voice or licensed audio.
        audio = Path(temporary) / "synth.wav"
        with wave.open(str(audio), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(48000)
            samples = bytearray()
            for n in range(48000 * DURATION):
                t = n / 48000
                beat = math.exp(-((t * 2) % 1) * 8)
                freq = (110, 138.59, 164.81, 123.47)[int(t) % 4]
                value = .13 * beat * math.sin(2 * math.pi * freq * t) + .025 * math.sin(2 * math.pi * 220 * t)
                samples.extend(struct.pack("<h", int(value * 32767)))
            out.writeframes(samples)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-i", str(audio),
                        "-c:v", "copy", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(destination)], check=True)


def extract(video, time, target):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(time), "-i", str(video),
                    "-frames:v", "1", "-update", "1", str(target)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="Generated media directory, outside this repository")
    parser.add_argument("--reuse-source", action="store_true", help="Reuse an existing synthetic-source.mp4 in the output directory")
    args = parser.parse_args()
    destination = args.output_dir.resolve()
    repo = Path(__file__).resolve().parents[1]
    if destination == repo or repo in destination.parents:
        parser.error("Choose an output directory outside the repository; generated media must not be committed.")
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            parser.error(f"{command} is required")
    destination.mkdir(parents=True, exist_ok=True)
    source = destination / "synthetic-source.mp4"
    if not args.reuse_source or not source.exists():
        print("Creating original synthetic footage", flush=True)
        make_source(source)
    source_info = probe_source(source)
    assert (source_info["width"], source_info["height"]) == (WIDTH, HEIGHT)
    manifest = {
        "disclosure": "Original procedural synthetic gameplay; not real game capture. Authored captions, not automatic transcription. Real Hi-Lite export engine outputs.",
        "source": str(source), "source_info": source_info, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_captions": CAPTIONS, "source_cuts": CUTS, "edited_captions": remap_captions(CAPTIONS, CUTS),
        "outputs": [], "frames": [],
    }
    variants = [
        ("landscape-captions", "16:9", "#FFFFFF", "bottom", 76),
        ("portrait-captions", "9:16", "#FFFFFF", "bottom", 88),
        ("square-captions", "1:1", "#FFFFFF", "bottom", 72),
        ("styled-captions", "16:9", "#AAFFD5", "bottom", 84),
        ("clean-edited", "16:9", "#FFFFFF", "bottom", 76),
    ]
    for name, aspect, color, position, size in variants:
        print(f"Exporting {name}", flush=True)
        target = destination / f"{name}.mp4"
        settings = {"aspect_ratio": aspect, "burn_captions": name != "clean-edited", "crop": {"x": .5, "y": .5},
                    "caption_style": {"font_family": "DejaVu Sans", "font_size": size, "font_color": color,
                                      "outline_color": "#07121F", "outline_width": 4, "position": position}}
        result = export_source_ranges(source, CUTS, target, captions=CAPTIONS, settings=settings)
        actual = probe_source(target)
        assert (actual["width"], actual["height"]) == (result["width"], result["height"])
        assert abs(actual["duration"] - 7) < 1 / FPS + .03
        assert actual["has_audio"]
        for kind, subtitle in result["subtitles"].items():
            (destination / f"{name}.{kind}").write_text(subtitle, encoding="utf-8")
            assert "That drift saved the run." in subtitle
            assert "Now! Take the inside line." not in subtitle
        assert result["subtitles"]["srt"].index("That drift") < result["subtitles"]["srt"].index("Hold the boost")
        manifest["outputs"].append({"name": name, "settings": settings, "actual_probe": actual, "result": result})
        for time, shot in ((1.0, "drift"), (4.0, "boost"), (6.0, "finish")):
            frame = destination / f"{name}-{shot}.png"
            extract(target, time, frame)
            manifest["frames"].append({"path": str(frame), "output": target.name, "output_time": time,
                                       "source_time": {1.0: 7.0, 4.0: 2.0, 6.0: 10.0}[time]})
        (destination / "showcase-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"READY {name}: {actual['width']}x{actual['height']} / {actual['duration']} seconds", flush=True)
    for time in (2, 4, 7, 10):
        extract(source, time, destination / f"source-{time:02d}s.png")
    print(f"Completed: {destination / 'showcase-manifest.json'}", flush=True)


if __name__ == "__main__":
    main()
