"""Shared Shorts-pipeline machinery: config, Gemini/TTS/visuals/FFmpeg/upload.

Niche-agnostic: every niche-specific behavior arrives via a `niche` module.
A niche module must provide (see niche_ranking.py for the reference):

    MODE                  str, e.g. "ranking" (also the history.json tag)
    NICHE_LABEL           str, human-readable channel description
    build_prompt(history) -> (prompt, slots)
    fallback_script(history) -> (meta, slots)
    validate_extra(data)  -> dict merged into validated meta ({} if none)
    sanitize_title(title, slots) -> str (channel title format enforcement)
    similarity_reason(data, slots, recent) -> str | None
    format_history_line(h) -> str
    AVOID_INTRO, AVOID_OUTRO   str (history avoidance prompt wrapper)
    USE_BEAT_QUERY        bool (True: Pexels searches the beat's own query)
    AMBIENT_QUERIES       tuple (used when USE_BEAT_QUERY is False)
    USE_MOOD              bool (force horror mood on Pexels queries)
    beat_anchor(beats)    -> str (CF still continuity anchor, "" if none)
    image_prompt(beat_text, query, anchor) -> str (CF fallback prompt)
    history_fields(slots, meta) -> dict (extra history.json entry fields)
    board_filter(beats, items, durations, get_font, has_drawtext, title="") -> str

Import direction is one-way: niche modules import core, core never imports
a niche (pipeline.py wires them). To delete a niche, delete its file and
remove its line from the NICHES registry in pipeline.py.
"""
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import time
import base64
from pathlib import Path
from typing import Any

import requests

OUT = Path("out")
ASSETS = Path("assets")
OUT.mkdir(exist_ok=True)
ASSETS.mkdir(exist_ok=True)

VOICE = "en-US-ChristopherNeural"  # deep male narrator (News/Novel, Authority)
VISIBILITY = os.getenv("VISIBILITY", "private")  # private = safe for unverified OAuth apps
def _parse_gemini_keys() -> list[str]:
    """All configured Gemini keys: GEMINI_API_KEY (comma-separated, so a
    single key keeps working) plus GEMINI_API_KEY_2.._10 failover keys."""
    raws = [os.getenv("GEMINI_API_KEY", "")]
    raws += [os.getenv(f"GEMINI_API_KEY_{i}", "") for i in range(2, 11)]
    keys: list[str] = []
    for raw in raws:
        for k in raw.split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    return keys


GEMINI_KEYS = _parse_gemini_keys()
GEMINI_KEY = GEMINI_KEYS[0] if GEMINI_KEYS else ""  # first key; back-compat
_GEMINI_KEY_IDX = 0  # working key; advances on quota rejection
# Tried in order; first working model wins. Override via GEMINI_MODELS="model-a,model-b".
GEMINI_MODELS = [m.strip() for m in
                 os.getenv("GEMINI_MODELS",
                            "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-2.5-flash,gemini-2.5-flash-lite").split(",")
                 if m.strip()]
PEXELS_KEY = os.getenv("PEXELS_API_KEY", "")
# Minimum Pexels file width to prefer (default 1080: 1:1 with the 1080x1920
# canvas, no upscale softness). Smaller files still play as fallback.
PEXELS_MIN_WIDTH = int(os.getenv("PEXELS_MIN_WIDTH", "1080"))
MIN_AUDIO_SEC = float(os.getenv("MIN_AUDIO_SEC", "10"))
MAX_AUDIO_SEC = float(os.getenv("MAX_AUDIO_SEC", "180"))
# TTS prosody: slightly slower + lower reads more dramatic.
# edge-tts takes relative rate (-100%..+200%) and pitch (e.g. -10Hz..+10Hz).
TTS_RATE = os.getenv("TTS_RATE", "-5%")
TTS_PITCH = os.getenv("TTS_PITCH", "-10Hz")
TTS_VOLUME = os.getenv("TTS_VOLUME", "+0%")
# TTS engine chain (default gemini -> edge): "gemini" (per-beat Gemini TTS,
# voices alternate, beat-level captions since Gemini returns audio only),
# or "edge" (free edge-tts only, word-level captions). Missing keys skip
# with a warning; any engine failure warns and moves to the next.
TTS_ENGINE = os.getenv("TTS_ENGINE", "gemini").strip().lower()
# Reserved for future OpenRouter use (currently no engine reads it).
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Tried in order; first working model wins (mirrors GEMINI_MODELS).
GEMINI_TTS_MODELS = [m.strip() for m in
                     os.getenv("GEMINI_TTS_MODELS",
                               "gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts").split(",")
                     if m.strip()]
# One voice = classic single narrator; two = man/woman alternation per beat
# (e.g. "Charon,Kore"). 30 prebuilt voices, see AI Studio voice library.
GEMINI_TTS_VOICES = ([v.strip() for v in
                      os.getenv("GEMINI_TTS_VOICES", "Charon").split(",")
                      if v.strip()] or ["Charon"])
DEFAULT_GEMINI_TTS_STYLE = (
    "Read in a premium, cinematic documentary voice with confident pacing, "
    "subtle drama, and a sense of wonder. Clear, polished, and authoritative, "
    "with strong emphasis on reveal moments."
)
GEMINI_TTS_STYLE = os.getenv("GEMINI_TTS_STYLE", DEFAULT_GEMINI_TTS_STYLE)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def pick_tts_style(topic: str, text: str) -> str:
    """Choose a narrator style tuned to the topic and storytelling tone."""
    haystack = f"{topic} {text}".lower()

    if any(k in haystack for k in [
        "planet", "space", "galaxy", "moon", "star", "ocean", "mountain",
        "building", "statue", "island", "river", "desert", "volcano"
    ]):
        return (
            "Read in a premium, cinematic documentary voice with confident pacing, "
            "clear authority, and a sense of scale and wonder. Keep it polished, "
            "immersive, and slightly dramatic at the ranking reveals."
        )

    if any(k in haystack for k in [
        "deadly", "danger", "horror", "haunted", "ghost", "creature",
        "predator", "attack", "monster", "scary", "venom"
    ]):
        return (
            "Read in a dark, ominous, high-tension narration voice with restrained horror, "
            "measured pacing, and strong emphasis on danger and surprise."
        )

    if any(k in haystack for k in [
        "animal", "wild", "snake", "shark", "elephant", "giraffe", "cat",
        "dog", "predator", "nature"
    ]):
        return (
            "Read in a dramatic but accessible countdown voice with strong emphasis on each reveal, "
            "clear pacing, and an energetic sense of awe."
        )

    return (
        "Read in a calm, confident narrator voice with steady pacing, authoritative tone, "
        "and clean emphasis on each reveal."
    )

W, H = 1080, 1920  # vertical Shorts


