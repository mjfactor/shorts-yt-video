"""Offline story demo: render a sample Short with zero API keys, never uploads.

Usage:
    python demo.py
    python demo.py --tts edge --words 60

Uses the story niche's sample_content() plus bundled / offline narration
and local sample backgrounds: real niche narrate() (STORY_TTS_ENGINE wired
in-process only; --tts auto = gemini with a key, else edge preview). Edge
needs network; when unreachable it falls back to a tone WAV with a clear
warning. Captions are always center word-pop ASS burned over the card +
BG track. Backgrounds: real files from BG_DIR when present, else generated
FFmpeg testsrc color clips (testsrc / rgbtestsrc / smptebars, 1080x1920).

Output: out/demo_story.mp4 (playable 1080x1920). Requires only ffmpeg +
ffprobe on PATH.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).parent))

import core
from niches.story import niche as story_niche

OUT = Path("out")      # mirrors core.OUT
ASSETS = Path("assets")  # mirrors core.ASSETS
OUT.mkdir(exist_ok=True)
ASSETS.mkdir(exist_ok=True)

NICHE_NAME = "story"

# Story demo targets the story niche's 100-120s window (narration_speed,
# TARGET_MIN/MAX in niches/story/config.py); tone length is clamped here.
STORY_MIN_SEC = 80.0
STORY_MAX_SEC = 85.0
STORY_WPS = 2.9  # measured speech rate default in niches/story/config.py

# Background testsrc palette (FFmpeg lavfi sources, scaled to canvas).
BG_SOURCES = ("testsrc", "rgbtestsrc", "smptebars")
BG_EXTS = (".mp4", ".mov", ".mkv", ".webm")


def _bg_dir() -> Path:
    """Background dir: STORY_BG_DIR wins, then BG_DIR, else default."""
    raw = (os.getenv("STORY_BG_DIR", "") or "").strip() \
        or (os.getenv("BG_DIR", "") or "").strip() \
        or "assets/backgrounds"
    return Path(raw)


def _local_backgrounds(bg_dir: Path) -> list[Path]:
    """Real BG files in bg_dir, sorted; empty when missing/empty."""
    try:
        files = sorted(p for p in bg_dir.iterdir()
                       if p.is_file() and p.suffix.lower() in BG_EXTS)
    except OSError:
        files = []
    return files


def _generate_tone_wav(duration: float, wav_path: Path):
    """Bundled/offline narration stand-in: clearly audible sine WAV via lavfi."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"sine=frequency=220:sample_rate=44100:duration={duration}",
        "-af", "volume=0.25",
        "-c:a", "pcm_s16le",
        str(wav_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _generate_bg_clip(source: str, duration: float, out_path: Path):
    """One 1080x1920 sample BG clip from an FFmpeg lavfi color source."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"{source}=size=1280x720:rate=30:duration={duration}",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
               "crop=1080:1920",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _trim_to_n_words(text: str, n: int | None) -> str:
    """First N words for quick previews (None/non-positive = full text)."""
    if not n or n <= 0:
        return text
    toks = text.split()
    if len(toks) <= n:
        return text
    cut = " ".join(toks[:n])
    # Prefer a sentence end inside the budget when one exists.
    best = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if best > 0:
        return cut[: best + 1]
    return cut


def _story_engine_choice(tts: str) -> str:
    """Resolve --tts: explicit wins; auto = gemini only with a key, else edge."""
    if tts in ("gemini", "edge"):
        return tts
    try:
        from engine.gemini_client import parse_keys
        has_key = bool(parse_keys())
    except Exception:
        has_key = bool((os.getenv("GEMINI_API_KEY", "") or "").strip())
    return "gemini" if has_key else "edge"


def _story_narrate_preview(niche, content, engine_name: str, workdir: Path):
    """Real niche narrate() with STORY_TTS_ENGINE wired in-process only.

    The env override is restored afterwards (no persistent env writes).
    Raises on failure so the caller can fall back to tone + proportional.
    """
    prev = os.environ.get("STORY_TTS_ENGINE")
    os.environ["STORY_TTS_ENGINE"] = engine_name
    try:
        from niches.story.config import StoryConfig
        cfg = StoryConfig.from_env()
        return niche.narrate(content, cfg, workdir)
    finally:
        if prev is None:
            os.environ.pop("STORY_TTS_ENGINE", None)
        else:
            os.environ["STORY_TTS_ENGINE"] = prev


def _render_story_captioned(bg_clips: list[Path], audio_path: Path,
                            words: list[dict], content, out_path: Path,
                            workdir: Path) -> None:
    """Concat BG track, overlay the story card, burn center word-pop ASS."""
    from engine.ass import write_ass
    from engine.ffmpeg_utils import subtitles_filter
    from engine.overlay_card import render_card
    from engine.srt import write_srt
    from niches.story.narration import card_end_time
    from niches.story.visuals import CARD_Y_OFFSET, STORY_CARD_THEME

    lst = workdir / "demo_bg_concat.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in bg_clips),
                   encoding="utf-8")
    bg_track = workdir / "demo_bg_track.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(bg_track)],
                   check=True, capture_output=True, text=True)
    extras = content.extras or {}
    card_text = extras.get("card_text", content.title)
    card_png = render_card(
        card_text, extras.get("card_highlight_word", ""),
        handle=os.getenv("STORY_CHANNEL_HANDLE",
                         os.getenv("CHANNEL_HANDLE", "@UnhingedStories")),
        avatar_path=os.getenv("STORY_AVATAR_PATH",
                              os.getenv("AVATAR_PATH", "assets/avatar.png")),
        out_png=workdir / "demo_card.png", theme=STORY_CARD_THEME)
    words = [dict(w) for w in (words or [])]
    card_dur = max(3.0, card_end_time(
        words, card_text, extras.get("hook", "")) + 0.4)
    # Suppress burned-in subtitles during card display: the card text
    # serves as the on-screen visual for the opening narration.
    caption_words = [w for w in words
                     if float(w.get("start", 0)) >= card_dur]
    # Style here must mirror the production word-pop (engine.ass
    # write_wordpop_ass) or a demo render cannot validate a caption change.
    # Do NOT hand-set an anchor: the \pos height comes from engine.ass
    # pop_tag() / CAPTION_POS_Y, shared by both writers.
    ass_path = write_ass(
        caption_words, workdir / "demo_captions.ass",
        style={"fontname": "Montserrat ExtraBold",
               "fontsize": 168,
               "primary_colour": "&H00FFFFFF",
               "outline": 9, "shadow": 3, "alignment": 5, "margin_v": 200,
               "play_res_x": 1080, "play_res_y": 1920},
        key_words=set(extras.get("key_words", [])),
        upper=False, gapless=True)
    write_srt(words, workdir / "demo_captions.srt", gapless=True)
    subs = subtitles_filter(ass_path.resolve())
    card_src = (str(card_png.resolve()).replace("\\", "/")
                .replace("'", "'\\''").replace(":", "\\:"))
    filt = (
        f"movie='{card_src}',format=rgba[card];"
        f"[0:v][card]overlay=(W-w)/2:(H-h)/2{CARD_Y_OFFSET:+d}:"
        f"enable='between(t,0,{card_dur:.2f})'[ov];"
        f"[ov]{subs},format=yuv420p[vout]")
    subprocess.run(["ffmpeg", "-y", "-i", str(bg_track), "-i", str(audio_path),
                    "-filter_complex", filt,
                    "-map", "[vout]", "-map", "1:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-preset", "veryfast",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest", str(out_path)],
                   check=True, capture_output=True, text=True)


# Regression guard: narration must stay audible (digital silence is -inf;
# the old volume=0.05 tone sat at -26 dB and read as "no audio").
AUDIBLE_FLOOR_DB = -40.0


def _ffprobe_streams(path: Path) -> list[dict]:
    """Stream table for a media file (ffprobe JSON). Raises on failure."""
    r = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "stream=index,codec_name,codec_type",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout).get("streams", [])


def _mean_volume_db(path: Path) -> float | None:
    """volumedetect mean_volume in dB (None when unparseable)."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path),
         "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    m = re.search(r"mean_volume:\s*(-?inf|-?\d+(?:\.\d+)?)\s*dB", r.stderr)
    if not m:
        return None
    return float(m.group(1))


def _assert_audible_audio(mp4_path: Path) -> tuple[int, float]:
    """Fail loudly when the render dropped narration or it is silent.

    Returns (audio_stream_count, mean_volume_db). Raises RuntimeError.
    """
    streams = _ffprobe_streams(mp4_path)
    n_audio = sum(1 for s in streams if s.get("codec_type") == "audio")
    if n_audio < 1:
        raise RuntimeError(
            f"{mp4_path} has no audio stream (render dropped the narration)")
    mean_db = _mean_volume_db(mp4_path)
    if mean_db is None:
        raise RuntimeError(
            f"{mp4_path}: volumedetect produced no mean_volume")
    if not (mean_db > AUDIBLE_FLOOR_DB):
        raise RuntimeError(
            f"{mp4_path}: mean_volume {mean_db:.1f} dB at/below silence "
            f"floor {AUDIBLE_FLOOR_DB:.0f} dB")
    core.log(f"Audio check: {n_audio} audio stream(s), "
             f"mean_volume {mean_db:.1f} dB")
    return n_audio, mean_db


def _probe(path: Path) -> str:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        return r.stdout.strip()
    except Exception as e:
        return f"probe failed: {e}"


def render_demo(tts: str = "auto", words: int | None = None) -> Path:
    """Render the story demo MP4. No API keys required, never uploads."""
    niche = story_niche
    content = niche.sample_content()
    core.log(f"=== DEMO: story ({getattr(niche, 'LABEL', '')}) ===")
    core.log(f"Sample title: {content.title}")

    # Story preview: trim + real niche narrate() (edge needs network;
    # unreachable -> tone WAV fallback with proportional timings).
    content.narration_text = _trim_to_n_words(
        content.narration_text, words)
    nwords = len(content.narration_text.split())
    engine_name = _story_engine_choice(tts)
    core.log(f"Story preview: engine={engine_name} ({nwords} words)")
    story_workdir = ASSETS / "demo_story_work"
    story_workdir.mkdir(parents=True, exist_ok=True)
    try:
        result = _story_narrate_preview(
            niche, content, engine_name, story_workdir)
        audio_path = result.audio
        duration = result.duration
        story_words = list(result.extras.get("words") or [])
        core.log(f"Story preview voice: {result.voice} "
                 f"({result.extras.get('tts_engine')}, "
                 f"{len(story_words)} timed words) [{result.extras.get('fit')}]")
    except Exception as e:
        core.warn(f"Story narrate failed ({e}); tone WAV fallback "
                  f"(edge-tts needs network).")
        floor = STORY_MIN_SEC if not words else 5.0
        duration = min(max(nwords / STORY_WPS, floor), STORY_MAX_SEC)
        audio_path = ASSETS / "demo_story.wav"
        core.log(f"Generating offline tone WAV ({duration:.1f}s, no API)...")
        _generate_tone_wav(duration, audio_path)
        from engine.align import proportional_fallback
        story_words = proportional_fallback(
            content.narration_text, duration)
        core.log(f"Fallback timings: {len(story_words)} proportional words")
    core.log(f"Demo duration: {duration:.1f}s for {nwords} words")
    if nwords < 400:
        # Honest scope note: the 126-word bundled sample sits below the
        # 100-120s target budget (~430-520 words), so this render checks
        # timing/plumbing — it is not a full-length proof. Never pad or
        # fake duration to reach the window.
        core.log("NOTE: bundled sample is below the 100-120s target "
                 "budget; demo is a short timing check, not full-length "
                 "proof.")

    srt_path = ASSETS / "demo_story.srt"
    from engine.srt import write_srt
    write_srt(story_words, srt_path, gapless=True)

    # Backgrounds: BG_DIR files when present, else testsrc color clips.
    bg_dir = _bg_dir()
    local = _local_backgrounds(bg_dir)
    bg_clips: list[Path] = []
    if local:
        core.log(f"Using {len(local)} local background(s) from {bg_dir}")
        bg_clips = local[:3]
    else:
        core.log(f"BG_DIR '{bg_dir}' empty/missing: generating "
                  f"{len(BG_SOURCES)} testsrc color clips (no API)...")
        seg = duration / len(BG_SOURCES)
        for i, src in enumerate(BG_SOURCES):
            clip = ASSETS / f"demo_story_bg{i}.mp4"
            _generate_bg_clip(src, seg, clip)
            bg_clips.append(clip)

    out_path = OUT / "demo_story.mp4"
    core.log(f"Rendering demo video to {out_path}...")
    # Card + center word-pop captions burned over the BG track.
    _render_story_captioned(bg_clips, audio_path, story_words,
                            content, out_path, story_workdir)

    n_audio, mean_db = _assert_audible_audio(out_path)
    size_kb = out_path.stat().st_size // 1024
    core.log(f"Demo story: {out_path} ({size_kb} KB, "
             f"{n_audio} audio stream(s), mean {mean_db:.1f} dB)")
    core.log(f"Demo story probe: {_probe(out_path)}")
    return out_path


def main(argv: list[str] | None = None) -> None:
    """Render the story demo."""
    ap = argparse.ArgumentParser(
        description="Offline story demo renderer (no API keys, never uploads).")
    ap.add_argument("--tts", default="auto",
                    choices=["auto", "gemini", "edge"],
                    help="story narration engine (default: auto = gemini "
                         "with a key, else edge preview)")
    ap.add_argument("--words", type=int, default=None,
                    help="trim sample narration to N words for quick "
                         "previews (default: full)")
    args = ap.parse_args(argv)

    core.log("Starting demo mode (no API keys needed, never uploads)")
    try:
        out = render_demo(tts=args.tts, words=args.words)
    except Exception as e:
        core.err(f"Demo story FAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Clean up demo intermediates (keep the final mp4 in out/).
        for p in ASSETS.glob("demo_story*"):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.suffix.lower() != ".mp4" or "_bg" in p.name:
                    p.unlink()
            except OSError:
                pass
        core.cleanup_intermediates()

    size_kb = out.stat().st_size // 1024
    print(f"Demo output: {out} ({size_kb} KB)")
    core.log("Demo rendered successfully!")


if __name__ == "__main__":
    main()
