"""Local demo mode: render 3 explainer Shorts without any API keys.

Usage: python demo.py

Renders complete example videos to out/demo_*.mp4 using:
- Fixture storyboard JSON from demo_fixtures/
- Silent/tone audio generated via FFmpeg (no TTS API)
- Programmatic visuals from explainer_visuals (no Pexels)
- The existing core.render() FFmpeg pipeline

Never publishes anything. Requires only ffmpeg + ffprobe on PATH.
"""
import json
import subprocess
import sys
from pathlib import Path

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).parent))

import core
import explainer_visuals

OUT = Path("out")
ASSETS = Path("assets")
OUT.mkdir(exist_ok=True)
ASSETS.mkdir(exist_ok=True)

FIXTURES = Path("demo_fixtures")

# Demo topics to render
DEMO_TOPICS = ["website", "rag", "microwave"]


def _generate_silent_audio(duration: float, mp3_path: Path):
    """Generate a quiet sine tone as demo narration audio via FFmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"sine=frequency=220:sample_rate=44100:duration={duration}",
        "-af", "volume=0.05",
        "-c:a", "libmp3lame", "-b:a", "64k",
        str(mp3_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _generate_srt(beats: list[dict], duration: float, srt_path: Path):
    """Generate SRT captions from beat texts, evenly distributed over duration."""
    total_words = sum(len(b["text"].split()) for b in beats) or 1
    entries = []
    t = 0.0
    for i, b in enumerate(beats):
        beat_dur = duration * len(b["text"].split()) / total_words
        start = t
        end = t + beat_dur
        entries.append(f"{i + 1}\n{_srt_ts(start)} --> {_srt_ts(end)}\n{b['text']}\n")
        t = end
    srt_path.write_text("\n".join(entries), encoding="utf-8")


def _srt_ts(sec: float) -> str:
    """Format seconds as SRT timestamp."""
    h, rem = divmod(max(0, sec), 3600)
    m, rem = divmod(rem, 60)
    s, ms = divmod(rem, 1)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(ms * 1000):03d}"


def _generate_bed(duration: float, bed_path: Path):
    """Generate a quiet ambient bed via FFmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"anoisesrc=color=brown:sample_rate=44100:duration={duration}:seed=42",
        "-af", "lowpass=f=200,volume=0.03",
        "-c:a", "libmp3lame",
        str(bed_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def render_demo(name: str, fixture_path: Path):
    """Render one demo video from a fixture JSON."""
    core.log(f"=== DEMO: {name} ===")
    meta = json.loads(fixture_path.read_text(encoding="utf-8"))
    beats = meta["beats"]

    # Calculate target duration: ~0.5s per word (120 WPM for demo)
    word_count = len(meta["narration"].split())
    duration = max(10.0, word_count * 0.5)  # ~0.5s per word
    core.log(f"Demo duration: {duration:.1f}s for {word_count} words")

    # 1. Generate demo audio
    mp3_path = ASSETS / f"demo_{name}.mp3"
    srt_path = ASSETS / f"demo_{name}.srt"
    bed_path = ASSETS / f"demo_{name}_bed.mp3"

    core.log("Generating demo audio...")
    _generate_silent_audio(duration, mp3_path)
    _generate_srt(beats, duration, srt_path)
    _generate_bed(duration, bed_path)

    # 2. Generate visuals using explainer_visuals
    core.log("Generating demo visuals...")
    clips = []
    total_words = sum(len(b["text"].split()) for b in beats) or 1

    for i, b in enumerate(beats):
        scene_path = explainer_visuals.build_scene(b, i, len(beats))
        if scene_path is None:
            # Fallback: render a text_card for stock_footage hints in demo
            fallback_beat = dict(b)
            fallback_beat["visual_hint"] = {
                "type": "text_card",
                "description": b.get("visual_hint", {}).get("description", b["text"][:200]),
                "label": b.get("query", "").replace("_", " ").title(),
            }
            scene_path = explainer_visuals.build_scene(fallback_beat, i, len(beats))
        if scene_path is None:
            scene_path = core.beat_card(i, b["text"])
        # 2 cuts per beat (each gets the same PNG — Ken Burns or static)
        clips.append((scene_path, None, i))
        clips.append((scene_path, None, i))

    # 3. Calculate per-beat durations
    durations = [duration * len(b["text"].split()) / total_words for b in beats]

    # 4. Render with FFmpeg via core.render()
    out_path = OUT / f"demo_{name}.mp4"
    core.log(f"Rendering demo video to {out_path}...")

    # Create a dummy niche for the board_filter (no overlay)
    class _DemoNiche:
        MODE = "explainer"
        BURN_SUBTITLES = False
        @staticmethod
        def board_filter(*a, **kw):
            return ""

    core.render(clips, mp3_path, bed_path, srt_path, durations,
                out_path, _DemoNiche, beats=beats, items=[], title=meta["title"])

    # Verify output
    size_kb = out_path.stat().st_size // 1024
    core.log(f"Demo {name}: {out_path} ({size_kb} KB)")

    # Check resolution
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name",
             "-of", "csv=p=0", str(out_path)],
            capture_output=True, text=True, check=True,
        )
        core.log(f"Demo {name} probe: {r.stdout.strip()}")
    except Exception as e:
        core.warn(f"Demo {name} probe failed: {e}")

    return out_path


def main():
    """Render all 3 demo topics."""
    core.log("Starting demo mode (no API keys needed)")
    results = []
    for name in DEMO_TOPICS:
        fixture_path = FIXTURES / f"{name}.json"
        if not fixture_path.exists():
            core.err(f"Fixture not found: {fixture_path}")
            continue
        try:
            out = render_demo(name, fixture_path)
            results.append((name, out, True))
        except Exception as e:
            core.err(f"Demo {name} FAILED: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, None, False))

    # Summary
    core.log("=== DEMO RESULTS ===")
    for name, out, ok in results:
        status = "OK" if ok else "FAILED"
        path_str = str(out) if out else "N/A"
        core.log(f"  {name}: {status} -> {path_str}")

    # Clean up demo intermediates
    for p in ASSETS.glob("demo_*"):
        try:
            p.unlink()
        except OSError:
            pass
    for p in ASSETS.glob("explainer_scene_*"):
        try:
            p.unlink()
        except OSError:
            pass
    core.cleanup_intermediates()

    failed = sum(1 for _, _, ok in results if not ok)
    if failed:
        core.err(f"{failed}/{len(results)} demos failed!")
        sys.exit(1)
    core.log(f"All {len(results)} demos rendered successfully!")


if __name__ == "__main__":
    main()
