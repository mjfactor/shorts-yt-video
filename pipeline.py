"""Zero-cost YouTube Shorts pipeline: Gemini -> edge-tts -> Pexels/Pillow -> FFmpeg -> YouTube.
Run: python pipeline.py  (all config via env vars, see .env.example)
Output: out/<date>_short.mp4 (+ .srt, metadata.json)
"""
import datetime
import json
import os
import random
import shutil
import subprocess
import sys
import textwrap
import time
import base64
from pathlib import Path

import requests

OUT = Path("out")
ASSETS = Path("assets")
OUT.mkdir(exist_ok=True)
ASSETS.mkdir(exist_ok=True)
STATE_FILE = Path("series_state.json")
MAX_EPISODES_PER_ARC = 5  # episodes per story arc, then a fresh arc starts

# Hardcoded channel settings (change here, no env vars needed).
NICHE = "scary true horror stories with cliffhanger"
VOICE = "en-US-ChristopherNeural"  # deep male narrator (News/Novel, Authority)
VISIBILITY = os.getenv("VISIBILITY", "private")  # private = safe for unverified OAuth apps
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
# Tried in order; first working model wins. Override via GEMINI_MODELS="model-a,model-b".
GEMINI_MODELS = [m.strip() for m in
                 os.getenv("GEMINI_MODELS",
                           "gemini-3.6-flash,gemini-2.5-flash,gemini-2.5-flash-lite").split(",")
                 if m.strip()]
