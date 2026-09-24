"""Orchestration-only core: niche-agnostic Shorts pipeline wiring.

Owns cross-cutting concerns only: logging, retry, grouped history,
run() stage wiring (content -> narration -> visuals -> files ->
upload -> history), resumable upload, failure pings, and intermediate
cleanup. All creative/render policy lives in niches/* + engine/*.

History on disk is grouped per niche: {name: [...]}. Flat legacy
lists migrate on load without loss (see niches.history_migrate).

Pre-Protocol root niche modules were removed in Phase 3c now that the
niches/* ports are green; run() accepts Protocol niches only
(generate_content/narrate/build_visuals). Do not re-add shims here.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import requests

from niches.base import NicheConfig
from niches.history_migrate import flatten_history, migrate_history

OUT = Path("out")
ASSETS = Path("assets")
OUT.mkdir(exist_ok=True)
ASSETS.mkdir(exist_ok=True)

W, H = 1080, 1920  # vertical Shorts canvas

VISIBILITY = os.getenv("VISIBILITY", "private")  # private until OAuth audit
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def _print(msg: str) -> None:
    """print() that survives non-UTF-8 consoles (Windows cp1252)."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode(), flush=True)


def log(msg: str) -> None:
    _print(f"[pipeline] {msg}")


def warn(msg: str) -> None:
    """Degraded-but-continuing path. Surfaces as an annotated warning."""
    _print(f"::warning::{msg}")
    log(f"WARNING: {msg}")


def err(msg: str) -> None:
    """Fatal path. Surfaces as an annotated error."""
    _print(f"::error::{msg}")
    log(f"ERROR: {msg}")


def _http_status(e: Exception) -> int | None:
    """Best-effort HTTP status extraction across requests-style errors."""
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


def retry(name: str, func, tries: int = 3, backoff: int = 5,
          fatal: tuple = ()) -> Any:
    """Retry temporary failures; permanent 4xx and `fatal` raise at once."""
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


# Cross-run dedup memory lives in committed history.json. On disk the
# shape is grouped per niche ({name: [...]}); flat legacy lists are
# migrated on load without loss. load/save never fail the run.
HISTORY_PATH = Path("history.json")
HISTORY_KEEP = 110  # entries kept on disk across all niches


def load_history(path: Path = HISTORY_PATH) -> list[dict]:
    """Flat entries, newest last. Empty on first run or corrupt file (warn)."""
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            flat = [h for h in flatten_history(data) if isinstance(h, dict)]
        elif isinstance(data, list):
            flat = [h for h in data if isinstance(h, dict)]
            if flat and not isinstance(data, dict):
                try:  # migrate-on-load: persist grouped shape (best-effort)
                    path.write_text(json.dumps(migrate_history(flat), indent=2,
                                               ensure_ascii=False),
                                    encoding="utf-8")
                except Exception:
                    pass
        else:
            warn(f"{path} has unexpected shape, ignoring.")
            return []
        return flat[-HISTORY_KEEP:]
    except Exception as e:
        warn(f"Could not read {path} ({e}), continuing without dedup memory.")
        return []


def save_history(entry: dict, path: Path = HISTORY_PATH) -> None:
    """Append one run entry (grouped per niche), trim total. Never raises."""
    try:
        flat = load_history(path)
        flat.append(entry)
        flat = flat[-HISTORY_KEEP:]
        path.write_text(json.dumps(migrate_history(flat), indent=2,
                                   ensure_ascii=False),
                        encoding="utf-8")
        log(f"History: recorded '{entry.get('title', '?')}' "
            f"({len(flat)} entries in {path}).")
    except Exception as e:
        warn(f"Could not write {path} ({e}).")


# Protocol niches only; legacy shims removed in Phase 3c (see docstring).


def _niche_name(niche) -> str:
    """Display/history key: NAME, else MODE, else generic."""
    return (getattr(niche, "NAME", None)
            or getattr(niche, "MODE", None)
            or "niche")


def _resolve_config(niche, name: str) -> NicheConfig:
    """Build the niche's own config (generic NicheConfig subclass scan).

    Picks the first NicheConfig subclass attribute exposing from_env();
    falls back to base NicheConfig. No per-niche branches.
    """
    if hasattr(niche, "load_config"):
        return niche.load_config()
    candidates = []
    for attr in sorted(dir(niche)):
        try:
            obj = getattr(niche, attr)
        except Exception:
            continue
        if (isinstance(obj, type) and issubclass(obj, NicheConfig)
                and obj is not NicheConfig
                and hasattr(obj, "from_env")):
            candidates.append(obj)
    if candidates:
        return candidates[0].from_env(name)
    return NicheConfig(niche_name=name)


