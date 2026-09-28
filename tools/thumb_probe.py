#!/usr/bin/env python3
"""thumb_probe: does thumbnails().set actually apply to a YouTube Short?

Standalone diagnostic, NOT part of the pipeline. It imports nothing from the
repo (no core, no niches, no engine) — history.json is only read as JSON via
pathlib. Never uploads or edits a video. The single write it performs is the
one thumbnails().set call under test, against a real uploaded Short.

Question answered: does the Data API v3 thumbnails().set endpoint actually
change the thumbnail of a Short, or does it return 200 and get discarded?

Three mutually exclusive modes:

  (default)     set       -- uploads the test card and verifies (the only
                             mode that mutates anything)
  --readback            -- READ-ONLY. Cache-busted fetch of the images
                             i.ytimg.com serves RIGHT NOW plus a Pillow
                             pixel classification (magenta card vs video
                             frame). Uploads nothing, mutates nothing.
  --verify-only         -- READ-ONLY. Re-reads hasCustomThumbnail + the
                             thumbnail URL set and nothing else. No upload, no
                             image fetch. Compares against a prior report at
                             --json when one exists for the same video;
                             with no prior report it prints current state only
                             and says so (comparison is then manual).

Why --readback exists: i.ytimg.com serves `Cache-Control: public,
max-age=7200` (~2h edge cache, same URL, no invalidation), so a normal
thumbnail fetch can keep showing the OLD image long after a successful
set(). Appending a unique `?cb=<random>` token forces a fresh origin fetch,
which turns "has it settled?" from a waiting game into a read-only check.

Usage:
    python tools/thumb_probe.py                    # newest story upload, generated card
    python tools/thumb_probe.py --video-id ABC123  # explicit video
    python tools/thumb_probe.py --image out/x.jpg  # use your own JPEG/PNG
    python tools/thumb_probe.py --wait-sec 180     # longer CDN-cache window
    python tools/thumb_probe.py --readback         # read-only, no upload
    python tools/thumb_probe.py --verify-only      # read-only, no upload

SAFETY: the default (set) mode OVERWRITES the thumbnail of the chosen video
with a magenta "API THUMB TEST" card. That is the point of the test, but it
is a real mutation of a real Short. Restore it from YouTube Studio (desktop)
with "Select from video" afterwards. --readback and --verify-only mutate
nothing.

SECRETS: YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN are read from the
environment and NEVER printed, logged, or written to any report. Only the key
names and a present/missing boolean are ever emitted.

Exit codes:
    0  probe completed (verdict printed; see the report JSON)
    2  cannot run: missing YT_* env vars, or video not found / not visible
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "out"
HISTORY = REPO_ROOT / "history.json"
CARD_PATH = OUT / "thumb_probe_src.jpg"
DEFAULT_REPORT = "out/thumb_probe.json"

YT_ENV = ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")

# thumbnails().set only accepts these; the API rejects anything else (400
# invalidImage), which is why the test card is generated as JPEG.
MIMETYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

# ---------- readback (no upload, no mutation) ----------
# i.ytimg.com serves `Cache-Control: public, max-age=7200` (~2h edge cache,
# same URL, no invalidation), so a plain GET returns the OLD thumbnail long
# after a successful set(). Appending a unique ?cb= token forces a fresh
# origin fetch and shows what YouTube serves RIGHT NOW.
READBACK_SIZES = ("hqdefault", "maxresdefault", "sddefault")
READBACK_PRIMARY = "hqdefault"  # the one size that always exists
READBACK_UA = "thumb-probe/1.0 (read-only diagnostic)"
READBACK_TIMEOUT = 20.0

# Pixel-classification bands for the served thumbnail (see _analyze_image).
# A blend of card + frame usually lands between NEGLIGIBLE and PRESENT.
MAGENTA_PRESENT_MIN = 0.25       # >= this share of magenta-ish pixels
MAGENTA_NEGLIGIBLE_MAX = 0.05    # <= this share means the card is NOT served
VIDEO_FRAME_MIN_DISTINCT = 5     # of 8 quantize() buckets = "photo-like"
SAMPLE_MAX_EDGE = 512            # downscale cap for per-pixel sampling


# ---------- logging (mirrors core.log/warn/err, no core import) ----------

def _print(msg: str) -> None:
    """print() that survives non-UTF-8 consoles (Windows cp1252)."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode(), flush=True)


def log(msg: str) -> None:
    _print(f"[thumb-probe] {msg}")


def warn(msg: str) -> None:
    _print(f"::warning::{msg}")
    log(f"WARNING: {msg}")


def err(msg: str) -> None:
    _print(f"::error::{msg}")
    log(f"ERROR: {msg}")


def _http_status(e: Exception) -> int | None:
    """Best-effort HTTP status from googleapiclient / requests style errors."""
    direct = getattr(e, "status_code", None)
    if isinstance(direct, int):
        return direct
    resp = getattr(e, "resp", None) or getattr(e, "response", None)
    if resp is not None:
        try:
            code = getattr(resp, "status", None)
            if isinstance(code, int):
                return code
            if isinstance(resp, dict):
                return int(resp.get("status"))  # type: ignore[arg-type]
        except Exception:
            return None
    return None


