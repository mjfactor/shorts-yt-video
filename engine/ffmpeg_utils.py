"""Subprocess + probing helpers for FFmpeg/ffprobe (arg lists only)."""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

TAIL_LINES = 40


def _print(msg: str) -> None:
    """print() that survives non-UTF-8 consoles (Windows cp1252)."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode(), flush=True)


def log(msg: str) -> None:
    _print(f"[engine] {msg}")


def warn(msg: str) -> None:
    _print(f"::warning::{msg}")
    log(f"WARNING: {msg}")


def _http_status(exc: Exception) -> int | None:
    """Best-effort HTTP status across requests-style errors."""
    direct = getattr(exc, "status_code", None)
    if isinstance(direct, int):
        return direct
    resp = getattr(exc, "response", None) or getattr(exc, "resp", None)
    if isinstance(resp, dict):
        try:
            return int(resp.get("status"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    if resp is not None:
        for attr in ("status_code", "status"):
            val = getattr(resp, attr, None)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
    return None


def retry(name: str, func: Callable[[], Any], tries: int = 3,
          backoff: int = 5, fatal: tuple = ()) -> Any:
    """Retry transient failures; permanent 4xx and `fatal` types fail fast."""
    for attempt in range(1, tries + 1):
        try:
            return func()
        except fatal:
            raise
        except Exception as e:
            status = _http_status(e)
            if status is not None and 400 <= status < 500 and status not in (408, 429):
                raise
            if attempt == tries:
                raise
            wait = backoff * attempt
            log(f"{name} failed (attempt {attempt}/{tries}: {e}), retrying in {wait}s...")
            time.sleep(wait)


def run_ffmpeg(cmd: list[str], step: str = "ffmpeg",
               cwd: str | Path | None = None) -> None:
    """Run an FFmpeg arg list; on failure print cmd + stderr tail, then raise.

    cwd runs the command in another directory (used for cwd-relative ASS
    burn-in: relative subtitle/font paths dodge drive-colon issues).
    """
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True,
                       cwd=str(cwd) if cwd is not None else None)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()[-TAIL_LINES:]
        log(f"ffmpeg failed at {step} (exit {e.returncode}): {' '.join(cmd)}")
        for line in tail:
            log(f"  {line}")
        raise RuntimeError(f"ffmpeg failed at {step}: " + " | ".join(tail)) from e


def ffprobe_duration(path: str | Path) -> float:
    """Media duration in seconds via ffprobe."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


def escape_filter_path(path: str | Path) -> str:
    """Escape a file path for use inside an FFmpeg filter argument."""
    return (str(path).replace("\\", "/").replace("'", "'\\''")
            .replace(":", "\\:").replace(",", "\\,"))


def subtitles_filter(path: str | Path) -> str:
    """Plain subtitles filter (no force_style: commas break some builds)."""
    return f"subtitles='{escape_filter_path(path)}'"


def ass_cwd_args(ass_path: str | Path,
                 fontsdir: str | Path | None = None) -> tuple[list[str], str]:
    """ASS burn-in args using a cwd-relative filename (no drive-colon issues).

    Returns (args, cwd): run the FFmpeg command with cwd=cwd.
    """
    folder = Path(ass_path).resolve().parent
    rel = Path(ass_path).resolve().name
    # fontsdir needs BOTH backslash-escaped colons/commas AND single
    # quotes (spaces!): either alone still kills the graph with
    # "No option name near". No shell is involved, so these quotes are
    # filter syntax, not shell syntax. (Matches subtitles_filter, which
    # has always combined both and works.)
    fdir = (str(Path(fontsdir) if fontsdir else folder).replace("\\", "/")
            .replace(":", "\\:").replace(",", "\\,"))
    return ["-vf", f"ass={rel}:fontsdir='{fdir}'"], str(folder)


def find_font(candidates: list[str] | tuple[str, ...]) -> Path | None:
    """First existing font path from candidates, else None."""
    for p in candidates:
        if p and Path(p).exists():
            return Path(p)
    return None


_filter_cache: dict[str, bool] = {}


def has_ffmpeg_filter(name: str) -> bool:
    """True when this FFmpeg build provides filter `name` (cached)."""
    if name not in _filter_cache:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                               capture_output=True, text=True, timeout=30)
            _filter_cache[name] = name in (r.stdout or "")
        except Exception:
            _filter_cache[name] = False
        if not _filter_cache[name]:
            warn(f"FFmpeg {name} filter unavailable.")
    return _filter_cache[name]


def require_ass_filter() -> None:
    """Raise a clear error when this FFmpeg lacks libass burn-in support."""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                           capture_output=True, text=True, timeout=30)
        present = re.search(r"(?m)^\s*\S+\s+ass\s", r.stdout or "") is not None
    except Exception:
        present = False
    if not present:
        raise RuntimeError(
            "FFmpeg 'ass' filter missing (this build lacks libass); "
            "burned captions unavailable. Install an FFmpeg build with "
            "libass enabled.")
