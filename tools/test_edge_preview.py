"""Edge preview: text -> edge voice -> captioned 1080x1920 mp4.

Renders through the SAME story pipeline as production: the SAME engine
edge client (synth), the SAME ASS/SRT writers (via build_visuals), the
SAME planner cuts/speedups (local clips only, total = audio + 0.4) and
the SAME final render function (niches.story.visuals.build_visuals).

Usage:
    python tools/test_edge_preview.py --card "Hook line here"
    python tools/test_edge_preview.py --text-file notes.txt --voice NAME \\
        --rate +30% --bg-dir assets/backgrounds --bgm music.mp3
"""
import argparse
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import edge_tts_client as edge_client
from engine.ffmpeg_utils import ffprobe_duration, log
from niches.base import Content, NarrationResult
from niches.story import visuals as story_visuals
from niches.story.config import StoryConfig
from niches.story.narration import _map_edge_words, card_end_time


def _default_text(n_words: int = 55) -> str:
    """Bundled sample narration head (~55 words, ~20s of speech)."""
    from niches.story import niche as story_niche

    toks = story_niche.sample_content().narration_text.split()
    return " ".join(toks[:n_words])


def _local_clip_count(bg_dir: str) -> int:
    """Usable video files in a BG dir (missing/empty -> 0)."""
    try:
        return sum(1 for p in Path(bg_dir).iterdir()
                   if p.is_file() and p.suffix.lower() in
                   (".mp4", ".mov", ".webm", ".mkv"))
    except OSError:
        return 0


def _ensure_local_clips(cfg, total: float, workdir: Path) -> None:
    """Point cfg.bg_dir at generated testsrc clips when it is empty.

    Keeps the tool offline and local-clips-only; real BG_DIR files win
    whenever present.
    """
    from engine.ffmpeg_utils import run_ffmpeg

    if _local_clip_count(cfg.bg_dir) > 0:
        log(f"Preview BG: using local clips from {cfg.bg_dir}.")
        return
    gen = workdir / "preview_bg"
    gen.mkdir(parents=True, exist_ok=True)
    # Enough DISTINCT clips that the strict no-repeat planner never hits
    # SHORTFALL on long previews: one clip per ~2.5s of video, capped.
    import math
    n = min(48, max(12, math.ceil(float(total) / 2.5)))
    palette = ("0x1a3a5c", "0x5c1a3a", "0x1a5c3a", "0x5c4a1a", "0x3a1a5c",
               "0x1a5c5c", "0x6b2d0f", "0x0f4c4c")
    for i in range(n):
        kind = i % 4
        if kind == 0:
            lavfi = (f"testsrc=size=1280x720:rate=30:duration=8:"
                     f"decimals={i % 3 + 1}")
        elif kind == 1:
            lavfi = "rgbtestsrc=size=1280x720:rate=30:duration=8"
        elif kind == 2:
            lavfi = (f"gradients=size=1280x720:rate=30:duration=8:"
                     f"speed={0.1 + 0.05 * (i % 5)}")
        else:
            lavfi = (f"color=c={palette[i % len(palette)]}:"
                     f"s=1280x720:r=30:d=8")
        run_ffmpeg(["ffmpeg", "-y",
                    "-f", "lavfi", "-i", lavfi,
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
                           "crop=1080:1920",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-preset", "veryfast", str(gen / f"bg{i:02d}.mp4")],
                   f"preview-bg-{i}")
    cfg.bg_dir = str(gen)
    log(f"Preview BG: generated {n} distinct clips (8.0s each).")


def _preview_wav(src_mp3: Path, out_wav: Path) -> Path:
    """mp3 -> wav convert (same shape as the niche edge path)."""
    from engine.ffmpeg_utils import run_ffmpeg

    run_ffmpeg(["ffmpeg", "-y", "-i", str(src_mp3),
                "-c:a", "pcm_s16le", str(out_wav)], "preview-wav")
    return out_wav


def main(argv: list[str] | None = None) -> None:
    """Synth + SAME-render preview. Prints stats; cmd on failure."""
    ap = argparse.ArgumentParser(
        description="Edge-tts captioned preview (same story render).")
    ap.add_argument("--text-file", default=None,
                    help="narration text file (default: bundled sample head)")
    ap.add_argument("--voice", default=None,
                    help="edge voice (default: first EDGE_VOICES pool entry)")
    ap.add_argument("--rate", default="+20%",
                    help="edge rate flag (default: +20%%)")
    ap.add_argument("--pitch", default="+0Hz",
                    help="edge pitch flag (default: +0Hz; e.g. +150Hz)")
    ap.add_argument("--bg-dir", default="assets/backgrounds",
                    help="local BG clips only, never Pexels (default: %(default)s)")
    ap.add_argument("--card", default=None,
                    help="card text; card overlay renders iff set")
    ap.add_argument("--bgm", default=None,
                    help="BGM bed file (default: random bgm pool, "
                         "else synthesized drone)")
    args = ap.parse_args(argv)

    try:
        text = (Path(args.text_file).read_text(encoding="utf-8").strip()
                if args.text_file else _default_text())
        if not text:
            raise ValueError("empty narration text")
        cfg = StoryConfig.from_env()
        voice = args.voice or (cfg.edge_voices[0] if cfg.edge_voices
                               else cfg.edge_voice)
        tmp = tempfile.TemporaryDirectory()
        workdir = Path(tmp.name)
        raw_mp3 = workdir / "preview_raw.mp3"
        log(f"Preview synth: voice={voice} rate={args.rate} pitch={args.pitch} "
            f"({len(text.split())} words)")
        events = edge_client.synth_sync(text, voice, args.rate, raw_mp3,
                                           pitch=args.pitch)
        words = _map_edge_words(events, text)
        wav = _preview_wav(raw_mp3, workdir / "preview.wav")
        dur = ffprobe_duration(wav)
        card_text = args.card or ""
        hook = text.split(".")[0].strip() + "."
        content = Content(
            title=card_text or "Edge preview",
            narration_text=text, description="", tags=["preview"],
            extras={"beats": [], "card_text": card_text,
                    "card_highlight_word": "",
                    "hook": hook if hook else text[:60],
                    "key_words": [],
                    "genre": "preview", "topic": "preview"})
        narration = NarrationResult(
            audio=wav, duration=dur, words=len(text.split()),
            voice=voice, style=f"edge-tts {args.rate} {args.pitch}",
            extras={"words": words,
                    "card_end": max(cfg.card_seconds,
                                    card_end_time(words, card_text, hook)
                                    + 0.15),
                    "key_words": [], "genre": "preview"})
        cfg.bg_dir = args.bg_dir
        if args.bgm:
            cfg.bed_path = args.bgm
        _ensure_local_clips(cfg, dur + 0.4, workdir)
        result = story_visuals.build_visuals(
            content, narration, cfg, workdir, no_card=args.card is None)
        out = Path("out") / "edge_preview.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(result.mp4.read_bytes())
        wps = len(text.split()) / dur if dur > 0 else 0.0
        print(f"Preview: {out} ({dur:.1f}s, {len(text.split())} words, "
              f"{wps:.2f} wps, {result.extras.get('segments')} segments, "
              f"card={'on' if args.card else 'off'})")
    except Exception as e:
        print(f"Preview FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