def _api_reasons(e: Exception) -> list[str]:
    """error.errors[].reason values from an HttpError, best effort."""
    out: list[str] = []
    try:
        content = getattr(e, "content", None)
        if isinstance(content, (bytes, bytearray)):
            content = content.decode("utf-8", "replace")
        if isinstance(content, str):
            content = json.loads(content)
        for item in (content or {}).get("error", {}).get("errors", []):
            reason = item.get("reason")
            if reason:
                out.append(str(reason))
    except Exception:
        pass
    return out


# ---------- step 0: image source ----------

def _load_font(size: int):
    from PIL import ImageFont

    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "arialbd.ttf",
                 "arial.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:  # Pillow >= 10.1 can scale the built-in font
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - older Pillow
        return ImageFont.load_default()


def _make_test_card(path: Path) -> Path:
    """Magenta 1080x1920 card so the new thumb is unmistakable in Studio."""
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1080, 1920), (255, 0, 255))
    draw = ImageDraw.Draw(img)
    # Magenta frame: any non-magenta frame means the OLD thumb is still live.
    draw.rectangle([40, 40, 1040, 1880], outline=(0, 0, 0), width=18)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    big = _load_font(130)
    small = _load_font(64)
    for text, font, y in (("API THUMB", big, 640), ("TEST", big, 790),
                          (stamp, small, 1000), ("thumbnails().set probe", small, 1090)):
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((1080 - (box[2] - box[0])) / 2, y), text, fill=(0, 0, 0), font=font)

    img.save(path, format="JPEG", quality=92)
    return path


# ---------- step 0b: video id from history.json (read-only) ----------

def _pick_video_id() -> str | None:
    """Newest story entry with a real videoId; grouped or legacy flat shape."""
    if not HISTORY.exists():
        warn(f"history.json not found at {HISTORY}; pass --video-id explicitly.")
        return None
    try:
        data = json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception as e:
        warn(f"history.json unreadable ({type(e).__name__}: {e}); "
             f"pass --video-id explicitly.")
        return None

    entries: list[dict] = []
    if isinstance(data, dict):
        entries = list(data.get("story") or [])
        if not entries:
            warn("history.json has no 'story' entries; pass --video-id explicitly.")
    elif isinstance(data, list):
        # Legacy flat list: prefer entries tagged story, else take all.
        tagged = [e for e in data if isinstance(e, dict) and e.get("niche") == "story"]
        entries = tagged or [e for e in data if isinstance(e, dict)]
        warn("history.json is a legacy flat list; using flat fallback view.")
    else:
        warn(f"history.json has unexpected shape {type(data).__name__}; "
             f"pass --video-id explicitly.")
        return None

    usable = [e for e in entries
              if isinstance(e.get("videoId"), str)
              and str(e.get("videoId") or "").strip()]
    if not usable:
        warn("no story entry with a videoId in history.json; pass --video-id.")
        return None
    usable.sort(key=lambda e: str(e.get("date") or ""), reverse=True)
    chosen = usable[0]
    log(f"auto-picked videoId={chosen['videoId']} (date={chosen.get('date')!r})")
    return str(chosen["videoId"]).strip()


# ---------- step 2/4: videos().list snapshot ----------

def _snapshot(yt, video_id: str, label: str) -> dict:
    """videos().list(part=snippet,contentDetails,status). Never raises."""
    snap = {"label": label, "readable": False, "found": False,
            "thumbnails": {}, "hasCustomThumbnail": None,
            "privacyStatus": None, "http_status": None, "error": None,
            "error_reasons": []}
    try:
        resp = yt.videos().list(part="snippet,contentDetails,status",
                                id=video_id).execute() or {}
    except Exception as e:
        status = _http_status(e)
        snap.update(readable=False, http_status=status,
                    error=f"{type(e).__name__}: {e}",
                    error_reasons=_api_reasons(e))
        if status == 403:
            warn(f"{label} videos().list blocked (HTTP 403): this OAuth token "
                 f"likely lacks READ scope (upload-only scope is a known trap "
                 f"here). Verification is limited to the set() response; "
                 f"re-run get_refresh_token.py with the full youtube scope.")
        else:
            warn(f"{label} videos().list failed ({type(e).__name__}: {e}).")
        return snap

    items = resp.get("items") or []
    if not items:
        snap.update(readable=True, found=False, error="empty items list")
        return snap

    item = items[0]
    thumbs = ((item.get("snippet") or {}).get("thumbnails") or {})
    snap.update(
        readable=True,
        found=True,
        thumbnails={k: str(v.get("url")) for k, v in sorted(thumbs.items())
                    if isinstance(v, dict) and v.get("url")},
        hasCustomThumbnail=(item.get("contentDetails") or {}).get("hasCustomThumbnail"),
        privacyStatus=(item.get("status") or {}).get("privacyStatus"),
    )
    return snap


# ---------- step 3: thumbnails().set ----------