def _print(msg: str):
    """print() that survives non-UTF-8 consoles (Windows cp1252). Without
    this, any Gemini emoji in a log line would raise UnicodeEncodeError —
    either failing the run or masking the real error with a bogus warning."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode(), flush=True)


def log(msg: str):
    _print(f"[pipeline] {msg}")


def warn(msg: str):
    """Degraded-but-continuing path. Surfaces as an annotated warning in Actions."""
    _print(f"::warning::{msg}")
    log(f"WARNING: {msg}")


def err(msg: str):
    """Fatal path. Surfaces as an annotated error in Actions."""
    _print(f"::error::{msg}")
    log(f"ERROR: {msg}")


def _http_status(e: Exception):
    """Best-effort HTTP status extraction across requests / googleapiclient errors."""
    direct = getattr(e, "status_code", None)
    if isinstance(direct, int):
        return direct
    resp = getattr(e, "response", None)
    if resp is None:
        resp = getattr(e, "resp", None)
    if resp is not None:
        if isinstance(resp, dict):
            try:
                return int(resp.get("status"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
        for attr in ("status_code", "status"):
            val = getattr(resp, attr, None)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
    return None


def retry(name: str, func, tries: int = 3, backoff: int = 5, fatal=()) -> Any:
    """Retry temporary failures (network blips, 5xx, 429, process errors).

    Permanent client errors (other 4xx, e.g. bad API key / bad credentials)
    and `fatal` exception types fail immediately without retrying.
    """
    for attempt in range(1, tries + 1):
        try:
            return func()
        except fatal:
            raise
        except Exception as e:
            status = _http_status(e)
            if status is not None and 400 <= status < 500 and status not in (408, 429):
                err(f"{name} permanent failure (HTTP {status}): {e}")
                raise
            if attempt == tries:
                raise
            wait = backoff * attempt
            log(f"{name} failed (attempt {attempt}/{tries}: {e}), retrying in {wait}s...")
            time.sleep(wait)


# Cross-run dedup memory. CI runners are ephemeral, so the run history lives
# in a committed file (history.json) instead of memory. Every successful run
# appends one entry; the next run feeds recent entries into the Gemini prompt
# and forces fresh slots, plus a similarity gate retries when Gemini repeats
# a recent entry anyway. Never fails the run.
HISTORY_PATH = Path("history.json")
HISTORY_KEEP = 50  # entries stored on disk
HISTORY_PROMPT_N = 20  # recent entries quoted in the prompt
SIMILARITY_RETRIES = 2  # extra script attempts after a too-similar draft


def load_history(path: Path = HISTORY_PATH) -> list[dict]:
    """Recent runs, newest last. Empty on first run or corrupt file (warn)."""
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            warn(f"{path} has unexpected shape, ignoring.")
            return []
        return [h for h in data if isinstance(h, dict)][-HISTORY_KEEP:]
    except Exception as e:
        warn(f"Could not read {path} ({e}), continuing without dedup memory.")
        return []


def save_history(entry: dict, path: Path = HISTORY_PATH):
    """Append one run entry, trim to HISTORY_KEEP. Never raises."""
    try:
        history = load_history(path)
        history.append(entry)
        path.write_text(json.dumps(history[-HISTORY_KEEP:], indent=2,
                                   ensure_ascii=False),
                        encoding="utf-8")
        log(f"History: recorded '{entry.get('title', '?')}' "
            f"({len(history[-HISTORY_KEEP:])} entries in {path}).")
    except Exception as e:
        warn(f"Could not write {path} ({e}).")


def history_block(history: list[dict], mode: str, fmt, intro: str, outro: str) -> str:
    """Prompt section listing recent same-niche entries Gemini must not repeat."""
    recent = [h for h in history[-HISTORY_PROMPT_N:]
              if h.get("niche", "horror") == mode]
    if not recent:
        return ""
    return intro + "\n".join(fmt(h) for h in recent) + "\n" + outro


def _pick_avoiding(pool: tuple, recent: set):
    """Random pool pick excluding recently used values (falls back to random)."""
    fresh = [v for v in pool if v not in recent]
    return random.choice(fresh) if fresh else random.choice(pool)


def _story_signature(text: str) -> set:
    """Significant lowercase words, for overlap comparison."""
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "at",
            "it", "its", "is", "was", "are", "be", "i", "you", "he", "she",
            "they", "we", "my", "his", "her", "with", "for", "as", "that",
            "this", "but", "not", "so", "if", "then", "there", "here"}
    return {w.strip(".,!?;:\"'").lower() for w in text.split()
            if len(w) > 3} - stop


def similar_to_history(data: dict, slots: dict, history: list[dict], niche):
    """Return a repeat reason if the draft is too close to history, else None."""
    recent = [h for h in history[-HISTORY_PROMPT_N:]
              if h.get("niche", "horror") == niche.MODE]
    if not recent:
        return None
    recent_titles = {str(h.get("title", "")).strip().casefold() for h in recent}
    if str(data.get("title", "")).strip().casefold() in recent_titles:
        return "title already used"
    reason = niche.similarity_reason(data, slots, recent)
    if reason is not None:
        return reason
    new_sig = _story_signature(
        f"{data.get('title', '')} {data.get('narration', '')}")
    for h in recent:
        old_sig = _story_signature(
            f"{h.get('title', '')} {h.get('narration', '')}")
        if not old_sig or not new_sig:
            continue
        overlap = len(new_sig & old_sig) / len(new_sig | old_sig)
        if overlap > 0.35:
            return (f"story overlaps '{h.get('title', '?')}' "
                    f"(word overlap {overlap:.0%})")
    return None


# ---------- 1. Script (target ~1:30: 200-220 words, 7 beats) ----------
def _validate_script(data, niche) -> dict:
    """Strict-check Gemini output. Raises ValueError with the reason if unusable."""
    if not isinstance(data, dict):
        raise ValueError("Gemini output is not a JSON object")
    title = data.get("title")
    narration = data.get("narration")
    description = data.get("description")
    tags = data.get("tags")
    beats = data.get("beats")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("missing/invalid 'title' (must be non-empty string)")
    if not isinstance(narration, str) or len(narration.split()) < 20:
        raise ValueError("'narration' missing or too short (<20 words)")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("missing/invalid 'description' (must be non-empty string)")
    if (not isinstance(tags, list) or not tags
            or not all(isinstance(t, str) and t.strip() for t in tags)):
        raise ValueError("'tags' must be a non-empty list of strings")
    if not isinstance(beats, list) or not beats:
        raise ValueError("'beats' must be a non-empty list")
    norm_beats = []
    for b in beats[:8]:
        if not isinstance(b, dict):
            raise ValueError("each beat must be an object with text+query")
        t, q = b.get("text"), b.get("query")
        if not isinstance(t, str) or not t.strip():
            raise ValueError("beat 'text' must be a non-empty string")
        if not isinstance(q, str) or not q.strip():
            raise ValueError("beat 'query' must be a non-empty string")
        # Optional on-screen rank digit (ranking niche). Digits only, so
        # "#5" -> "5" and prose ("number one") -> "" (no overlay).
        lab = b.get("label", "")
        if not isinstance(lab, str):
            lab = ""
        lab = "".join(c for c in lab if c.isdigit())[:2]
        norm_beats.append({"text": t.strip(), "query": q.strip(), "label": lab})
    if sum(len(b["text"].split()) for b in norm_beats) < 20:
        raise ValueError("'beats' cover too little text (<20 words total)")
    # Normalize so downstream steps never choke on oversized input.
    out = {
        "title": title.strip()[:100],
        "narration": narration.strip(),
        "description": description.strip(),
        "tags": [t.strip() for t in tags if t.strip()][:10],
        "beats": norm_beats,
    }
    out.update(niche.validate_extra(data))
    return out


def _fetch_gemini(url: str, payload: dict, tries: int = 3, backoff: int = 5):
    """POST to Gemini with key rotation + retries. `url` is keyless.

    Sticks with the working key; a quota/invalid-key rejection (429 or other
    4xx) fails over to the next key immediately, while transient 5xx/network
    errors retry with backoff first. Raises the last error when all keys fail.
    """
    global _GEMINI_KEY_IDX
    if not GEMINI_KEYS:
        raise ValueError("GEMINI_API_KEY missing")
    n = len(GEMINI_KEYS)
    last_err: Exception | None = None
    for k in range(n):
        idx = (_GEMINI_KEY_IDX + k) % n
        sep = "&" if "?" in url else "?"
        keyed = f"{url}{sep}key={GEMINI_KEYS[idx]}"
        for attempt in range(1, tries + 1):
            try:
                r = requests.post(keyed, json=payload, timeout=60)
                r.raise_for_status()
                _GEMINI_KEY_IDX = idx  # stick with the working key
                return r.json()
            except Exception as e:
                last_err = e
                status = _http_status(e)
                if status is not None and 400 <= status < 500 and status != 408:
                    if k < n - 1:
                        log(f"Gemini key {idx + 1}/{n} rejected (HTTP {status}), "
                            "failing over to next key...")
                    break  # 4xx won't heal with retries — try next key
                if attempt == tries:
                    break
                wait = backoff * attempt
                log(f"Gemini(key {idx + 1}/{n}) failed (attempt {attempt}/{tries}: {e}), "
                    f"retrying in {wait}s...")
                time.sleep(wait)
    assert last_err is not None
    raise last_err


def gen_script(niche):
    """Call Gemini free API. Returns (meta, slots).

    Every run is standalone — no arcs, no continuity. Drafts that are too
    similar to history.json are regenerated (up to SIMILARITY_RETRIES).
    Fallback matches the active niche too (slots avoid recent history).
    """
    history = load_history()
    if GEMINI_KEYS:
        for attempt in range(1 + SIMILARITY_RETRIES):
            prompt, slots = niche.build_prompt(history)
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            data = None
            for model in GEMINI_MODELS:
                try:
                    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                           f"{model}:generateContent")
                    raw = _fetch_gemini(url, payload)
                    text = raw["candidates"][0]["content"]["parts"][0]["text"]
                    # strip markdown fences
                    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                    data = _validate_script(json.loads(text), niche)
                    break
                except Exception as e:
                    log(f"Gemini model {model} unusable ({e}), trying next...")
            if data is None:
                log("All Gemini models failed, using fallback.")
                break
            reason = similar_to_history(data, slots, history, niche)
            # Sanitize AFTER the similarity verdict: stripping banned hooks
            # beforehand would hide them from the regen backstop.
            if reason is None:
                data["title"] = niche.sanitize_title(data.get("title", ""), slots)
                log(f"Gemini script OK: {data.get('title')}")
                return data, slots
            if attempt < SIMILARITY_RETRIES:
                warn(f"Gemini draft too similar to history ({reason}), "
                     f"regenerating (attempt {attempt + 1}/{SIMILARITY_RETRIES})...")
                continue
            warn(f"Gemini draft still similar ({reason}), accepting "
                 f"to avoid failing the run.")
            data["title"] = niche.sanitize_title(data.get("title", ""), slots)
            return data, slots
    warn("Gemini unavailable: using standalone fallback script.")
    return niche.fallback_script(history)


# ---------- 2. Audio (edge-tts default, free, no key; Gemini TTS pilot) ----------
def _gen_audio_edge(narration: str, mp3: Path, srt: Path):
    """Single edge-tts call: media + word-level SRT. Raises on failure."""
    # edge-tts CLI writes media + subtitles in one call.
    # NOTE: rate/pitch/volume/text use --flag=value form because values like
    # -10% start with a dash, which some edge-tts builds parse as a new flag
    # ("argument --rate: expected one argument") when passed as separate args.
    cmd = [
        sys.executable, "-m", "edge_tts",
        "--voice", VOICE,
        f"--rate={TTS_RATE}",
        f"--pitch={TTS_PITCH}",
        f"--volume={TTS_VOLUME}",
        f"--text={narration}",
        "--write-media", str(mp3),
        "--write-subtitles", str(srt),
    ]
    log(f"TTS voice={VOICE} rate={TTS_RATE} pitch={TTS_PITCH} volume={TTS_VOLUME}")
    retry("edge-tts", lambda: subprocess.run(cmd, check=True),
          tries=3, backoff=10)


def _gemini_tts_beat(text: str, voice: str, topic: str = "") -> tuple[bytes, int]:
    """One beat of raw PCM audio via Gemini TTS. Returns (pcm, sample_rate).

    Tries GEMINI_TTS_MODELS in order; raises when all fail. The caller falls
    back to edge-tts for the WHOLE narration on any failure — per-beat
    retries would multiply a total outage by the beat count.
    """
    if not GEMINI_KEYS:
        raise ValueError("GEMINI_API_KEY missing (TTS_ENGINE=gemini needs it)")
    prompt = f"{pick_tts_style(topic, text) if topic else GEMINI_TTS_STYLE} Say the following text:\n\n{text}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    last_err: Exception | None = None
    for model in GEMINI_TTS_MODELS:
        try:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent")
            raw = _fetch_gemini(url, payload)
            parts = raw["candidates"][0]["content"].get("parts", [])
            inline = next((p.get("inlineData") for p in parts if p.get("inlineData")), None)
            if not inline or not inline.get("data"):
                raise ValueError(f"TTS model {model} returned no audio")
            rate_m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
            rate = int(rate_m.group(1)) if rate_m else 24000
            return base64.b64decode(inline["data"]), rate
        except Exception as e:
            last_err = e
            log(f"Gemini TTS model {model} unusable ({e}), trying next...")
    raise ValueError(f"All Gemini TTS models failed ({last_err})")


def _format_srt_ts(sec: float) -> str:
    """Seconds -> SRT timestamp (HH:MM:SS,mmm)."""
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, rem = divmod(rem, 60)
    s, ms = divmod(rem, 1)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(ms * 1000):03d}"


def _assemble_beats(beats: list[dict], mp3: Path, srt: Path,
                    synth, tag: str, topic: str = "") -> list[float]:
    """Shared per-beat assembly for audio-only TTS engines (no word timings).

    synth(i, beat) -> (voice_label, seg_mp3_path); raises on failure (the
    caller then tries the next engine — never per-beat retries). Returns
    per-beat durations; writes concatenated mp3 + beat-level SRT.
    Intermediates are tts_* (see INTERMEDIATE_PATTERNS).
    """
    parts, cues, durations = [], [], []
    t = 0.0
    for i, b in enumerate(beats):
        voice, seg = synth(i, b, topic) if topic else synth(i, b)
        d = audio_duration(seg)
        parts.append(seg)
        cues.append((t, t + d, " ".join(b["text"].split())))
        durations.append(d)
        t += d
        log(f"TTS {tag} beat {i + 1}/{len(beats)} voice={voice} {d:.1f}s")
    lst = ASSETS / "tts_parts.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                 "-c:a", "libmp3lame", str(mp3)], f"tts-{tag}-concat")
    srt.write_text("".join(
        f"{n}\n{_format_srt_ts(s)} --> {_format_srt_ts(e)}\n{txt}\n\n"
        for n, (s, e, txt) in enumerate(cues, 1)), encoding="utf-8")
    return durations


def _gemini_synth(i: int, beat: dict, topic: str = ""):
    """Gemini per-beat synth step: (voice, seg_mp3). Raises on failure."""
    voice = GEMINI_TTS_VOICES[i % len(GEMINI_TTS_VOICES)]
    pcm, rate = _gemini_tts_beat(beat["text"], voice, topic)
    raw_pcm = ASSETS / f"tts_beat_{i}.pcm"
    seg = ASSETS / f"tts_beat_{i}.mp3"
    raw_pcm.write_bytes(pcm)
    _run_ffmpeg(["ffmpeg", "-y", "-f", "s16le", "-ar", str(rate), "-ac", "1",
                 "-i", str(raw_pcm), "-c:a", "libmp3lame", str(seg)],
                f"tts-beat-{i}-pcm")
    return voice, seg


def gen_audio(narration: str, stem: Path,
              beats: list[dict] | None = None,
              topic: str = "") -> tuple[Path, Path, list[float] | None]:
    """Narrate + produce captions. Returns (mp3, srt, beat_durations|None).

    engine=gemini (default): per-beat Gemini TTS, real per-beat durations
    for visuals, beat-level SRT. Missing key or any failure warns and falls
    back to edge for the whole narration (fail-fast on first bad beat, never
    per-beat retries). engine=edge: one edge-tts call, word-level SRT.
    """
    mp3 = stem.with_suffix(".mp3")
    srt = stem.with_suffix(".srt")
    if TTS_ENGINE not in ("gemini", "edge"):
        warn(f"Unknown TTS_ENGINE={TTS_ENGINE!r}, using edge-tts.")
    if beats and TTS_ENGINE == "gemini":
        if GEMINI_KEYS:
            try:
                return mp3, srt, _assemble_beats(beats, mp3, srt, _gemini_synth, "gemini", topic)
            except Exception as e:
                warn(f"Gemini TTS failed ({e}), falling back to edge-tts.")
        else:
            warn("GEMINI_API_KEY missing (TTS_ENGINE=gemini needs it), "
                 "falling back to edge-tts.")
    _gen_audio_edge(narration, mp3, srt)
    return mp3, srt, None


def check_audio_duration(dur: float, words: int):
    """Sanity gate: refuse to render/upload a broken-length narration."""
    log(f"Narration {dur:.1f}s, {words} words")
    if not (MIN_AUDIO_SEC <= dur <= MAX_AUDIO_SEC):
        raise ValueError(
            f"narration audio {dur:.1f}s outside sane range "
            f"[{MIN_AUDIO_SEC:g}-{MAX_AUDIO_SEC:g}s] for {words} words; "
            "refusing to render a broken video."
        )


def notify_failure(error: Exception):
    """Best-effort failure ping via Discord and/or Telegram. Never raises."""
    if not (DISCORD_WEBHOOK_URL or (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)):
        return
    repo = os.getenv("GITHUB_REPOSITORY", "local")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    link = f"{server}/{repo}/actions/runs/{run_id}" if run_id else "local run"
    text = (f"youtube-story FAILED\n{type(error).__name__}: {error}\nRun: {link}")
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": text[:1900]},
                          timeout=15).raise_for_status()
            log("Failure notified via Discord.")
        except Exception as e:
            log(f"Discord notify failed (non-fatal): {e}")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                          timeout=15).raise_for_status()
            log("Failure notified via Telegram.")
        except Exception as e:
            log(f"Telegram notify failed (non-fatal): {e}")


# Caption style for vertical Shorts: tiny bold yellow text with a solid black
# outline + shadow, anchored top-center just below the title (above the
# leaderboard). Yellow holds contrast on bright daylight footage where white
# washes out. DejaVu Sans ships with the Ubuntu runner (libass falls back
# if missing).
# NOTE: applied via generated .ass file, NOT force_style (comma-bearing
# force_style silently yields zero pixels on some FFmpeg/libass builds).
SUB_STYLE = ("FontName=DejaVu Sans,FontSize=34,Bold=-1,"
             "PrimaryColour=&H0000FFFF,OutlineColour=&HFF000000,"
             "BorderStyle=1,Outline=3,Shadow=1,"
             "Alignment=8,MarginV=320,WrapStyle=0")


def _srt_to_ass(srt: Path) -> Path:
    """Convert edge-tts SRT to a styled ASS (top-center below title, tiny text).

    Avoids force_style entirely: style lives in the file header, so the
    subtitles filter stays a plain path with no commas to misparse.
    """
    ass = srt.with_suffix(".ass")
    raw = srt.read_text(encoding="utf-8-sig")

    def _ts(ts: str) -> str:
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", ts.strip())
        if m is None:
            raise ValueError(f"bad SRT timestamp: {ts!r}")
        h, mi, s, ms = map(int, m.groups())
        return f"{h}:{mi:02d}:{s:02d}.{ms // 10:02d}"

    events = []
    for block in re.split(r"\r?\n\r?\n", raw.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip() != ""]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start, _, end = lines[1].partition("-->")
        text = "\\N".join(lines[2:])
        events.append(
            f"Dialogue: 0,{_ts(start)}, {_ts(end).strip()},Default,,0,0,0,,{text}")

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {W}\n"
        f"PlayResY: {H}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,DejaVu Sans,34,&H0000FFFF,&H000019FF,&HFF000000,"
        "&H00000000,-1,0,0,0,100,100,0,0,1,3,1,8,10,10,320,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    ass.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return ass


def _subtitles_vf(srt: Path):
    """Burn-in video filter with styled captions, or None if no subs."""
    if not srt.exists():
        return None
    try:
        target = _srt_to_ass(srt)
    except Exception as e:
        warn(f"ASS conversion failed ({e}), burning raw SRT.")
        target = srt
    esc = (str(target.resolve()).replace("\\", "/").replace("'", "\\'").replace(":", "\\:").replace(",", "\\,"))
    # Plain path, no force_style: commas in force_style break some builds.
    return f"subtitles='{esc}'"


# Optional override: put your own loopable track(s) at bgm.mp3 (repo root,
# e.g. from the YouTube Audio Library) or assets/bgm.mp3 (local only).
# Numbered tracks (bgm1.mp3, bgm2.mp3, bgm3.mp3) are picked at random each
# run so videos don't all sound identical. Random, not strict rotation:
# bed choice is intentionally stateless (history.json tracks stories, not
# music) — but with 3 tracks repeats are rare and harmless.
def _bgm_pool() -> list:
    """Available custom bed tracks. Repo-root bgm*.mp3 wins (CI-visible);
    assets/bgm*.mp3 is the local-only fallback. Sorted for stable logs."""
    pool = sorted(Path(".").glob("bgm*.mp3"))
    if not pool:
        pool = sorted(ASSETS.glob("bgm*.mp3"))
    return [p for p in pool if p.is_file()]


def make_bed(duration: float) -> Path:
    """Background audio matching narration length. Never fails the run."""
    bed = ASSETS / "bed.mp3"
    pool = _bgm_pool()
    custom = random.choice(pool) if pool else None
    if custom is not None:
        log(f"Bed: looping custom track {custom} (1 of {len(pool)} in pool)")
        _run_ffmpeg(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(custom),
                     "-t", str(duration), "-af", "volume=0.20",
                     "-c:a", "libmp3lame", str(bed)], "bed-loop")
    else:
        # Layered dark drone: brown-noise texture (the phone-audible part,
        # lowpassed so it stays warm) + 55Hz sub root (A1, felt on earbuds)
        # + 58.27Hz dissonant second voice (A#1). The ~3.3Hz beating between
        # the two sines is what reads as dread; slow tremolo swell + a short
        # echo tail give it cavern space. Generated = zero copyright risk.
        log("Bed: generating layered copyright-safe drone")
        fc = ("[0:a]lowpass=f=300,volume=0.6[ns];"
              "[1:a]volume=0.25[s1];[2:a]volume=0.25[s2];"
              "[ns][s1][s2]amix=inputs=3:normalize=1,"
              "tremolo=f=0.2:d=0.7,volume=0.5,"
              "aecho=0.8:0.5:50:0.3[aout]")
        _run_ffmpeg(["ffmpeg", "-y",
                     "-f", "lavfi", "-i",
                     f"anoisesrc=color=brown:sample_rate=44100:duration={duration}:seed=7",
                     "-f", "lavfi", "-i",
                     f"sine=frequency=55:sample_rate=44100:duration={duration}",
                     "-f", "lavfi", "-i",
                     f"sine=frequency=58.27:sample_rate=44100:duration={duration}",
                     "-filter_complex", fc, "-map", "[aout]",
                     "-t", str(duration),
                     "-c:a", "libmp3lame", str(bed)], "bed-drone")
    return bed


def audio_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


# ---------- 3. Visuals (one clip per story beat, in order) ----------
MOOD_WORDS = ("dark", "night", "creepy", "scary", "abandoned", "haunted",
              "fog", "eerie", "gloom", "shadow", "spooky", "terror", "ghost")


def _mood_query(q: str) -> str:
    """Force a horror mood so generic queries can't return daylight clips."""
    if any(w in q.lower() for w in MOOD_WORDS):
        return q
    return f"{q} dark"