def _as_protocol(niche):
    """Protocol niche as-is; anything else is a TypeError."""
    if (hasattr(niche, "generate_content") and hasattr(niche, "narrate")
            and hasattr(niche, "build_visuals")):
        return niche
    raise TypeError(f"Niche {niche!r} lacks the Protocol "
                    "(generate_content/narrate/build_visuals).")


def _stage(name: str, detail: str = "") -> None:
    """Stage banner (STORY/TTS/ALIGN/CARD/BG/RENDER/UPLOAD)."""
    log(f"--- {name} ---" + (f" {detail}" if detail else ""))


# Glob patterns for per-run intermediates. The pexels_* cache is
# intentionally NOT listed (re-downloads waste time/bandwidth), and no
# pattern matches it.
INTERMEDIATE_PATTERNS = ("seg_*.mp4", "slide_*.png",
                         "card_*.png", "*.ass", "bg_*.mp4", "beat_*.mp3",
                         "bed.mp3", "concat.txt", "video_only.mp4", "tts_*",
                         "tts_parts.txt")


def cleanup_intermediates(workdir: Path | None = None) -> None:
    """Delete per-run intermediates + workdir. Keeps the Pexels cache."""
    for pattern in INTERMEDIATE_PATTERNS:
        for p in ASSETS.glob(pattern):
            try:
                p.unlink()
            except OSError as e:
                log(f"cleanup warning: {p} ({e})")
    if workdir is not None:
        try:
            if Path(workdir).exists():
                shutil.rmtree(workdir, ignore_errors=True)
        except Exception as e:
            log(f"cleanup warning: {workdir} ({e})")
    log("Cleaned intermediates (Pexels cache kept).")


def upload(video: Path, meta: dict):
    """Resumable YouTube upload (categoryId 22, VISIBILITY). Skips w/o creds."""
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


def notify_failure(error: Exception) -> None:
    """Best-effort failure ping via Discord and/or Telegram. Never raises."""
    if not (DISCORD_WEBHOOK_URL or (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)):
        return
    repo = os.getenv("GITHUB_REPOSITORY", "local")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    link = f"{server}/{repo}/actions/runs/{run_id}" if run_id else "local run"
    text = (f"shorts pipeline FAILED\n{type(error).__name__}: {error}\nRun: {link}")
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


def run(niche) -> dict:
    """One full Short: history -> content -> narration -> visuals.

    Writes out/<stamp>_short.{mp4,srt,json}, uploads, records history,
    cleans intermediates, and returns artifact paths + video id.
    """
    name = _niche_name(niche)
    niche = _as_protocol(niche)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    stem = OUT / f"{stamp}_short"
    workdir = OUT / f"{stamp}_work"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        _stage("STORY", f"niche='{name}'")
        history = load_history()
        cfg = _resolve_config(niche, name)
        log(f"History: {len(history)} entries; config={type(cfg).__name__}")
        content = niche.generate_content(history, cfg)
        log(f"Content: '{content.title}' "
            f"({len(content.narration_text.split())} words)")

        _stage("TTS")
        narration = niche.narrate(content, cfg, workdir)
        log(f"Audio: {narration.audio} ({narration.duration:.1f}s, "
            f"{narration.words} words, voice={narration.voice})")

        _stage("ALIGN")
        words = (narration.extras or {}).get("words") or []
        log(f"Align: {len(words)} timed words; "
            f"beat_durations={(narration.extras or {}).get('beat_durations') is not None}")

        _stage("CARD")
        _stage("BG")
        _stage("RENDER")
        visuals = niche.build_visuals(content, narration, cfg, workdir)
        log(f"Visuals: {visuals.mp4} "
            f"(cuts={(visuals.extras or {}).get('cuts')}, "
            f"subs={(visuals.extras or {}).get('subtitles_burned')})")

        out_mp4 = stem.with_suffix(".mp4")
        shutil.copy2(visuals.mp4, out_mp4)
        src_srt = visuals.srt or (narration.extras or {}).get("srt")
        out_srt = stem.with_suffix(".srt")
        if src_srt is not None and Path(src_srt).exists():
            shutil.copy2(src_srt, out_srt)
        else:
            warn("No captions produced; continuing without .srt.")
            out_srt = None
        meta = niche.build_metadata(content, narration, visuals)
        out_json = stem.with_suffix(".json")
        out_json.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                            encoding="utf-8")

        _stage("UPLOAD")
        video_id = upload(out_mp4, meta)

        entry = {"date": stamp, "title": content.title, "niche": name,
                 "tags": list(content.tags)[:10],
                 "narration": content.narration_text,
                 "videoId": video_id}
        slots = (content.extras or {}).get("slots") or {}
        for key in ("topic", "topic_source", "topicSource", "category"):
            if slots.get(key):
                entry[key] = slots[key]
        save_history(entry)
        log("Done.")
        return {"mp4": out_mp4, "srt": out_srt, "metadata": out_json,
                "video_id": video_id}
    finally:
        cleanup_intermediates(workdir)
