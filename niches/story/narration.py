"""Story stage 2: chunked Gemini TTS with PCM cache and a duration-fit loop.

Flow: 60-90w sentence chunks (hash-cached PCM, resume missing) → gapless
concat (per-chunk silence trimmed, zero overlap) → atempo + silenceremove + loudnorm → fit loop
(100-120 proceed; 95-100/120-125 re-speed from cached PCM; else one
paced re-synth; else sentence trim) → estimated word timings on the
FINAL mix only (proportional fallback; no local transcription models).

Preview: STORY_TTS_ENGINE=edge runs one edge_tts library call (fast,
keyless) with service WordBoundary timings; no atempo/silenceremove on
the edge path so timings stay sample-true. No local models anywhere.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import re
import time
from pathlib import Path

from engine import edge_tts_client as edge_client
from engine import gemini_client as _gc
from engine import tts_cache
from engine.align import align_or_fallback
from engine.ffmpeg_utils import (ffprobe_duration, log, retry, run_ffmpeg,
                                 warn)
from engine.gemini_client import pcm_to_wav
from niches.base import Content, NarrationResult

from .config import DEFAULT_TTS_MODELS

from .config import StoryConfig

CHUNK_MIN_WORDS = 60
CHUNK_MAX_WORDS = 90
XFADE_SEC = 0.0  # gapless: chunk boundaries joined with zero overlap/pad
# Per-chunk gap trim: strips TTS leading/trailing silence before concat,
# then restores a short controlled pause (CHUNK_PAUSE_SEC, default 0.10s)
# so delivery stays fast but natural. Softer -50dB threshold removes
# digital padding without eating speech breath.
GAP_TRIM_DB = -50
GAP_TRIM_DUR = 0.02
DEFAULT_CHUNK_PAUSE_SEC = 0.10
MID_TARGET = 110.0  # midpoint of the 100-120s window

GENRE_STYLES = {
    "AITA": "Lively, candid storyteller energy, like a friend spilling dramatic news. Quick, expressive, a little sassy.",
    "revenge": "Mischievous, triumphant storyteller energy. Savor the setup, deliver the payback with a grin.",
    "workplace": "Animated, relatable coworker energy. Brisk office-drama pacing with punchy emphasis.",
    "relationship": "Warm, confiding storyteller energy. Intimate pace that leans into the emotional turns.",
    "scary": "Low, tense storyteller energy. Slow-burn dread, hushed emphasis, let the quiet moments breathe.",
    "never-again": "Wide-eyed, cautionary storyteller energy. Urgent pacing, learn-from-my-mistake emphasis.",
}
DEFAULT_STYLE = ("Energetic, engaging storyteller voice. Brisk natural pacing, "
                 "clear emphasis, authentic viral storytime delivery.")

# Stage-direction leak guard (mirrors content._REHOOK_LITERAL_RE): the
# narrator voices every word, so labels like "Rehook line:" must fail
# loud here if they ever slip past validation.
_STAGE_DIRECTION_RE = re.compile(r"re[\s-]*hook", re.IGNORECASE)


def _reject_stage_directions(text: str) -> None:
    """Raise when the narration carries a speakable stage direction."""
    hit = _STAGE_DIRECTION_RE.search(text or "")
    if hit:
        raise ValueError(
            f"stage direction {hit.group()!r} in narration "
            "(rehooks are metadata-only, never spoken)")

# Voice-name substrings for gender-aware pool filtering (matched
# case-insensitively against the configured pool; unknown names never
# match, so custom pools fall back to full-pool behavior).
_EDGE_MALE_HINTS = ("andrew", "guy")
_EDGE_FEMALE_HINTS = ("aria", "ava", "jenny")
_GEMINI_MALE_HINTS = ("charon", "puck", "fenrir")
_GEMINI_FEMALE_HINTS = ("kore", "aoede")


def _filter_by_gender(pool: list[str], gender: str,
                      male_hints: tuple[str, ...],
                      female_hints: tuple[str, ...]) -> list[str]:
    """Gender-matching subset of pool, or [] when unusable.

    Missing/unknown gender or no matching voice returns [] so callers
    fall back to the full-pool behavior.
    """
    g = (gender or "").strip().lower()
    hints = (male_hints if g == "male" else
             female_hints if g == "female" else ())
    if not hints:
        return []
    matched = [v for v in pool if any(h in str(v).lower() for h in hints)]
    return matched


def pick_voice(cfg: StoryConfig, seed_text: str = "",
               gender: str = "") -> tuple[str, str]:
    """Voice rotation across the pool; deterministic per story seed.

    When gender is "male"/"female", prefer the matching subset of the
    pool (male: Charon/Puck/Fenrir; female: Kore/Aoede); missing gender
    or no matching voice falls back to the full-pool rotation.
    """
    pool = [v for v in (cfg.tts_voices or ()) if v] or ["Kore"]
    subset = _filter_by_gender(pool, gender, _GEMINI_MALE_HINTS,
                               _GEMINI_FEMALE_HINTS)
    pool_label = "pool rotation"
    if subset:
        pool, pool_label = subset, f"gender-filtered {pool_label}"
    if len(pool) == 1:
        return pool[0], ("single voice configured" if pool_label == "pool rotation"
                         else f"{pool_label} (1 voice)")
    idx = int(hashlib.sha1(seed_text.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[idx], f"{pool_label} ({len(pool)} voices)"


def genre_style(genre: str) -> str:
    """Energetic delivery direction for the story's genre lane."""
    return GENRE_STYLES.get((genre or "").strip().lower(), DEFAULT_STYLE)