def pexels_clip(query: str, used: set | None = None, mood: bool = True):
    """First unused stock clip for one query. Returns None (no key/fail/empty).

    `used` holds Pexels video ids already shown in this run, so similar
    queries can't repeat the same clip twice in one story. `mood=False`
    skips the horror-mood forcing (ranking niche wants literal matches).
    """
    if not PEXELS_KEY:
        return None
    try:
        def _search():
            r = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": PEXELS_KEY},
                params={"query": _mood_query(query) if mood else query,
                        "per_page": 4,
                        "orientation": "portrait", "size": "medium"},
                timeout=30,
            )
            r.raise_for_status()
            return r
        r = retry("Pexels search", _search, tries=3, backoff=5)
        for v in r.json().get("videos", []):
            if used is not None and v.get("id") in used:
                continue  # same story must not show the same clip twice
            # Sharp-but-bounded: smallest file clearing PEXELS_MIN_WIDTH
            # (default 1080p, 1:1 with the canvas). Falls back to whatever
            # exists so clips with only SD files still play.
            cands = sorted((f for f in v.get("video_files", []) if f.get("link")),
                           key=lambda f: f.get("width", 0))
            big_enough = [f for f in cands if f.get("width", 0) >= PEXELS_MIN_WIDTH]
            pick = (big_enough or cands)
            if not pick:
                continue
            dest = ASSETS / f"pexels_{v['id']}.mp4"
            if not dest.exists():
                def _download(url=pick[0]["link"], dest=dest):
                    with requests.get(url, stream=True, timeout=120) as d:
                        d.raise_for_status()
                        with open(dest, "wb") as f:
                            shutil.copyfileobj(d.raw, f)
                try:
                    retry("Pexels download", _download, tries=2, backoff=5)
                except Exception:
                    if dest.exists():
                        dest.unlink()  # never keep a partial file
                    raise
            if used is not None:
                used.add(v["id"])
            return dest
    except Exception as e:
        warn(f"Pexels failed for '{query}' ({e}).")
    return None


