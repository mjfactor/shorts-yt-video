"""Pexels stock: portrait search, closest-to-canvas pick, id cache, prefetch."""
from __future__ import annotations

import random
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from .ffmpeg_utils import retry, warn

SEARCH_URL = "https://api.pexels.com/videos/search"

# ---------------------------------------------------------------------------
# Opt 2: module-level session with connection pooling.
# Reusing TCP connections across all API calls and downloads eliminates the
# per-request TLS handshake overhead (~100-200 ms each on a cold socket).
# pool_connections=8 keeps sockets open to 8 distinct hosts (CDN shards);
# pool_maxsize=20 allows up to 20 concurrent live connections per host.
# ---------------------------------------------------------------------------
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=20))
_SESSION.mount("http://",  HTTPAdapter(pool_connections=8, pool_maxsize=20))


def search_videos(query: str, api_key: str, per_page: int = 30,
                   orientation: str = "portrait", size: str = "medium",
                   timeout: int = 30,
                   session: requests.Session | None = None) -> list[dict]:
    """GET portrait videos; exponential backoff on 429. Returns video dicts.

    `session` is optional — callers can pass a shared session for extra
    connection reuse; defaults to the module-level pooled session.
    """
    sess = session or _SESSION
    result: list[dict] = []

    def _search():
        resp = sess.get(
            SEARCH_URL, headers={"Authorization": api_key},
            params={"query": query, "orientation": orientation,
                    "size": size, "per_page": per_page},
            timeout=timeout,
        )
        if resp.status_code == 429:
            raise requests.HTTPError("429 rate-limited", response=resp)
        resp.raise_for_status()
        return resp.json()

    for attempt in range(4):
        try:
            data = _search()
            result = (data or {}).get("videos", [])
            break
        except Exception as e:
            resp = getattr(e, "response", None)
            code = getattr(resp, "status_code", None)
            if code == 429 and attempt < 3:
                time.sleep(2 ** attempt + random.random())
                continue
            raise
    return result


def pick_video_file(video_files: list[dict], target_w: int = 1080,
                     target_h: int = 1920) -> dict | None:
    """Pick exact video/mp4: smallest height>=target_h, else tallest."""
    cands = [f for f in (video_files or [])
             if f.get("link")
             and str(f.get("file_type", "")).lower() == "video/mp4"]
    if not cands:
        return None

    def _h(f: dict) -> int:
        return int(f.get("height", 0) or 0)

    tall = [f for f in cands if _h(f) >= target_h]
    if tall:
        return min(tall,
                   key=lambda f: (_h(f), abs(int(f.get("width", 0) or 0)
                                            - target_w)))
    return max(cands, key=_h)


def parse_photographer(video: dict) -> str:
    """Photographer/user name from a Pexels video dict."""
    user = video.get("user") or {}
    return str(user.get("name", "") or video.get("photographer", "") or "").strip()


def cache_path(cache_dir: str | Path, video_id: int | str) -> Path:
    """Cache slot <cache_dir>/<id>.mp4 (dir created on demand)."""
    d = Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{video_id}.mp4"


def download(url: str, dest: str | Path, timeout: int = 120,
             session: requests.Session | None = None) -> Path:
    """Stream-download a clip; partial files are deleted on failure.

    `session` is optional — pass the shared pooled session to reuse the
    already-open TCP connection to Pexels CDN.
    Opt 3: copyfileobj uses 1 MB chunks (vs 16 KB default) to reduce
    Python-level loop overhead on large video files.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sess = session or _SESSION

    def _get():
        with sess.get(url, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                # 1 MB read buffer: fewer Python iterations per file vs
                # the 16 KB shutil default (especially noticeable on CI).
                shutil.copyfileobj(r.raw, f, length=1024 * 1024)

    try:
        retry("Pexels download", _get, tries=2, backoff=5)
    except Exception:
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        raise
    return dest


def fetch_clip(query: str, api_key: str, cache_dir: str | Path, *,
                per_page: int = 30, used: set | None = None,
                target_w: int = 1080, target_h: int = 1920,
                session: requests.Session | None = None) -> Path | None:
    """First unused clip for `query`, cached by id. None on no key/fail."""
    if not api_key:
        return None
    sess = session or _SESSION
    try:
        for video in search_videos(query, api_key, per_page=per_page,
                                   session=sess):
            vid = video.get("id")
            if vid is None:
                continue
            if used is not None and vid in used:
                continue
            picked = pick_video_file(video.get("video_files", []),
                                     target_w=target_w, target_h=target_h)
            if not picked:
                continue
            dest = cache_path(cache_dir, vid)
            if not dest.exists():
                download(picked["link"], dest, session=sess)
            if used is not None:
                used.add(vid)
            return dest
    except Exception as e:
        warn(f"Pexels failed for '{query}' ({e}).")
    return None


def prefetch(queries: list[str], api_key: str, cache_dir: str | Path, *,
              max_workers: int = 4, per_page: int = 30) -> dict[str, Path | None]:
    """Fetch clips for many queries concurrently. Per-query failures -> None."""
    out: dict[str, Path | None] = {}
    if not queries:
        return out
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futs = {pool.submit(fetch_clip, q, api_key, cache_dir,
                            per_page=per_page, session=_SESSION): q
                for q in dict.fromkeys(queries)}
        for fut in as_completed(futs):
            q = futs[fut]
            try:
                out[q] = fut.result()
            except Exception as e:
                warn(f"Pexels prefetch failed for '{q}' ({e}).")
                out[q] = None
    return out