PEXELS_KEY = os.getenv("PEXELS_API_KEY", "")
# Optional: Pollinations account key (sk_...) for watermark-free stills +
# higher rate limits. Without it, stills are anonymous (free, may watermark).
POLLINATIONS_KEY = os.getenv("POLLINATIONS_API_KEY", "")
MIN_AUDIO_SEC = float(os.getenv("MIN_AUDIO_SEC", "10"))
MAX_AUDIO_SEC = float(os.getenv("MAX_AUDIO_SEC", "180"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

W, H = 1080, 1920  # vertical Shorts


def log(msg: str):
    print(f"[pipeline] {msg}", flush=True)


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
                return int(resp.get("status"))
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


def retry(name: str, func, tries: int = 3, backoff: int = 5, fatal=()):
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
                log(f"{name} permanent failure (HTTP {status}): {e}")
                raise
            if attempt == tries:
                raise
            wait = backoff * attempt
            log(f"{name} failed (attempt {attempt}/{tries}: {e}), retrying in {wait}s...")
            time.sleep(wait)


# ---------- 1. Script ----------
FALLBACK_TOPICS = [
    "The camper who heard knocking at 3am",
    "The nurse who worked the night shift alone",
    "The house that whispered at midnight",
]


def _fallback_script() -> dict:
    topic = random.choice(FALLBACK_TOPICS)
    narration = (
        f"Do not watch this alone. {topic}. "
        f"It was 3am when she heard it again. Three slow knocks from inside the walls. "
        f"She told herself it was the pipes, just the old house settling. "
        f"But then it whispered her name, and her blood ran cold, because nobody else was home. "
        f"The knocking moved. From the walls to the ceiling to right behind her bedroom door. "
        f"She grabbed her phone with shaking hands, but there was no signal. "
        f"Then silence. The kind of silence that screams. "
        f"She ran to the front door, and it was locked from the outside. "
        f"Part 2 tomorrow. Follow so you don't miss what knocked back."
    )
    words = narration.split()
    beat_queries = ["dark bedroom night", "abandoned hallway night",
                    "creepy door dark", "foggy forest night"]
    beats = []
    for i in range(4):
        chunk = " ".join(words[i * 30:(i + 1) * 30])
        if chunk:
            beats.append({"text": chunk, "query": beat_queries[i]})
    return {
        "title": f"{topic} #shorts",
        "narration": narration,
        "description": f"A true scary horror short. Part 2 tomorrow! #shorts #horror #scarystory",
        "tags": ["shorts", "horror", "scarystory", "creepy", "truestory"],
        "beats": beats,
    }


def _validate_script(data) -> dict:
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
    for b in beats[:6]:
        if not isinstance(b, dict):
            raise ValueError("each beat must be an object with text+query")
        t, q = b.get("text"), b.get("query")
        if not isinstance(t, str) or not t.strip():
            raise ValueError("beat 'text' must be a non-empty string")
        if not isinstance(q, str) or not q.strip():
            raise ValueError("beat 'query' must be a non-empty string")
        norm_beats.append({"text": t.strip(), "query": q.strip()})
    if sum(len(b["text"].split()) for b in norm_beats) < 20:
        raise ValueError("'beats' cover too little text (<20 words total)")
    # Normalize so downstream steps never choke on oversized input.
    return {
        "title": title.strip()[:100],
        "narration": narration.strip(),
        "description": description.strip(),
        "tags": [t.strip() for t in tags if t.strip()][:10],
        "beats": norm_beats,
    }


def _validate_episode(data, expected_ep: int) -> dict:
    """Base validation + arc continuity fields. Raises ValueError if unusable.

    Also guarantees title/description relate to the arc: auto-appends the
    episode marker / series reference when Gemini omits them (logged).
    """
    base = _validate_script(data)
    arc = data.get("arc_title")
    ep = data.get("episode")
    recap = data.get("recap")
    if not isinstance(arc, str) or not arc.strip():
        raise ValueError("missing/invalid 'arc_title' (must be non-empty string)")
    if isinstance(ep, bool) or not isinstance(ep, int) or ep < 1:
        raise ValueError("'episode' must be a positive int")
    if not isinstance(recap, str) or not recap.strip():
        raise ValueError("missing/invalid 'recap' (must be non-empty string)")
    arc = arc.strip()
    if ep != expected_ep:
        log(f"Gemini returned episode {ep}, correcting to {expected_ep}.")
        ep = expected_ep
    finale = data.get("finale", False)
    if not isinstance(finale, bool):
        log(f"'finale' not a bool ({finale!r}), coercing to {bool(finale)}.")
        finale = bool(finale)
    title = base["title"][:90].rstrip()
    if str(ep) not in title:
        title = f"{title} Ep {ep}"
        log(f"Title lacked episode marker, fixed: {title}")
    base["title"] = title
    desc = base["description"]
    if arc.lower() not in desc.lower():
        desc = f"{desc} Episode {ep} of '{arc}'."
        log("Description lacked series reference, fixed.")
    base["description"] = desc
    base["arc_title"] = arc
    base["episode"] = ep
    base["recap"] = recap.strip()
    base["finale"] = finale
    return base


def load_state():
    """Read series continuity state. Returns None (fresh arc) if missing/invalid."""
    try:
        data = json.loads(STATE_FILE.read_text())
        if (isinstance(data, dict) and isinstance(data.get("arc_title"), str)
                and isinstance(data.get("episode"), int)
                and isinstance(data.get("recap"), str)):
            log(f"Continuing series '{data['arc_title']}' after episode {data['episode']}")
            return data
        log("series_state.json invalid, starting fresh arc.")
    except FileNotFoundError:
        log("No series state, starting fresh arc.")
    except Exception as e:
        log(f"series_state.json unreadable ({e}), starting fresh arc.")
    return None


def save_state(update: dict):
    STATE_FILE.write_text(json.dumps(update, indent=2))
    tag = " (FINALE - next run starts a new arc)" if update.get("finale") else ""
    log(f"Series state saved: '{update['arc_title']}' Ep {update['episode']}{tag}")


def _episode_prompt(state):
    """Build Gemini prompt. Returns (prompt, episode_number, is_continuation)."""
    base_rules = (
        "Return ONLY valid JSON with keys: title (<=60 chars, scary hook + episode marker like 'Ep 2'), "
        "narration (120-150 words, spoken English, chilling hook in first 3 seconds, slow dread build, "
        "cliffhanger ending unless this episode ends the story, no stage directions), "
        "description (2 sentences + 3 hashtags, must name the series title and episode number), "
        "tags (array of 5 strings), "
        "beats (array of 3-4 objects in story order, each {text: that beat's exact narration chunk, "
        "query: concrete dark stock-video search for that beat like 'woman opening door dark hallway'}; "
        "beat texts joined must equal the full narration), "
        "arc_title (series name, 3-6 words), episode (int), "
        "recap (3-4 sentences summarizing the whole arc so far, for continuity), "
        "finale (bool: true ONLY if this episode properly ends the story, else false)."
    )
    if state and not state.get("finale") and state.get("episode", 0) < MAX_EPISODES_PER_ARC:
        ep = state["episode"] + 1
        prompt = (
            f"Continue the scary YouTube Shorts horror series '{state['arc_title']}'. "
            f"Write Episode {ep}. Story so far: {state['recap']} "
            f"Start the narration with a one-line 'previously' hook, resolve the previous "
            f"cliffhanger and build new dread. If the story reaches its natural end in this "
            f"episode, write a proper ENDING and set finale:true instead of a cliffhanger; "
            f"otherwise end on a NEW cliffhanger "
            f"(the forced finale is Episode {MAX_EPISODES_PER_ARC}). " + base_rules
        )
        return prompt, ep, True
    prompt = (
        f"Start a NEW scary YouTube Shorts horror series about {NICHE}. "
        "Write Episode 1: chilling hook in first 3 seconds, slow dread build, "
        "end on a cliffhanger (set finale:false). " + base_rules
    )
    return prompt, 1, False


def _fetch_gemini(url: str, payload: dict):
    """POST to Gemini with retries for transient failures (5xx/429/network)."""
    def _do():
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        return r.json()
    return retry("Gemini", _do, tries=3, backoff=5)


def gen_script(state=None):
    """Call Gemini free API. Returns (meta, state_update_or_None).

    state_update advances the series arc and is saved only after a
    successful render+upload. Fallback returns a standalone story with
    no state change.
    """
    prompt, expected_ep, _ = _episode_prompt(state)
    if GEMINI_KEY:
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        for model in GEMINI_MODELS:
            try:
                url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                       f"{model}:generateContent?key={GEMINI_KEY}")
                raw = _fetch_gemini(url, payload)
                text = raw["candidates"][0]["content"]["parts"][0]["text"]
                # strip markdown fences
                text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                data = _validate_episode(json.loads(text), expected_ep)
                log(f"Gemini script OK ({model}): {data.get('title')}")
                if data["finale"]:
                    log("Gemini marked this episode as the FINALE.")
                update = {"arc_title": data["arc_title"], "episode": data["episode"],
                          "recap": data["recap"], "finale": data["finale"]}
                return data, update
            except Exception as e:
                log(f"Gemini model {model} unusable ({e}), trying next...")
        log("All Gemini models failed, using fallback.")
    return _fallback_script(), None


# ---------- 2. Audio (edge-tts, free, no key) ----------
def gen_audio(narration: str, stem: Path) -> tuple[Path, Path]:
    mp3 = stem.with_suffix(".mp3")
    srt = stem.with_suffix(".srt")
    # edge-tts CLI writes media + subtitles in one call
    cmd = [
        sys.executable, "-m", "edge_tts",
        "--voice", VOICE,
        "--text", narration,
        "--write-media", str(mp3),
        "--write-subtitles", str(srt),
    ]
    log(f"TTS voice={VOICE}")
    retry("edge-tts", lambda: subprocess.run(cmd, check=True),
          tries=3, backoff=10)
    return mp3, srt


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


def pexels_clip(query: str):
    """First usable stock clip for one query. Returns None (no key/fail/empty)."""
    if not PEXELS_KEY:
        return None
    try:
        def _search():
            r = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": PEXELS_KEY},
                params={"query": _mood_query(query), "per_page": 4,
                        "orientation": "portrait", "size": "medium"},
                timeout=30,
            )
            r.raise_for_status()
            return r
        r = retry("Pexels search", _search, tries=3, backoff=5)
        for v in r.json().get("videos", []):
            # Prefer the smallest file >=720p wide: keeps CI downloads fast.
            cands = sorted((f for f in v.get("video_files", []) if f.get("link")),
                           key=lambda f: f.get("width", 0))
            big_enough = [f for f in cands if f.get("width", 0) >= 720]
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
            return dest
    except Exception as e:
        log(f"Pexels failed for '{query}' ({e}).")
    return None