def beat_card(i: int, text: str) -> Path:
    """Title card carrying the beat's story text (gap filler, always matches)."""
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]
    colors = [(18, 18, 40), (40, 18, 30), (18, 40, 35), (35, 35, 18)]
    img = Image.new("RGB", (W, H), colors[i % len(colors)])
    d = ImageDraw.Draw(img)
    d.multiline_text((80, 800), textwrap.fill(text[:220], width=28),
                     fill=(255, 255, 255), spacing=8)
    p = ASSETS / f"slide_beat_{i}.png"
    img.save(p)
    return p


# Optional: Cloudflare Workers AI (free 10k neurons/day, no card) fallback
# when Pexels has no stock. Account ID + API token with Workers AI
# permission. Without them, this step is skipped silently.
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "")
# SDXL-Lightning: listed at $0.00/step (Beta) + native portrait size, so it
# beats FLUX on cost. Override back to FLUX if you ever want it:
# "@cf/black-forest-labs/flux-1-schnell".
CF_IMAGE_MODEL = os.getenv(
    "CF_IMAGE_MODEL", "@cf/bytedance/stable-diffusion-xl-lightning")
# More steps = better prompt adherence (exact moment/action), but each step
# burns Workers AI neurons out of the 10k/day free allowance. Lightning is a
# distilled few-step model: below ~8 steps it ignores prompt detail and falls
# back to generic horror frames; FLUX-schnell adheres better at the same cost
# in neurons, so prefer switching models over cranking steps past ~20.
CF_NUM_STEPS = int(os.getenv("CF_NUM_STEPS", "8"))
# Cuts per beat: 2 stills per beat ≈ 14-16 images/video, cutting mid-beat like
# real Shorts pacing (~4-7s per still) instead of one static slideshow frame
# per beat. 1 = old behavior (one still per beat). Costs 2x Cloudflare calls
# per run — inside the free allowance, which hard-stops instead of billing.
STILLS_PER_BEAT = int(os.getenv("STILLS_PER_BEAT", "2"))


