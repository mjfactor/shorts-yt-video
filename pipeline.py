"""Zero-cost YouTube Shorts pipeline: Gemini -> edge-tts -> Pexels/Pillow -> FFmpeg -> YouTube.
Run: python pipeline.py  (all config via env vars, see .env.example)
Output: out/<date>_short.mp4 (+ .srt, metadata.json)
"""
import asyncio
import datetime
import json
import os
import random
import shutil
import subprocess
import sys
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


# ---------- 1. Script ----------
FALLBACK_TOPICS = [
    "The camper who heard knocking at 3am",
    "The nurse who worked the night shift alone",
    "The house that whispered at midnight",
]


def gen_script() -> dict:
    """Call Gemini free API, fallback to template if no key/fail."""
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
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}"
            r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            # strip markdown fences
            text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(text)
            log(f"Gemini script OK: {data.get('title')}")
            return data
        except Exception as e:
            log(f"Gemini failed ({e}), using fallback.")
    topic = random.choice(FALLBACK_TOPICS)
    narration = (
        f"Do not watch this alone. {topic}. "
        f"It was 3am when she heard it again. Three slow knocks from inside the walls. "
        f"She told herself it was the pipes. But then it whispered her name. "
        f"She ran to the door, and it was locked from the outside. "
        f"Part 2 tomorrow. Follow so you don't miss what knocked back."
    )
    return {
        "title": f"{topic} #shorts",
        "narration": narration,
        "description": f"A true scary horror short. Part 2 tomorrow! #shorts #horror #scarystory",
        "tags": ["shorts", "horror", "scarystory", "creepy", "truestory"],
        "visual_queries": ["dark forest night", "abandoned hallway", "creepy door", "foggy night"],
    }


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
    subprocess.run(cmd, check=True)
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
            r = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": PEXELS_KEY},
                params={"query": q, "per_page": 3, "orientation": "portrait", "size": "medium"},
                timeout=30,
            )
            r.raise_for_status()
            for v in r.json().get("videos", []):
                files = sorted(v.get("video_files", []), key=lambda f: f.get("width", 0), reverse=True)
                if not files:
                    continue
                url = files[0]["link"]
                dest = ASSETS / f"pexels_{v['id']}.mp4"
                if not dest.exists():
                    with requests.get(url, stream=True, timeout=120) as d:
                        d.raise_for_status()
                        with open(dest, "wb") as f:
                            shutil.copyfileobj(d.raw, f)
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
        d.text((80, 800), chunk, fill=(255, 255, 255))
        p = ASSETS / f"slide_{i}.png"
        img.save(p)
        paths.append(p)
    return paths


# ---------- 4. Render with FFmpeg (CPU only) ----------
def render(clips: list[Path], audio: Path, srt: Path, target_dur: float, out_path: Path):
    """Normalize clips to 1080x1920, concat to match audio length, mux + burn subs."""
    # Build per-clip normalized segments
    segs = []
    per = max(2.0, target_dur / max(1, len(clips)))
    for i, c in enumerate(clips):
        seg = ASSETS / f"seg_{i}.mp4"
        if c.suffix == ".png":
            subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", str(c),
                            "-t", str(per), "-vf", f"scale={W}:{H}",
                            "-pix_fmt", "yuv420p", str(seg)],
                           check=True, capture_output=True)
        else:
            subprocess.run(["ffmpeg", "-y", "-i", str(c),
                            "-t", str(per),
                            "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}",
                            "-pix_fmt", "yuv420p", "-an", str(seg)],
                           check=True, capture_output=True)
        segs.append(seg)
    concat = ASSETS / "concat.txt"
    concat.write_text("".join(f"file '{s.resolve()}'\n" for s in segs))
    video_only = ASSETS / "video_only.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-c", "copy", str(video_only)],
                   check=True, capture_output=True)
    # Mux audio + burn subtitles (caption filter needs .srt path escaped)
    srt_esc = str(srt.resolve()).replace("\\", "/").replace(":", "\\:")
    vf = f"subtitles='{srt_esc}'" if srt.exists() else "null"
    subprocess.run(["ffmpeg", "-y", "-i", str(video_only), "-i", str(audio),
                    "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-c:a", "aac", "-shortest", str(out_path)],
                   check=True)
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
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
    log(f"Uploaded -> videoId={resp['id']}")
    return resp["id"]


def main():
    date = datetime.date.today().isoformat()
    stem = OUT / f"{date}_short"
    meta = gen_script()
    (stem.with_suffix(".json")).write_text(json.dumps(meta, indent=2))
    mp3, srt = gen_audio(meta["narration"], stem)
    dur = audio_duration(mp3)
    log(f"Narration {dur:.1f}s")
    clips = pexels_download(meta.get("visual_queries", [])) or pillow_slides(meta["narration"])
    out_mp4 = stem.with_suffix(".mp4")
    render(clips, mp3, srt, dur, out_mp4)
    upload(out_mp4, meta)
    log("Done.")


if __name__ == "__main__":
    main()