def beat_card(i: int, text: str) -> Path:
    """Title card carrying the beat's story text (gap filler, always matches)."""
    from PIL import Image, ImageDraw
    colors = [(18, 18, 40), (40, 18, 30), (18, 40, 35), (35, 35, 18)]
    img = Image.new("RGB", (W, H), colors[i % len(colors)])
    d = ImageDraw.Draw(img)
    d.multiline_text((80, 800), textwrap.fill(text[:220], width=28),
                     fill=(255, 255, 255), spacing=8)
    p = ASSETS / f"slide_beat_{i}.png"
    img.save(p)
    return p


POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
# Optional: Cloudflare Workers AI (free 10k neurons/day, no card) for
# watermark-free FLUX stills. Account ID + API token with Workers AI
# permission. Without them, this step is skipped silently.
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "")
# SDXL-Lightning: listed at $0.00/step (Beta) + native portrait size, so it
# beats FLUX on cost. Override back to FLUX if you ever want it:
# "@cf/black-forest-labs/flux-1-schnell".
CF_IMAGE_MODEL = os.getenv(
    "CF_IMAGE_MODEL", "@cf/bytedance/stable-diffusion-xl-lightning")
# Pollen budget: 0.25 pollen ≈ 125 schnell stills ≈ 0.36 pollen/month at
# 2 stills/video × 3/day. Only the first beats (hook + follow-through) get
# AI stills; later beats use beat-matched stock. Raise once pollen allows.
AI_STILLS_PER_VIDEO = 2