def _split_beat(text: str, n: int) -> list[str]:
    """Split beat narration into n word-even halves so mid-beat cuts each get
    their own image prompt (different moment, not the same frame twice)."""
    words = text.split()
    if n <= 1 or len(words) <= n:
        return [text]
    size = len(words) / n
    parts = [" ".join(words[int(i * size):int((i + 1) * size)]).strip()
             for i in range(n)]
    return [p for p in parts if p] or [text]


def _stock_offset(stock: Path, part_idx: int, parts: int):
    """Seek offset so mid-beat cuts show different stock footage. None = start."""
    if part_idx == 0 or parts <= 1:
        return None
    try:
        dur = audio_duration(stock)  # ffprobe reads video duration too
    except Exception:
        return None
    return max(0.0, dur * part_idx / parts)


# CTA filler that pollutes image prompts when fed to the model literally
# ("follow...", "part 2 tomorrow") — the model draws marketing, not story.
CTA_MARKERS = ("follow", "subscribe", "part 2", "part two", "tomorrow",
               "don't miss", "dont miss", "comment", "like and")


def _clean_beat_text(beat_text: str, limit: int = 350) -> str:
    """Strip CTA/filler sentences so the image model draws the story beat."""
    kept = []
    for sent in beat_text.replace("!", ".").replace("?", ".").split("."):
        s = sent.strip()
        if not s:
            continue
        if any(m in s.lower() for m in CTA_MARKERS):
            continue
        kept.append(s)
    text = ". ".join(kept).strip()
    return text[:limit].rstrip() or beat_text[:limit].rstrip()


