"""Explainer stage 2: calm-voice narration, gemini chain -> edge fallback.

Mirrors core.gen_audio params: per-beat Gemini TTS (single voice, atempo
calibrated to target WPM, beat-level SRT) with fail-fast fallback to one
whole-narration edge-tts call (word-level SRT). Raises on bad length.
"""

import subprocess
import sys
import time
from pathlib import Path

from engine.align import align_or_fallback
from engine.ffmpeg_utils import ffprobe_duration, log, run_ffmpeg, warn
from engine.gemini_client import GeminiClient, parse_keys, pcm_to_wav
from engine.tts_cache import hash_key
from niches.base import Content, NarrationResult, NicheConfig

_EXPLAINER_STYLE = (
    "Read in a warm, clear, conversational explainer voice with natural pacing "
    "(~145 words per minute), genuine curiosity, and the tone of a knowledgeable "
    "friend explaining something fascinating."
)
_HOOK_PREFIX = (
    "For this opening hook only, deliver with sharp intrigue, curiosity, and "
    "immediate engagement, drawing the viewer in with natural dramatic presence. "
)
_CLOSER_ROLE = (" Transition into a warm, conversational, and direct closing tone.")
_MID_ROLE = (" Keep a confident momentum, making the facts clear and compelling.")


def _explainer_style(topic: str, text: str, beat_idx: int,
                     total_beats: int) -> str:
    """Calm explainer delivery with hook/closer role adaptation."""
    _ = (topic, text)
    if beat_idx == 0:
        return f"{_HOOK_PREFIX}{_EXPLAINER_STYLE}"
    if beat_idx == total_beats - 1:
        return f"{_EXPLAINER_STYLE}{_CLOSER_ROLE}"
    return f"{_EXPLAINER_STYLE}{_MID_ROLE}"


def _srt_ts(sec: float) -> str:
    """Seconds -> SRT timestamp (HH:MM:SS,mmm)."""
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, rem = divmod(rem, 60)
    s, ms = divmod(rem, 1)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(ms * 1000):03d}"


def _edge_narrate(text: str, mp3: Path, srt: Path, cfg: NicheConfig) -> None:
    """One edge-tts call: media + word-level SRT. Raises on failure."""
    # --flag=value form: values like -10Hz misparse as new flags otherwise.
    cmd = [sys.executable, "-m", "edge_tts",
           "--voice", getattr(cfg, "voice", "en-US-ChristopherNeural"),
           f"--rate={getattr(cfg, 'tts_rate', '-5%')}",
           f"--pitch={getattr(cfg, 'tts_pitch', '-10Hz')}",
           f"--volume={getattr(cfg, 'tts_volume', '+0%')}",
           f"--text={text}",
           "--write-media", str(mp3),
           "--write-subtitles", str(srt)]
    log("TTS edge fallback "
        f"voice={getattr(cfg, 'voice', '')} rate={getattr(cfg, 'tts_rate', '')}")
    proc = subprocess.run(cmd, check=True)
    _ = proc