def _do_set(yt, video_id: str, image: Path) -> dict:
    """One thumbnails().set call. Never raises; records everything observed."""
    from googleapiclient.http import MediaFileUpload

    res = {"ok": False, "http_status": None, "item_urls": {},
           "error": None, "error_reasons": [], "mimetype": MIMETYPES.get(
               image.suffix.lower(), "image/jpeg")}
    try:
        media = MediaFileUpload(str(image), mimetype=res["mimetype"], resumable=False)
        resp = yt.thumbnails().set(videoId=video_id, media_body=media).execute() or {}
        res["ok"] = True
        for entry in (resp.get("items") or []):
            size = entry.get("size")
            if isinstance(size, dict):  # defensive: nested resource shape
                size, url = size.get("type"), entry.get("url")
            else:
                url = entry.get("url")
            if url:
                res["item_urls"][str(size or "?")] = str(url)
    except Exception as e:
        status = _http_status(e)
        res.update(http_status=status, error=f"{type(e).__name__}: {e}",
                   error_reasons=_api_reasons(e))
        if status == 400:
            err(f"thumbnails().set rejected the image (HTTP 400: {e}). "
                f"This is a bad image/request, NOT evidence about Shorts. "
                f"Check format (JPEG/PNG), aspect and the 2MB size limit.")
        elif status == 403:
            err(f"thumbnails().set forbidden (HTTP 403: {e}). Active rejection, "
                f"but not Shorts-specific (unverified channel / missing scope / "
                f"permissions), so it proves little about Shorts.")
        else:
            err(f"thumbnails().set failed (HTTP {status}: {e}). "
                f"Treat as transient/API error; retry the run.")
    return res


# ---------- readback: cache-busted i.ytimg.com fetch (no upload) ----------

def _cb_url(video_id: str, size: str) -> str:
    """Thumbnail URL with a unique ?cb= token to defeat the 2h edge cache."""
    return (f"https://i.ytimg.com/vi/{video_id}/{size}.jpg"
            f"?cb={os.urandom(6).hex()}")