def cloudflare_image(beat_text: str, query: str, i: int, anchor: str,
                     make_prompt):
    """Literal still via Workers AI free allowance (no card, no watermark).

    Default model SDXL-Lightning is listed at $0.00/step and takes native
    1080x1920, so cost is ~zero and no crop is needed. Free plan hard-stops
    at the 10k neuron cap (no overage billing possible). Returns None when
    creds are unset; raises on failure for the next fallback.
    """
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        return None
    prompt = make_prompt(beat_text, query, anchor)
    seed = random.randint(1, 999999)
    dest = ASSETS / f"cf_beat_{i}_{seed}.jpg"

    def _do():
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{CF_ACCOUNT_ID}/ai/run/{CF_IMAGE_MODEL}",
            headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
            json={"prompt": prompt, "seed": seed, "width": W, "height": H,
                  "num_steps": CF_NUM_STEPS,
                  "negative_prompt": (
                      "blurry, low quality, bad anatomy, extra limbs, duplicate person, "
                      "collage, split screen, multiple scenes, text, subtitles, watermark, "
                      "logo, daytime, sunlight, cheerful colors")},
            timeout=180)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if ct.startswith("image/"):
            raw = r.content  # SDXL models return raw PNG bytes
        else:  # FLUX-style JSON envelope with base64
            try:
                body = r.json()
            except Exception:
                raise ValueError(f"Cloudflare returned undecodable body ({ct})")
            if not isinstance(body, dict):
                raise ValueError(f"Cloudflare returned unexpected JSON ({ct})")
            if not body.get("success", False):
                raise ValueError(f"Cloudflare error: {body.get('errors')}")
            b64 = (body.get("result") or {}).get("image", "")
            if not b64:
                raise ValueError("Cloudflare returned no image")
            raw = base64.b64decode(b64)
        if len(raw) < 10_000:
            raise ValueError(f"Cloudflare image too small ({len(raw)}b)")
        dest.write_bytes(raw)
        return dest
    dest = retry("Cloudflare image", _do, tries=2, backoff=10)
    log(f"Beat {i + 1}: CF still generated ({dest.stat().st_size // 1024} KB).")
    return dest


def visuals_for_beats(beats: list[dict], niche) -> list[tuple]:
    """Per beat: STILLS_PER_BEAT cuts (Pexels stock -> CF still -> card).

    The niche decides the stock policy: ranking searches each beat's OWN
    query (literal item match); horror uses ambient B-roll from
    niche.AMBIENT_QUERIES, deliberately not story-matched. Returns
    [(clip, seek_or_None, beat_idx)]: stock fallbacks seek into different
    footage per half; durations stay per-beat and are split at render time.
    """
    clips = []
    used_pexels: set = set()
    use_beat = niche.USE_BEAT_QUERY
    # Continuity anchor from the opening beat so CF fallback stills share one
    # protagonist/setting instead of each inventing its own (horror only;
    # ranking stills are standalone subjects).
    anchor = niche.beat_anchor(beats)
    still_n = 0
    for i, b in enumerate(beats):
        parts = _split_beat(b["text"], STILLS_PER_BEAT)
        for j, part in enumerate(parts):
            clip, seek = None, None
            query = (b.get("query", "") if use_beat
                     else random.choice(niche.AMBIENT_QUERIES))
            stock = pexels_clip(query, used_pexels, mood=niche.USE_MOOD)
            if stock is not None:
                clip, seek = stock, _stock_offset(stock, j, len(parts))
            if clip is None:
                try:
                    clip = cloudflare_image(part, query, still_n, anchor,
                                            niche.image_prompt)
                except Exception as e:
                    warn(f"Beat {i + 1}.{j + 1} Cloudflare fallback failed ({e}).")
            if clip is None:
                warn(f"Beat {i + 1}.{j + 1} ('{query}'): no stock, using title card.")
                clip = beat_card(still_n, part)
            clips.append((clip, seek, i))
            still_n += 1
    log(f"Visuals: {len(clips)} cuts for {len(beats)} beats.")
    return clips


# Glob patterns for per-run FFmpeg intermediates. The pexels_*.mp4 cache
# is intentionally NOT listed: re-downloads waste time/bandwidth on re-runs.
INTERMEDIATE_PATTERNS = ("seg_*.mp4", "slide_*.png",
                         "cf_*.jpg", "bed.mp3", "concat.txt", "video_only.mp4",
                         "tts_*")


def cleanup_intermediates():
    """Delete per-run intermediates from assets/. Keeps the Pexels cache."""
    for pattern in INTERMEDIATE_PATTERNS:
        for p in ASSETS.glob(pattern):
            try:
                p.unlink()
            except OSError as e:
                log(f"cleanup warning: {p} ({e})")
    log("Cleaned intermediates (Pexels cache kept).")


# Overlay font candidates: DejaVuSans-Bold on CI runners, Arial Bold on
# local Windows. Probes are cached; misses warn once and never fail the run.
_RANK_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
               "C:/Windows/Fonts/arialbd.ttf",
               "C:/Windows/Fonts/arial.ttf")
_RANK_FONT = None
_RANK_FONT_WARNED = False
_HAS_DRAWTEXT = None


def _rank_font():
    """Usable overlay font path, or None (warns once)."""
    global _RANK_FONT, _RANK_FONT_WARNED
    if _RANK_FONT is None and not _RANK_FONT_WARNED:
        for p in _RANK_FONTS:
            if Path(p).exists():
                _RANK_FONT = p
                break
        if _RANK_FONT is None:
            _RANK_FONT_WARNED = True
            warn("No overlay font found — text overlays skipped.")
    return _RANK_FONT


def _has_drawtext() -> bool:
    """True when this FFmpeg build has the drawtext filter (cached, never raises)."""
    global _HAS_DRAWTEXT
    if _HAS_DRAWTEXT is None:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                               capture_output=True, text=True, timeout=30)
            _HAS_DRAWTEXT = "drawtext" in (r.stdout or "")
        except Exception:
            _HAS_DRAWTEXT = False
        if not _HAS_DRAWTEXT:
            warn("FFmpeg drawtext filter unavailable — text overlays skipped.")
    return _HAS_DRAWTEXT


