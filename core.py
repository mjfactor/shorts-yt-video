"""Shared Shorts-pipeline machinery: config, Gemini/TTS/visuals/FFmpeg/upload.

Niche-agnostic: every niche-specific behavior arrives via a `niche` module.
A niche module must provide (see niche_ranking.py for the reference):

    MODE                  str, e.g. "ranking" (also the history.json tag)
    NICHE_LABEL           str, human-readable channel description
    build_prompt(history) -> (prompt, slots)
    fallback_script(history) -> (meta, slots)
    validate_extra(data)  -> dict merged into validated meta ({} if none)
    sanitize_title(title, slots) -> str (channel title format enforcement)
    similarity_reason(data, slots, recent, item_recent=None) -> str | None
    format_history_line(h) -> str
    AVOID_INTRO, AVOID_OUTRO   str (history avoidance prompt wrapper)
    USE_BEAT_QUERY        bool (True: Pexels searches the beat's own query)
    AMBIENT_QUERIES       tuple (used when USE_BEAT_QUERY is False)
    USE_MOOD              bool (force horror mood on Pexels queries)
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


def _env_int(name: str, default: int) -> int:
    """Env int with safe fallback to default on missing/blank/garbage."""
    try:
        return int((os.getenv(name, "") or "").strip() or default)
    except (TypeError, ValueError):
        return default


# Failover pacing: short timeout + few tries so a dead endpoint fails over
# fast instead of burning minutes per dead model. GEMINI_TIMEOUT is the text
# path (script/topic); TTS voice calls run longer, so GEMINI_TTS_TIMEOUT
# (60-120 guidance) is used only by the per-beat TTS path. Bad values fall
# back to the defaults below.
GEMINI_TIMEOUT = max(1, _env_int("GEMINI_TIMEOUT", 40))
GEMINI_TTS_TIMEOUT = max(1, _env_int("GEMINI_TTS_TIMEOUT", 90))
GEMINI_TRIES = max(1, _env_int("GEMINI_TRIES", 2))
GEMINI_BACKOFF = max(0, _env_int("GEMINI_BACKOFF", 5))
# Tried in order; first working model wins. Override via GEMINI_MODELS="model-a,model-b".
GEMINI_MODELS = [m.strip() for m in
                 os.getenv("GEMINI_MODELS",
                            "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash,gemini-2.5-flash-lite").split(",")
                 if m.strip()]
# Pacing delay between Gemini TTS beats (default 15s to respect 3 RPM free-tier ceiling)
GEMINI_TTS_PACE_SEC = _env_int("GEMINI_TTS_PACE_SEC", 15)

# Standard safety thresholds calibrated for documentary/ranking facts (avoids false-positive topic blocks)
_GEMINI_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
]
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
# Background music volume (linear scale: 0.08 is subtle/balanced, 0.20 was old loud level):
BGM_VOLUME = float(os.getenv("BGM_VOLUME", "0.08"))
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
# Single narrator voice (default "Charon"); tone adapts dynamically to content
# and beat position. A comma-separated pool alternates voices per run.
GEMINI_TTS_VOICES = ([v.strip() for v in
                      os.getenv("GEMINI_TTS_VOICES", "Charon").split(",")
                      if v.strip()] or ["Charon"])
# Target speech rate in words per minute for Gemini TTS delivery (default 145 WPM -> ~80-85s for 200 words).
# Calibrated via gentle atempo adjustment. 0 = unadjusted natural speed.
GEMINI_TTS_TARGET_WPM = _env_int("GEMINI_TTS_TARGET_WPM", 145)
DEFAULT_GEMINI_TTS_STYLE = (
    "Read in a natural, articulate documentary voice with steady, engaging "
    "pacing (~145 words per minute), confident delivery, and authentic storytelling tone. "
    "Polished and dynamic, with clear, grounded emphasis on each reveal."
)
# Resonant alternate body style: authoritative countdown delivery, confident and weighty.
DEEP_GEMINI_TTS_STYLE = (
    "Read in a resonant, articulate documentary voice with steady, measured "
    "pacing and grounded authority. Polished and commanding, without dragging "
    "or sounding detached."
)
# Opening hook (beat 0) gets natural intrigue: draw the viewer in without hysterical hype.
GEMINI_TTS_HOOK_PREFIX = (
    "For this opening hook only, deliver with sharp intrigue, curiosity, and "
    "immediate engagement, drawing the viewer in with natural dramatic presence. "
)
# Empty/whitespace env counts as unset so a CI empty string never kills direction.
GEMINI_TTS_STYLE = (os.getenv("GEMINI_TTS_STYLE") or "").strip() or DEFAULT_GEMINI_TTS_STYLE
# True when the user explicitly set a non-blank style: it wins for the body
# and per-run tone alternation is skipped that run.
GEMINI_TTS_STYLE_EXPLICIT = bool((os.getenv("GEMINI_TTS_STYLE") or "").strip())
# Single voice selected for this run by pick_run_voice() (None = not yet picked).
RUN_VOICE: str | None = None
# Delivery tone for this run ("adaptive" | "deep" | None), picked by
# pick_run_tone(); None means an explicit GEMINI_TTS_STYLE won instead.
RUN_TONE: str | None = None
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def pick_tts_style(topic: str, text: str, beat_idx: int = -1, total_beats: int = 7) -> str:
    """Choose a narrator style tuned to the topic, narration content, and beat position."""
    haystack = f"{topic} {text}".lower()

    if any(k in haystack for k in [
        "planet", "space", "galaxy", "moon", "star", "ocean", "mountain",
        "building", "statue", "island", "river", "desert", "volcano", "pyramid"
    ]):
        mood = (
            "Read in a premium, cinematic documentary voice with steady, engaging pacing "
            "(~145 words per minute), clear authority, and an authentic sense of scale and wonder."
        )
    elif any(k in haystack for k in [
        "deadly", "danger", "horror", "haunted", "ghost", "creature",
        "predator", "attack", "monster", "scary", "venom", "poison", "toxic", "lethal"
    ]):
        mood = (
            "Read in a tense, gripping documentary narration voice with measured, engaging pacing "
            "(~145 words per minute), subtle intensity, and strong focus on the sheer danger."
        )
    elif any(k in haystack for k in [
        "fastest", "speed", "hypercar", "jet", "missile", "mach", "quick", "rapid"
    ]):
        mood = (
            "Read in a sharp, dynamic documentary voice with crisp articulation, "
            "forward momentum (~145 words per minute), and an energetic sense of speed."
        )
    elif any(k in haystack for k in [
        "animal", "wild", "snake", "shark", "elephant", "cat",
        "dog", "predator", "nature", "wildlife", "bird", "fish"
    ]):
        mood = (
            "Read in an engaging wildlife documentary voice with crisp pronunciation, "
            "steady engaging cadence (~145 words per minute), and grounded fascination."
        )
    else:
        mood = (
            "Read in a calm, confident documentary voice with natural cadence, "
            "authoritative delivery, and articulate pacing (~145 words per minute)."
        )

    # Dynamic beat-role adaptation: hook intrigue -> countdown momentum -> #1 reveal -> warm closer
    if beat_idx == 0:
        role = " Hook the listener with intriguing curiosity and immediate presence."
    elif 0 < beat_idx < total_beats - 2:
        role = " Keep a confident countdown momentum, making the facts clear and compelling."
    elif beat_idx == total_beats - 2:
        role = " Give maximum dramatic weight and awe to this final number one reveal."
    elif beat_idx == total_beats - 1:
        role = " Transition into a warm, conversational, and direct closing tone."
    else:
        role = ""

    return f"{mood}{role}"

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
HISTORY_KEEP = 110  # entries stored on disk (100 videos + headroom)
HISTORY_PROMPT_N = 20  # recent entries quoted in the prompt (advisory bans only)
# Hard programmatic freshness gates scan the FULL kept history below, not
# just the 20 quoted above: the prompt block stays small for token sanity
# (advisory), while DEDUP_WINDOW enforces no-repeat topics across 100 videos.
DEDUP_WINDOW = HISTORY_KEEP
# Shared-items (>=2) gate looks back this far only: across 110 videos common
# animals would false-positive constantly, so items enforce vs the last 30
# while the topic gate enforces vs the full window.
DEDUP_ITEM_WINDOW = 30
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


def _pick_avoiding(pool: tuple, recent: set, history: list[dict] | None = None):
    """Pool pick excluding recently used values (case-insensitive both sides).

    Fresh candidates are chosen at random. When the pool is exhausted (every
    topic aired within the window), falls back to least-recently-used: the
    pool topic whose last airing is oldest (or never aired), using `history`
    (newest-last, like load_history); without history, legacy random pick.
    """
    folded = {str(v).casefold() for v in recent}
    fresh = [v for v in pool if str(v).casefold() not in folded]
    if fresh:
        return random.choice(fresh)
    if history:
        last_seen: dict[str, int] = {}
        for pos, h in enumerate(history):
            t = str(h.get("topic", "")).casefold()
            if t:
                last_seen[t] = pos  # newest last: larger pos = more recent
        def _recency(v) -> int:
            return last_seen.get(str(v).casefold(), -1)  # never aired first
        oldest = min(_recency(v) for v in pool)
        return random.choice([v for v in pool if _recency(v) == oldest])
    return random.choice(pool)


def _story_signature(text: str) -> set:
    """Significant lowercase words, for overlap comparison."""
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "at",
            "it", "its", "is", "was", "are", "be", "i", "you", "he", "she",
            "they", "we", "my", "his", "her", "with", "for", "as", "that",
            "this", "but", "not", "so", "if", "then", "there", "here"}
    return {w.strip(".,!?;:\"'").lower() for w in text.split()
            if len(w) > 3} - stop


def similar_to_history(data: dict, slots: dict, history: list[dict], niche):
    """Return a repeat reason if the draft is too close to history, else None.

    Title + topic gates enforce vs the full DEDUP_WINDOW (no repeat across
    100 videos); the shared-items and story-overlap gates enforce vs the
    recent DEDUP_ITEM_WINDOW only (exact matches don't false-positive).
    """
    window = [h for h in history[-DEDUP_WINDOW:]
              if h.get("niche", "horror") == niche.MODE]
    if not window:
        return None
    recent_titles = {str(h.get("title", "")).strip().casefold() for h in window}
    if str(data.get("title", "")).strip().casefold() in recent_titles:
        return "title already used"
    item_window = [h for h in history[-DEDUP_ITEM_WINDOW:]
                   if h.get("niche", "horror") == niche.MODE]
    reason = niche.similarity_reason(data, slots, window, item_window)
    if reason is not None:
        return reason
    new_sig = _story_signature(
        f"{data.get('title', '')} {data.get('narration', '')}")
    for h in item_window:
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
    _nwords = len(narration.split())
    if _nwords < 180:
        raise ValueError(f"'narration' too short ({_nwords} words, need 180+ for ~80-85s at 145 WPM)")
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
    _bwords = sum(len(b["text"].split()) for b in norm_beats)
    if _bwords < 180:
        raise ValueError(f"'beats' cover too little text ({_bwords} words, need 180+ for ~80-85s at 145 WPM)")
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


def _fetch_gemini(url: str, payload: dict, tries: int | None = None,
                  backoff: int | None = None, timeout: int | None = None):
    """POST to Gemini with key rotation + retries. `url` is keyless.

    Sticks with the working key; invalid-key rejections (other 4xx) fail
    over to the next key immediately, as does a connect/read timeout (a
    timing-out endpoint won't heal in one backoff — fast 5xx responses may
    still retry). HTTP 429 is per-project quota, so it backs off with
    jitter on the SAME key instead of rotating (key-hopping would deepen
    exhaustion); 429 retries count toward the key's tries. Raises the last
    error when all keys fail. Defaults come from the GEMINI_TIMEOUT (text)
    / GEMINI_TTS_TIMEOUT (voice), GEMINI_TRIES, GEMINI_BACKOFF env tunables.
    """
    if tries is None:
        tries = GEMINI_TRIES
    if backoff is None:
        backoff = GEMINI_BACKOFF
    if timeout is None:
        timeout = GEMINI_TIMEOUT
    global _GEMINI_KEY_IDX
    if not GEMINI_KEYS:
        raise ValueError("GEMINI_API_KEY missing")
    n = len(GEMINI_KEYS)
    last_err: Exception | None = None
    server_only_failures = 0  # keys that exhausted attempts with only 5xx/timeout/network errors
    for k in range(n):
        idx = (_GEMINI_KEY_IDX + k) % n
        sep = "&" if "?" in url else "?"
        keyed = f"{url}{sep}key={GEMINI_KEYS[idx]}"
        key_server_only = True  # stays True unless this key sees a 4xx rejection
        for attempt in range(1, tries + 1):
            try:
                r = requests.post(keyed, json=payload, timeout=timeout)
                r.raise_for_status()
                _GEMINI_KEY_IDX = idx  # stick with the working key
                return r.json()
            except Exception as e:
                last_err = e
                status = _http_status(e)
                if status == 429:
                    # Per-project quota: rotating keys deepens exhaustion, so
                    # back off with jitter on the SAME key; the retry counts
                    # toward this key's tries, then normal failover applies.
                    key_server_only = False
                    if attempt == tries:
                        break
                    # Rate limits (RPM) require waiting for the sliding 1-minute window to reset
                    wait = max(20.0, backoff * attempt * 4) + random.uniform(1.0, 3.0)
                    log(f"Gemini key {idx + 1}/{n} rate-limited (429, "
                        f"attempt {attempt}/{tries}), waiting {wait:.1f}s for RPM quota window to reset...")
                    time.sleep(wait)
                    continue
                if status is not None and 400 <= status < 500 and status != 408:
                    key_server_only = False
                    if k < n - 1:
                        log(f"Gemini key {idx + 1}/{n} rejected (HTTP {status}), "
                            "failing over to next key...")
                    break  # 4xx won't heal with retries — try next key
                if isinstance(e, requests.exceptions.Timeout):
                    if k < n - 1:
                        log(f"Gemini key {idx + 1}/{n} timed out "
                            f"(attempt {attempt}/{tries}), failing over to next key...")
                    break  # timeouts won't heal in one backoff — try next key
                if attempt == tries:
                    break
                wait = backoff * attempt
                log(f"Gemini(key {idx + 1}/{n}) failed (attempt {attempt}/{tries}: {e}), "
                    f"retrying in {wait}s...")
                time.sleep(wait)
        if key_server_only:
            server_only_failures += 1
            if server_only_failures >= 2 and k < n - 1:
                log(f"Gemini endpoint looks down ({server_only_failures} keys failed "
                    "with only server-side errors), skipping remaining keys...")
                break
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
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.7,
                    "maxOutputTokens": 4096,
                },
                "safetySettings": _GEMINI_SAFETY_SETTINGS,
            }
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
                    time.sleep(2)
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
    meta, slots = niche.fallback_script(history)
    # Verdict for the record only: with no Gemini there is nothing to
    # regenerate with, so a hit still airs (warn, never raise).
    reason = similar_to_history(meta, slots, history, niche)
    if reason is not None:
        warn(f"Offline fallback repeats history ({reason}), airing anyway.")
    return meta, slots


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


def pick_run_voice(history_len: int | None, stamp: str = "") -> tuple[str, str]:
    """Voice for the whole run (default single Charon, no voice alternation).

    Pool is GEMINI_TTS_VOICES; multiple configured voices alternate
    deterministically: history entry count parity (even -> pool[0],
    odd -> pool[1]); when history is unavailable (None), fall back to the
    stamp's day-of-month parity. Returns (voice, reason) so run() can log
    why this voice was picked.
    """
    pool = GEMINI_TTS_VOICES or ["Charon"]
    if len(pool) == 1:
        return pool[0], "single voice configured (no alternation)"
    if history_len is not None:
        return pool[history_len % len(pool)], f"history parity (entries={history_len})"
    try:
        day = int(stamp[8:10])
    except (ValueError, IndexError):
        day = datetime.datetime.now().day
    return pool[day % len(pool)], "date-stamp day parity (history unavailable)"


def pick_run_tone(history_len: int | None, stamp: str = "") -> tuple[str, str]:
    """Delivery tone for the run. Default is adaptive narration-dependent delivery."""
    return "adaptive", "narration-dependent dynamic delivery"


def _gemini_tts_beat(text: str, voice: str, topic: str = "",
                     is_hook: bool = False, beat_idx: int = -1,
                     total_beats: int = 7) -> tuple[bytes, int]:
    """One beat of raw PCM audio via Gemini TTS. Returns (pcm, sample_rate).

    Tries GEMINI_TTS_MODELS in order; raises when all fail. The caller falls
    back to edge-tts for the WHOLE narration on any failure — per-beat
    retries would multiply a total outage by the beat count. Beat text is
    always sent verbatim; delivery style adapts dynamically per beat.
    """
    if not GEMINI_KEYS:
        raise ValueError("GEMINI_API_KEY missing (TTS_ENGINE=gemini needs it)")
    if topic:
        base_style = pick_tts_style(topic, text, beat_idx, total_beats)
    elif GEMINI_TTS_STYLE_EXPLICIT:
        base_style = GEMINI_TTS_STYLE
    elif (RUN_TONE or "adaptive") == "deep":
        base_style = DEEP_GEMINI_TTS_STYLE
    else:
        base_style = pick_tts_style("", text, beat_idx, total_beats) if beat_idx >= 0 else DEFAULT_GEMINI_TTS_STYLE

    prefix = (GEMINI_TTS_HOOK_PREFIX if (is_hook or beat_idx == 0) and not topic else "")
    prompt = f"{prefix}{base_style} Say the following text:\n\n{text}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
        "safetySettings": _GEMINI_SAFETY_SETTINGS,
    }
    last_err: Exception | None = None
    for model in GEMINI_TTS_MODELS:
        try:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent")
            raw = _fetch_gemini(url, payload, timeout=GEMINI_TTS_TIMEOUT)
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
        if i > 0 and tag == "gemini" and GEMINI_TTS_PACE_SEC > 0:
            log(f"Gemini TTS pacing: waiting {GEMINI_TTS_PACE_SEC}s between beats to respect RPM limits...")
            time.sleep(GEMINI_TTS_PACE_SEC)
        try:
            voice, seg = synth(i, b, topic=topic, total_beats=len(beats))
        except TypeError:
            try:
                voice, seg = synth(i, b, topic)
            except TypeError:
                voice, seg = synth(i, b)
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


def _gemini_synth(i: int, beat: dict, topic: str = "", total_beats: int = 7):
    """Gemini per-beat synth step: (voice, seg_mp3). Raises on failure.

    Single voice per run (RUN_VOICE picked in run()); delivery style adapts
    dynamically to the beat position and topic narration. Calibrates to
    GEMINI_TTS_TARGET_WPM via gentle atempo adjustment.
    """
    voice = RUN_VOICE or (GEMINI_TTS_VOICES[i % len(GEMINI_TTS_VOICES)]
                          if GEMINI_TTS_VOICES else "Charon")
    pcm, rate = _gemini_tts_beat(beat["text"], voice, topic, is_hook=(i == 0),
                                 beat_idx=i, total_beats=total_beats)
    raw_pcm = ASSETS / f"tts_beat_{i}.pcm"
    raw_seg = ASSETS / f"tts_beat_{i}_raw.mp3"
    seg = ASSETS / f"tts_beat_{i}.mp3"
    raw_pcm.write_bytes(pcm)
    _run_ffmpeg(["ffmpeg", "-y", "-f", "s16le", "-ar", str(rate), "-ac", "1",
                 "-i", str(raw_pcm), "-c:a", "libmp3lame", str(raw_seg)],
                f"tts-beat-{i}-pcm")

    target_wpm = GEMINI_TTS_TARGET_WPM
    if target_wpm > 0:
        words = len(beat["text"].split())
        raw_dur = audio_duration(raw_seg)
        if raw_dur > 0 and words > 0:
            current_wpm = (words / raw_dur) * 60.0
            factor = target_wpm / current_wpm
            factor = max(0.85, min(1.35, factor))
            if abs(factor - 1.0) >= 0.03:
                _run_ffmpeg(["ffmpeg", "-y", "-i", str(raw_seg),
                             "-filter:a", f"atempo={factor:.3f}",
                             "-c:a", "libmp3lame", str(seg)],
                            f"tts-beat-{i}-atempo")
                return voice, seg

    if raw_seg.exists():
        if seg.exists():
            seg.unlink()
        raw_seg.rename(seg)
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
    esc = (str(target.resolve()).replace("\\", "/").replace("'", "'\\''").replace(":", "\\:").replace(",", "\\,"))
    # Plain path, no force_style: commas in force_style break some builds.
    return f"subtitles='{esc}'"


# Optional override: put your own loopable track(s) at bgm.mp3 (repo root,
# e.g. from the YouTube Audio Library) or assets/bgm.mp3 (local only).
# Numbered tracks (bgm1.mp3..bgm9.mp3) play in strict rotation: run() passes
# its history entry count as the slot, so track = pool[slot % len(pool)]
# over the sorted pool — every track plays before any repeats, and no two
# back-to-back runs share a bed. slot=None (history unreadable) falls back
# to a random pick, preserving the old stateless behavior for that edge case.
def _bgm_pool() -> list:
    """Available custom bed tracks. Repo-root bgm*.mp3 wins (CI-visible);
    assets/bgm*.mp3 is the local-only fallback. Sorted for stable logs."""
    pool = sorted(Path(".").glob("bgm*.mp3"))
    if not pool:
        pool = sorted(ASSETS.glob("bgm*.mp3"))
    return [p for p in pool if p.is_file()]


def make_bed(duration: float, slot: int | None = None) -> tuple[Path, str | None]:
    """Background audio matching narration length. Never fails the run.

    Returns (bed_mp3, track_filename|None): the filename (drone -> None) is
    recorded in history so rotation stays auditable. A numeric slot picks
    pool[slot % len(pool)] (strict rotation over the sorted pool); slot=None
    keeps the legacy random pick.
    """
    bed = ASSETS / "bed.mp3"
    pool = _bgm_pool()
    custom = None
    if pool:
        if slot is not None:
            idx = slot % len(pool)
            custom = pool[idx]
            log(f"Bed: track {idx + 1}/{len(pool)} {custom.name} by rotation")
        else:
            custom = random.choice(pool)
            log(f"Bed: looping custom track {custom} (1 of {len(pool)} in pool)")
    if custom is not None:
        _run_ffmpeg(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(custom),
                     "-t", str(duration), "-af", f"volume={BGM_VOLUME:.2f}",
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
    return bed, (custom.name if custom is not None else None)


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


_QUERY_STOPWORDS = frozenset({
    "a", "an", "the", "in", "on", "of", "at", "to", "for",
    "with", "from", "and", "or", "is", "are", "its", "it",
    "cinematic", "close", "creeping", "slowly", "swimming",
    "underwater", "slithering",
})

# Scenery/habitat words that can dilute stock queries away from the actual subject
_GENERIC_HABITAT_WORDS = frozenset({
    "desert", "forest", "savanna", "savannah", "jungle", "ocean", "water",
    "river", "sea", "mountain", "mountains", "sky", "underwater", "swamp",
    "grassland", "plains", "trees", "dunes",
})

# Domestic/indoor indicators to reject when the topic is wild animals
_DOMESTIC_DISQUALIFIERS = frozenset({
    "indoor", "indoors", "sofa", "couch", "living room", "pet", "pets",
    "sweater", "costume", "leash", "collar", "bedroom", "blanket",
    "domestic", "housecat", "kitten", "puppy", "furniture",
    "home", "apartment", "rug", "carpet",
})


def _is_wild_topic(topic: str) -> bool:
    """True if topic is about wildlife, nature, or wild creatures."""
    t = (topic or "").lower()
    wild_words = {
        "wild", "wildlife", "nature", "predator", "predators", "safari",
        "jungle", "forest", "ocean", "sea", "marine", "deep sea",
        "deadliest", "poisonous", "venomous", "carnivore",
    }
    return any(w in t for w in wild_words)


def _clean_stock_query(q: str, item_name: str = "") -> str:
    """Strip generic habitat/scenery words from query unless they are part of item_name."""
    clean_q = (q or "").strip()
    if not clean_q:
        return ""
    item_tokens = set(re.findall(r"[a-z0-9]+", (item_name or "").lower()))
    words = clean_q.split()
    kept = []
    for w in words:
        wl = re.sub(r"[^a-z0-9]", "", w.lower())
        if wl in _GENERIC_HABITAT_WORDS and wl not in item_tokens:
            continue
        kept.append(w)
    return " ".join(kept) if kept else clean_q


def _has_domestic_disqualifier(haystack: str) -> bool:
    """Check if text mentions domestic/indoor keywords."""
    h = (haystack or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", h))
    return bool(tokens & _DOMESTIC_DISQUALIFIERS)


def _query_words(q: str) -> list[str]:
    """Lowercase alphanum tokens minus stopwords."""
    return [w for w in re.findall(r"[a-z0-9]+", (q or "").lower())
            if w not in _QUERY_STOPWORDS]


def _query_variants(q: str) -> list[str]:
    """Full query + drop-last-word backoff until 2 words remain (max 3)."""
    full = (q or "").strip()
    if not full:
        return []
    words = full.split()
    if len(words) <= 2:
        return [full]
    variants = [full]
    while len(words) > 2 and len(variants) < 3:
        words = words[:-1]
        variants.append(" ".join(words))
    seen: list[str] = []
    for v in variants:
        if v not in seen:
            seen.append(v)
    return seen


def _score_text(haystack, words: list[str]) -> int:
    """Count of query words present as substrings in haystack."""
    h = (haystack or "").lower()
    return sum(1 for w in words if w and w in h)


def _score_candidate(alt: str, url: str, words: list[str], phrase: str = "") -> int:
    """Score candidate text against query words, with extra bonus for exact phrase."""
    h_alt = (alt or "").lower()
    h_url = (url or "").lower()
    tokens_alt = set(re.findall(r"[a-z0-9]+", h_alt))
    tokens_url = set(re.findall(r"[a-z0-9]+", h_url))
    alt_hits = sum(1 for w in words if w and (w in tokens_alt or (len(w) >= 6 and w in h_alt)))
    url_hits = sum(1 for w in words if w and (w in tokens_url or (len(w) >= 6 and w in h_url)))
    score = 2 * alt_hits + url_hits
    if phrase and len(phrase.split()) > 1:
        phrase_clean = phrase.lower()
        if phrase_clean in h_alt or phrase_clean in re.sub(r"[-_]", " ", h_alt):
            score += 6
        elif phrase_clean in re.sub(r"[-_]", " ", h_url):
            score += 4
    return score


_BOGUS_FAMILY_WORDS = frozenset({
    "said", "say", "says", "told", "talk", "speak", "go", "goes", "went", "get", "gets",
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by",
    "al", "el", "del", "de", "da", "di", "du", "la", "le", "les", "san", "santa", "von", "van", "der", "den",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "first", "second", "third",
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "jr", "sr",
    "big", "large", "small", "tiny", "new", "old", "top", "pro", "max", "ultra", "plus", "super", "hyper",
    "gt", "gts", "gtr", "rs", "amg", "sv", "evo", "svj", "ss", "v8", "v10", "v12", "edition", "series",
})

_DOMAIN_SYNONYMS: dict[str, set[str]] = {
    # Watercraft / Marine
    "yacht": {
        "yacht", "yachts", "superyacht", "superyachts", "megayacht", "megayachts",
        "boat", "boats", "ship", "ships", "vessel", "vessels", "watercraft",
        "sailing", "sail", "ocean", "sea", "marine", "deck", "harbor", "marina", "port", "water",
    },
    "ship": {
        "ship", "ships", "vessel", "vessels", "boat", "boats", "cruise", "cruises",
        "freighter", "cargo", "container", "tanker", "ocean", "sea", "harbor", "port", "marine", "sailing", "water",
    },
    "boat": {
        "boat", "boats", "ship", "ships", "vessel", "vessels", "yacht", "yachts",
        "watercraft", "sailing", "water", "sea", "ocean", "lake", "river", "harbor",
    },
    "submarine": {
        "submarine", "submarines", "sub", "subs", "submersible", "underwater",
        "naval", "navy", "ocean", "sea", "torpedo", "water",
    },
    "shipwreck": {
        "shipwreck", "shipwrecks", "wreck", "wrecks", "sunken", "underwater",
        "ocean", "sea", "submerged", "coral", "reef", "ship",
    },
    # Aviation
    "airplane": {
        "plane", "planes", "airplane", "airplanes", "aircraft", "jet", "jets",
        "aviation", "flight", "flying", "airliner", "airliners", "airport", "runway", "sky",
    },
    "plane": {
        "plane", "planes", "airplane", "airplanes", "aircraft", "jet", "jets",
        "aviation", "flight", "flying", "airliner", "airliners", "airport", "runway", "sky",
    },
    "aircraft": {
        "plane", "planes", "airplane", "airplanes", "aircraft", "jet", "jets",
        "aviation", "flight", "flying", "airliner", "airliners", "airport", "runway", "sky",
    },
    "jet": {
        "jet", "jets", "fighter", "plane", "planes", "aircraft", "airplane", "airplanes",
        "aviation", "cockpit", "supersonic", "sky", "flight",
    },
    "helicopter": {
        "helicopter", "helicopters", "chopper", "choppers", "rotor", "rotors",
        "aircraft", "aviation", "flight", "sky",
    },
    # Ground Vehicles
    "car": {
        "car", "cars", "automobile", "automobiles", "supercar", "supercars",
        "hypercar", "hypercars", "vehicle", "vehicles", "auto", "motor", "drive", "driving", "road", "racing",
    },
    "supercar": {
        "car", "cars", "automobile", "automobiles", "supercar", "supercars",
        "hypercar", "hypercars", "vehicle", "vehicles", "auto", "motor", "drive", "driving", "road", "speed",
    },
    "truck": {
        "truck", "trucks", "pickup", "pickups", "vehicle", "vehicles", "semi", "lorry", "road", "drive",
    },
    "motorcycle": {
        "motorcycle", "motorcycles", "motorbike", "motorbikes", "bike", "bikes", "moto", "rider", "riding", "road",
    },
    "train": {
        "train", "trains", "locomotive", "locomotives", "railway", "railroad",
        "rail", "rails", "metro", "subway", "bullet", "tracks",
    },
    "tank": {
        "tank", "tanks", "armor", "armored", "armour", "military", "army",
        "combat", "cannon", "turret", "tracks", "vehicle",
    },
    # Space & Science
    "rocket": {
        "rocket", "rockets", "spacecraft", "spaceship", "launch", "space",
        "nasa", "booster", "orbit", "astronaut", "propulsion",
    },
    "telescope": {
        "telescope", "telescopes", "observatory", "astronomy", "space", "optics", "stars",
    },
    "planet": {
        "planet", "planets", "solar", "system", "space", "astronomy", "orbit", "cosmos",
    },
    "moon": {
        "moon", "moons", "lunar", "orbit", "space", "astronomy", "crater", "craters",
    },
    # Architecture & Civil Engineering
    "building": {
        "building", "buildings", "skyscraper", "skyscrapers", "architecture",
        "tower", "towers", "edifice", "facade", "structure", "structures", "city", "skyline",
    },
    "skyscraper": {
        "skyscraper", "skyscrapers", "building", "buildings", "tower", "towers",
        "architecture", "highrise", "high-rise", "skyline", "city",
    },
    "tower": {
        "tower", "towers", "building", "buildings", "skyscraper", "skyscrapers",
        "structure", "structures", "spire", "architecture",
    },
    "bridge": {
        "bridge", "bridges", "suspension", "viaduct", "overpass", "arch",
        "cables", "crossing", "river", "span", "water",
    },
    "castle": {
        "castle", "castles", "fortress", "fortresses", "fort", "palace", "palaces",
        "citadel", "medieval", "stone", "walls",
    },
    "palace": {
        "palace", "palaces", "castle", "castles", "royal", "mansion", "estate",
        "residence", "architecture", "court",
    },
    "temple": {
        "temple", "temples", "shrine", "shrines", "monastery", "sanctuary", "ancient", "stone", "architecture",
    },
    "church": {
        "church", "churches", "cathedral", "cathedrals", "basilica", "chapel", "architecture",
    },
    "mosque": {
        "mosque", "mosques", "minaret", "minarets", "dome", "architecture",
    },
    "dam": {
        "dam", "dams", "reservoir", "hydroelectric", "spillway", "water", "concrete", "lake", "river",
    },
    "tunnel": {
        "tunnel", "tunnels", "subway", "highway", "rail", "underground", "road",
    },
    "airport": {
        "airport", "airports", "terminal", "runway", "tarmac", "aviation", "airplane", "airplanes",
    },
    "lighthouse": {
        "lighthouse", "lighthouses", "beacon", "coast", "coastal", "ocean", "sea", "tower",
    },
    "fountain": {
        "fountain", "fountains", "water", "spray", "cascade", "pool", "monument",
    },
    "coaster": {
        "roller coaster", "rollercoaster", "coaster", "coasters", "ride", "rides", "amusement", "tracks",
    },
    "wheel": {
        "ferris wheel", "ferris", "wheel", "wheels", "amusement", "carnival", "fair",
    },
    "aquarium": {
        "aquarium", "aquariums", "tank", "fish", "marine", "underwater", "reef", "ocean",
    },
    "museum": {
        "museum", "museums", "exhibit", "exhibition", "gallery", "art", "sculpture", "artifact",
    },
    "library": {
        "library", "libraries", "books", "bookshelves", "reading",
    },
    "stadium": {
        "stadium", "stadiums", "arena", "arenas", "pitch", "field", "sports",
    },
}

_ANIMAL_DOMAINS: dict[str, set[str]] = {
    "snake": {"snake", "snakes", "serpent", "serpents", "reptile", "reptiles", "viper", "vipers", "cobra", "cobras", "python", "mamba", "venomous"},
    "spider": {"spider", "spiders", "arachnid", "arachnids", "tarantula", "web"},
    "shark": {"shark", "sharks", "predator", "ocean", "sea", "marine", "underwater", "fin", "fish"},
    "bird": {"bird", "birds", "avian", "feather", "feathers", "flight", "flying", "wing", "wings", "eagle", "hawk", "owl", "falcon"},
    "eagle": {"eagle", "eagles", "bird", "birds", "raptor", "flying", "flight"},
    "owl": {"owl", "owls", "bird", "birds", "raptor", "nocturnal", "feathers"},
    "cat": {"cat", "cats", "feline", "felines", "kitten", "panther", "tiger", "lion", "leopard", "cheetah", "jaguar"},
    "dog": {"dog", "dogs", "canine", "canines", "puppy", "puppies", "hound", "retriever", "terrier"},
    "bear": {"bear", "bears", "grizzly", "polar bear", "cub", "predator", "fur"},
    "crocodile": {"crocodile", "crocodiles", "alligator", "alligators", "reptile", "reptiles", "swamp", "river"},
    "reptile": {"reptile", "reptiles", "lizard", "lizards", "snake", "snakes", "iguana", "gecko"},
    "fish": {"fish", "fishes", "underwater", "ocean", "sea", "marine", "aquarium", "reef", "swimming"},
    "whale": {"whale", "whales", "cetacean", "ocean", "sea", "marine", "mammal"},
    "frog": {"frog", "frogs", "amphibian", "amphibians", "toad", "toads", "jungle"},
    "crab": {"crab", "crabs", "crustacean", "ocean", "sea", "beach", "sand", "shell"},
    "insect": {"insect", "insects", "bug", "bugs", "beetle", "butterfly", "moth", "ant", "bee"},
    "dinosaur": {"dinosaur", "dinosaurs", "fossil", "fossils", "jurassic", "prehistoric", "t-rex"},
}


def _get_topic_domain_info(topic: str) -> tuple[str, set[str], bool]:
    """Extract (base_subject, domain_keywords, is_object_topic) from topic."""
    t_clean = (topic or "").lower()
    t_tokens = set(re.findall(r"[a-z0-9]+", t_clean))

    for k, syns in _DOMAIN_SYNONYMS.items():
        if (k in t_tokens or (k.endswith("s") and k[:-1] in t_tokens)
                or (k + "s") in t_tokens or (" " in k and k in t_clean)):
            return k, syns, True

    for k, syns in _ANIMAL_DOMAINS.items():
        if (k in t_tokens or (k.endswith("s") and k[:-1] in t_tokens)
                or (k + "s") in t_tokens or (" " in k and k in t_clean)):
            return k, syns, False

    tqueries = _topic_stock_queries(topic)
    if tqueries:
        subj = tqueries[-1][0]
        reqs = set(tqueries[-1][1])
        return subj, reqs, False

    return "", set(), False


def _item_require_words(item: str, domain: set[str] | None = None) -> dict:
    """Structured requirements for a ranked item name.

    Head: the primary subject noun (last token, e.g. 'cat' in 'Sand Cat').
    Modifiers: distinguishing qualifiers (e.g. 'sand' in 'Sand Cat').
    Phrase: the full item name string.
    Domain: allowed topic domain keywords (e.g. yacht/marine synonyms).
    """
    try:
        clean = (item or "").lower().strip()
        tokens = re.findall(r"[a-z0-9]+", clean)
        if not tokens:
            return {}
        head = tokens[-1]
        heads = [head]
        if head.endswith("s") and len(head) > 3:
            heads.append(head[:-1])
        else:
            heads.append(head + "s")
        modifiers = tokens[:-1]
        return {
            "heads": heads,
            "modifiers": modifiers,
            "phrase": clean,
            "domain": domain or set(),
        }
    except Exception:
        return {}


def _meets_require(haystack: str, require: Any) -> bool:
    """True when no gate applies or candidate text satisfies the requirement."""
    if not require:
        return True
    h = (haystack or "").lower()
    h_tokens = set(re.findall(r"[a-z0-9]+", h))
    if not h_tokens:
        return False
    h_clean = re.sub(r"[-_]", " ", h)

    if isinstance(require, dict):
        domain = require.get("domain")
        if domain:
            # Candidate MUST match at least one domain keyword to prevent homonym pollution
            # (e.g. rejecting solar eclipses or city traffic when ranking yachts)
            has_domain = (any(d in h_tokens for d in domain if " " not in d) or
                          any(d in h_clean for d in domain if " " in d))
            if not has_domain:
                return False

        heads = require.get("heads", [])
        modifiers = require.get("modifiers", [])
        phrase = require.get("phrase", "")
        # Multi-word exact phrase match (e.g. 'sand cat' or 'burj khalifa')
        if phrase and len(phrase.split()) > 1 and (phrase in h or phrase in h_clean):
            return True
        # Head noun is strictly mandatory as an exact word token
        if heads and not any(hd in h_tokens for hd in heads):
            return False
        # If item has distinguishing modifiers, at least one modifier must be an exact word token
        if modifiers and not any(m in h_tokens for m in modifiers):
            return False
        return True

    if isinstance(require, (list, tuple, set)):
        for w in require:
            w_str = str(w).lower().strip()
            if " " in w_str:
                if w_str in h or w_str in h_clean:
                    return True
            else:
                if w_str in h_tokens or (len(w_str) >= 6 and w_str in h):
                    return True
        return False

    return True


def _family_query_for_item(item: str, topic: str = "") -> tuple[str, list[str]]:
    """(query, require) guessing the subject family from an item name.

    Generic, no species dictionary: ranking items are '<adjective> <family>'
    shaped, so the last word is the family guess ('Green Anaconda' ->
    'anaconda', 'Bald Eagle' -> 'eagle', 'Eiffel Tower' -> 'tower').
    For wild topics with domestic-ambiguous animals ('cat', 'dog', 'horse', 'pig'),
    prepends 'wild' so fallback doesn't pull house pets.
    For object topics, proper names with no category noun (e.g. 'Al Said', 'Eclipse',
    'Dubai', 'Chiron') return empty so the pipeline cleanly falls back to topic stock.
    """
    try:
        tokens = re.findall(r"[a-z0-9]+", (item or "").lower())
        if not tokens:
            return "", []
        fam = tokens[-1]
        if fam in _BOGUS_FAMILY_WORDS or len(fam) < 3:
            return "", []

        _subj, domain_kw, is_obj = _get_topic_domain_info(topic)
        if is_obj:
            if fam not in domain_kw:
                return "", []
            return fam, [fam]

        if _is_wild_topic(topic) and fam in {"cat", "dog", "pig", "horse", "duck", "rabbit", "goat", "sheep"}:
            return f"wild {fam}", [f"wild {fam}", fam]
        return fam, [fam]
    except Exception:
        return "", []


_TOPIC_META_NOUNS = frozenset({
    "breeds", "breed", "species", "types", "type", "kinds", "kind",
    "varieties", "variety", "categories", "category",
})
_NON_PLURAL_NOUNS = frozenset({
    "this", "gas", "glass", "grass", "boss", "cross", "crisis", "basis",
    "status", "circus", "canvas", "apparatus", "series", "species",
})
_SUPERLATIVES = {
    "largest": "large", "biggest": "big", "fastest": "fast", "tallest": "tall",
    "deepest": "deep", "deadliest": "deadly", "heaviest": "heavy", "wild": "wild",
    "scariest": "scary", "oldest": "old", "strongest": "strong", "longest": "long",
    "highest": "high", "smallest": "small", "coldest": "cold", "hottest": "hot",
    "giant": "giant", "massive": "massive",
}


def _topic_stock_queries(topic: str) -> list[tuple[str, list[str]]]:
    """Prioritized list of (query, require) fallback pairs for the topic's subject.

    Overcomes Pexels gaps by extracting the actual underlying subject noun:
    - Avoids meta-nouns like 'breeds' or 'species' (e.g. 'dog breeds' -> 'dog').
    - Skips adjectives ending with '-ous' (e.g. 'venomous snakes' -> 'snake').
    - Maps superlatives to natural stock search terms (e.g. 'largest dogs' -> 'large dog').
    """
    try:
        raw_tokens = re.findall(r"[a-z0-9]+", (topic or "").lower())
        tokens = [w for w in raw_tokens if w not in _QUERY_STOPWORDS and w not in {
            "world", "earth", "ever", "all", "time", "history", "top", "5", "five", "most"
        }]
        if not tokens:
            return []

        primary_token = None
        qualifier = None

        # Check for superlative anywhere in tokens
        for w in tokens:
            if w in _SUPERLATIVES:
                qualifier = _SUPERLATIVES[w]
                break
            elif w.endswith(("ous", "ive", "ful")):
                qualifier = w
                break

        # Check if there is a meta noun (e.g. 'breeds', 'species')
        for idx, w in enumerate(tokens):
            if w in _TOPIC_META_NOUNS and idx > 0:
                primary_token = tokens[idx - 1]
                if not qualifier and idx > 1 and tokens[idx - 2] not in _QUERY_STOPWORDS:
                    qualifier = tokens[idx - 2]
                break

        # If no meta noun, scan from right to left for plural noun
        if not primary_token:
            for idx in range(len(tokens) - 1, -1, -1):
                w = tokens[idx]
                if w.endswith("ous") or w in _NON_PLURAL_NOUNS:
                    continue
                if len(w) >= 3 and (w.endswith("s") or w in {"aircraft", "sheep", "deer", "fish"}):
                    primary_token = w
                    if not qualifier and idx > 0 and tokens[idx - 1] not in _QUERY_STOPWORDS and not tokens[idx - 1].isdigit():
                        qualifier = tokens[idx - 1]
                    break

        if not primary_token and tokens:
            primary_token = tokens[-1]
            if not qualifier and len(tokens) > 1 and not tokens[-2].isdigit():
                qualifier = tokens[-2]

        p = primary_token or ""
        if p.endswith("ies") and len(p) > 4:
            sing = p[:-3] + "y"
            plur = p
        elif p.endswith(("ses", "xes", "ches", "shes")) and len(p) > 4:
            sing = p[:-2]
            plur = p
        elif p.endswith("s") and not p.endswith("ss") and len(p) > 3 and not p.endswith("ous"):
            sing = p[:-1]
            plur = p
        else:
            sing = p
            plur = p if p in {"aircraft", "sheep", "deer", "fish"} else (p + "s")

        clean_qual = qualifier or ""
        if clean_qual in _SUPERLATIVES:
            clean_qual = _SUPERLATIVES[clean_qual]

        queries = []
        if clean_qual and clean_qual not in {"most", "the", sing}:
            q_desc = f"{clean_qual} {sing}"
            req_desc = [f"{clean_qual} {plur}", f"{clean_qual} {sing}", plur, sing]
            queries.append((q_desc, req_desc))

        q_base = sing
        req_base = [plur, sing] if plur != sing else [sing]
        if (q_base, req_base) not in queries:
            queries.append((q_base, req_base))
        return queries
    except Exception:
        return []


def _topic_query_for_topic(topic: str) -> tuple[str, list[str]]:
    """(query, require) for the topic's generic subject noun, or ("", [])."""
    queries = _topic_stock_queries(topic)
    return queries[0] if queries else ("", [])


def _best_video(query: str, used: set | None = None, mood: bool = True,
                require: Any = None, is_wild: bool = False, phrase: str = ""):
    """Best literal-match stock video for one query (read-only on `used`).

    Returns (dest|None, raw_score, commit_key|None).
    """
    if not PEXELS_KEY:
        return None, 0, None
    try:
        words = _query_words(query)
        threshold = min(2, len(words)) if len(words) > 1 else 1
        best_video = None
        best_pick_file = None
        best_score = -1
        for variant in _query_variants(query):
            def _search(variant=variant):
                r = requests.get(
                    "https://api.pexels.com/videos/search",
                    headers={"Authorization": PEXELS_KEY},
                    params={"query": _mood_query(variant) if mood else variant,
                            "per_page": 8,
                            "orientation": "portrait", "size": "medium"},
                    timeout=30,
                )
                r.raise_for_status()
                return r
            r = retry("Pexels search", _search, tries=3, backoff=5)
            for v in r.json().get("videos", []):
                if used is not None and v.get("id") in used:
                    continue
                url = v.get("url", "")
                v_tags = " ".join(t.get("name", "") if isinstance(t, dict) else str(t) for t in v.get("tags", []))
                haystack = f"{url} {v_tags}".strip() if v_tags else url
                if is_wild and _has_domestic_disqualifier(haystack):
                    continue
                if not _meets_require(haystack, require):
                    continue
                score = _score_candidate(v_tags or url, url, words, phrase)
                if score < threshold:
                    continue
                cands = sorted((f for f in v.get("video_files", []) if f.get("link")),
                               key=lambda f: f.get("width", 0))
                big_enough = [f for f in cands if f.get("width", 0) >= PEXELS_MIN_WIDTH]
                pick = (big_enough or cands)
                if not pick:
                    continue
                if score > best_score:
                    best_score = score
                    best_video = v
                    best_pick_file = pick[0]
            if best_video is not None and best_score >= threshold:
                break
        if best_video is None or best_pick_file is None:
            return None, 0, None
        dest = ASSETS / f"pexels_{best_video['id']}.mp4"
        if not dest.exists():
            def _download(url=best_pick_file["link"], dest=dest):
                with requests.get(url, stream=True, timeout=120) as d:
                    d.raise_for_status()
                    with open(dest, "wb") as f:
                        shutil.copyfileobj(d.raw, f)
            try:
                retry("Pexels download", _download, tries=2, backoff=5)
            except Exception:
                if dest.exists():
                    dest.unlink()
                raise
        return dest, best_score, best_video["id"]
    except Exception as e:
        warn(f"Pexels failed for '{query}' ({e}).")
    return None, 0, None


def pexels_clip(query: str, used: set | None = None, mood: bool = True):
    """First unused stock clip for one query. Returns None (no key/fail/empty)."""
    dest, _score, key = _best_video(query, used, mood)
    if dest is not None and key is not None and used is not None:
        used.add(key)
    return dest


def _best_photos(query: str, used: set | None = None, n: int = 1,
                 require: Any = None, is_wild: bool = False, phrase: str = ""):
    """Top-N literal-match stock photos for one query (read-only on `used`)."""
    if n <= 0 or not PEXELS_KEY:
        return []
    try:
        words = _query_words(query)
        threshold = min(2, len(words)) if len(words) > 1 else 1
        pool: dict = {}
        for variant in _query_variants(query):
            def _search(variant=variant):
                r = requests.get(
                    "https://api.pexels.com/v1/search",
                    headers={"Authorization": PEXELS_KEY},
                    params={"query": variant,
                            "per_page": 10,
                            "orientation": "portrait", "size": "large"},
                    timeout=30,
                )
                r.raise_for_status()
                return r
            r = retry("Pexels photo search", _search, tries=3, backoff=5)
            for p in r.json().get("photos", []):
                pid = p.get("id")
                if pid in pool:
                    continue
                if used is not None and f"photo_{pid}" in used:
                    continue
                alt = p.get("alt", "")
                url = p.get("url", "")
                haystack = f"{alt} {url}"
                if is_wild and _has_domestic_disqualifier(haystack):
                    continue
                if not _meets_require(haystack, require):
                    continue
                score = _score_candidate(alt, url, words, phrase)
                if score < threshold:
                    continue
                w = p.get("width", 0) or 0
                if w and w < PEXELS_MIN_WIDTH:
                    continue
                src = p.get("src", {})
                if not src.get("original"):
                    continue
                h = p.get("height", 0) or 0
                if w and h:
                    aspect = w / h
                    rank = abs(aspect - 9 / 16)
                else:
                    rank = 1.0
                area = (w or 0) * (h or 0)
                pool[pid] = (p, score, area, rank)
            if len(pool) >= n:
                break
        if not pool:
            return []
        ranked = sorted(pool.values(), key=lambda t: (-t[1], -t[2], t[3]))
        out = []
        for (p, score, _area, _rank) in ranked[:max(1, n)]:
            dest = ASSETS / f"pexels_{p['id']}.jpg"
            if not dest.exists():
                def _download(url=p["src"]["original"], dest=dest):
                    with requests.get(url, stream=True, timeout=120) as d:
                        d.raise_for_status()
                        with open(dest, "wb") as f:
                            shutil.copyfileobj(d.raw, f)
                try:
                    retry("Pexels photo download", _download, tries=2, backoff=5)
                except Exception as e:
                    if dest.exists():
                        dest.unlink()
                    warn(f"Pexels photo download failed for '{query}' ({e}).")
                    continue
            out.append((dest, score, f"photo_{p['id']}", p.get("alt", "")))
        return out
    except Exception as e:
        warn(f"Pexels photo failed for '{query}' ({e}).")
    return []


def _best_photo(query: str, used: set | None = None,
                require: Any = None, is_wild: bool = False, phrase: str = ""):
    """Best literal-match stock photo for one query (read-only on `used`)."""
    res = _best_photos(query, used, 1, require, is_wild=is_wild, phrase=phrase)
    return res[0] if res else (None, 0, None, "")


def pexels_photo(query: str, used: set | None = None):
    """Best literal-match stock photo for one query (Ken Burns at render).

    Returns an assets/pexels_{id}.jpg Path (the .jpg suffix triggers the
    render() Ken Burns branch) or None. `used` keys photos as f"photo_{id}"
    so they never collide with video ids in the shared per-run set.
    """
    dest, _score, key, *_rest = _best_photo(query, used)
    if dest is not None and key is not None and used is not None:
        used.add(key)
    return dest


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


# Cuts per beat: 3 cuts per beat ≈ 21 clips/video, cutting mid-beat like
# real Shorts pacing (~4-7s per cut) instead of one static slideshow frame
# per beat. 1 = old behavior (one clip per beat). Second-half cuts seek into
# different stock footage (see _stock_offset).
STILLS_PER_BEAT = int(os.getenv("STILLS_PER_BEAT", "3"))


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


def visuals_for_beats(beats: list[dict], niche, items: list | None = None,
                      topic: str = "") -> list[tuple]:
    """Per beat: STILLS_PER_BEAT cuts (Pexels video vs photos -> card).

    The niche decides the stock policy: ranking searches each beat's OWN
    query (literal item match); horror uses ambient B-roll from
    niche.AMBIENT_QUERIES, deliberately not story-matched. Returns
    [(clip, seek_or_None, beat_idx)]. Video and photos compete head to
    head at beat level on normalized accuracy (video raw slug score * 2
    vs best-photo 2*alt + url); higher wins, ties go to video (motion
    preferred). Labeled beats (#5..#1, positionally zipped with `items`
    like board_filter) search in tiers: exact (beat query + distinctive
    item word: long >=6-char word, else any word) -> family (item's last
    word as the family guess, e.g. 'Green Anaconda' -> 'anaconda', gated
    on that word, so a close relative fills in) -> topic (the topic's
    generic plural noun, e.g. 'snakes', gated on both spellings) ->
    Pillow title card. Wrong-family stock never airs at any tier; the
    card is the last resort only. Unlabeled beats (hook/closer) keep the
    single ungated search. A video win reuses ONE clip across the beat's
    cuts (later cuts seek via _stock_offset for continuity); a photo win
    emits one DISTINCT photo per cut (cycling plist[0] only when Pexels
    returned fewer than parts), each Ken Burns'd at render. Both missing
    renders Pillow title cards. Durations stay per-beat, split at render
    time. Fallback tiers only run on a miss, so extra Pexels searches are
    bounded (~2 per empty tier, never on a hit).
    """
    clips = []
    used_pexels: set = set()
    use_beat = niche.USE_BEAT_QUERY
    # Labeled beats in reveal order (#5 first) zip positionally with items
    # (#5 -> items[0] ... #1 -> items[4]); hook/closer stay ungated.
    labeled = [(idx, str(b.get("label", "")))
               for idx, b in enumerate(beats or [])
               if str(b.get("label", "")).isdigit()]
    names = list(items or [])
    gate_for: dict = {}
    fam_for: dict = {}
    is_wild = _is_wild_topic(topic)
    base_subj, domain_kw, is_obj = _get_topic_domain_info(topic)
    for (beat_idx, _lab), name in zip(labeled, names + [""] * len(labeled)):
        gate_for[beat_idx] = _item_require_words(name, domain=domain_kw) if name else {}
        fam_for[beat_idx] = _family_query_for_item(name, topic) if name else ("", [])
    tqueries = _topic_stock_queries(topic)
    still_n = 0
    n_video = n_photo = n_card = 0
    for i, b in enumerate(beats):
        parts = _split_beat(b["text"], STILLS_PER_BEAT)
        raw_query = (b.get("query", "") if use_beat
                     else random.choice(niche.AMBIENT_QUERIES))
        tiered = bool(use_beat and gate_for.get(i))
        require = gate_for.get(i)
        item_phrase = require.get("phrase", "") if isinstance(require, dict) else ""
        clean_q = _clean_stock_query(raw_query, item_phrase) if tiered else raw_query
        if tiered and is_obj and base_subj:
            q_lower = clean_q.lower()
            if not any(k in q_lower for k in (domain_kw | {base_subj})):
                query = f"{clean_q} {base_subj}"
            else:
                query = clean_q
        else:
            query = clean_q
        # _best_* only read `used`; the winner's key(s) are committed below
        # so losers never burn their ids for later beats.
        vpath, vraw, vkey = _best_video(query, used_pexels, mood=niche.USE_MOOD,
                                        require=require, is_wild=is_wild, phrase=item_phrase)
        plist = _best_photos(query, used_pexels, len(parts), require=require,
                             is_wild=is_wild, phrase=item_phrase)
        tier, eff_query = "exact", query
        if tiered and vpath is None and not plist:
            fquery, frequire = fam_for.get(i, ("", []))
            if fquery and frequire:
                fpath, fraw, fkey = _best_video(
                    fquery, used_pexels, mood=niche.USE_MOOD, require=frequire,
                    is_wild=is_wild, phrase=fquery)
                fplist = _best_photos(fquery, used_pexels, len(parts), require=frequire,
                                      is_wild=is_wild, phrase=fquery)
                if fpath is not None or fplist:
                    vpath, vraw, vkey, plist = fpath, fraw, fkey, fplist
                    tier, eff_query = "family", fquery
        if vpath is None and not plist and tqueries:
            for tq, treq in tqueries:
                tpath, traw, tkey = _best_video(
                    tq, used_pexels, mood=niche.USE_MOOD, require=treq,
                    is_wild=is_wild, phrase=tq)
                tplist = _best_photos(tq, used_pexels, len(parts), require=treq,
                                      is_wild=is_wild, phrase=tq)
                if tpath is not None or tplist:
                    vpath, vraw, vkey, plist = tpath, traw, tkey, tplist
                    tier, eff_query = "topic", tq
                    break
        # If still no stock for this beat, relax used_pexels to reuse good topic stock
        if vpath is None and not plist and tqueries:
            for tq, treq in tqueries:
                tpath, traw, tkey = _best_video(
                    tq, used=None, mood=niche.USE_MOOD, require=treq,
                    is_wild=is_wild, phrase=tq)
                tplist = _best_photos(tq, used=None, n=len(parts), require=treq,
                                      is_wild=is_wild, phrase=tq)
                if tpath is not None or tplist:
                    vpath, vraw, vkey, plist = tpath, traw, tkey, tplist
                    tier, eff_query = "topic-reuse", tq
                    break
        vnorm = vraw * 2
        pbest = plist[0][1] if plist else 0
        vclip, photolist, kind = None, [], "card"
        winner, alt_snip = "none", ""
        if plist and (vpath is None or pbest > vnorm):
            photolist, kind = plist, f"{len(parts)}xphoto"
            for (_, _, k, *_rest) in plist:
                used_pexels.add(k)
            n_photo += len(parts)
            winner = plist[0][2]
            alt_snip = f" alt='{(plist[0][3] if len(plist[0]) > 3 else '')[:60]}'"
        elif vpath is not None:
            vclip, kind = vpath, "video"
            used_pexels.add(vkey)
            n_video += len(parts)
            winner = f"video_{vkey}"
        else:
            # Fallback: borrow from previous clips or cached assets so NO plain text title cards air
            available_visuals = [c for (c, _, _) in clips if c and c.suffix in {".mp4", ".jpg"}]
            if not available_visuals:
                available_visuals = list(ASSETS.glob("pexels_*.mp4")) + list(ASSETS.glob("pexels_*.jpg"))
            if available_visuals:
                vclip = available_visuals[i % len(available_visuals)]
                if vclip.suffix == ".mp4":
                    kind = "video"
                    n_video += len(parts)
                    winner = f"reused_{vclip.stem}"
                else:
                    photolist = [(vclip, 1, f"reused_{vclip.stem}", "reused visual")]
                    kind = f"{len(parts)}xphoto"
                    n_photo += len(parts)
                    winner = f"reused_{vclip.stem}"
                tier = "reused"
            else:
                n_card += len(parts)
                tier = "card"
        via = f"->{eff_query}" if eff_query != query else ""
        log(f"Beat {i + 1} ('{query}'{via}): {kind} tier={tier} winner={winner}{alt_snip} (vscore={vnorm}, pscore={pbest}).")
        for j, part in enumerate(parts):
            clip, seek = None, None
            if vclip is not None:
                clip, seek = vclip, _stock_offset(vclip, j, len(parts))
            elif photolist:
                clip, seek = photolist[j][0] if j < len(photolist) else photolist[0][0], None
            if clip is None:
                available_visuals = [c for (c, _, _) in clips if c and c.suffix in {".mp4", ".jpg"}]
                if not available_visuals:
                    available_visuals = list(ASSETS.glob("pexels_*.mp4")) + list(ASSETS.glob("pexels_*.jpg"))
                if available_visuals:
                    clip = available_visuals[still_n % len(available_visuals)]
                    seek = 2.0 * (j + 1) if clip.suffix == ".mp4" else None
                    warn(f"Beat {i + 1}.{j + 1} ('{query}'): no stock, reusing run visual {clip.name}.")
                else:
                    warn(f"Beat {i + 1}.{j + 1} ('{query}'): emergency fallback card.")
                    clip = beat_card(still_n, part)
            clips.append((clip, seek, i))
            still_n += 1
    log(f"Visuals: {len(clips)} cuts for {len(beats)} beats "
        f"({n_video} video, {n_photo} photo, {n_card} card).")
    return clips


# Glob patterns for per-run FFmpeg intermediates. The pexels_*.mp4 cache
# is intentionally NOT listed: re-downloads waste time/bandwidth on re-runs.
INTERMEDIATE_PATTERNS = ("seg_*.mp4", "slide_*.png",
                         "bed.mp3", "concat.txt", "video_only.mp4",
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


# Mystery #1 twist (visual-only): the #1 beat stays heavily blurred for the
# first ~2s of its screen time, then snaps clear at the reveal. Gemini TTS
# gives beat-level SRT only, so the exact reveal word time is unknown — the
# window is min(2s, half the beat). Applied at the FINAL mux (absolute
# timeline, like the leaderboard), so per-segment clocks and cuts are untouched.
MYSTERY_BLUR_MAX_SECS = 2.0


def _mystery_blur_filter(beats: list[dict], durations: list[float]) -> str:
    """boxblur snippet with trailing comma for the #1 beat's reveal, or "".

    Finds the beat labeled "1", blurs [start, start+min(2s, dur/2)] on the
    finished timeline, then snaps clear. "" when N/A (no labeled beat,
    bad/zero durations) so the mux is byte-identical to the no-twist render.
    """
    try:
        idx = next(i for i, b in enumerate(beats or [])
                   if str((b or {}).get("label", "")) == "1")
    except StopIteration:
        return ""
    starts: list[float] = []
    t = 0.0
    for d in durations or []:
        starts.append(t)
        try:
            t += max(0.0, float(d))
        except (TypeError, ValueError):
            return ""
    if idx >= len(starts) or idx >= len(durations or []):
        return ""
    try:
        dur = max(0.0, float(durations[idx]))
    except (TypeError, ValueError):
        return ""
    blur_len = min(MYSTERY_BLUR_MAX_SECS, dur / 2)
    if blur_len <= 0.1:
        return ""
    start = starts[idx]
    log(f"Mystery #1 blur: beat {idx + 1} blurred "
        f"{start:.2f}s->{start + blur_len:.2f}s (beat dur {dur:.2f}s), clear after.")
    return (f"boxblur=20:5:enable='between(t,{start:.2f},{start + blur_len:.2f})',")


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
                zb = (f"zoompan=z='if(eq(on,1),1.3,max(zoom-0.0015,1.0))':d={frames}:"
                      f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}")
            _run_ffmpeg(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(c),
                         # cover-crop: any input aspect fills 1080x1920 undistorted
                         "-vf", f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
                                f"crop={W * 2}:{H * 2},{zb},fps=30",
                          "-t", str(seg_dur), "-pix_fmt", "yuv420p", "-c:v", "libx264",
                          "-preset", "veryfast", "-crf", "20", "-maxrate", "12M",
                          "-bufsize", "24M", str(seg)], f"segment-{i}-kenburns")
        elif c.suffix == ".png":
            _run_ffmpeg(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(c),
                         "-t", str(seg_dur), "-vf", f"scale={W}:{H},fps=30",
                          "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                          "-crf", "20", "-maxrate", "12M", "-bufsize", "24M",
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
                     "-crf", "20", "-maxrate", "12M", "-bufsize", "24M",
                     "-an", str(seg)]
            _run_ffmpeg(cmd, f"segment-{i}-clip")
        segs.append(seg)
    concat = ASSETS / "concat.txt"
    concat.write_text("".join(f"file '{s.resolve()}'\n" for s in segs))
    video_only = ASSETS / "video_only.mp4"
    _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                  "-i", str(concat), "-c:v", "libx264", "-preset", "veryfast",
                  "-crf", "20", "-maxrate", "12M", "-bufsize", "24M",
                  "-pix_fmt", "yuv420p", str(video_only)], "concat")
    # Mux narration + bed + tiny bottom captions + niche overlay.
    # Bed is mixed quietly under the voice (normalize=0 keeps narration full).
    # The overlay lives here (not per-segment): only the finished timeline has
    # absolute timestamps for between(t,a,b) reveal windows.
    vf_subs = _subtitles_vf(srt)
    board = niche.board_filter(beats or [], items or [], durations,
                               _rank_font, _has_drawtext, title)
    blur = _mystery_blur_filter(beats or [], durations)
    achain = "[1:a][2:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"

    def _mux_cmd(blur_s: str, board_s: str, subs_s):
        v = (f"[0:v]{blur_s}{board_s}{subs_s},format=yuv420p[vout]" if subs_s
             else f"[0:v]{blur_s}{board_s}format=yuv420p[vout]")
        return ["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio), "-i", str(bed),
                "-filter_complex", f"{v};{achain}",
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                "-maxrate", "12M", "-bufsize", "24M", "-c:a", "aac", "-b:a", "128k",
                "-ar", "44100", "-movflags", "+faststart", "-shortest", str(out_path)]

    vchain = (f"[0:v]{blur}{board}{vf_subs},format=yuv420p[vout]" if vf_subs
              else f"[0:v]{blur}{board}format=yuv420p[vout]")
    cmd = ["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio), "-i", str(bed),
           "-filter_complex", f"{vchain};{achain}",
           "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
           "-maxrate", "12M", "-bufsize", "24M", "-c:a", "aac", "-b:a", "128k",
           "-ar", "44100", "-movflags", "+faststart", "-shortest", str(out_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if blur and "boxblur" in stderr:
            warn("mystery #1 blur filter failed, rendering without blur.")
            blur = ""
            cmd = _mux_cmd(blur, board, vf_subs)
            try:
                _run_ffmpeg(cmd, "mux-no-blur")
            except subprocess.CalledProcessError as e_nb:
                e = e_nb
                stderr = e_nb.stderr or ""
            else:
                log(f"Rendered {out_path} ({out_path.stat().st_size // 1024} KB)")
                return
        # NOTE: check drawtext first. The filtergraph dump names both filters,
        # so a subtitles check would always win when board+subs are present
        # and retry with a still-broken board. "No such filter" also points
        # at the board: an unbalanced quote splits the graph at the next
        # comma (e.g. No such filter: '26.62' from enable's between(t,a,b)).
        board_failed = bool(board and ("drawtext" in stderr or "No such filter" in stderr))
        subs_failed = bool(vf_subs and "subtitles" in stderr)
        if board_failed:
            warn("niche overlay filter failed, rendering without it.")
            cmd = _mux_cmd(blur, "", vf_subs)
            try:
                _run_ffmpeg(cmd, "mux-no-board")
            except subprocess.CalledProcessError:
                if not subs_failed:
                    raise
                warn("subtitles filter also failed, rendering plain.")
                cmd = _mux_cmd(blur, "", None)
                _run_ffmpeg(cmd, "mux-plain")
        elif subs_failed:
            warn("subtitles filter failed, rendering without burned captions.")
            cmd = _mux_cmd(blur, board, None)
            try:
                _run_ffmpeg(cmd, "mux-no-subs")
            except subprocess.CalledProcessError as e2:
                stderr2 = e2.stderr or ""
                if not (board and ("drawtext" in stderr2 or "No such filter" in stderr2)):
                    raise
                warn("niche overlay filter also failed, rendering without it.")
                cmd = _mux_cmd(blur, "", vf_subs)
                try:
                    _run_ffmpeg(cmd, "mux-no-board")
                except subprocess.CalledProcessError:
                    warn("subtitles filter also failed, rendering plain.")
                    cmd = _mux_cmd(blur, "", None)
                    _run_ffmpeg(cmd, "mux-plain")
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
    global RUN_VOICE, RUN_TONE
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stem = OUT / f"{stamp}_short"
    # Voice for this run (default single Charon; a multi-voice pool alternates
    # via history entry-count parity, date-stamp day parity when unavailable).
    # Body tone alternates on history parity (even -> energetic, odd -> deep)
    # unless GEMINI_TTS_STYLE is explicitly set, which wins for the body.
    try:
        _hist_len: int | None = len(load_history())
    except Exception:
        _hist_len = None
    RUN_VOICE, _voice_why = pick_run_voice(_hist_len, stamp)
    if GEMINI_TTS_STYLE_EXPLICIT:
        RUN_TONE = None
        _tone_desc = "override (GEMINI_TTS_STYLE explicitly set, alternation skipped)"
    else:
        RUN_TONE, _tone_why = pick_run_tone(_hist_len, stamp)
        _tone_desc = f"{RUN_TONE} ({_tone_why})"
    log(f"TTS run voice={RUN_VOICE} ({_voice_why}, pool={GEMINI_TTS_VOICES}); tone={_tone_desc}")
    # Non-secret effective config so Actions logs show what actually ran.
    if TTS_ENGINE == "gemini":
        tts_desc = (f"gemini(models={GEMINI_TTS_MODELS},voices={GEMINI_TTS_VOICES})"
                    "->edge")
    else:
        tts_desc = f"edge(voice={VOICE})"
    log(f"Config: mode={niche.MODE} tts={tts_desc} rate={TTS_RATE} pitch={TTS_PITCH} niche='{niche.NICHE_LABEL}' visibility={VISIBILITY} "
        f"min/max={MIN_AUDIO_SEC:g}/{MAX_AUDIO_SEC:g}s models={GEMINI_MODELS} "
        f"keys: gemini_keys={len(GEMINI_KEYS)} pexels={bool(PEXELS_KEY)} "
        f"yt={(bool(os.getenv('YT_CLIENT_ID')) and bool(os.getenv('YT_CLIENT_SECRET')) and bool(os.getenv('YT_REFRESH_TOKEN')))} "
        f"buffer={bool(BUFFER_API_KEY)}")  # key presence only, never the key itself
    meta, slots = gen_script(niche)
    (stem.with_suffix(".json")).write_text(json.dumps(meta, indent=2))
    mp3, srt, beat_durs = gen_audio(meta["narration"], stem, meta.get("beats"),
                                    (slots or {}).get("topic", ""))
    log(f"Audio: mp3={mp3.stat().st_size // 1024} KB srt_exists={srt.exists()} "
        f"srt_bytes={srt.stat().st_size if srt.exists() else 0}")
    dur = audio_duration(mp3)
    check_audio_duration(dur, len(meta["narration"].split()))
    beats = meta["beats"]
    clips = visuals_for_beats(beats, niche, list(meta.get("items", [])),
                              (slots or {}).get("topic", ""))
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
    bed, bed_name = make_bed(dur, _hist_len)
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
             "bed": bed_name,
             "videoId": video_id,
             "tiktokBufferPostId": tiktok_post_id}
    if isinstance(slots, dict):
        entry.update(niche.history_fields(slots, meta))
    save_history(entry)
    log("Done.")
