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
import subprocess
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


# Glob patterns for per-run intermediates. The stock-footage cache
# (assets/pexels_cache) is intentionally NOT listed (re-downloads waste
# time/bandwidth), and no pattern matches it.
INTERMEDIATE_PATTERNS = ("seg_*.mp4", "slide_*.png",
                         "card_*.png", "*.ass", "bg_*.mp4", "beat_*.mp3",
                         "bed.mp3", "concat.txt", "video_only.mp4", "tts_*",
                         "tts_parts.txt")


def cleanup_intermediates(workdir: Path | None = None) -> None:
    """Delete per-run intermediates + workdir. Keeps the download cache."""
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
    log("Cleaned intermediates (download cache kept).")


# Thumbnail robustness (success-but-Studio-shows-auto): YouTube still
# processes the video right after videos().insert returns the ID, so an
# immediate thumbnails().set can 200-accept-then-discard (auto-frames win)
# or 200-success-but-ignore on non-YPP Shorts. Sequence is warn-only and
# never fails the run: wait for processing -> set (log items[0]) ->
# verify hasCustomThumbnail with bounded retries.
THUMB_PROCESS_TIMEOUT_SEC = 180
THUMB_PROCESS_POLL_SEC = 10
THUMB_VERIFY_DELAY_SEC = 10
THUMB_SET_RETRIES = 3


def _thumb_tuning() -> tuple[float, float, float, int]:
    """Thumbnail wait/verify tuning (env overrides win, call-time read)."""
    def _f(name: str, default: float) -> float:
        raw = (os.getenv(name, "") or "").strip()
        if not raw:
            return default
        try:
            val = float(raw)
        except ValueError:
            warn(f"{name}={raw!r} invalid; using default {default}.")
            return default
        return val if val >= 0 else default

    def _i(name: str, default: int) -> int:
        raw = (os.getenv(name, "") or "").strip()
        if not raw:
            return default
        try:
            val = int(float(raw))
        except ValueError:
            warn(f"{name}={raw!r} invalid; using default {default}.")
            return default
        return val if val > 0 else default

    return (_f("YT_THUMB_WAIT_TIMEOUT_SEC", THUMB_PROCESS_TIMEOUT_SEC),
            _f("YT_THUMB_WAIT_POLL_SEC", THUMB_PROCESS_POLL_SEC),
            _f("YT_THUMB_VERIFY_DELAY_SEC", THUMB_VERIFY_DELAY_SEC),
            _i("YT_THUMB_SET_RETRIES", THUMB_SET_RETRIES))


def _wait_for_video_processable(yt, video_id: str) -> bool:
    """Poll until processingStatus==succeeded + thumbnails usable.

    Bounded timeout; failed/terminated stops early. Warn-only: always
    returns True/False, never raises (caller still attempts set).
    """
    timeout, poll, _, _ = _thumb_tuning()
    deadline = time.time() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = yt.videos().list(
                part="processingDetails,status",
                id=video_id).execute() or {}
        except Exception as e:
            status = _http_status(e)
            if status == 429:
                warn(f"YouTube thumbnail wait rate-limited (429), backing off.")
            elif status == 403:
                warn("YouTube thumbnail wait skipped (HTTP 403: OAuth token lacks read scope; "
                     "re-run get_refresh_token.py with full youtube scope to enable); "
                     "proceeding to set anyway.")
            else:
                warn(f"YouTube thumbnail wait lookup failed "
                     f"({type(e).__name__}: {e}); proceeding to set anyway.")
            return False
        items = resp.get("items") or []
        if not items:
            warn("YouTube thumbnail wait: video not yet visible "
                 "(empty videos.list); proceeding to set anyway.")
            return False
        details = items[0].get("processingDetails") or {}
        proc_status = str(details.get("processingStatus", "") or "")
        thumbs_avail = str(details.get("thumbnailsAvailability", "") or "")
        if proc_status.lower() == "succeeded" and (
                not thumbs_avail or thumbs_avail.lower() == "available"):
            # No thumbnailsAvailability field on some responses: succeeded
            # alone means processable; proceed to set.
            return True
        if proc_status.lower() in ("failed", "terminated"):
            warn(f"YouTube thumbnail wait stopped early "
                 f"(processingStatus={proc_status!r}); attempting set anyway.")
            return False
        now = time.time()
        if now >= deadline:
            warn(f"YouTube thumbnail wait timed out after {timeout:.0f}s "
                 f"(last processingStatus={proc_status!r} "
                 f"thumbnailsAvailability={thumbs_avail!r}); "
                 f"attempting set anyway (may be overwritten by processing).")
            return False
        wait = min(poll * (1.3 ** (attempt - 1)), 30.0, max(deadline - now, 0.1))
        log(f"Thumbnail wait: processingStatus={proc_status!r} "
            f"thumbnailsAvailability={thumbs_avail!r} "
            f"(attempt {attempt}); rechecking in {wait:.0f}s...")
        time.sleep(wait)