# ---------- 4. Render with FFmpeg (CPU only) ----------
def _run_ffmpeg(cmd, step: str):
    """Run ffmpeg quietly, but log stderr tail on failure (else it stays hidden)."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()[-20:]
        log(f"ffmpeg failed at {step} (exit {e.returncode}):")
        for line in tail:
            log(f"  {line}")
        raise


def render(clips: list[tuple], audio: Path, bed: Path, srt: Path,
           durations: list[float], out_path: Path, niche,
           beats: list[dict] | None = None, items: list[str] | None = None,
           title: str = ""):
    """Normalize clips to 1080x1920, splitting each beat's duration evenly
    across its cuts, mux narration + bed + tiny bottom captions + niche
    overlay. `beats`/`items`/`title` feed the niche overlay ("" when N/A)."""
    # Build per-clip normalized segments (uniform fps/size/codec so concat is safe
    # even when Pexels clips have mixed frame rates).
    segs = []
    cuts_in_beat: dict = {}
    for c, _, beat_idx in clips:
        cuts_in_beat[beat_idx] = cuts_in_beat.get(beat_idx, 0) + 1
    for i, (c, seek, beat_idx) in enumerate(clips):
        beat_dur = durations[beat_idx] if beat_idx < len(durations) else 2.0
        seg_dur = max(2.0, beat_dur / max(1, cuts_in_beat.get(beat_idx, 1)))
        seg = ASSETS / f"seg_{i}.mp4"
        if c.suffix == ".jpg":
            # Ken Burns: slow zoom over the AI still (alternate in/out per beat).
            frames = max(60, int(seg_dur * 30))
            if i % 2 == 0:
                zb = (f"zoompan=z='min(zoom+0.0015,1.3)':d={frames}:"
                      f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}")
            else:
                zb = (f"zoompan=z='if(eq(in,1),1.3,max(zoom-0.0015,1.0))':d={frames}:"
                      f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}")
            _run_ffmpeg(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(c),
                         # cover-crop: any input aspect fills 1080x1920 undistorted
                         "-vf", f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
                                f"crop={W * 2}:{H * 2},{zb},fps=30",
                         "-t", str(seg_dur), "-pix_fmt", "yuv420p", "-c:v", "libx264",
                         "-preset", "veryfast", str(seg)], f"segment-{i}-kenburns")
        elif c.suffix == ".png":
            _run_ffmpeg(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(c),
                         "-t", str(seg_dur), "-vf", f"scale={W}:{H},fps=30",
                         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                         str(seg)], f"segment-{i}-slide")
        else:
            # Stock halves seek into different footage; loop guards short stock
            # so a segment can never run dry and truncate the narration.
            cmd = ["ffmpeg", "-y"]
            if seek:
                cmd += ["-ss", str(seek)]
            cmd += ["-stream_loop", "-1", "-i", str(c),
                    "-t", str(seg_dur),
                    "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                    "-an", str(seg)]
            _run_ffmpeg(cmd, f"segment-{i}-clip")
        segs.append(seg)
    concat = ASSETS / "concat.txt"
    concat.write_text("".join(f"file '{s.resolve()}'\n" for s in segs))
    video_only = ASSETS / "video_only.mp4"
    _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(concat), "-c:v", "libx264", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", str(video_only)], "concat")
    # Mux narration + bed + tiny bottom captions + niche overlay.
    # Bed is mixed quietly under the voice (normalize=0 keeps narration full).
    # The overlay lives here (not per-segment): only the finished timeline has
    # absolute timestamps for between(t,a,b) reveal windows.
    vf_subs = _subtitles_vf(srt)
    board = niche.board_filter(beats or [], items or [], durations,
                               _rank_font, _has_drawtext, title)
    vchain = (f"[0:v]{board}{vf_subs},format=yuv420p[vout]" if vf_subs
              else f"[0:v]{board}format=yuv420p[vout]")
    achain = "[1:a][2:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    cmd = ["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio), "-i", str(bed),
           "-filter_complex", f"{vchain};{achain}",
           "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
           "-shortest", str(out_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        if vf_subs and "subtitles" in (e.stderr or ""):
            warn("subtitles filter failed, rendering without burned captions.")
            cmd = ["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio), "-i", str(bed),
                   "-filter_complex", f"[0:v]{board}format=yuv420p[vout];{achain}",
                   "-map", "[vout]", "-map", "[aout]",
                   "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                   "-shortest", str(out_path)]
            _run_ffmpeg(cmd, "mux-no-subs")
        elif board and "drawtext" in (e.stderr or ""):
            warn("niche overlay filter failed, rendering without it.")
            plain = (f"[0:v]{vf_subs},format=yuv420p[vout]" if vf_subs
                     else "[0:v]format=yuv420p[vout]")
            cmd = ["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio), "-i", str(bed),
                   "-filter_complex", f"{plain};{achain}",
                   "-map", "[vout]", "-map", "[aout]",
                   "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                   "-shortest", str(out_path)]
            _run_ffmpeg(cmd, "mux-no-board")
        else:
            tail = (e.stderr or "").strip().splitlines()[-20:]
            log(f"ffmpeg failed at mux (exit {e.returncode}):")
            for line in tail:
                log(f"  {line}")
            raise
    log(f"Rendered {out_path} ({out_path.stat().st_size // 1024} KB)")


# ---------- 5. YouTube upload ----------
def upload(video: Path, meta: dict):
    cid, sec, ref = (os.getenv("YT_CLIENT_ID", ""), os.getenv("YT_CLIENT_SECRET", ""),
                     os.getenv("YT_REFRESH_TOKEN", ""))
    if not (cid and sec and ref):
        warn("YouTube creds missing -> skipping upload (video kept in out/).")
        return None
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    try:
        from google.auth.exceptions import RefreshError
    except ImportError:  # pragma: no cover - very old google-auth
        RefreshError = ()
    creds = Credentials(None, refresh_token=ref, client_id=cid,
                        client_secret=sec,
                        token_uri="https://oauth2.googleapis.com/token",
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": meta["title"][:100], "description": meta["description"],
                    "tags": meta.get("tags", [])[:10], "categoryId": "22"},
        "status": {"privacyStatus": VISIBILITY, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video), mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    # Resumable upload: next_chunk() resumes where it left off, so each
    # chunk is safely retried. Bad credentials fail fast (fatal, no retry).
    try:
        resp = None
        while resp is None:
            _, resp = retry("YouTube upload", req.next_chunk, tries=5, backoff=10,
                            fatal=RefreshError)
    except RefreshError as e:
        log("YouTube credentials rejected (bad/expired YT_REFRESH_TOKEN or wrong "
            f"YT_CLIENT_ID/YT_CLIENT_SECRET): {e}")
        log("Fix: re-run get_refresh_token.py locally and update GitHub Secrets.")
        raise
    log(f"Uploaded -> videoId={resp['id']}")
    return resp["id"]


# ---------- 5b. TikTok via Buffer (free, no TikTok audit needed) ----------
# Buffer's own TikTok app is already audited, so posts via Buffer can be
# public — unlike a self-registered TikTok Direct-Post app, which is
# SELF_ONLY/private until it passes TikTok's audit.
# Docs: https://developers.buffer.com (auth / create-video-post /
# hosting-media / posts-and-scheduling / character-limits / error-handling).
BUFFER_API_KEY = os.getenv("BUFFER_API_KEY", "").strip()
BUFFER_TIKTOK_CHANNEL_ID = os.getenv("BUFFER_TIKTOK_CHANNEL_ID", "").strip()
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "") or "epuxrxa5"
CLOUDINARY_UPLOAD_PRESET = os.getenv("CLOUDINARY_UPLOAD_PRESET", "") or "buffer_shorts"
BUFFER_API_URL = "https://api.buffer.com"
# TikTok captions with a video attached: max 2200 UTF-16 code units
# (developers.buffer.com/guides/character-limits.html).
TIKTOK_CAPTION_LIMIT = 2200


def _buffer_graphql(query: str, variables: dict | None = None) -> dict:
    """POST one GraphQL query to Buffer. Retries transport errors; raises on
    system errors (errors[] array: UNAUTHORIZED/FORBIDDEN/NOT_FOUND/etc.).
    Pass user text via `variables`, never interpolated into the query string
    (GraphQL strings forbid raw control chars like \\r — interpolation
    corrupts the document and Buffer answers 500 'Syntax Error')."""
    payload: dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables

    def do():
        r = requests.post(
            BUFFER_API_URL,
            headers={"Content-Type": "application/json",
                      "Authorization": f"Bearer {BUFFER_API_KEY}"},
            json=payload,
            timeout=60,
        )
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            # Attach the response body (truncated): Buffer's error text says
            # more than the status code (e.g. bad-key details on a 500).
            raise requests.HTTPError(f"{e} | body: {(r.text or '')[:300]}",
                                      response=r) from e
        return r.json()
    data = retry("Buffer API", do, tries=3, backoff=10)
    if isinstance(data, dict) and data.get("errors"):
        first = data["errors"][0]
        code = (first.get("extensions") or {}).get("code", "")
        raise ValueError(f"Buffer API error ({code}): {first.get('message', '?')}")
    return data.get("data") or {}


def _buffer_tiktok_channel() -> str | None:
    """TikTok channel id: explicit env wins, else auto-pick service==tiktok."""
    if BUFFER_TIKTOK_CHANNEL_ID:
        return BUFFER_TIKTOK_CHANNEL_ID
    data = _buffer_graphql(
        "query GetOrganizations { account { organizations { id name } } }")
    orgs = ((data.get("account") or {}).get("organizations")) or []
    if not orgs:
        raise ValueError("Buffer account has no organizations.")
    for org in orgs:
        oid = org.get("id", "")
        data = _buffer_graphql(
            f'query GetChannels {{ channels(input: {{ organizationId: "{oid}" }}) '
            "{ id displayName service } }")
        for ch in data.get("channels") or []:
            if str(ch.get("service", "")).lower() == "tiktok":
                log(f"Buffer: using TikTok channel '{ch.get('displayName', '?')}' ({ch['id']})")
                return ch["id"]
    raise ValueError("No TikTok channel found in Buffer (connect one at account.buffer.com/channels).")


def _cloudinary_upload(video: Path) -> str:
    """Host the mp4 at a public URL (Buffer has no upload endpoint — the
    asset url must be public HTTPS and stay live until the post publishes).
    Uses a Cloudinary unsigned preset (free tier); returns secure_url."""
    def do():
        with open(video, "rb") as f:
            r = requests.post(
                f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload",
                files={"file": (video.name, f, "video/mp4")},
                data={"upload_preset": CLOUDINARY_UPLOAD_PRESET},
                timeout=300,
            )
        r.raise_for_status()
        return r.json()
    resp = retry("Cloudinary upload", do, tries=3, backoff=10)
    url = resp.get("secure_url", "")
    if not url:
        raise ValueError(f"Cloudinary upload gave no secure_url: {resp}")
    return url


def _tiktok_caption(meta: dict) -> str:
    """title + description cut to 2200 UTF-16 code units (emoji count 2)."""
    text = f"{meta.get('title', '')}\n\n{meta.get('description', '')}".strip()
    units = sum(2 if ord(c) > 0xFFFF else 1 for c in text)
    if units <= TIKTOK_CAPTION_LIMIT:
        return text
    out, used = [], 0
    for c in text:
        w = 2 if ord(c) > 0xFFFF else 1
        if used + w > TIKTOK_CAPTION_LIMIT - 1:
            break
        out.append(c)
        used += w
    return "".join(out).rstrip() + "…"


def buffer_tiktok_upload(video: Path, meta: dict):
    """Queue the Short to TikTok via Buffer. Never raises (warn + None)."""
    if not BUFFER_API_KEY:
        warn("Buffer key missing -> skipping TikTok (YouTube path unaffected).")
        return None
    if not (CLOUDINARY_CLOUD_NAME and CLOUDINARY_UPLOAD_PRESET):
        warn("Cloudinary creds missing -> skipping TikTok "
             "(Buffer needs a public video URL and has no upload endpoint).")
        return None
    try:
        channel_id = _buffer_tiktok_channel()
        public_url = _cloudinary_upload(video)
        # Text travels as JSON variables (CreatePostInput): no manual
        # escaping, so quotes/emoji/newlines/\r in titles can't break the
        # GraphQL document the way f-string interpolation did (500 Syntax
        # Error on the sharks run).
        mutation = (
            "mutation CreatePost($input: CreatePostInput!) { "
            "createPost(input: $input) { "
            "... on PostActionSuccess { post { id dueAt } } "
            "... on LimitReachedError { message } "
            "... on InvalidInputError { message } "
            "... on MutationError { message } } }"
        )
        variables = {
            "input": {
                "text": _tiktok_caption(meta),
                "channelId": channel_id,
                "schedulingType": "automatic",
                "mode": "addToQueue",
                "assets": [{"video": {"url": public_url,
                                      "metadata": {"thumbnailOffset": 2000}}}],
            }
        }
        data = _buffer_graphql(mutation, variables)
        result = data.get("createPost") or {}
        post = result.get("post")
        if post and post.get("id"):
            log(f"TikTok queued via Buffer -> post={post['id']} dueAt={post.get('dueAt', '?')}")
            return post["id"]
        raise ValueError(f"Buffer rejected post: {result.get('message', '?')}")
    except Exception as e:
        warn(f"TikTok via Buffer skipped ({type(e).__name__}: {e}).")
        return None


def run(niche):
    """One full Short for the given niche module. Entry point for pipeline.py."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stem = OUT / f"{stamp}_short"
    # Non-secret effective config so Actions logs show what actually ran.
    if TTS_ENGINE == "gemini":
        tts_desc = (f"gemini(models={GEMINI_TTS_MODELS},voices={GEMINI_TTS_VOICES})"
                    "->edge")
    else:
        tts_desc = f"edge(voice={VOICE})"
    log(f"Config: mode={niche.MODE} tts={tts_desc} rate={TTS_RATE} pitch={TTS_PITCH} niche='{niche.NICHE_LABEL}' visibility={VISIBILITY} "
        f"min/max={MIN_AUDIO_SEC:g}/{MAX_AUDIO_SEC:g}s models={GEMINI_MODELS} "
        f"cf_model={CF_IMAGE_MODEL} "
        f"keys: gemini_keys={len(GEMINI_KEYS)} pexels={bool(PEXELS_KEY)} "
        f"cf={bool(CF_ACCOUNT_ID and CF_API_TOKEN)} "
        f"yt={(bool(os.getenv('YT_CLIENT_ID')) and bool(os.getenv('YT_CLIENT_SECRET')) and bool(os.getenv('YT_REFRESH_TOKEN')))} "
        f"buffer={bool(BUFFER_API_KEY)}")  # key presence only, never the key itself
    meta, slots = gen_script(niche)
    (stem.with_suffix(".json")).write_text(json.dumps(meta, indent=2))
    mp3, srt, beat_durs = gen_audio(meta["narration"], stem, meta.get("beats"))
    log(f"Audio: mp3={mp3.stat().st_size // 1024} KB srt_exists={srt.exists()} "
        f"srt_bytes={srt.stat().st_size if srt.exists() else 0}")
    dur = audio_duration(mp3)
    check_audio_duration(dur, len(meta["narration"].split()))
    beats = meta["beats"]
    clips = visuals_for_beats(beats, niche)
    if beat_durs and len(beat_durs) == len(beats) and sum(beat_durs) > 0:
        # Measured per-beat TTS timings (Gemini path): rescale to the real
        # muxed total so concat rounding can't drift visuals off the voice.
        scale = dur / sum(beat_durs)
        durations = [d * scale for d in beat_durs]
    else:
        # Weight each beat's screen time by its share of words (speech rate is
        # roughly constant, so each clip covers its own story moment).
        total_words = sum(len(b["text"].split()) for b in beats) or 1
        durations = [dur * len(b["text"].split()) / total_words for b in beats]
    bed = make_bed(dur)
    out_mp4 = stem.with_suffix(".mp4")
    try:
        render(clips, mp3, bed, srt, durations, out_mp4, niche,
               beats, list(meta.get("items", [])), meta.get("title", ""))
        video_id = upload(out_mp4, meta)
        # TikTok dual-post via Buffer: independent of YouTube, never fails the run.
        tiktok_post_id = buffer_tiktok_upload(out_mp4, meta)
    finally:
        cleanup_intermediates()
    # Record this video so future runs can't repeat it. Runs before any
    # video exists (render failure) are NOT recorded — nothing aired.
    # save_history never raises, so dedup memory can't fail the run.
    entry = {"date": stamp, "title": meta.get("title", ""),
             "niche": niche.MODE,
             "tags": meta.get("tags", [])[:10],
             "narration": meta.get("narration", ""),
             "videoId": video_id,
             "tiktokBufferPostId": tiktok_post_id}
    if isinstance(slots, dict):
        entry.update(niche.history_fields(slots, meta))
    save_history(entry)
    log("Done.")
