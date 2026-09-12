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
from pathlib import Path

import requests

OUT = Path("out")
ASSETS = Path("assets")
OUT.mkdir(exist_ok=True)
ASSETS.mkdir(exist_ok=True)

NICHE = os.getenv("NICHE", "scary true horror stories with cliffhanger")
VOICE = os.getenv("VOICE", "en-US-AvaNeural")
VISIBILITY = os.getenv("VISIBILITY", "private")  # private = safe for unverified OAuth apps
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
PEXELS_KEY = os.getenv("PEXELS_API_KEY", "")

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
    return {
        "title": f"{topic} #shorts",
        "narration": narration,
        "description": f"A true scary horror short. Part 2 tomorrow! #shorts #horror #scarystory",
        "tags": ["shorts", "horror", "scarystory", "creepy", "truestory"],
        "visual_queries": ["dark forest night", "abandoned hallway", "creepy door", "foggy night"],
    }


def _validate_script(data) -> dict:
    """Strict-check Gemini output. Raises ValueError with the reason if unusable."""
    if not isinstance(data, dict):
        raise ValueError("Gemini output is not a JSON object")
    title = data.get("title")
    narration = data.get("narration")
    description = data.get("description")
    tags = data.get("tags")
    queries = data.get("visual_queries")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("missing/invalid 'title' (must be non-empty string)")
    if not isinstance(narration, str) or len(narration.split()) < 20:
        raise ValueError("'narration' missing or too short (<20 words)")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("missing/invalid 'description' (must be non-empty string)")
    if (not isinstance(tags, list) or not tags
            or not all(isinstance(t, str) and t.strip() for t in tags)):
        raise ValueError("'tags' must be a non-empty list of strings")
    if (not isinstance(queries, list) or not queries
            or not all(isinstance(q, str) and q.strip() for q in queries)):
        raise ValueError("'visual_queries' must be a non-empty list of strings")
    # Normalize so downstream steps never choke on oversized input.
    return {
        "title": title.strip()[:100],
        "narration": narration.strip(),
        "description": description.strip(),
        "tags": [t.strip() for t in tags if t.strip()][:10],
        "visual_queries": [q.strip() for q in queries if q.strip()][:6],
    }


def _fetch_gemini(url: str, payload: dict):
    """POST to Gemini with retries for transient failures (5xx/429/network)."""
    def _do():
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        return r.json()
    return retry("Gemini", _do, tries=3, backoff=5)


def gen_script() -> dict:
    """Call Gemini free API, fall back to template if no key / failure / bad output."""
    prompt = (
        f"Write a scary YouTube Short script about {NICHE}. "
        "Return ONLY valid JSON with keys: title (<=60 chars, scary hook + #shorts), narration "
        "(120-150 words, spoken English, chilling hook in first 3 seconds, slow dread build, "
        "cliffhanger ending with 'Part 2 tomorrow', no stage directions), "
        "description (2 sentences + 3 hashtags), tags (array of 5 strings), "
        "visual_queries (array of 4 short dark stock-video search terms like 'dark forest night', 'abandoned hallway')."
    )
    if GEMINI_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
            raw = _fetch_gemini(url, {"contents": [{"parts": [{"text": prompt}]}]})
            text = raw["candidates"][0]["content"]["parts"][0]["text"]
            # strip markdown fences
            text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = _validate_script(json.loads(text))
            log(f"Gemini script OK: {data.get('title')}")
            return data
        except Exception as e:
            log(f"Gemini failed ({e}), using fallback.")
    return _fallback_script()


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


def audio_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


# ---------- 3. Visuals ----------
def pexels_download(queries: list[str], need: int = 4) -> list[Path]:
    """Download vertical-ish stock clips. Returns [] on failure so caller falls back."""
    if not PEXELS_KEY:
        return []
    clips: list[Path] = []
    try:
        for q in queries:
            if len(clips) >= need:
                break
            def _search(q=q):
                r = requests.get(
                    "https://api.pexels.com/videos/search",
                    headers={"Authorization": PEXELS_KEY},
                    params={"query": q, "per_page": 3,
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
                url = pick[0]["link"]
                dest = ASSETS / f"pexels_{v['id']}.mp4"
                if not dest.exists():
                    def _download(url=url, dest=dest):
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
                clips.append(dest)
                if len(clips) >= need:
                    break
    except Exception as e:
        log(f"Pexels failed ({e}), will use fallback slides.")
    return clips[:need]


def pillow_slides(narration: str, n: int = 4) -> list[Path]:
    from PIL import Image, ImageDraw
    words = narration.split()
    paths = []
    colors = [(18, 18, 40), (40, 18, 30), (18, 40, 35), (35, 35, 18)]
    for i in range(n):
        img = Image.new("RGB", (W, H), colors[i % len(colors)])
        d = ImageDraw.Draw(img)
        chunk = " ".join(words[i * 30:(i + 1) * 30])[:220]
        wrapped = textwrap.fill(chunk, width=28)
        d.multiline_text((80, 800), wrapped, fill=(255, 255, 255), spacing=8)
        p = ASSETS / f"slide_{i}.png"
        img.save(p)
        paths.append(p)
    return paths


# Glob patterns for per-run FFmpeg intermediates. The pexels_*.mp4 cache
# is intentionally NOT listed: re-downloads waste time/bandwidth on re-runs.
INTERMEDIATE_PATTERNS = ("seg_*.mp4", "slide_*.png", "concat.txt", "video_only.mp4")


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


def render(clips: list[Path], audio: Path, srt: Path, target_dur: float, out_path: Path):
    """Normalize clips to 1080x1920, concat to match audio length, mux + burn subs."""
    # Build per-clip normalized segments (uniform fps/size/codec so concat is safe
    # even when Pexels clips have mixed frame rates).
    segs = []
    per = max(2.0, target_dur / max(1, len(clips)))
    for i, c in enumerate(clips):
        seg = ASSETS / f"seg_{i}.mp4"
        if c.suffix == ".png":
            _run_ffmpeg(["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(c),
                         "-t", str(per), "-vf", f"scale={W}:{H},fps=30",
                         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                         str(seg)], f"segment-{i}-slide")
        else:
            _run_ffmpeg(["ffmpeg", "-y", "-i", str(c),
                         "-t", str(per),
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
    meta = gen_script()
    (stem.with_suffix(".json")).write_text(json.dumps(meta, indent=2))
    mp3, srt = gen_audio(meta["narration"], stem)
    dur = audio_duration(mp3)
    log(f"Narration {dur:.1f}s")
    clips = pexels_download(meta.get("visual_queries", [])) or pillow_slides(meta["narration"])
    out_mp4 = stem.with_suffix(".mp4")
    try:
        render(clips, mp3, srt, dur, out_mp4)
        upload(out_mp4, meta)
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
        sys.exit(1)