def _fetch_cached_busted(url: str) -> tuple[dict, bytes | None]:
    """GET url. Returns (record, data). Never raises."""
    res = {"url": url, "ok": False, "http_status": None, "bytes": 0,
           "sha256_first12": None, "content_type": None, "path": None,
           "error": None}
    req = urllib.request.Request(
        url, headers={"User-Agent": READBACK_UA, "Cache-Control": "no-cache",
                      "Pragma": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=READBACK_TIMEOUT) as resp:
            data = resp.read()
            res["http_status"] = getattr(resp, "status", 200)
            res["content_type"] = resp.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        # maxresdefault legitimately 404s on low-res source videos.
        res["http_status"] = e.code
        res["error"] = f"HTTPError {e.code}: {e.reason}"
        if e.code == 404:
            # Expected for maxresdefault on low-res source, and for any size
            # when the id is wrong or the video is private.
            log(f"readback {size_label(url)}: HTTP 404 — no thumbnail at this "
                f"size for that id (low-res source, or a wrong/private id); "
                f"continuing.")
        else:
            warn(f"readback fetch failed ({e.code}: {e.reason}) for {url}")
        return res, None
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        warn(f"readback fetch failed ({type(e).__name__}: {e}) for {url}")
        return res, None
    res.update(ok=True, bytes=len(data),
               sha256_first12=hashlib.sha256(data).hexdigest()[:12])
    return res, data


def size_label(url: str) -> str:
    """Extract the size token (hqdefault/...) from a thumbnail URL."""
    tail = url.split("/")[-1].split("?")[0]
    return tail.rsplit(".", 1)[0] or "?"


# ---------- image analysis: is the served thumb our card or a video frame? ----------

def _analyze_image(path: Path) -> dict:
    """Classify the SERVED thumbnail. Never raises; UNREADABLE on any failure.

    Per-pixel sampling runs on a downscaled copy (SAMPLE_MAX_EDGE) for speed;
    the distinct-colour count uses the full RGB image quantized to 8 buckets.
    """
    res = {"ok": False, "path": str(path), "width": None, "height": None,
           "magenta_fraction": None, "black_fraction": None,
           "mean_luminance": None, "distinct_colors": None,
           "verdict": "UNREADABLE", "sentence": "Could not fetch or decode "
           "the served thumbnail, so no pixel verdict is possible."}
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover - Pillow is in requirements
        res["error"] = f"Pillow unavailable: {e}"
        return res
    try:
        with Image.open(path) as im:
            im.load()
            rgb = im.convert("RGB")
            res["width"], res["height"] = rgb.size
            small = rgb.copy()
            # Pillow>=10.0 is pinned in requirements.txt, so Image.Resampling
            # (added in 9.1) is always present.
            small.thumbnail((SAMPLE_MAX_EDGE, SAMPLE_MAX_EDGE), Image.Resampling.BILINEAR)
            # Iterate raw RGB bytes: faster than getdata() and stub-safe.
            raw = small.tobytes()
            magenta = black = 0
            lum_total = 0.0
            total = len(raw) // 3
            for i in range(0, total * 3, 3):
                r, g, b = raw[i], raw[i + 1], raw[i + 2]
                if r > 120 and b > 120 and g < 90:  # magenta-ish
                    magenta += 1
                if r < 60 and g < 60 and b < 60:    # near-black
                    black += 1
                lum_total += 0.299 * r + 0.587 * g + 0.114 * b
            total = max(1, total)
            buckets = rgb.quantize(colors=8).getcolors() or []
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        warn(f"image analysis failed for {path} ({type(e).__name__}: {e}).")
        return res

    mag_frac = magenta / total
    res.update(ok=True, magenta_fraction=round(mag_frac, 4),
               black_fraction=round(black / total, 4),
               mean_luminance=round(lum_total / total, 1),
               distinct_colors=len(buckets))

    if mag_frac >= MAGENTA_PRESENT_MIN:
        verdict = "MAGENTA_CARD_PRESENT"
        sentence = ("YouTube IS serving the custom thumbnail — at least "
                    f"{mag_frac:.0%} of the served pixels are the magenta test "
                    "card.")
        if mag_frac < 0.55:
            sentence += (" Note the share is not near-100%, so this is a "
                         "composite/blend rather than the clean card; open the "
                         "saved image to see it.")
    elif mag_frac <= MAGENTA_NEGLIGIBLE_MAX:
        if len(buckets) >= VIDEO_FRAME_MIN_DISTINCT:
            verdict = "VIDEO_FRAME_PRESENT"
            sentence = ("YouTube is serving a VIDEO FRAME, not the custom "
                        f"thumbnail (magenta {mag_frac:.1%}, "
                        f"{len(buckets)}/8 dominant colours).")
        else:
            verdict = "AMBIGUOUS_BLEND"
            sentence = (f"The test card is not being served (magenta only "
                        f"{mag_frac:.1%}), but the frame is too uniform "
                        f"({len(buckets)}/8 colours) to call it a video frame.")
    else:
        verdict = "AMBIGUOUS_BLEND"
        sentence = ("Partially updated / blended — inconclusive, re-run later "
                    f"(magenta {mag_frac:.1%}, black {black / total:.1%}, "
                    f"mean luminance {lum_total / total:.0f}, "
                    f"{len(buckets)}/8 dominant colours).")

    res.update(verdict=verdict, sentence=sentence)
    return res


# ---------- mode runners: readback / verify-only (never upload) ----------

def _run_readback(yt, video_id: str, report_path: Path) -> tuple[int, dict]:
    """Cache-busted readback. NO upload, NO mutation of any kind."""
    log(f"READBACK MODE for {video_id} — no upload will be attempted.")
    snap = _snapshot(yt, video_id, "readback")
    report = {"mode": "readback", "video_id": video_id,
              "snapshot": snap, "fetches": {}, "analysis": None,
              "started_utc": datetime.datetime.now(
                  datetime.timezone.utc).isoformat(timespec="seconds")}
    if snap["readable"] and snap["found"]:
        log(f"api state: privacyStatus={snap['privacyStatus']!r} "
            f"hasCustomThumbnail={snap['hasCustomThumbnail']!r} "
            f"snippet thumbnails={json.dumps(snap['thumbnails'])}")
    elif not snap["readable"]:
        warn("videos().list unreadable (token likely lacks READ scope); the "
             "image readback below still works — it needs no API scope.")
    else:
        warn("videos().list returned no items (private/unlisted video or the "
             "token does not own it). The image readback below still works.")

    # Cache-busted i.ytimg.com GETs: what is served RIGHT NOW.
    OUT.mkdir(parents=True, exist_ok=True)
    for size in READBACK_SIZES:
        res, data = _fetch_cached_busted(_cb_url(video_id, size))
        if data:
            dest = OUT / f"thumb_readback_{size}.jpg"
            dest.write_bytes(data)
            res["path"] = str(dest)
        report["fetches"][size] = res
        log(f"readback {size}: HTTP {res['http_status']} bytes={res['bytes']} "
            f"sha256[:12]={res['sha256_first12'] or '-'} "
            f"-> {res['path'] or '(not saved)'}")

    primary = report["fetches"].get(READBACK_PRIMARY) or {}
    if primary.get("ok") and primary.get("path"):
        analysis = _analyze_image(Path(primary["path"]))
        report["analysis"] = analysis
        log("IMAGE_ANALYSIS: " + json.dumps(
            {k: analysis[k] for k in ("magenta_fraction", "black_fraction",
                                      "mean_luminance", "distinct_colors",
                                      "width", "height")}))
        log(f"IMAGE_ANALYSIS: {analysis['verdict']} — {analysis['sentence']}")
        verdict = f"READBACK_{analysis['verdict']}"
        explanation = (f"Cache-busted readback of {video_id}: "
                       f"{analysis['verdict']}. {analysis['sentence']} "
                       f"The ?cb= token defeats the ~2h i.ytimg.com edge cache, "
                       f"so this reflects current serving, not a stale CDN copy.")
    else:
        verdict = "READBACK_UNREADABLE"
        explanation = (f"Could not fetch {READBACK_PRIMARY}.jpg for {video_id} "
                       f"(HTTP {primary.get('http_status')}); the served image "
                       f"could not be inspected.")
        err(f"readback: no usable {READBACK_PRIMARY} image to analyse.")

    report["verdict"] = verdict
    report["explanation"] = explanation
    log("=" * 68)
    log(f"READBACK: {verdict}")
    log(explanation)
    log("=" * 68)
    _write_json(report_path, report)
    _append_step_summary_mode(report, verdict, explanation)
    return 0, report


def _run_verify_only(yt, video_id: str, report_path: Path) -> tuple[int, dict]:
    """Re-read hasCustomThumbnail + URL set. NO upload, NO image fetch.

    Compares against a prior report at report_path when one exists, so it can
    say whether the state settled since the last run; with no prior report (or
    a prior one for a different video) it just prints current state and says
    so — comparison is then manual.
    """
    log(f"VERIFY-ONLY MODE for {video_id} — no upload, no image fetch.")
    snap = _snapshot(yt, video_id, "verify-only")
    report = {"mode": "verify-only", "video_id": video_id, "snapshot": snap,
              "comparison": None,
              "checked_utc": datetime.datetime.now(
                  datetime.timezone.utc).isoformat(timespec="seconds")}
    if not snap["readable"]:
        err("verify-only: videos().list unreadable (HTTP "
            f"{snap['http_status']}); current state unknown.")
        verdict, explanation = "VERIFY_UNREADABLE", (
            "videos().list was blocked, most likely an upload-only token "
            "missing READ scope. Re-run get_refresh_token.py with the full "
            "youtube scope, or use --readback (which needs no API scope).")
    elif not snap["found"]:
        err(f"verify-only: video {video_id} not found / not visible.")
        verdict, explanation = "VERIFY_VIDEO_NOT_FOUND", (
            "videos().list returned no items for this id.")
    else:
        log(f"current: privacyStatus={snap['privacyStatus']!r} "
            f"hasCustomThumbnail={snap['hasCustomThumbnail']!r} "
            f"thumbnails={json.dumps(snap['thumbnails'])}")

        prior, prior_hct, before_urls = None, None, {}
        if report_path.exists():
            try:
                prior = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception as e:
                warn(f"prior report at {report_path} unreadable "
                     f"({type(e).__name__}: {e}); comparison skipped.")
        if isinstance(prior, dict) and prior.get("video_id") == video_id:
            base = (prior.get("before") if prior.get("mode") == "set"
                    else prior.get("snapshot")) or {}
            prior_hct = base.get("hasCustomThumbnail")
            before_urls = base.get("thumbnails") or {}
        now_urls = snap["thumbnails"] or {}
        if prior_hct is not None or before_urls:
            changed = [s for s in set(before_urls) | set(now_urls)
                       if before_urls.get(s) != now_urls.get(s)]
            settled = not changed and prior_hct == snap["hasCustomThumbnail"]
            report["comparison"] = {
                "prior_report": str(report_path),
                "prior_hasCustomThumbnail": prior_hct,
                "current_hasCustomThumbnail": snap["hasCustomThumbnail"],
                "changed_sizes": sorted(changed), "settled": settled}
            verdict = "VERIFY_SETTLED" if settled else "VERIFY_CHANGED"
            explanation = (
                f"Compared with {report_path.name}: hasCustomThumbnail "
                f"{prior_hct!r} -> {snap['hasCustomThumbnail']!r}; changed sizes: "
                f"{sorted(changed) or 'none'}. "
                + ("State looks settled." if settled else
                   "State changed since the prior report — re-check later."))
        else:
            report["comparison"] = {
                "prior_report": None,
                "note": "no comparable prior report; comparison is manual"}
            verdict = "VERIFY_CURRENT_STATE"
            explanation = ("No comparable prior report found, so this is a "
                           "point-in-time reading only and comparison is "
                           "manual. Run --readback for a pixel-level answer, or "
                           "re-run --verify-only later to see if it moved.")
        log(f"comparison: {json.dumps(report['comparison'])}")

    report["verdict"] = verdict
    report["explanation"] = explanation
    log("=" * 68)
    log(f"VERIFY: {verdict}")
    log(explanation)
    log("=" * 68)
    _write_json(report_path, report)
    _append_step_summary_mode(report, verdict, explanation)
    return 0, report


# ---------- step 5: verdict ----------

def _verdict(set_res: dict, pre: dict, imm: dict, delayed: dict) -> tuple[str, str]:
    """Return (verdict, explanation). Precedence per spec."""
    if not set_res["ok"]:
        status = set_res.get("http_status")
        if status == 400:
            return "set_error_400", (
                "The set request was malformed and never reached the Shorts "
                "question. Fix the image (JPEG/PNG, <=2MB, sane aspect) and re-run.")
        if status == 403:
            return "set_error_403", (
                "The set call was actively rejected. 403 is not Shorts-specific "
                "(unverified channel, missing permissions, upload-only token), so "
                "it does not answer whether Shorts accept custom thumbnails.")
        return "set_error_other", (
            f"Transient or non-4xx API error (HTTP {status}). Nothing can be "
            f"concluded; re-run the probe.")

    # Only the VERIFY reads can prove a change; `pre` is just the baseline, so a
    # readable before with blocked verifies is still INCONCLUSIVE.
    reads = [s for s in (imm, delayed) if s["readable"]]
    hct_true = any(s.get("hasCustomThumbnail") is True for s in reads)
    changed: list[str] = []
    for label, snap in (("immediate", imm), ("delayed", delayed)):
        if not snap["readable"] or not snap["found"]:
            continue
        for size, url in snap["thumbnails"].items():
            if pre["thumbnails"].get(size) != url:
                changed.append(f"{size} ({label})")

    if changed or hct_true:
        signals = []
        if changed:
            signals.append("thumbnail URL changed: " + ", ".join(changed))
        if hct_true:
            signals.append("contentDetails.hasCustomThumbnail == true")
        return "SUCCESS_LIKELY", (
            "The API reports the custom thumbnail as applied ("
            + "; ".join(signals) + "). NOTE: URLs are CDN-cached, so treat the "
            "DELAYED check as authoritative. Confirm manually: open the Short in "
            "YouTube Studio on DESKTOP and check the channel Shorts tab and search "
            "results in an incognito window. Do NOT judge from the Shorts swipe "
            "feed — it autoplays the video and never shows a custom thumbnail.")

    if not reads:
        return "INCONCLUSIVE_NO_READ_SCOPE", (
            "set() returned 200 but every verify read was blocked (403), so the "
            "result cannot be determined programmatically. Re-run "
            "get_refresh_token.py with the full youtube read scope, or check "
            "YouTube Studio manually.")

    return "SILENTLY_IGNORED", (
        "set() returned 200, yet after both checks the thumbnail URLs are "
        "unchanged and hasCustomThumbnail is not true. This is the suspected "
        "real-world behaviour for Shorts (API accepts the upload, then discards "
        "it). Consequence: the thumbnails().set upload path does NOT work for "
        "Shorts and should not be relied on — set custom Shorts thumbnails by "
        "hand in YouTube Studio on desktop.")


# ---------- step 6/7: report + step summary ----------

def _write_json(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    log(f"report written -> {path}")


def _append_step_summary(report: dict, verdict: str, explanation: str) -> None:
    """Best effort: GITHUB_STEP_SUMMARY is unset locally, so this no-ops."""
    target = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not target:
        return
    set_res = report["set"]
    lines = [
        "## Shorts thumbnail API probe",
        "",
        f"**Verdict: `{verdict}`**",
        "",
        explanation,
        "",
        f"- Video ID: `{report['video_id']}`",
        f"- Image: `{report['image']}`",
        f"- Wait between checks: {report['wait_sec']}s",
        f"- thumbnails().set: "
        f"{'ok' if set_res['ok'] else 'FAILED'} "
        f"(HTTP {set_res.get('http_status')})",
        f"- URLs returned by set(): "
        f"{json.dumps(set_res.get('item_urls') or {}) or '(none)'}",
        "",
        "### Thumbnails before / after",
        "",
        "| size | before | after (delayed) |",
        "| --- | --- | --- |",
    ]
    before = (report.get("before") or {}).get("thumbnails") or {}
    after = (report.get("after_delayed") or {}).get("thumbnails") or {}
    for size in sorted(set(before) | set(after)):
        lines.append(f"| {size} | {before.get(size, '(none)')} | "
                     f"{after.get(size, '(none)')} |")
    lines += [
        "",
        "### Signals",
        "",
        f"- hasCustomThumbnail readable: "
        f"{(report.get('before') or {}).get('readable')}",
        f"- before: {json.dumps((report.get('before') or {}).get('hasCustomThumbnail'))}",
        f"- immediate: "
        f"{json.dumps((report.get('after_immediate') or {}).get('hasCustomThumbnail'))}",
        f"- delayed (authoritative): "
        f"{json.dumps((report.get('after_delayed') or {}).get('hasCustomThumbnail'))}",
        f"- privacyStatus: {json.dumps((report.get('before') or {}).get('privacyStatus'))}",
        "",
        "### Manual confirmation (required)",
        "",
        "1. Open the Short in **YouTube Studio on desktop** and check its thumbnail.",
        "2. Check the **channel Shorts tab** and **search results in an incognito "
        "window** (cache-free).",
        "3. Do **not** judge from the Shorts swipe feed — it autoplays the video "
        "and never renders a custom thumbnail.",
        "",
    ]
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as e:
        warn(f"could not write GITHUB_STEP_SUMMARY ({type(e).__name__}: {e}).")


def _append_step_summary_mode(report: dict, verdict: str, explanation: str) -> None:
    """Step-summary block for --readback / --verify-only (no upload involved)."""
    target = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not target:
        return
    snap = report.get("snapshot") or {}
    mode = report.get("mode", "?")
    lines = [
        f"## Shorts thumbnail probe — {mode}",
        "",
        f"**Verdict: `{verdict}`**",
        "",
        explanation,
        "",
        f"- Video ID: `{report.get('video_id')}`",
        f"- privacyStatus: {json.dumps(snap.get('privacyStatus'))}",
        f"- hasCustomThumbnail: {json.dumps(snap.get('hasCustomThumbnail'))}",
        f"- API read ok: {snap.get('readable')}",
    ]
    if mode == "readback":
        analysis = report.get("analysis") or {}
        lines.append("")
        lines.append("### Served image analysis (cache-busted `?cb=`)")
        lines.append("")
        lines.append("| size | HTTP | bytes | sha256[:12] | file |")
        lines.append("| --- | --- | --- | --- | --- |")
        for size, res in (report.get("fetches") or {}).items():
            lines.append(
                f"| {size} | {res.get('http_status')} | {res.get('bytes')} | "
                f"{res.get('sha256_first12') or '-'} | "
                f"{Path(res['path']).name if res.get('path') else '(none)'} |")
        lines += [
            "",
            f"- `IMAGE_ANALYSIS`: **{analysis.get('verdict')}**",
            f"- magenta-ish fraction: "
            f"{json.dumps(analysis.get('magenta_fraction'))} "
            f"(card present >= {MAGENTA_PRESENT_MIN})",
            f"- near-black fraction: {json.dumps(analysis.get('black_fraction'))}",
            f"- mean luminance: {json.dumps(analysis.get('mean_luminance'))}",
            f"- distinct colours: {json.dumps(analysis.get('distinct_colors'))}/8",
            f"- pixels analysed: {analysis.get('width')}x{analysis.get('height')}",
            "",
            "Note: `i.ytimg.com` caches for ~2h, so the `?cb=` token is what "
            "makes this a current reading rather than a stale CDN copy.",
        ]
    else:
        comp = report.get("comparison") or {}
        lines += [
            "",
            "### Comparison vs prior report",
            "",
            f"- prior report: {comp.get('prior_report') or '(none)'}",
            f"- hasCustomThumbnail: "
            f"{json.dumps(comp.get('prior_hasCustomThumbnail'))} -> "
            f"{json.dumps(comp.get('current_hasCustomThumbnail'))}",
            f"- changed sizes: {json.dumps(comp.get('changed_sizes'))}",
            f"- settled: {json.dumps(comp.get('settled'))}",
            f"- note: {comp.get('note', '')}",
        ]
    lines += [
        "",
        "This mode uploaded nothing and changed no thumbnail.",
        "",
    ]
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as e:
        warn(f"could not write GITHUB_STEP_SUMMARY ({type(e).__name__}: {e}).")


# ---------- main ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Probe whether thumbnails().set applies to a YouTube Short.")
    ap.add_argument("--video-id", default=None,
                    help="Shorts video ID. Omit to auto-pick the newest story "
                         "upload from history.json.")
    ap.add_argument("--image", default=None,
                    help="Existing JPEG/PNG to upload. Omit to generate a magenta "
                         "test card at out/thumb_probe_src.jpg.")
    ap.add_argument("--wait-sec", type=int, default=60,
                    help="Seconds between the immediate and delayed verify "
                         "(YouTube/CDN caches thumbnail URLs). Default 60.")
    ap.add_argument("--json", default=DEFAULT_REPORT,
                    help=f"Report path. Default {DEFAULT_REPORT}.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--readback", action="store_true",
                      help="Read-only cache-busted mode: fetches the thumbnails "
                           "YouTube is serving RIGHT NOW (i.ytimg.com is cached "
                           "~2h, so a unique ?cb= token is appended), saves them "
                           "to out/thumb_readback_*.jpg and classifies the pixels. "
                           "Uploads nothing.")
    mode.add_argument("--verify-only", action="store_true",
                      help="Re-read hasCustomThumbnail + the thumbnail URL set "
                           "only. Uploads nothing and fetches no images. "
                           "Compares against a prior report at --json when one "
                           "exists, otherwise prints current state only.")
    args = ap.parse_args(argv)

    wait = max(0, args.wait_sec)
    report_path = (Path(args.json) if Path(args.json).is_absolute()
                   else REPO_ROOT / args.json)
    run_mode = ("readback" if args.readback else
                "verify-only" if args.verify_only else "set")
    if run_mode != "set":
        if args.image:
            warn(f"--image is ignored in {run_mode} mode (nothing is uploaded).")
        if args.image or wait != 60:
            warn(f"--wait-sec is ignored in {run_mode} mode (no upload, no "
                 f"delayed re-check).")

    log("=" * 68)
    if run_mode == "set":
        log("SAFETY: this probe OVERWRITES the thumbnail of the chosen video "
            "with a magenta 'API THUMB TEST' card. Restore it in YouTube Studio "
            "(desktop) via 'Select from video' when done.")
    else:
        log(f"SAFETY: {run_mode} mode is strictly READ-ONLY — it uploads "
            f"nothing, calls thumbnails().set never, and changes no thumbnail.")
    log("SAFETY: no secret values are ever printed — only YT_* key names and "
        "present/missing booleans.")
    log("=" * 68)

    # 1. preflight
    present = {name: bool((os.getenv(name) or "").strip()) for name in YT_ENV}
    for name in YT_ENV:
        log(f"env {name}: {'present' if present[name] else 'MISSING'}")
    missing = [n for n, ok in present.items() if not ok]
    if missing:
        err("cannot run: missing required env var(s): " + ", ".join(missing))
        log("VERDICT: CANNOT_RUN — set YT_CLIENT_ID / YT_CLIENT_SECRET / "
            "YT_REFRESH_TOKEN (GitHub Secrets) or your shell, then re-run. "
            "No values are needed in the chat.")
        return 2

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    cid = (os.getenv("YT_CLIENT_ID") or "").strip()
    sec = (os.getenv("YT_CLIENT_SECRET") or "").strip()
    ref = (os.getenv("YT_REFRESH_TOKEN") or "").strip()
    creds = Credentials(None, refresh_token=ref, client_id=cid,
                        client_secret=sec,
                        token_uri="https://oauth2.googleapis.com/token")
    try:
        yt = build("youtube", "v3", credentials=creds)
    except Exception as e:
        err(f"cannot run: could not build the YouTube client "
            f"({type(e).__name__}: {e}).")
        log("VERDICT: CANNOT_RUN — client construction failed.")
        return 2

    # 0b. video id
    if args.video_id:
        video_id = str(args.video_id).strip()
        log(f"using --video-id={video_id} (history.json untouched)")
    else:
        video_id = _pick_video_id() or ""
        if not video_id:
            err("cannot run: no video id available and none was passed.")
            log("VERDICT: CANNOT_RUN — pass --video-id ID.")
            return 2

    # read-only modes bail out here — nothing below uploads anything
    if run_mode == "readback":
        return _run_readback(yt, video_id, report_path)[0]
    if run_mode == "verify-only":
        return _run_verify_only(yt, video_id, report_path)[0]

    # 0a. image source
    if args.image:
        image = Path(args.image)
        if not image.is_absolute():
            image = (REPO_ROOT / image)
        if not image.exists():
            err(f"cannot run: --image path does not exist: {image}")
            log("VERDICT: CANNOT_RUN — bad --image path.")
            return 2
        if image.suffix.lower() not in MIMETYPES:
            err(f"cannot run: --image must be JPEG/PNG, got {image.suffix!r}.")
            log("VERDICT: CANNOT_RUN — unsupported image format.")
            return 2
        log(f"using provided image {image} ({image.stat().st_size} bytes)")
    else:
        image = _make_test_card(CARD_PATH)
        log(f"generated magenta test card -> {image} "
            f"({image.stat().st_size} bytes, 1080x1920)")

    report = {"mode": "set", "video_id": video_id, "image": str(image),
              "wait_sec": wait,
              "started_utc": datetime.datetime.now(
                  datetime.timezone.utc).isoformat(timespec="seconds")}

    # 2. before snapshot
    pre = _snapshot(yt, video_id, "before")
    report["before"] = pre
    if not pre["readable"]:
        warn("before-snapshot unreadable; continuing to set anyway (a token with "
             "only youtube.upload scope gets 403 on videos().list).")
    elif not pre["found"]:
        err(f"video {video_id} not found or not visible to this token "
            f"(empty videos().list items).")
        report.update(set={"ok": False, "http_status": 404, "item_urls": {},
                          "error": "video not visible", "error_reasons": []},
                      verdict="VIDEO_NOT_FOUND",
                      explanation="videos().list returned no items for this id.")
        _write_json(report_path, report)
        log("VERDICT: VIDEO_NOT_FOUND — check the id, its privacy status, and "
            "that the token belongs to the owning channel.")
        return 2
    else:
        log(f"before: privacyStatus={pre['privacyStatus']!r} "
            f"hasCustomThumbnail={pre['hasCustomThumbnail']!r} "
            f"sizes={sorted(pre['thumbnails'])}")

    # 3. set
    set_res = _do_set(yt, video_id, image)
    report["set"] = set_res
    if set_res["ok"]:
        log(f"set: HTTP 200, returned URLs: "
            f"{json.dumps(set_res['item_urls']) or '(none)'}")
    else:
        log(f"set: FAILED HTTP {set_res.get('http_status')} "
            f"reasons={set_res.get('error_reasons')}")

    # 4. verify immediately, wait, verify again
    imm = _snapshot(yt, video_id, "after-immediate")
    report["after_immediate"] = imm
    log(f"immediate verify: readable={imm['readable']} "
        f"hasCustomThumbnail={imm['hasCustomThumbnail']!r} "
        f"sizes={sorted(imm['thumbnails'])}")

    if wait:
        log(f"sleeping {wait}s before the authoritative (delayed) check "
            f"— YouTube/CDN caches thumbnail URLs...")
        time.sleep(wait)
    else:
        log("--wait-sec 0: skipping the delayed check (URLs may be cached)")

    delayed = _snapshot(yt, video_id, "after-delayed")
    report["after_delayed"] = delayed
    log(f"delayed verify: readable={delayed['readable']} "
        f"hasCustomThumbnail={delayed['hasCustomThumbnail']!r} "
        f"sizes={sorted(delayed['thumbnails'])}")

    # 5. verdict
    verdict, explanation = _verdict(set_res, pre, imm, delayed)
    report["verdict"] = verdict
    report["explanation"] = explanation
    log("=" * 68)
    log(f"VERDICT: {verdict}")
    log(explanation)
    log("=" * 68)

    _write_json(report_path, report)
    _append_step_summary(report, verdict, explanation)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("interrupted by user")
        sys.exit(130)