def _set_thumbnail_once(yt, video_id: str, thumb_path: Path):
    """One thumbnails().set call (429/5xx retried). Returns API response.

    Raises on failure (caller maps 403/429 to clear warns). Logs
    response items[0] so no-exception is never mistaken for success.
    """
    from googleapiclient.http import MediaFileUpload

    def do():
        return yt.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumb_path),
                                       mimetype="image/jpeg")).execute()
    resp = retry("YouTube thumbnail set", do, tries=3, backoff=10)
    try:
        items = (resp or {}).get("items") or []
        first = items[0] if items else resp
        log(f"Thumbnail set response: {first!r}")
    except Exception:
        log(f"Thumbnail set response received (unparsed).")
    return resp


def _has_custom_thumbnail(yt, video_id: str) -> bool | None:
    """Owner verify: contentDetails.hasCustomThumbnail==true.

    Returns:
        True: verified custom thumbnail is active.
        False: check succeeded, but hasCustomThumbnail is not true.
        None: check failed (e.g. HTTP 403 lacks read scope or API error).
    """
    try:
        resp = yt.videos().list(
            part="snippet,contentDetails",
            id=video_id).execute() or {}
    except Exception as e:
        status = _http_status(e)
        if status == 403:
            warn("YouTube thumbnail verify skipped (HTTP 403: OAuth token lacks read scope; "
                 "re-run get_refresh_token.py with full youtube scope to enable).")
        else:
            warn(f"YouTube thumbnail verify lookup failed "
                 f"({type(e).__name__}: {e}).")
        return None
    items = resp.get("items") or []
    if not items:
        return False
    details = items[0].get("contentDetails") or {}
    return details.get("hasCustomThumbnail") is True


