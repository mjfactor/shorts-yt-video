#!/usr/bin/env python3
"""prep_backgrounds: fetch/normalize gameplay footage for assets/backgrounds.

Developer/CI helper only. It is NOT imported by the pipeline, is NOT in
requirements.txt, and the files it writes are used once per run and never
committed (CI only adds history.json back).

The renderer (niches/story/visuals.py) consumes exactly one continuous take
from a random file in assets/backgrounds, played at STORY_BG_SINGLE_SPEED
(1.8x) from a random offset. That needs about 198s of source for a ~110s
output, so this tool caps each output at --max-seconds (default 210) and
reframes every input to 1080x1920 portrait at 30fps.

Usage examples:
    # Scan a folder of hand-picked clips (sorted order -> bg1, bg2, ...)
    python tools/prep_backgrounds.py --source-folder ~/gameplay

    # Explicit files, fixed window, overwrite existing bg files
    python tools/prep_backgrounds.py clip_a.mp4 clip_b.mov --start 0:05:00 --force

    # URLs, fetching only a section of a long compilation
    python tools/prep_backgrounds.py --url "https://youtu.be/ID" --section 0:10:00-0:14:00

    # Dry run: show parsed inputs + required source length, no work at all
    python tools/prep_backgrounds.py --source-folder ~/gameplay --list

URL support needs yt-dlp on PATH or installed as a module; it is
deliberately not a project dependency. Without it, URLs fail with a clear
message and local files still process.
"""
from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CANVAS_W, CANVAS_H = 1080, 1920
OUT_FPS = 30
DEFAULT_MAX_SECONDS = 210.0
DEFAULT_CRF = 27
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
# Same scale/crop idiom as visuals._seg_filter (reframe, never stretch).
REFRAME = (f"scale={CANVAS_W}:{CANVAS_H}:"
           "force_original_aspect_ratio=increase,"
           f"crop={CANVAS_W}:{CANVAS_H}")