def _gemini_beats(beats: list[dict], topic: str, cfg: NicheConfig,
                  workdir: Path) -> tuple[list[Path], list[float], str]:
    """Per-beat Gemini synth -> (segments, durations, voice). Fail-fast."""
    keys = parse_keys()
    if not keys:
        raise ValueError("GEMINI_API_KEY missing (TTS_ENGINE=gemini needs it)")
    voices = list(getattr(cfg, "gemini_voices", []) or ["Charon"])
    voice = voices[0]
    models = list(getattr(cfg, "gemini_tts_models", []) or [])
    tts_timeout = getattr(cfg, "gemini_tts_timeout", 90)
    pace = getattr(cfg, "gemini_tts_pace_sec", 15)
    target_wpm = getattr(cfg, "gemini_target_wpm", 145)
    client = GeminiClient(keys, tts_timeout=tts_timeout)
    segs, durs = [], []
    for i, b in enumerate(beats):
        if i > 0 and pace > 0:
            log(f"Gemini TTS pacing: waiting {pace}s between beats...")
            time.sleep(pace)
        style = _explainer_style(topic, b["text"], i, len(beats))
        key = hash_key(b["text"], voice, style)
        seg = workdir / f"tts_beat_{i}_{key[:12]}.mp3"
        if not (seg.exists() and seg.stat().st_size > 0):
            pcm, rate = client.synthesize_speech(
                b["text"], voice=voice, models=models, style=style)
            raw_pcm = workdir / f"tts_beat_{i}.pcm"
            raw_seg = workdir / f"tts_beat_{i}_raw.mp3"
            raw_pcm.write_bytes(pcm)
            run_ffmpeg(["ffmpeg", "-y", "-f", "s16le", "-ar", str(rate),
                        "-ac", "1", "-i", str(raw_pcm),
                        "-c:a", "libmp3lame", str(raw_seg)],
                       f"tts-beat-{i}-pcm")
            pcm_to_wav(pcm, rate, workdir / f"tts_beat_{i}.wav")
            words = len(b["text"].split())
            raw_dur = ffprobe_duration(raw_seg)
            factor = 1.0
            if target_wpm > 0 and raw_dur > 0 and words > 0:
                factor = max(0.85, min(1.35,
                                       target_wpm / ((words / raw_dur) * 60.0)))
            if abs(factor - 1.0) >= 0.03:
                run_ffmpeg(["ffmpeg", "-y", "-i", str(raw_seg),
                            "-filter:a", f"atempo={factor:.3f}",
                            "-c:a", "libmp3lame", str(seg)],
                           f"tts-beat-{i}-atempo")
            else:
                if seg.exists():
                    seg.unlink()
                raw_seg.rename(seg)
            for tmp in (raw_pcm, raw_seg):
                try:
                    if tmp.exists() and tmp != seg:
                        tmp.unlink()
                except OSError:
                    pass
        d = ffprobe_duration(seg)
        segs.append(seg)
        durs.append(d)
        log(f"TTS gemini beat {i + 1}/{len(beats)} voice={voice} {d:.1f}s")
    return segs, durs, voice


def narrate(content: Content, cfg: NicheConfig,
            workdir: Path) -> NarrationResult:
    """Synthesize content.narration_text -> NarrationResult (never partial)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    text = content.narration_text
    beats = list((content.extras or {}).get("beats") or [])
    topic = str((content.extras or {}).get("topic", ""))
    mp3 = workdir / "narration.mp3"
    srt = workdir / "narration.srt"
    engine = getattr(cfg, "tts_engine", "gemini")
    if engine not in ("gemini", "edge"):
        warn(f"Unknown TTS_ENGINE={engine!r}, using edge-tts.")
        engine = "edge"
    beat_durs: list[float] | None = None
    voice = ""
    if beats and engine == "gemini":
        try:
            segs, beat_durs, voice = _gemini_beats(beats, topic, cfg, workdir)
            lst = workdir / "tts_parts.txt"
            lst.write_text("".join(f"file '{p.resolve()}'\n" for p in segs))
            run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c:a", "libmp3lame", str(mp3)],
                       "tts-gemini-concat")
            cues, t = [], 0.0
            for d, b in zip(beat_durs, beats):
                cues.append((t, t + d, " ".join(b["text"].split())))
                t += d
            srt.write_text("".join(
                f"{n}\n{_srt_ts(s)} --> {_srt_ts(e)}\n{txt}\n\n"
                for n, (s, e, txt) in enumerate(cues, 1)), encoding="utf-8")
            engine = "gemini"
        except Exception as e:
            warn(f"Gemini TTS failed ({e}), falling back to edge-tts.")
            beat_durs, voice, engine = None, "", "edge"
    if beat_durs is None:
        _edge_narrate(text, mp3, srt, cfg)
        voice = getattr(cfg, "voice", "en-US-ChristopherNeural")
        engine = "edge"
    dur = ffprobe_duration(mp3)
    words = len(text.split())
    lo = getattr(cfg, "min_audio_sec", 10.0)
    hi = getattr(cfg, "max_audio_sec", 180.0)
    log(f"Narration {dur:.1f}s, {words} words")
    if not (lo <= dur <= hi):
        raise ValueError(f"Narration {dur:.1f}s outside [{lo:g}-{hi:g}s] "
                         f"for {words} words; refusing to render.")
    word_timings = align_or_fallback(mp3, text, total_dur=dur)
    return NarrationResult(audio=mp3, duration=dur, words=words, voice=voice,
                           style="explainer-calm",
                           extras={"srt": srt, "beat_durations": beat_durs,
                                   "engine": engine, "topic": topic,
                                   "words": word_timings})