def _set_and_verify_thumbnail(yt, video_id: str, thumb_path: Path) -> bool:
    """Wait -> set -> verify loop (bounded retries). Warn-only, never raises."""
    _, _, verify_delay, max_tries = _thumb_tuning()
    _wait_for_video_processable(yt, video_id)
    for attempt in range(1, max_tries + 1):
        try:
            _set_thumbnail_once(yt, video_id, thumb_path)
        except Exception as e:
            status = _http_status(e)
            if status == 403:
                warn(f"YouTube thumbnail not applied (HTTP 403: {e}). "
                     f"Channel likely unverified or OAuth lacks permission; "
                     f"verify in YouTube Studio (desktop) that custom "
                     f"thumbnails are enabled.")
                return False
            warn(f"YouTube thumbnail skipped ({type(e).__name__}: {e}).")
            return False
        time.sleep(verify_delay)
        verified = _has_custom_thumbnail(yt, video_id)
        if verified is True:
            log(f"Thumbnail set from {thumb_path.name} "
                f"(verified hasCustomThumbnail=true).")
            # Clean up thumbnail file after successful upload
            try:
                thumb_path.unlink(missing_ok=True)
                log(f"Thumbnail file {thumb_path.name} deleted after successful upload.")
            except Exception as e:
                warn(f"Failed to delete thumbnail {thumb_path.name}: {e}")
            return True
        if verified is None:
            # Lookup failed (e.g. HTTP 403 token lacks read scope or API error).
            # The set call already succeeded above; do not retry or warn success-but-ignored.
            log(f"Thumbnail set from {thumb_path.name} "
                f"(set accepted; verification skipped).")
            # Clean up thumbnail file after successful upload
            try:
                thumb_path.unlink(missing_ok=True)
                log(f"Thumbnail file {thumb_path.name} deleted after successful upload.")
            except Exception as e:
                warn(f"Failed to delete thumbnail {thumb_path.name}: {e}")
            return True
        if attempt < max_tries:
            log(f"Thumbnail verify attempt {attempt}/{max_tries}: "
                 f"hasCustomThumbnail not yet true; retrying set+verify...")
    warn(f"YouTube thumbnail success-but-ignored "
         f"(set accepted but Studio still shows auto-frames; "
         f"hasCustomThumbnail=false after {max_tries}x verify). "
         f"Shorts/custom-thumbnail gating is the usual cause; set it "
         f"manually in YouTube Studio on desktop (mobile cannot set "
         f"custom Shorts thumbnails).")
    return False


def upload(video: Path, meta: dict, thumb: Path | str | None = None):
    """Resumable YouTube upload (categoryId 22, VISIBILITY). Skips w/o creds.

    When `thumb` points at an existing JPG, it is set as the video
    thumbnail via thumbnails().set (warn-only — unverified channel /
    oversize / HttpError never fails the run).
    """
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
                        token_uri="https://oauth2.googleapis.com/token")
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
    if thumb is not None:
        try:
            thumb_path = Path(thumb)
            if not thumb_path.exists():
                warn(f"YouTube thumbnail skipped (missing file: {thumb_path}).")
            elif thumb_path.suffix.lower() not in (".jpg", ".jpeg"):
                warn(f"YouTube thumbnail skipped (not a JPEG: {thumb_path}).")
            elif thumb_path.stat().st_size >= 2 * 1024 * 1024:
                warn(f"YouTube thumbnail skipped "
                     f"({thumb_path.stat().st_size / 1024:.0f} KB >= 2MB limit).")
            else:
                # Robust sequence: wait for processing -> set -> verify.
                # Warn-only; never fails the run.
                _set_and_verify_thumbnail(yt, resp["id"], thumb_path)
        except Exception as e:
            warn(f"YouTube thumbnail skipped ({type(e).__name__}: {e}).")
    return resp["id"]


# ---------- TikTok via Buffer (free, no TikTok audit needed) ----------
# Buffer's own TikTok app is already audited, so posts via Buffer can be
# public — unlike a self-registered TikTok Direct-Post app, which is
# SELF_ONLY/private until it passes TikTok's audit.
# Docs: https://developers.buffer.com (auth / create-video-post /
# hosting-media / posts-and-scheduling / character-limits / error-handling).
# Env is read at call time (not import time) so tests can mock env safely.
BUFFER_API_URL = "https://api.buffer.com"
# TikTok captions with a video attached: max 2200 UTF-16 code units
# (developers.buffer.com/guides/character-limits.html).
TIKTOK_CAPTION_LIMIT = 2200
# Cloudinary free unsigned uploads cap at 100MB hard (chunked uploads and
# incoming eager transforms do NOT raise it). The 1080x1920 YouTube master
# (~103MB) would HTTP 413 there, so large files get a 720p proxy for the
# TikTok-via-Buffer path only. Threshold sits ~10% under the cap as margin;
# override at call time with BUFFER_TIKTOK_PROXY_THRESHOLD_MB when needed.
CLOUDINARY_FREE_CAP_BYTES = 100 * 1024 * 1024
TIKTOK_PROXY_THRESHOLD_BYTES = 90 * 1024 * 1024