def _log(msg: str) -> None:
    print(f"[prep-bg] {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"::warning::prep_backgrounds: {msg}", flush=True)


def probe_duration(path: Path) -> float:
    """Duration in seconds (0.0 when unknown)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=120)
        return float((r.stdout or "0").strip() or 0.0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _fsize(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024.0 or unit == "GB":
            return f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}GB"


def parse_clock(text: str) -> float:
    """'HH:MM:SS' | 'MM:SS' | 'SS' -> seconds."""
    parts = [float(p) for p in str(text).strip().split(":") if p != ""]
    if not parts or len(parts) > 3:
        raise ValueError(f"bad time value {text!r} (use HH:MM:SS)")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60.0 + p
    return seconds


def parse_section(text: str) -> tuple[float, float | None]:
    """'START-END' -> (start_seconds, end_seconds or None)."""
    m = re.match(r"^\s*([0-9:.]+)\s*-\s*([0-9:.]+)\s*$", str(text or ""))
    if not m:
        raise ValueError(f"bad --section {text!r} (use HH:MM:SS-HH:MM:SS)")
    start, end = parse_clock(m.group(1)), parse_clock(m.group(2))
    if end <= start:
        raise ValueError(f"--section end must be after start ({text!r})")
    return start, end


def find_yt_dlp() -> list[str] | None:
    """['yt-dlp'] or [sys.executable, '-m', 'yt_dlp'], else None."""
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    try:
        import yt_dlp  # noqa: F401
    except Exception:
        return None
    return [sys.executable, "-m", "yt_dlp"]


def fetch_url(url: str, workdir: Path, section: str | None) -> Path:
    """Download (or just the requested section of) a URL with yt-dlp."""
    cmd = find_yt_dlp()
    if cmd is None:
        raise RuntimeError(
            "yt-dlp is required for URL inputs but was not found on PATH or "
            "as a module. Install it for this run only, e.g. "
            "`python -m pip install yt-dlp` (it is intentionally NOT a "
            "project dependency), or pass local files instead.")
    out_tmpl = str(workdir / "dl_%(id)s.%(ext)s")
    cmd = cmd + ["--no-playlist", "--no-progress", "-f",
                 "bv*[height<=1080]+ba/b[height<=1080]/b", "-o", out_tmpl]
    if section:
        start, end = parse_section(section)
        cmd += ["--download-sections", f"*{start:.2f}-{end:.2f}"]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True)
    files = sorted(workdir.glob("dl_*"))
    files = [f for f in files if f.suffix.lower() in VIDEO_EXTS]
    if r.returncode != 0 or not files:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"yt-dlp failed for {url} (exit {r.returncode}): "
                           f"{' | '.join(tail)}")
    return files[0]


def collect_inputs(args) -> list[Path]:
    """Explicit files + folder scan, then sorted order for reproducibility."""
    items: list[Path] = []
    for raw in args.inputs or []:
        p = Path(raw).expanduser()
        if p.is_file():
            items.append(p)
        else:
            _warn(f"input not found, skipping: {p}")
    for raw in args.source_folder or []:
        d = Path(raw).expanduser()
        if not d.is_dir():
            _warn(f"source folder not found, skipping: {d}")
            continue
        found = [p for p in d.rglob("*")
                 if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
        if not found:
            _warn(f"no video files under {d}")
        items.extend(sorted(found))
    seen, out = set(), []
    for p in items:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def next_index(out_dir: Path, force: bool) -> int:
    """Next bg number, or 1 when reusing from the start under --force."""
    if force:
        return 1
    used = 0
    for p in out_dir.glob("bg*.mp4"):
        m = re.match(r"^bg(\d+)\.mp4$", p.name)
        if m:
            used = max(used, int(m.group(1)))
    return used + 1


def transcode(src: Path, dst: Path, *, start: float, length: float,
              crf: int, keep_audio: bool) -> None:
    """One reframe pass: cover-scale + center-crop, 30fps, yuv420p, faststart."""
    vf = f"{REFRAME},fps={OUT_FPS},format=yuv420p"
    cmd = ["ffmpeg", "-y", "-ss", f"{max(0.0, start):.3f}", "-i", str(src),
           "-t", f"{length:.3f}", "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
           "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if not keep_audio:
        cmd += ["-an"]
    cmd.append(str(dst))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists():
        tail = (r.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError(f"ffmpeg failed for {src.name}: {' | '.join(tail)}")


def prep_one(src: Path, dst: Path, *, start_flag: float | None,
             max_seconds: float, required: float, crf: int,
             keep_audio: bool) -> tuple[float, int, float, int, float, bool]:
    """Returns (in_dur, in_size, out_dur, out_size, out_start, short_window)."""
    in_dur = probe_duration(src)
    in_size = _fsize(src)
    span = min(max_seconds, in_dur) if in_dur > 0 else max_seconds
    if start_flag is not None:
        start = start_flag
    elif in_dur > span:
        start = random.uniform(0.0, in_dur - span)
    else:
        start = 0.0
    short = span < required - 0.01
    if short:
        _warn(f"{src.name}: window {span:.1f}s is shorter than the "
              f"{required:.1f}s the pipeline needs, so the render will loop "
              f"this clip to cover the whole output.")
    transcode(src, dst, start=start, length=span, crf=crf,
              keep_audio=keep_audio)
    return in_dur, in_size, probe_duration(dst), _fsize(dst), start, short


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Normalize gameplay footage into assets/backgrounds as "
                    "bg1.mp4, bg2.mp4, ...")
    ap.add_argument("inputs", nargs="*", help="local video files")
    ap.add_argument("--source-folder", action="append", default=[],
                    help="folder to scan recursively for videos (repeatable)")
    ap.add_argument("--url", action="append", default=[],
                    help="http(s) URL fetched with yt-dlp (repeatable)")
    ap.add_argument("--section", default=None,
                    help="URL time range as HH:MM:SS-HH:MM:SS")
    ap.add_argument("--out-dir", default=None,
                    help="output dir (default assets/backgrounds)")
    ap.add_argument("--speed", type=float, default=1.8,
                    help="renderer playback speed the window must cover "
                         "(default 1.8, matches STORY_BG_SINGLE_SPEED)")
    ap.add_argument("--output-seconds", type=float, default=110.0,
                    help="rendered output length to cover (default 110)")
    ap.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
                    help=f"cap per output (default {DEFAULT_MAX_SECONDS:.0f})")
    ap.add_argument("--start", type=float, default=None,
                    help="window start in seconds (default: random)")
    ap.add_argument("--crf", type=int, default=DEFAULT_CRF,
                    help=f"x264 CRF (default {DEFAULT_CRF})")
    ap.add_argument("--keep-audio", action="store_true",
                    help="keep the audio track (backgrounds are muted by "
                         "default)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing bg files instead of continuing "
                         "the numbering")
    ap.add_argument("--list", action="store_true",
                    help="print parsed inputs and the required source length, "
                         "then exit without downloading or encoding")
    ap.add_argument("--seed", type=int, default=None,
                    help="seed the random window offsets (reproducible runs)")
    args = ap.parse_args(argv)

    if args.seed is not None:
        random.seed(args.seed)

    repo = Path(__file__).resolve().parents[1]
    out_dir = Path(args.out_dir).expanduser() if args.out_dir \
        else repo / "assets" / "backgrounds"
    speed = max(0.2, float(args.speed))
    required = float(args.output_seconds) * speed

    locals_ = collect_inputs(args)
    url_count = len(args.url or [])
    total_inputs = len(locals_) + url_count

    if args.list:
        _log(f"out dir          : {out_dir}")
        _log(f"inputs (local)   : {len(locals_)}")
        for p in locals_:
            _log(f"  - {p} ({probe_duration(p):.1f}s, "
                 f"{human_size(_fsize(p))})")
        _log(f"inputs (url)     : {url_count}"
             + (f" (yt-dlp {'found' if find_yt_dlp() else 'MISSING'})"
                if url_count else ""))
        for u in args.url or []:
            _log(f"  - {u}")
        _log(f"required source  : {required:.1f}s "
             f"({args.output_seconds:.0f}s output @ {speed:g}x)")
        _log(f"max per output   : {args.max_seconds:.0f}s")
        _log("--list: no work done.")
        return 0

    if not total_inputs:
        _warn("no inputs: pass file paths, --source-folder, or --url")
        print("SUMMARY prep_backgrounds: 0 ok / 0 failed (no inputs)")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    gitkeep = out_dir / ".gitkeep"
    index = next_index(out_dir, args.force)
    produced: list[str] = []
    ok = failed = 0
    in_bytes = out_bytes = 0

    with tempfile.TemporaryDirectory(prefix="prep_bg_") as td:
        work = Path(td)
        queue: list[tuple[str, Path | None, str | None]] = [
            (str(p), p, None) for p in locals_]
        queue += [(u, None, u) for u in (args.url or [])]
        for label, local, url in queue:
            dst = out_dir / f"bg{index}.mp4"
            try:
                if url is not None:
                    fetched = fetch_url(url, work, args.section)
                    label = url
                else:
                    fetched = local
                assert fetched is not None
                in_dur, in_size, out_dur, out_size, start, _short = prep_one(
                    fetched, dst, start_flag=args.start,
                    max_seconds=args.max_seconds, required=required,
                    crf=args.crf, keep_audio=args.keep_audio)
                saved = (1.0 - out_size / in_size) * 100.0 if in_size else 0.0
                _log(f"{label} -> {dst.name}: in {in_dur:.1f}s / "
                     f"{human_size(in_size)} -> out {out_dur:.1f}s / "
                     f"{human_size(out_size)} ({saved:+.0f}% size, "
                     f"start {start:.1f}s)")
                produced.append(dst.name)
                in_bytes += in_size
                out_bytes += out_size
                ok += 1
                index += 1
            except Exception as e:  # one bad input never aborts the batch
                failed += 1
                _warn(f"{label}: {e}")
                if dst.exists() and _fsize(dst) == 0:
                    dst.unlink(missing_ok=True)

    if gitkeep.exists() or not gitkeep.exists():
        # .gitkeep is never touched by this tool; assert it for the log.
        _log(f".gitkeep present : {gitkeep.exists()}")

    total_in = human_size(in_bytes)
    total_out = human_size(out_bytes)
    saved = (1.0 - out_bytes / in_bytes) * 100.0 if in_bytes else 0.0
    _log(f"batch total: {ok} ok / {failed} failed, "
         f"{total_in} -> {total_out} ({saved:+.0f}%)")
    _log(f"produced: {', '.join(produced) if produced else '(none)'}")
    print(f"SUMMARY prep_backgrounds: {ok} ok / {failed} failed; "
          f"files: {','.join(produced) if produced else 'none'}")
    if not ok and failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