def _pollinations_target(beat_text: str, seed: int):
    """Build (url, params) for one FLUX still. Pure function, no network."""
    import urllib.parse
    prompt = (f"{beat_text}, dark horror cinematic film still, eerie night "
              f"atmosphere, photorealistic, highly detailed")[:600]
    url = f"{POLLINATIONS_BASE}/{urllib.parse.quote(prompt)}"
    params = {"model": "flux", "width": W, "height": H, "seed": seed,
              "safe": "true"}
    return url, params


def _pollinations_headers():
    """Bearer auth when an account key is set, else anonymous (free)."""
    if POLLINATIONS_KEY:
        return {"Authorization": f"Bearer {POLLINATIONS_KEY}"}
    return {}


def pollinations_image(beat_text: str, i: int) -> Path:
    """Generate a literal horror still for one beat (FLUX).

    Authenticated (POLLINATIONS_API_KEY set): watermark-free + faster pacing.
    Anonymous: free, 1 req/15s pacing, may carry a small watermark.
    Raises on failure so the caller falls back to Pexels, then title cards.
    The key is sent as a header and never logged.
    """
    seed = random.randint(1, 999999)
    url, params = _pollinations_target(beat_text, seed)
    headers = _pollinations_headers()
    dest = ASSETS / f"poll_beat_{i}_{seed}.jpg"
    if i > 0:
        time.sleep(6 if POLLINATIONS_KEY else 16)  # tier rate limit
    def _do():
        r = requests.get(url, params=params, headers=headers, timeout=300)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if not ct.startswith("image/") or len(r.content) < 10_000:
            raise ValueError(f"Pollinations returned non-image ({ct}, {len(r.content)}b)")
        dest.write_bytes(r.content)
    retry("Pollinations", _do, tries=2, backoff=20)
    tier = "account (no watermark)" if POLLINATIONS_KEY else "anonymous"
    log(f"Beat {i + 1}: AI still generated ({dest.stat().st_size // 1024} KB, {tier}).")
    return dest


def cloudflare_image(beat_text: str, i: int):
    """Literal still via Workers AI free allowance (no card, no watermark).

    Default model SDXL-Lightning is listed at $0.00/step and takes native
    1080x1920, so cost is ~zero and no crop is needed. Free plan hard-stops
    at the 10k neuron cap (no overage billing possible). Returns None when
    creds are unset; raises on failure for the next fallback.
    """
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        return None
    prompt = (f"{beat_text}, dark horror cinematic film still, eerie night "
              f"atmosphere, photorealistic, highly detailed")[:1500]
    seed = random.randint(1, 999999)
    dest = ASSETS / f"cf_beat_{i}_{seed}.jpg"

    def _do():
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{CF_ACCOUNT_ID}/ai/run/{CF_IMAGE_MODEL}",
            headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
            json={"prompt": prompt, "seed": seed, "width": W, "height": H,
                  "num_steps": 8,
                  "negative_prompt": "blurry, watermark, text, daytime, cheerful"},
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