def chunk_text(text: str) -> list[str]:
    """Split narration into 60-90 word chunks on sentence boundaries."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()]
    chunks, cur, cur_n = [], [], 0
    for sent in sentences:
        n = len(sent.split())
        if cur and cur_n + n > CHUNK_MAX_WORDS:
            chunks.append(" ".join(cur))
            cur, cur_n = [], 0
        cur.append(sent)
        cur_n += n
        if cur_n >= CHUNK_MIN_WORDS:
            chunks.append(" ".join(cur))
            cur, cur_n = [], 0
    if cur:
        if chunks and len(cur[0].split()) < 25:
            chunks[-1] = f"{chunks[-1]} {cur[0]}"
        else:
            chunks.append(" ".join(cur))
    return [c for c in chunks if c]


def _pcm_paths(cache_dir: Path, key: str) -> tuple[Path, Path]:
    """(pcm slot, rate sidecar) for a chunk hash."""
    return tts_cache.path_for(cache_dir, key, ".pcm"), tts_cache.path_for(cache_dir, key, ".rate.json")


def _load_cached_pcm(cache_dir: Path, key: str) -> tuple[bytes, int] | None:
    """Cached (pcm, rate) or None."""
    pcm_p, rate_p = _pcm_paths(cache_dir, key)
    try:
        if pcm_p.exists() and rate_p.exists() and pcm_p.stat().st_size > 0:
            return pcm_p.read_bytes(), int(json.loads(rate_p.read_text())["rate"])
    except (OSError, ValueError, KeyError):
        pass
    return None


def _store_pcm(cache_dir: Path, key: str, pcm: bytes, rate: int) -> None:
    """Cache PCM bytes + rate sidecar."""
    tts_cache.store_bytes(pcm, cache_dir, key, ".pcm")
    tts_cache.path_for(cache_dir, key, ".rate.json").write_text(json.dumps({"rate": rate}))


def _synth_chunk(client: _gc.GeminiClient, cfg: StoryConfig, text: str,
                 voice: str, style: str, cache_dir: Path) -> tuple[bytes, int]:
    """Synthesize one chunk (cached); sequential callers add pacing/backoff."""
    key = tts_cache.hash_key(text, voice, style, 1.0)
    hit = _load_cached_pcm(cache_dir, key)
    if hit is not None:
        return hit
    models = list(getattr(cfg, "tts_models", None)
                    or ([cfg.tts_model] if getattr(cfg, "tts_model", "")
                        else [])) or list(DEFAULT_TTS_MODELS)
    models = [m for m in (str(m).strip() for m in models) if m] \
        or list(DEFAULT_TTS_MODELS)
    pcm, rate = retry("gemini-tts-chunk",
                      lambda: client.synthesize_speech(
                          text, voice=voice, models=models, style=style),
                      tries=cfg.tries, backoff=cfg.backoff)
    _store_pcm(cache_dir, key, pcm, rate)
    return pcm, rate


def _pcm_to_chunk_mp3(pcm: bytes, rate: int, speed: float,
                      out_mp3: Path) -> Path:
    """PCM → speed-adjusted chunk mp3."""
    tmp_pcm = out_mp3.with_suffix(".pcm")
    tmp_pcm.write_bytes(pcm)
    wav = pcm_to_wav(tmp_pcm.read_bytes(), rate, out_mp3.with_suffix(".wav"))
    run_ffmpeg(["ffmpeg", "-y", "-i", str(wav),
                "-af", f"atempo={max(0.5, float(speed)):.3f}",
                "-c:a", "libmp3lame", str(out_mp3)], "chunk-atempo")
    return out_mp3


def _trim_gap(src: Path, dst: Path) -> Path:
    """Strip leading/trailing silence from one chunk (gapless join).

    Same areverse idiom as _postprocess (exact on all builds); -50dB/20ms
    removes digital padding without eating speech. Re-encodes.
    """
    filt = (f"silenceremove=start_periods=1:start_duration={GAP_TRIM_DUR}:"
            f"start_threshold={GAP_TRIM_DB}dB,"
            f"areverse,silenceremove=start_periods=1:"
            f"start_duration={GAP_TRIM_DUR}:"
            f"start_threshold={GAP_TRIM_DB}dB,areverse")
    run_ffmpeg(["ffmpeg", "-y", "-i", str(src),
                "-af", filt, "-c:a", "libmp3lame", str(dst)],
               "chunk-gaptrim")
    return dst


def _pad_pause(src: Path, dst: Path, sec: float) -> Path:
    """Append a short controlled silence after a trimmed chunk.

    sec<=0 is a plain re-encode-free copy path (no pause added).
    """
    if sec <= 0:
        run_ffmpeg(["ffmpeg", "-y", "-i", str(src),
                    "-c:a", "libmp3lame", str(dst)], "chunk-nopad")
        return dst
    run_ffmpeg(["ffmpeg", "-y", "-i", str(src),
                "-af", f"apad=pad_dur={float(sec):.3f}",
                "-c:a", "libmp3lame", str(dst)], "chunk-pad")
    return dst


def _concat_crossfade(chunks: list[Path], out_mp3: Path,
                      pause_sec: float = 0.0) -> Path:
    """Join chunk mp3s: trim each, restore a short pause (all but last),
    then concat with zero overlap (XFADE_SEC>0 keeps the legacy xfade)."""
    trimmed: list[Path] = []
    for i, c in enumerate(chunks):
        t = c.parent / f"{c.stem}_notrim{c.suffix}"
        _trim_gap(c, t)
        if pause_sec > 0 and i < len(chunks) - 1:
            p = c.parent / f"{c.stem}_pause{c.suffix}"
            _pad_pause(t, p, pause_sec)
            t = p
        trimmed.append(t)
    if len(trimmed) == 1:
        run_ffmpeg(["ffmpeg", "-y", "-i", str(trimmed[0]),
                    "-c:a", "libmp3lame", str(out_mp3)], "chunk-copy")
        return out_mp3
    if XFADE_SEC > 0:
        inputs: list[str] = []
        for c in trimmed:
            inputs += ["-i", str(c)]
        filt, cur = "", "[0:a]"
        for i in range(1, len(trimmed)):
            nxt = f"[a{i}]" if i < len(trimmed) - 1 else "[aout]"
            filt += f"{cur}[{i}:a]acrossfade=d={XFADE_SEC:.2f}:curve=tri{nxt};"
            cur = nxt
        run_ffmpeg(["ffmpeg", "-y", *inputs, "-filter_complex", filt,
                    "-map", "[aout]", "-c:a", "libmp3lame", str(out_mp3)],
                   "chunk-xfade")
        return out_mp3
    # Exact concat: zero overlap; only the controlled pause added above.
    lst = out_mp3.parent / f"{out_mp3.stem}_gapless.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in trimmed),
                   encoding="utf-8")
    run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(lst), "-c", "copy", str(out_mp3)],
               "chunk-concat")
    return out_mp3


def _postprocess(raw_mp3: Path, out_mp3: Path) -> Path:
    """Leading/trailing silence trim + broadcast loudnorm on the full mix.
    No padding/pauses added; inter-chunk gaps already removed at concat.

    Trailing trim uses the areverse idiom: the stop_periods form
    mis-trims speech on some builds (ffmpeg 9.0.1 collapsed 26s of
    -21 dB speech to 5.2s), while the start-only form is exact.
    """
    trim = ("silenceremove=start_periods=1:start_duration=0.1:"
            "start_threshold=-60dB,"
            "areverse,silenceremove=start_periods=1:start_duration=0.25:"
            "start_threshold=-60dB,areverse")
    run_ffmpeg(["ffmpeg", "-y", "-i", str(raw_mp3),
                "-af", f"{trim},loudnorm=I=-16:TP=-1.5:LRA=11",
                "-c:a", "libmp3lame", str(out_mp3)], "narr-post")
    return out_mp3


def _fit_speed(duration: float) -> float:
    """Re-speed factor toward 110s, clamped to the 1.05-1.25 magnitude band."""
    factor = duration / MID_TARGET
    if factor >= 1.0:
        return min(1.25, max(1.05, factor))
    return max(1 / 1.25, min(1 / 1.05, factor))


def _trim_to_sentence(audio: Path, words: list[dict], max_seconds: float,
                      out_mp3: Path) -> Path:
    """Cut at the last sentence end before MAX; hard cut as fallback."""
    end = 0.0
    for w in words:
        if float(w.get("end", 0.0)) > max_seconds:
            break
        if str(w.get("word", "")).rstrip().endswith((".", "!", "?", "…")):
            end = float(w.get("end", 0.0))
    end = end or max_seconds
    run_ffmpeg(["ffmpeg", "-y", "-i", str(audio),
                "-t", f"{end:.3f}", "-c:a", "libmp3lame", str(out_mp3)],
               "narr-trim")
    return out_mp3


def _edge_voice_for(cfg: StoryConfig, seed_text: str = "",
                    gender: str = "") -> tuple[str, str]:
    """One edge voice per video, kept for the whole video.

    When gender is "male"/"female", pick from the matching subset of
    the configured pool (male: Andrew/Guy; female: Jenny/Ava, matched
    by voice-name substring). Missing gender or no matching voice
    falls back to the full-pool pick.
    """
    _ = seed_text
    pool = [v for v in (cfg.edge_voices or ()) if v]
    if not pool and (cfg.edge_voice or "").strip():
        pool = [cfg.edge_voice.strip()]
    pool = pool or ["en-US-JennyNeural"]
    subset = _filter_by_gender(pool, gender, _EDGE_MALE_HINTS,
                               _EDGE_FEMALE_HINTS)
    if subset:
        voice = random.choice(subset)
        why = (f"edge gender-filtered pick ({len(subset)} voices)"
               if len(subset) > 1 else "single gender-filtered edge voice")
        return voice, why
    voice = random.choice(pool)
    why = (f"edge pool pick ({len(pool)} voices)" if len(pool) > 1
           else "single edge voice")
    return voice, why


def _edge_rate_for(cfg: StoryConfig, voice: str) -> str:
    """Effective edge SSML rate for a picked voice.

    Female voices (Jenny/Ava name match) run a slower natural cadence, so
    they get cfg.edge_rate_female (default +28%); every other voice keeps
    the global cfg.edge_rate (default +20%). Unknown/custom voices never
    match, so they stay on the global path. The word budget stays global
    (STORY_LENGTH_OVERSHOOT absorbs the residual spread), and the shared
    +0..+40% refit clamp in _edge_refit_rate applies unchanged.
    """
    name = str(voice or "").lower()
    if any(h in name for h in _EDGE_FEMALE_HINTS):
        return getattr(cfg, "edge_rate_female", "+28%") or "+28%"
    return cfg.edge_rate


def _parse_edge_rate(rate: str) -> float:
    """'+20%' -> 0.20 (edge relative-rate flag)."""
    m = re.match(r"\s*([+-]?)(\d+(?:\.\d+)?)\s*%?\s*$", rate or "")
    if not m:
        return 0.0
    val = float(m.group(2)) / 100.0
    return -val if m.group(1) == "-" else val


def _edge_refit_rate(rate: str, dur: float) -> str:
    """Rate re-aimed at the 110s midpoint: (1+r)*dur/110-1."""
    new = (1.0 + _parse_edge_rate(rate)) * dur / MID_TARGET - 1.0
    new = min(0.40, max(0.0, new))  # clamp +0..+40%
    return f"+{int(round(new * 100))}%"


def _split_sentences(text: str) -> list[str]:
    """Sentence chunks (edge chunk-fallback only)."""
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text.strip())
            if s.strip()]


def _map_edge_words(events: list[dict], text: str) -> list[dict]:
    """Library events -> per-word [{word, start, end}] timings.

    Counts match: narration words in order on event times (keeps the
    caller's spelling/punctuation). Otherwise: edge event texts + log.
    """
    want = text.split()
    if events and len(events) == len(want):
        return [{"word": w,
                 "start": round(float(ev.get("start", 0.0)), 3),
                 "end": round(float(ev.get("end", 0.0)), 3)}
                for ev, w in zip(events, want)]
    log(f"Story edge timings: event count ({len(events)}) != words "
        f"({len(want)}); using edge event texts.")
    return [{"word": str(ev.get("word", "")),
             "start": round(float(ev.get("start", 0.0)), 3),
             "end": round(float(ev.get("end", 0.0)), 3)} for ev in events]


def _check_edge_sanity(words: list[dict], dur: float) -> str | None:
    """Insanity reason for edge timings (None when sane)."""
    prev = 0.0
    for w in words:
        try:
            s, e = float(w.get("start", 0.0)), float(w.get("end", 0.0))
        except (TypeError, ValueError):
            return f"non-numeric timing on {w.get('word', '?')!r}"
        if e < s:
            return f"end<start on {w.get('word', '?')!r} ({s:.3f}>{e:.3f})"
        if s < prev - 1e-6:
            return f"non-decreasing violation at {w.get('word', '?')!r}"
        prev = s
    if words and float(words[-1].get("end", 0.0)) > dur + 0.3:
        return (f"last end {words[-1].get('end')} beyond "
                f"duration {dur:.1f}s+0.3")
    return None


def _concat_edge_parts(parts: list[Path], out_mp3: Path) -> Path:
    """Gapless concat for the edge chunk fallback (parts pre-trimmed)."""
    lst = out_mp3.parent / f"{out_mp3.stem}_parts.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts),
                   encoding="utf-8")
    run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(lst), "-c", "copy", str(out_mp3)], "edge-concat")
    return out_mp3


def _edge_chunked(text: str, voice: str, rate: str, cfg: StoryConfig,
                  workdir: Path) -> tuple[Path, list[dict], int]:
    """Sentence-chunk fallback (whole-script call failed): same voice+rate
    every chunk; timings offset by ffprobe'd chunk durations. Raises when
    any chunk fails."""
    sents = _split_sentences(text)
    if len(sents) < 2:
        raise ValueError("edge chunk fallback needs 2+ sentences")
    parts: list[Path] = []
    words: list[dict] = []
    offset = 0.0
    for i, sent in enumerate(sents):
        seg = workdir / f"narr_edge_c{i:02d}.mp3"
        chunk_events = edge_client.synth_sync(
            sent,
            voice,
            rate,
            seg,
            attempts=2,
            waits=(2.0, 4.0),
            pitch=getattr(cfg, "edge_pitch", "+0Hz"),
        )
        # Trim per-sentence silence BEFORE measuring the offset, then
        # restore a short controlled pause (all but last sentence) so the
        # pace stays fast but natural. Offsets use final durations.
        gap_trim = getattr(cfg, "trim_chunk_silence", True)
        pause_sec = float(getattr(cfg, "chunk_pause_sec",
                                  DEFAULT_CHUNK_PAUSE_SEC) or 0.0)
        if gap_trim:
            tseg = workdir / f"narr_edge_c{i:02d}_notrim.mp3"
            _trim_gap(seg, tseg)
            tseg.replace(seg)
            if pause_sec > 0 and i < len(sents) - 1:
                pseg = workdir / f"narr_edge_c{i:02d}_pause.mp3"
                _pad_pause(seg, pseg, pause_sec)
                pseg.replace(seg)
        dur = ffprobe_duration(seg)
        if dur <= 0:
            raise ValueError(f"edge chunk {i} has no duration")
        for w in _map_edge_words(chunk_events, sent):
            words.append({"word": w["word"],
                          "start": round(w["start"] + offset, 3),
                          "end": round(w["end"] + offset, 3)})
        offset += dur
        parts.append(seg)
        log(f"Story edge chunk {i + 1}/{len(sents)} "
            f"({dur:.1f}s, offset {offset:.1f}s)")
    mp3 = _concat_edge_parts(parts, workdir / "narr_edge_chunks.mp3")
    return mp3, words, len(parts)


def _edge_single_surgery(raw_mp3: Path, words: list[dict], text: str,
                         cfg: StoryConfig, workdir: Path,
                         tag: str = "seg") -> tuple[Path, list[dict]]:
    """Sentence-segment surgery on a whole-script single-call mp3.

    Splits the mp3 at sentence boundaries from WordBoundary timings
    aligned with _split_sentences, _trim_gap each segment, restores
    cfg.chunk_pause_sec on all but last, then gapless-concats and
    re-offsets timings from ffprobe'd segment durations (like
    _edge_chunked, plus a monotonic clamp so WordBoundary end-overshoot
    never overlaps). Offline ffmpeg only — the one network call already
    happened. Returns (mp3, words); bails out unchanged when trimming
    is disabled or words cannot align to sentences.
    """
    sents = _split_sentences(text)
    if len(sents) < 2 or not words:
        return raw_mp3, words
    if not getattr(cfg, "trim_chunk_silence", True):
        return raw_mp3, words
    counts = [len(s.split()) for s in sents]
    if sum(counts) != len(words):
        warn(f"Story edge surgery: {len(words)} timed words vs "
             f"{sum(counts)} sentence words; keeping single-call audio.")
        return raw_mp3, words
    raw_dur = ffprobe_duration(raw_mp3)
    if raw_dur <= 0:
        raise ValueError("edge single-call mp3 has no duration")
    pause_sec = float(getattr(cfg, "chunk_pause_sec",
                              DEFAULT_CHUNK_PAUSE_SEC) or 0.0)
    parts: list[Path] = []
    new_words: list[dict] = []
    offset, idx, prev_end, pos = 0.0, 0, 0.0, 0.0
    for i, cnt in enumerate(counts):
        seg_words = words[idx:idx + cnt]
        idx += cnt
        s0 = max(0.0, float(seg_words[0].get("start", 0.0)) - 0.05)
        s1 = min(raw_dur, float(seg_words[-1].get("end", 0.0)) + 0.05)
        s0 = max(s0, pos)  # clamp: end-overshoot never overlaps
        if s1 <= s0:
            s1 = min(raw_dur, s0 + 0.05)
        seg = workdir / f"narr_edge_{tag}{i:02d}.mp3"
        run_ffmpeg(["ffmpeg", "-y", "-ss", f"{s0:.3f}", "-i", str(raw_mp3),
                    "-t", f"{max(0.05, s1 - s0):.3f}",
                    "-c:a", "libmp3lame", str(seg)], "edge-seg-slice")
        tseg = workdir / f"narr_edge_{tag}{i:02d}_notrim.mp3"
        _trim_gap(seg, tseg)
        tseg.replace(seg)
        if pause_sec > 0 and i < len(counts) - 1:
            pseg = workdir / f"narr_edge_{tag}{i:02d}_pause.mp3"
            _pad_pause(seg, pseg, pause_sec)
            pseg.replace(seg)
        dur = ffprobe_duration(seg)
        if dur <= 0:
            raise ValueError(f"edge surgery segment {i} has no duration")
        for w in seg_words:
            # Re-base to the slice start (single-call timeline is absolute;
            # chunked-path events are per-chunk relative), then offset.
            s = round(float(w.get("start", 0.0)) - s0 + offset, 3)
            e = round(float(w.get("end", 0.0)) - s0 + offset, 3)
            s = max(s, prev_end)  # monotonic clamp
            e = max(e, s)
            new_words.append({"word": str(w.get("word", "")),
                              "start": s, "end": e})
            prev_end = e
        offset += dur
        pos = s1
        parts.append(seg)
    mp3 = _concat_edge_parts(parts, workdir / f"narr_edge_{tag}.mp3")
    log(f"Story edge surgery: {len(parts)} segments, "
        f"{raw_dur:.1f}s -> {ffprobe_duration(mp3):.1f}s.")
    return mp3, new_words


def _synth_single_seg(text: str, voice: str, rate: str, cfg: StoryConfig,
                      workdir: Path, tag: str) -> tuple[Path, list[dict]]:
    """One edge synth_sync + offline sentence-segment surgery."""
    seg_mp3 = workdir / f"narr_edge_{tag}_raw.mp3"
    events = edge_client.synth_sync(
        text, voice, rate, seg_mp3,
        pitch=getattr(cfg, "edge_pitch", "+0Hz"))
    words = _map_edge_words(events, text)
    return _edge_single_surgery(seg_mp3, words, text, cfg, workdir, tag)


def _edge_wav(src_mp3: Path, out_wav: Path) -> Path:
    """mp3 -> wav via ffmpeg. Plain convert only: no volume gain (edge
    speech already peaks ~-4 dB) and no atempo/silenceremove/loudnorm,
    so timings stay sample-true to the synth."""
    run_ffmpeg(["ffmpeg", "-y", "-i", str(src_mp3),
                "-c:a", "pcm_s16le", str(out_wav)], "edge-wav")
    return out_wav


def _gemini_fallback(content: Content, cfg: StoryConfig, workdir: Path,
                     edge_error: Exception) -> NarrationResult:
    """Whole-script gemini TTS after edge exhaustion (opt-in only).

    Only reached when TTS_PROVIDER_ORDER enables gemini (default is
    edge-only, OFF). Never mixes: the full existing gemini path runs on
    the whole script; its proportional timings span the ffprobe duration.
    """
    log(f"Story edge exhausted ({edge_error}); falling back to whole-script "
        f"gemini TTS (never mixed).")
    gemini_cfg = dataclasses.replace(cfg, tts_engine="gemini")
    result = narrate(content, gemini_cfg, workdir)
    extras = dict(result.extras or {})
    extras["tts_provider"] = "gemini"
    log("Story gemini fallback: estimated timings, captions may drift by "
        "a few tenths of a second.")
    return dataclasses.replace(result, extras=extras)


def _narrate_edge(content: Content, cfg: StoryConfig,
                  workdir: Path) -> NarrationResult:
    """Keyless edge preview via the edge_tts library (unofficial endpoint,
    no SLA — preview path only; no commercial-rights claim on voices).

    Whole-script single call; sentence chunks only on observed failure.
    The single-call mp3 gets offline sentence-segment surgery (trim +
    controlled pause, timings re-offset) so baked TTS pauses never air;
    no atempo/loudnorm here so levels stay intact.
    When TTS_PROVIDER_ORDER includes gemini (default edge,gemini),
    edge exhaustion falls back to whole-script gemini TTS.
    """
    text = content.narration_text.strip()
    genre = content.extras.get("genre", "")
    text, card_prepended = _ensure_card_first(
        text, content.extras.get("card_text", ""))
    if card_prepended:
        log("Story edge narration: prepended card line (legacy content).")
    _reject_stage_directions(text)
    order = (getattr(cfg, "tts_provider_order", "edge,gemini") or "edge,gemini")
    log(f"Story edge provider order: {order}")
    gender = str(content.extras.get("narrator_gender", "") or "")
    voice, voice_why = _edge_voice_for(cfg, f"{content.title}|{genre}", gender)
    rate = _edge_rate_for(cfg, voice)
    is_female_voice = any(h in str(voice).lower() for h in _EDGE_FEMALE_HINTS)
    rate_note = "female rate" if is_female_voice else "global rate"
    log(f"Story TTS edge voice={voice} ({voice_why}) rate={rate} ({rate_note}) "
        f"genre={genre or '?'} narrator_gender={gender or '?'}")
    raw_mp3 = workdir / "narr_edge_raw.mp3"
    edge_error: Exception | None = None
    words: list[dict] = []
    timing_note = ""
    n_chunks = 1
    try:
        raw_mp3, words = _synth_single_seg(
            text, voice, rate, cfg, workdir, "raw")
        timing_note = f"edge boundaries ({len(words)} words)"
        n_chunks = 1
    except Exception as e:
        edge_error = e
        warn(f"Story edge single call failed ({e}); sentence-chunk fallback...")
        try:
            raw_mp3, words, n_chunks = _edge_chunked(
                text, voice, rate, cfg, workdir)
            timing_note = f"edge chunks ({len(words)} words)"
            edge_error = None
        except Exception as e2:
            edge_error = e2
    if edge_error is not None:
        providers = [p.strip() for p in order.split(",") if p.strip()]
        if "gemini" in providers:
            return _gemini_fallback(content, cfg, workdir, edge_error)
        raise edge_error
    wav = _edge_wav(raw_mp3, workdir / "narr_edge.wav")
    dur = ffprobe_duration(wav)
    mp3: Path = wav
    fit_note = f"edge first pass {dur:.1f}s @ {rate} ({timing_note})"
    fit_action: str | None = None
    if cfg.target_min <= dur <= cfg.target_max:
        fit_note += f" (in {cfg.target_min:g}-{cfg.target_max:g} window)"
    elif cfg.target_min - 5 <= dur <= cfg.target_max + 5:
        # Slight miss: max 2 re-synths with recomputed rate.
        for attempt in (1, 2):
            rate = _edge_refit_rate(rate, dur)
            log(f"Story edge fit: re-synth {attempt}/2 @ {rate}...")
            raw_mp3, words = _synth_single_seg(
                text, voice, rate, cfg, workdir, f"refit{attempt}")
            n_chunks = 1
            wav = _edge_wav(
                raw_mp3, workdir / f"narr_edge_refit{attempt}.wav")
            dur = ffprobe_duration(wav)
            mp3 = wav
            fit_note += f" -> re-synth {dur:.1f}s @ {rate}"
            if cfg.target_min <= dur <= cfg.target_max:
                break
    else:
        # Far-off: flag for the Gemini-text step. No trivially-callable
        # lengthen/tighten helper exists in content.py (full draft flow
        # only), so record the action string instead of calling it.
        direction = "lengthen" if dur < cfg.target_min else "tighten"
        fit_action = (f"gemini-text-{direction}: {len(text.split())} words "
                      f"gave {dur:.1f}s, need {cfg.target_min:g}-"
                      f"{cfg.target_max:g}s")
        warn(f"Story edge far off window ({dur:.1f}s); {fit_action}.")
        fit_note += f" ({fit_action})"
    if dur > cfg.max_seconds:
        if cfg.max_seconds < cfg.target_min:
            raise ValueError(
                f"STORY_MAX_SECONDS={cfg.max_seconds:g} trims below "
                f"TARGET_MIN={cfg.target_min:g}; narration {dur:.1f}s for a "
                f"{cfg.target_min:g}-{cfg.target_max:g}s target — raise "
                "STORY_MAX_SECONDS (>= target max).")
        # Sentence trim past MAX (hard cut fallback inside).
        log(f"Story edge fit: trimming {dur:.1f}s to sentence before "
            f"{cfg.max_seconds:g}s...")
        mp3 = _trim_to_sentence(mp3, words, cfg.max_seconds,
                                workdir / "narr_edge_trim.mp3")
        dur = ffprobe_duration(mp3)
        words = [w for w in words if float(w.get("start", 0.0)) < dur]
        fit_note += f" -> trimmed {dur:.1f}s"
        warn(f"Story edge over MAX_SECONDS ({cfg.max_seconds:g}s); trimmed.")
    reason = _check_edge_sanity(words, dur)
    if reason is not None:
        warn(f"Story edge timing sanity: {reason}; keeping timings.")
        fit_note += f" (timing sanity: {reason})"
    card_end = max(cfg.card_seconds,
                   card_end_time(words, content.extras.get("card_text", ""),
                                 content.extras.get("hook", "")) + 0.15)
    wps_measured = len(text.split()) / dur if dur > 0 else 0.0
    if dur > 0 and wps_measured > 6.0:
        # Fail loud on truncated/sped-up audio (a stop-trim bug once
        # shipped 126 words in 5.2s = 24 wps); never air it silently.
        raise ValueError(
            f"Story edge narration implausible: {len(text.split())} words "
            f"in {dur:.1f}s ({wps_measured:.1f} wps) — expected 2.5-5 wps.")
    log(f"Story edge narration final: {dur:.1f}s, {len(words)} timed words "
        f"({wps_measured:.2f} wps), card_end={card_end:.1f}s [{fit_note}]")
    return NarrationResult(
        audio=mp3, duration=dur, words=len(text.split()),
        voice=voice, style=f"edge-tts {rate}",
        extras={"words": words, "card_end": card_end,
                "chunks": n_chunks, "tts_engine": "edge",
                "wps_measured": round(wps_measured, 3), "fit": fit_note,
                "fit_action": fit_action,
                "card_text": content.extras.get("card_text", ""),
                "card_highlight_word": content.extras.get("card_highlight_word", ""),
                "key_words": list(content.extras.get("key_words", [])),
                "genre": genre, "title": content.title})


def _norm_card_words(s: str) -> list[str]:
    """Lowercased words, punctuation stripped, for card-line matching."""
    return [w.strip(".,!?\"'…").lower() for w in s.split()
            if w.strip(".,!?\"'…")]


def _starts_with_card(text: str, card_text: str) -> bool:
    """First sentence of text matches card_text (case-insensitive,
    trailing punctuation ignored). Empty card_text always matches."""
    if not _norm_card_words(card_text):
        return True
    first = re.split(r"[.!?…]+", text.strip(), maxsplit=1)[0]
    return _norm_card_words(first) == _norm_card_words(card_text)


def _ensure_card_first(text: str, card_text: str) -> tuple[str, bool]:
    """Prepend '<card>. ' when the narration doesn't open with the card
    line (legacy content). Idempotent: already-first text is unchanged."""
    if _starts_with_card(text, card_text):
        return text, False
    card = card_text.strip()
    if card and card[-1] not in ".!?…":
        card += "."
    return f"{card} {text.strip()}", True


def card_end_time(words: list[dict], card_text: str, hook: str) -> float:
    """End time of the spoken card line.

    Anchored: only the FIRST N words may match (card_text → hook →
    first-words fallback). The narrator speaks the card first, so any
    later repeat must not move the fade.
    """
    seq = [str(w.get("word", "")).lower().strip(".,!?\"'") for w in words]

    def _head_end(phrase: str) -> float | None:
        toks = [w.lower().strip(".,!?\"'") for w in phrase.split()
                if w.strip()]
        if not toks or len(seq) < len(toks):
            return None
        if seq[:len(toks)] == toks:
            return float(words[len(toks) - 1].get("end", 0.0))
        return None

    for phrase in (card_text, hook):
        end = _head_end(phrase or "")
        if end:
            return end
    if words:
        return float(words[min(7, len(words) - 1)].get("end", 2.0))
    return 2.0


def narrate(content: Content, cfg: StoryConfig,
            workdir: Path) -> NarrationResult:
    """Synthesize the story mix. Extras carry words, card_end, fit notes."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    cache_dir = workdir / "tts_cache"
    cache_dir.mkdir(exist_ok=True)
    text = content.narration_text.strip()
    genre = content.extras.get("genre", "")
    text, card_prepended = _ensure_card_first(
        text, content.extras.get("card_text", ""))
    if card_prepended:
        log("Story narration: prepended card line (legacy content did not "
            "open with it).")
        content = dataclasses.replace(content, narration_text=text)
    _reject_stage_directions(text)
    engine = (getattr(cfg, "tts_engine", "edge") or "edge").strip().lower()
    if engine not in ("gemini", "edge"):
        warn(f"Unknown STORY_TTS_ENGINE={engine!r}; using edge.")
        engine = "edge"
    if engine == "edge":
        return _narrate_edge(content, cfg, workdir)
    voice, voice_why = pick_voice(cfg, f"{content.title}|{genre}",
                                 str(content.extras.get("narrator_gender", "") or ""))
    style = genre_style(genre)
    log(f"Story TTS voice={voice} ({voice_why}) genre={genre or '?'}")
    client = _gc.GeminiClient(list(cfg.gemini_keys), timeout=cfg.timeout,
                              tts_timeout=cfg.tts_timeout,
                              tries=cfg.tries, backoff=cfg.backoff)
    chunks = chunk_text(text)
    log(f"Story TTS: {len(chunks)} chunks, {len(text.split())} words")
    pause = (float(getattr(cfg, "chunk_pause_sec", 0.10) or 0.0)
             if getattr(cfg, "trim_chunk_silence", True) else 0.0)

    def _render_all(cur_style: str, tag: str) -> Path:
        """Synthesize (resume cached) + trim/pause concat + postprocess."""
        chunk_mp3s = []
        for i, ch in enumerate(chunks):
            if i > 0:
                time.sleep(2)
            key = tts_cache.hash_key(ch, voice, cur_style, 1.0)
            hit = _load_cached_pcm(cache_dir, key)
            if hit is None:
                log(f"Story TTS chunk {i + 1}/{len(chunks)} ({len(ch.split())}w)...")
                pcm, rate = _synth_chunk(client, cfg, ch, voice, cur_style, cache_dir)
            else:
                pcm, rate = hit
                log(f"Story TTS chunk {i + 1}/{len(chunks)} (cached)")
            chunk_mp3s.append(_pcm_to_chunk_mp3(
                pcm, rate, cfg.narration_speed, workdir / f"chunk_{tag}_{i}.mp3"))
        raw = _concat_crossfade(chunk_mp3s, workdir / f"narr_raw_{tag}.mp3",
                                pause)
        return _postprocess(raw, workdir / f"narr_{tag}.mp3")

    mp3 = _render_all(style, "a")
    dur = ffprobe_duration(mp3)
    fit_note = f"first pass {dur:.1f}s"
    log(f"Story narration: {dur:.1f}s ({fit_note})")

    if not (cfg.target_min - 5 <= dur <= cfg.target_max + 5):
        # Band 2: re-speed from cached PCM (no new TTS spend).
        factor = _fit_speed(dur)
        log(f"Story fit: re-speed x{factor:.3f} from cached PCM...")
        sped = []
        for i, ch in enumerate(chunks):
            key = tts_cache.hash_key(ch, voice, style, 1.0)
            hit = _load_cached_pcm(cache_dir, key)
            if hit is None:  # pragma: no cover - cache always warm here
                hit = _synth_chunk(client, cfg, ch, voice, style, cache_dir)
            pcm, rate = hit
            sped.append(_pcm_to_chunk_mp3(
                pcm, rate, cfg.narration_speed * factor,
                workdir / f"chunk_b_{i}.mp3"))
        mp3 = _postprocess(_concat_crossfade(sped, workdir / "narr_raw_b.mp3",
                                             pause),
                           workdir / "narr_b.mp3")
        dur = ffprobe_duration(mp3)
        fit_note += f" -> re-speed {dur:.1f}s"

    if not (cfg.target_min <= dur <= cfg.target_max):
        if dur > cfg.target_max + 5 or dur < cfg.target_min - 5:
            # Band 3: one paced re-synth (lengthen/tighten delivery).
            direction = ("Speak about fifteen percent faster with tighter delivery."
                         if dur > cfg.target_max else
                         "Speak about fifteen percent slower and more deliberate.")
            log(f"Story fit: paced re-synth ({direction[:40]}...)...")
            mp3 = _render_all(f"{style} {direction}", "c")
            dur = ffprobe_duration(mp3)
            fit_note += f" -> re-synth {dur:.1f}s"

    words = align_or_fallback(mp3, text, total_dur=dur,
                              model_size=cfg.whisper_model)
    if dur > cfg.max_seconds:
        # Band 4: sentence trim before MAX (hard cut fallback inside).
        log(f"Story fit: trimming {dur:.1f}s to sentence before {cfg.max_seconds:g}s...")
        mp3 = _trim_to_sentence(mp3, words, cfg.max_seconds, workdir / "narr_trim.mp3")
        dur = ffprobe_duration(mp3)
        words = align_or_fallback(mp3, text, total_dur=dur,
                                  model_size=cfg.whisper_model)
        fit_note += f" -> trimmed {dur:.1f}s"

    card_end = card_end_time(words, content.extras.get("card_text", ""),
                             content.extras.get("hook", ""))
    wps_measured = len(text.split()) / dur if dur > 0 else 0.0
    log(f"Story narration final: {dur:.1f}s, {len(words)} timed words "
        f"({wps_measured:.2f} wps), card_end={card_end:.1f}s [{fit_note}]")
    return NarrationResult(
        audio=mp3, duration=dur, words=len(text.split()),
        voice=voice, style=style,
        extras={"words": words, "card_end": card_end,
                "chunks": len(chunks), "tts_engine": "gemini",
                "wps_measured": round(wps_measured, 3), "fit": fit_note,
                "card_text": content.extras.get("card_text", ""),
                "card_highlight_word": content.extras.get("card_highlight_word", ""),
                "key_words": list(content.extras.get("key_words", [])),
                "genre": genre, "title": content.title})