def _tiktok_proxy_threshold_bytes() -> int:
    """Proxy threshold, honouring an optional MB env override (read at call time)."""
    raw = (os.getenv("BUFFER_TIKTOK_PROXY_THRESHOLD_MB", "") or "").strip()
    if raw:
        try:
            return int(float(raw) * 1024 * 1024)
        except ValueError:
            warn(f"BUFFER_TIKTOK_PROXY_THRESHOLD_MB={raw!r} invalid; "
                 f"using default {TIKTOK_PROXY_THRESHOLD_BYTES} bytes.")
    return TIKTOK_PROXY_THRESHOLD_BYTES


def _transcode_tiktok_proxy(src: Path) -> Path | None:
    """Transcode a smaller 720x1280 proxy next to `src` for Cloudinary upload.

    Returns the proxy path, or None on any ffmpeg failure (caller falls back
    to the original-file attempt). The YouTube master is never modified.
    """
    proxy = src.with_name(f"{src.stem}_tiktok_proxy.mp4")
    cmd = ["ffmpeg", "-y", "-i", str(src),
           "-vf", "scale=720:1280",
           "-c:v", "libx264", "-crf", "27", "-preset", "veryfast",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
           "-movflags", "+faststart", str(proxy)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    except Exception as e:
        warn(f"TikTok proxy transcode failed ({type(e).__name__}: {e}); "
             f"falling back to original file.")
        try:
            if proxy.exists():
                proxy.unlink()
        except OSError:
            pass
        return None
    try:
        size = proxy.stat().st_size
    except OSError as e:
        warn(f"TikTok proxy unreadable ({e}); falling back to original file.")
        return None
    if size >= CLOUDINARY_FREE_CAP_BYTES:
        warn(f"TikTok proxy still {size / 1024 / 1024:.1f}MB "
             f"(>= {CLOUDINARY_FREE_CAP_BYTES / 1024 / 1024:.0f}MB cap); "
             f"uploading proxy anyway (smaller than master).")
    else:
        log(f"TikTok proxy: {proxy.name} "
            f"({size / 1024 / 1024:.1f}MB < "
            f"{CLOUDINARY_FREE_CAP_BYTES / 1024 / 1024:.0f}MB cap).")
    return proxy


def _buffer_graphql(query: str, api_key: str, variables: dict | None = None,
                    stage: str = "graphql") -> dict:
    """POST one GraphQL query to Buffer. Retries transport errors; raises on
    system errors (errors[] array: UNAUTHORIZED/FORBIDDEN/NOT_FOUND/etc.).

    `stage` tags logs (orgs/channels/createPost) so failures are
    attributable. HTTP failures log status + first 2KB of body. A 500 here
    means pre-GraphQL transport (never HTTP 200 + errors[]/MutationError),
    so it is retried via retry(); other 4xx fail fast (429 honored)."""
    def do():
        payload: dict = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        r = requests.post(
            BUFFER_API_URL,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=60,
        )
        try:
            r.raise_for_status()
        except Exception:
            try:
                status = getattr(r, "status_code", "?")
            except Exception:
                status = "?"
            try:
                body = (getattr(r, "text", "") or "")[:2048]
            except Exception:
                body = "<unreadable body>"
            log(f"Buffer {stage}: HTTP {status} failure; body[:2KB]={body!r}")
            raise
        return r.json()
    data = retry(f"Buffer {stage}", do, tries=3, backoff=10)
    if isinstance(data, dict) and data.get("errors"):
        first = data["errors"][0]
        code = (first.get("extensions") or {}).get("code", "")
        log(f"Buffer {stage}: GraphQL errors[] code={code or '?'} "
            f"message={first.get('message', '?')!r}")
        raise ValueError(f"Buffer API error ({code}): {first.get('message', '?')}")
    return data.get("data") or {}


def _buffer_tiktok_channel(api_key: str, explicit_id: str) -> str | None:
    """TikTok channel id: explicit env wins, else auto-pick service==tiktok."""
    if explicit_id:
        return explicit_id
    data = _buffer_graphql(
        "query GetOrganizations { account { organizations { id name } } }",
        api_key, stage="orgs")
    orgs = ((data.get("account") or {}).get("organizations")) or []
    if not orgs:
        raise ValueError("Buffer account has no organizations.")
    for org in orgs:
        oid = org.get("id", "")
        data = _buffer_graphql(
            "query GetChannels($orgId: OrganizationId!) { channels(input: "
            "{ organizationId: $orgId }) { id name displayName service } }",
            api_key, variables={"orgId": oid}, stage="channels")
        for ch in data.get("channels") or []:
            if str(ch.get("service", "")).lower() == "tiktok":
                label = ch.get("displayName") or ch.get("name") or "?"
                log(f"Buffer channels: using TikTok channel '{label}' ({ch['id']})")
                return ch["id"]
    raise ValueError("No TikTok channel found in Buffer (connect one at account.buffer.com/channels).")


def _cloudinary_upload(video: Path, cloud_name: str, preset: str) -> str:
    """Host the mp4 at a public URL (Buffer has no upload endpoint — the
    asset url must be public HTTPS and stay live until the post publishes).
    Uses a Cloudinary unsigned preset (free tier); returns secure_url."""
    def do():
        with open(video, "rb") as f:
            r = requests.post(
                f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload",
                files={"file": (video.name, f, "video/mp4")},
                data={"upload_preset": preset},
                timeout=300,
            )
        try:
            r.raise_for_status()
        except Exception:
            try:
                status = getattr(r, "status_code", "?")
            except Exception:
                status = "?"
            try:
                body = (getattr(r, "text", "") or "")[:2048]
            except Exception:
                body = "<unreadable body>"
            log(f"Buffer cloudinary: HTTP {status} failure; body[:2KB]={body!r}")
            raise
        return r.json()
    resp = retry("Buffer cloudinary", do, tries=3, backoff=10)
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


def _buffer_public_url_ok(url: str) -> bool:
    """Logged-out reachability probe for the hosted asset URL (warn-only).

    Buffer fetches the asset server-side, so a non-public URL fails the
    post. HEAD first, then a short ranged GET fallback (some CDNs reject
    HEAD). Any 2xx/3xx counts as reachable; all errors return False."""
    for method in ("head", "get"):
        try:
            if method == "head":
                r = requests.head(url, timeout=15, allow_redirects=True)
            else:
                r = requests.get(url, timeout=15, stream=True,
                                 headers={"Range": "bytes=0-0"})
            status = getattr(r, "status_code", 0) or 0
            if 200 <= status < 400:
                return True
            log(f"Buffer cloudinary: reachability {method.upper()} "
                f"got HTTP {status} for {url}")
        except Exception as e:
            log(f"Buffer cloudinary: reachability {method.upper()} "
                f"error for {url}: {type(e).__name__}: {e}")
    return False


def buffer_tiktok_upload(video: Path, meta: dict):
    """Queue the Short to TikTok via Buffer. Never raises (warn + None)."""
    api_key = os.getenv("BUFFER_API_KEY", "")
    if not api_key:
        warn("Buffer key missing -> skipping TikTok (YouTube path unaffected).")
        return None
    raw_cloud = (os.getenv("CLOUDINARY_CLOUD_NAME", "") or "").strip()
    raw_preset = (os.getenv("CLOUDINARY_UPLOAD_PRESET", "") or "").strip()
    cloud_name = raw_cloud or "epuxrxa5"
    preset = raw_preset or "buffer_shorts"
    if not raw_cloud or not raw_preset:
        log("Buffer cloudinary: using shared default "
            f"(cloud={cloud_name!r} preset={preset!r}; override with "
            "CLOUDINARY_CLOUD_NAME/CLOUDINARY_UPLOAD_PRESET).")
    try:
        channel_id = _buffer_tiktok_channel(
            api_key, os.getenv("BUFFER_TIKTOK_CHANNEL_ID", ""))
        upload_target = Path(video)
        proxy: Path | None = None
        try:
            try:
                src_size = upload_target.stat().st_size
            except OSError:
                src_size = -1
            if src_size >= _tiktok_proxy_threshold_bytes():
                log(f"Buffer cloudinary: master {src_size / 1024 / 1024:.1f}MB "
                    f"near 100MB unsigned cap; transcoding 720p TikTok proxy "
                    f"(YouTube master untouched).")
                try:
                    proxy = _transcode_tiktok_proxy(upload_target)
                except Exception as e:
                    warn(f"TikTok proxy path failed ({type(e).__name__}: {e}); "
                         f"falling back to original file.")
                    proxy = None
                if proxy is not None:
                    upload_target = proxy
            public_url = _cloudinary_upload(upload_target, cloud_name, preset)
            if not _buffer_public_url_ok(public_url):
                warn(f"TikTok via Buffer skipped (public_url not publicly "
                     f"reachable: {public_url}).")
                return None
            caption = _tiktok_caption(meta)
            mutation = (
                "mutation CreatePost($text: String!, $channelId: ChannelId!, $url: String!) { "
                "createPost( input: { "
                "text: $text channelId: $channelId "
                "schedulingType: automatic mode: addToQueue "
                "assets: [{ video: { url: $url "
                "metadata: { thumbnailOffset: 2000 } } }] } ) { "
                "... on PostActionSuccess { post { id dueAt } } "
                "... on LimitReachedError { message } "
                "... on InvalidInputError { message } "
                "... on MutationError { message } } }"
            )
            data = _buffer_graphql(
                mutation, api_key,
                variables={"text": caption, "channelId": channel_id,
                           "url": public_url},
                stage="createPost")
            result = data.get("createPost") or {}
            post = result.get("post")
            if post and post.get("id"):
                log(f"TikTok queued via Buffer -> post={post['id']} dueAt={post.get('dueAt', '?')}")
                return post["id"]
            raise ValueError(f"Buffer rejected post: {result.get('message', '?')}")
        finally:
            if proxy is not None and proxy != Path(video):
                try:
                    if proxy.exists():
                        proxy.unlink()
                        log(f"TikTok proxy removed: {proxy.name}.")
                except OSError as e:
                    log(f"TikTok proxy cleanup warning: {proxy} ({e})")
    except Exception as e:
        warn(f"TikTok via Buffer skipped ({type(e).__name__}: {e}).")
        return None


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
        thumb_src = (visuals.extras or {}).get("thumb")
        out_thumb = OUT / f"{stamp}_thumb.jpg"
        if thumb_src is not None and Path(thumb_src).exists():
            shutil.copy2(thumb_src, out_thumb)
        else:
            out_thumb = None

        _stage("UPLOAD")
        video_id = upload(out_mp4, meta, thumb=out_thumb)
        # TikTok dual-post via Buffer: independent of YouTube, never fails
        # the run (warn + None on missing creds or any error).
        tiktok_post_id = buffer_tiktok_upload(out_mp4, meta)

        entry = {"date": stamp, "title": content.title, "niche": name,
                 "tags": list(content.tags)[:10],
                 "narration": content.narration_text,
                 "videoId": video_id,
                 "tiktokBufferPostId": tiktok_post_id,
                 "used_ids": list((visuals.used_ids or []))}
        slots = (content.extras or {}).get("slots") or {}
        for key in ("topic", "topic_source", "topicSource", "category"):
            if slots.get(key):
                entry[key] = slots[key]
        narrator = str((content.extras or {}).get("narrator_gender", "")
                       or slots.get("narrator_gender", "")).strip()
        if narrator:
            entry["narratorGender"] = narrator
        save_history(entry)
        log("Done.")
        return {"mp4": out_mp4, "srt": out_srt, "metadata": out_json,
                "thumb": out_thumb, "video_id": video_id,
                "tiktok_buffer_post_id": tiktok_post_id}
    finally:
        cleanup_intermediates(workdir)