def visuals_for_beats(beats: list[dict]) -> list[Path]:
    """Per beat: Cloudflare still -> Pollinations -> Pexels -> card."""
    clips = []
    for i, b in enumerate(beats):
        clip = None
        try:
            clip = cloudflare_image(b["text"], i)
        except Exception as e:
            log(f"Beat {i + 1} Cloudflare image failed ({e}), trying Pollinations.")
        if clip is None and i < AI_STILLS_PER_VIDEO:
            try:
                clip = pollinations_image(b["text"], i)
            except Exception as e:
                if _http_status(e) == 402:
                    log("Pollen exhausted (HTTP 402): AI stills off for this run, "
                        "using stock. Top up via quests or lower AI_STILLS_PER_VIDEO.")
                else:
                    log(f"Beat {i + 1} AI still failed ({e}), trying Pexels.")
        if clip is None:
            clip = pexels_clip(b["query"])
        if clip is None:
            log(f"Beat {i + 1} ('{b['query']}'): no stock, using title card.")
            clip = beat_card(i, b["text"])
        clips.append(clip)
    return clips


# Glob patterns for per-run FFmpeg intermediates. The pexels_*.mp4 cache
# is intentionally NOT listed: re-downloads waste time/bandwidth on re-runs.
INTERMEDIATE_PATTERNS = ("seg_*.mp4", "slide_*.png", "poll_*.jpg",
                         "cf_*.jpg", "concat.txt", "video_only.mp4")


def cleanup_intermediates():
    """Delete per-run intermediates from assets/. Keeps the Pexels cache."""
    for pattern in INTERMEDIATE_PATTERNS:
        for p in ASSETS.glob(pattern):
            try:
                p.unlink()
            except OSError as e:
                log(f"cleanup warning: {p} ({e})")
    log("Cleaned intermediates (Pexels cache kept).")


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


def render(clips: list[Path], audio: Path, srt: Path, durations: list[float], out_path: Path):
    """Normalize clips to 1080x1920, one segment per beat duration, mux + burn subs."""
    # Build per-clip normalized segments (uniform fps/size/codec so concat is safe
    # even when Pexels clips have mixed frame rates).
    segs = []
    for i, c in enumerate(clips):
        seg_dur = max(2.0, durations[i] if i < len(durations) else 2.0)
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
            _run_ffmpeg(["ffmpeg", "-y", "-i", str(c),
                         "-t", str(seg_dur),
                         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30",
                         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                         "-an", str(seg)], f"segment-{i}-clip")
        segs.append(seg)
    concat = ASSETS / "concat.txt"
    concat.write_text("".join(f"file '{s.resolve()}'\n" for s in segs))
    video_only = ASSETS / "video_only.mp4"
    _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(concat), "-c:v", "libx264", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", str(video_only)], "concat")
    # Mux audio + burn subtitles (caption filter needs .srt path escaped).
    # Falls back to no subtitles if the ffmpeg build lacks libass.
    srt_esc = str(srt.resolve()).replace("\\", "/").replace(":", "\\:")
    vf_subs = f"subtitles='{srt_esc}'" if srt.exists() else None
    cmd = ["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio)]
    if vf_subs:
        cmd += ["-vf", vf_subs]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
            "-shortest", str(out_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        if vf_subs and "subtitles" in (e.stderr or ""):
            log("subtitles filter unavailable, rendering without burned captions.")
            cmd = ["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio),
                   "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                   "-shortest", str(out_path)]
            _run_ffmpeg(cmd, "mux-no-subs")
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
        log("YouTube creds missing -> skipping upload (video kept in out/).")
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


def main():
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stem = OUT / f"{stamp}_short"
    state = load_state()
    meta, state_update = gen_script(state)
    (stem.with_suffix(".json")).write_text(json.dumps(meta, indent=2))
    mp3, srt = gen_audio(meta["narration"], stem)
    dur = audio_duration(mp3)
    check_audio_duration(dur, len(meta["narration"].split()))
    beats = meta["beats"]
    clips = visuals_for_beats(beats)
    # Weight each beat's screen time by its share of words (speech rate is
    # roughly constant, so each clip covers its own story moment).
    total_words = sum(len(b["text"].split()) for b in beats) or 1
    durations = [dur * len(b["text"].split()) / total_words for b in beats]
    out_mp4 = stem.with_suffix(".mp4")
    try:
        render(clips, mp3, srt, durations, out_mp4)
        upload(out_mp4, meta)
        # Advance the arc only if the video actually got made+uploaded.
        if state_update is not None:
            save_state(state_update)
    finally:
        cleanup_intermediates()
    log("Done.")


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception as e:
        log(f"PIPELINE FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        notify_failure(e)
        sys.exit(1)
