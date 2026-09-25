"""Ranking stage 2: per-beat Gemini TTS with edge-tts fallback.

Mirrors core.gen_audio for the ranking niche: one voice per run, beat-level
SRT, atempo calibration to the target WPM, cache resume, duration guard.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

from engine import gemini_client as _gc
from engine import tts_cache
from engine.ffmpeg_utils import ffprobe_duration, log, run_ffmpeg, warn
from niches.base import Content, NarrationResult

from .config import RankingConfig

HOOK_PREFIX = (
    "For this opening hook only, deliver with sharp intrigue, curiosity, and "
    "immediate engagement, drawing the viewer in with natural dramatic presence. "
)
BODY_STYLE = (
    "Read in a confident countdown voice with steady, engaging pacing "
    "(~145 words per minute), clear grounded emphasis on each ranked reveal, "
    "and authentic storytelling tone."
)


def pick_voice(cfg: RankingConfig, title: str = "") -> tuple[str, str]:
    """Run voice from the pool; single voice = no alternation (default)."""
    pool = [v for v in (cfg.voices or (cfg.voice,)) if v] or ["Charon"]
    if len(pool) == 1:
        return pool[0], "single voice configured (no alternation)"
    idx = int(hashlib.sha1(title.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[idx], f"pool rotation by content hash ({len(pool)} voices)"


def beat_style(cfg: RankingConfig, beat_idx: int) -> str:
    """Delivery direction: explicit style wins, else hook prefix + body."""
    if (cfg.tts_style or "").strip():
        return cfg.tts_style.strip()
    if beat_idx == 0:
        return f"{HOOK_PREFIX}{BODY_STYLE}"
    return BODY_STYLE


def _srt_ts(sec: float) -> str:
    """Seconds -> SRT timestamp HH:MM:SS,mmm."""
    from math import floor

    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - floor(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _synth_beat(client: _gc.GeminiClient, cfg: RankingConfig, voice: str,
                text: str, beat_idx: int, total: int, workdir: Path) -> Path:
    """Synthesize one beat mp3 (cache-resumed); raises on failure."""
    key = tts_cache.hash_key(text, voice, beat_style(cfg, beat_idx), 1.0)
    seg = workdir / f"tts_beat_{beat_idx}_{key}.mp3"
    if seg.exists() and seg.stat().st_size > 0:
        log(f"TTS beat {beat_idx + 1}/{total} voice={voice} (cached)")
        return seg
    pcm, rate = client.synthesize_speech(
        text, voice=voice, models=list(cfg.tts_models),
        style=beat_style(cfg, beat_idx))
    raw_pcm = workdir / f"tts_beat_{beat_idx}.pcm"
    raw_seg = workdir / f"tts_beat_{beat_idx}_raw.mp3"
    raw_pcm.write_bytes(pcm)
    run_ffmpeg(["ffmpeg", "-y", "-f", "s16le", "-ar", str(rate), "-ac", "1",
                "-i", str(raw_pcm), "-c:a", "libmp3lame", str(raw_seg)],
               f"tts-beat-{beat_idx}-pcm")
    target_wpm = cfg.target_wpm
    if target_wpm > 0:
        words = len(text.split())
        raw_dur = ffprobe_duration(raw_seg)
        if raw_dur > 0 and words > 0:
            factor = max(0.85, min(1.35, target_wpm / ((words / raw_dur) * 60.0)))
            if abs(factor - 1.0) >= 0.03:
                run_ffmpeg(["ffmpeg", "-y", "-i", str(raw_seg),
                            "-filter:a", f"atempo={factor:.3f}",
                            "-c:a", "libmp3lame", str(seg)],
                           f"tts-beat-{beat_idx}-atempo")
                return seg
    if raw_seg.exists():
        if seg.exists():
            seg.unlink()
        raw_seg.rename(seg)
    return seg


def _assemble_gemini(client: _gc.GeminiClient, cfg: RankingConfig,
                     beats: list[dict], voice: str, mp3: Path, srt: Path,
                     workdir: Path) -> list[float]:
    """Concat per-beat mp3s + beat-level SRT. Returns per-beat durations."""
    parts, cues, durations = [], [], []
    t = 0.0
    for i, b in enumerate(beats):
        if i > 0 and cfg.pace_sec > 0:
            log(f"Gemini TTS pacing: waiting {cfg.pace_sec}s between beats...")
            time.sleep(cfg.pace_sec)
        seg = _synth_beat(client, cfg, voice, b["text"], i, len(beats), workdir)
        d = ffprobe_duration(seg)
        parts.append(seg)
        cues.append((t, t + d, " ".join(b["text"].split())))
        durations.append(d)
        t += d
        log(f"TTS gemini beat {i + 1}/{len(beats)} voice={voice} {d:.1f}s")
    lst = workdir / "tts_parts.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                "-c:a", "libmp3lame", str(mp3)], "tts-gemini-concat")
    srt.write_text("".join(
        f"{n}\n{_srt_ts(s)} --> {_srt_ts(e)}\n{txt}\n\n"
        for n, (s, e, txt) in enumerate(cues, 1)), encoding="utf-8")
    return durations


def _narrate_edge(cfg: RankingConfig, narration: str, mp3: Path, srt: Path) -> None:
    """Single edge-tts call: media + word-level SRT (values need = form)."""
    cmd = [sys.executable, "-m", "edge_tts",
           "--voice", cfg.edge_voice,
           f"--rate={cfg.tts_rate}",
           f"--pitch={cfg.tts_pitch}",
           f"--volume={cfg.tts_volume}",
           f"--text={narration}",
           "--write-media", str(mp3),
           "--write-subtitles", str(srt)]
    log(f"TTS voice={cfg.edge_voice} rate={cfg.tts_rate} pitch={cfg.tts_pitch}")
    from engine.ffmpeg_utils import retry
    retry("edge-tts", lambda: subprocess.run(cmd, check=True),
          tries=3, backoff=10)


def narrate(content: Content, cfg: RankingConfig,
            workdir: Path) -> NarrationResult:
    """Synthesize narration mp3 + SRT. Returns NarrationResult (never None)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mp3 = workdir / "narration.mp3"
    srt = workdir / "narration.srt"
    beats = [dict(b) for b in (content.extras.get("beats") or [])]
    words = len(content.narration_text.split())
    voice, why = pick_voice(cfg, content.title)
    log(f"TTS run voice={voice} ({why})")
    durations: list[float] | None = None
    engine_used = "edge"
    keys = list(cfg.gemini_keys) or _gc.parse_keys()
    if beats and keys:
        try:
            client = _gc.GeminiClient(keys, timeout=cfg.timeout,
                                      tts_timeout=cfg.tts_timeout,
                                      tries=cfg.tries, backoff=cfg.backoff)
            durations = _assemble_gemini(client, cfg, beats, voice, mp3, srt, workdir)
            engine_used = "gemini"
        except Exception as e:
            warn(f"Gemini TTS failed ({e}), falling back to edge-tts.")
    if engine_used == "edge":
        if beats and not keys:
            warn("GEMINI_API_KEY missing, falling back to edge-tts.")
        _narrate_edge(cfg, content.narration_text, mp3, srt)
    total = ffprobe_duration(mp3)
    log(f"Narration {total:.1f}s, {words} words")
    if not (cfg.min_audio_sec <= total <= cfg.max_audio_sec):
        raise ValueError(
            f"narration audio {total:.1f}s outside sane range "
            f"[{cfg.min_audio_sec:g}-{cfg.max_audio_sec:g}s] for {words} words; "
            "refusing to render a broken video.")
    style = (cfg.tts_style or "").strip() or "adaptive"
    return NarrationResult(
        audio=mp3, duration=total, words=words, voice=voice, style=style,
        extras={"srt": str(srt), "beat_durations": durations,
                "beats": beats, "topic": content.extras.get("topic", ""),
                "items": list(content.extras.get("items", [])),
                "title": content.title, "tts_engine": engine_used})
