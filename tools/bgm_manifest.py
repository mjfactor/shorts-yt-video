#!/usr/bin/env python3
"""bgm_manifest: provenance record for the committed BGM pool.

Developer helper only. It is NOT imported by the pipeline, is NOT in
requirements.txt, and never runs in CI. Its only job is to keep
assets/background-musics/bgm_manifest.json honest so a Content ID claim is
a lookup instead of an ear-training exercise: every pool file carries a
sha256, a duration, a byte size, and hand-maintained human fields
(provider / source_url / title / artist / license / attribution /
instrumental / claimed / notes).

`claimed: true` blacklists a track from _pick_bgm
(niches/story/visuals.py) so a claimed file is never picked again - it
stays in the repo as evidence, it just stops playing.

Two classes of field, and sync treats them very differently:
  * machine fields (sha256, duration_sec, bytes, missing) are re-derived
    from the file on EVERY sync;
  * human fields are PRESERVED verbatim and never overwritten - they are
    the part a person writes.

Usage examples:
    # Add new tracks to the pool, then merge them into the manifest
    python tools/bgm_manifest.py sync

    # Read-only audit: pool vs manifest, exits nonzero on drift
    python tools/bgm_manifest.py check

    # Just the table (no audit, no exit code games)
    python tools/bgm_manifest.py show

    # Some other pool / pattern / manifest location
    python tools/bgm_manifest.py sync --dir assets/background-musics --glob "bgm*.mp3"
    python tools/bgm_manifest.py check --manifest /tmp/other.json

Notes:
  * `bgm5.mp3` was deleted after a Content ID claim and its name must NOT
    be reused: historical CI logs already mean that exact track.
  * A sync never drops an entry whose file is gone; it marks it
    `missing: true` and keeps the human fields for the claim record.
  * A changed sha256 on an existing entry is reported loudly - the file
    was re-downloaded, so the provenance may now be describing a
    different recording.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # tools/ is run directly, not as a package
    sys.path.insert(0, str(REPO))

from engine.ffmpeg_utils import ffprobe_duration  # noqa: E402

MANIFEST_VERSION = 1
MANIFEST_NAME = "bgm_manifest.json"
DEFAULT_DIR = "assets/background-musics"
DEFAULT_GLOB = "bgm*.mp3"

# Re-derived from the file on every sync.
MACHINE_FIELDS = ("sha256", "duration_sec", "bytes")
# Hand-maintained. sync NEVER touches these.
HUMAN_FIELDS = ("provider", "source_url", "title", "artist", "license",
                "attribution", "instrumental", "claimed", "notes")
# Machine-adjacent bookkeeping: derived, but must survive a vanished file.
MARKER_FIELDS = ("missing",)
BLANK_ENTRY = {
    "provider": "", "source_url": "", "title": "", "artist": "",
    "license": "", "attribution": "", "instrumental": None,
    "claimed": False, "notes": "",
}


def _log(msg: str) -> None:
    print(f"[bgm-manifest] {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"::warning::bgm_manifest: {msg}", flush=True)


def natural_key(name: str) -> list:
    """['bgm2.mp3'] before ['bgm10.mp3'] - plain sorted() gets that wrong."""
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", str(name))]


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024.0 or unit == "GB":
            return f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}GB"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_sec(path: Path) -> float:
    """Duration in seconds (0.0 when ffprobe is unavailable or fails)."""
    try:
        return float(ffprobe_duration(path))
    except Exception as e:  # never let a probe failure kill the tool
        _warn(f"{path.name}: ffprobe failed ({e}); duration recorded as 0.0")
        return 0.0


def pool_files(pool_dir: Path, pattern: str) -> list[Path]:
    if not pool_dir.is_dir():
        return []
    return sorted((p for p in pool_dir.glob(pattern) if p.is_file()),
                  key=lambda p: natural_key(p.name))


def load_manifest(path: Path) -> dict:
    """Manifest as {'version': int, 'tracks': {...}}; empty on any problem."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _log(f"no manifest at {path.as_posix()} yet; starting a fresh one.")
        return {"version": MANIFEST_VERSION, "tracks": {}}
    except (OSError, ValueError) as e:
        _warn(f"{path.as_posix()} is unreadable ({e}); NOT overwriting it. "
              "Fix or delete it, then re-run sync.")
        return {"version": MANIFEST_VERSION, "tracks": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("tracks"), dict):
        _warn(f"{path.as_posix()} is malformed (expected an object with a "
              "'tracks' object); NOT overwriting it.")
        return {"version": MANIFEST_VERSION, "tracks": {}}
    tracks = {str(k): v for k, v in raw["tracks"].items() if isinstance(v, dict)}
    return {"version": raw.get("version", MANIFEST_VERSION), "tracks": tracks}


FIELD_ORDER = MACHINE_FIELDS + HUMAN_FIELDS + MARKER_FIELDS


def order_entry(entry: dict) -> dict:
    """Canonical key order (machine first), unknown keys kept at the end
    so hand-added fields survive every sync."""
    out = {k: entry[k] for k in FIELD_ORDER if k in entry}
    out.update({k: v for k, v in entry.items() if k not in out})
    return out


def save_manifest(path: Path, manifest: dict) -> None:
    tracks = manifest.get("tracks") or {}
    manifest["version"] = MANIFEST_VERSION
    manifest["tracks"] = {k: order_entry(tracks[k])
                          for k in sorted(tracks, key=natural_key)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def cmd_sync(args) -> int:
    pool_dir = Path(args.dir).expanduser()
    manifest_path = (Path(args.manifest).expanduser() if args.manifest
                     else pool_dir / MANIFEST_NAME)
    manifest = load_manifest(manifest_path)
    tracks = manifest["tracks"]

    files = pool_files(pool_dir, args.glob)
    if not pool_dir.is_dir():
        _warn(f"pool dir not found: {pool_dir.as_posix()}")
    elif not files:
        _warn(f"no {args.glob} match in {pool_dir.as_posix()}")

    added = refreshed = 0
    for p in files:
        sha = sha256_file(p)
        entry = tracks.get(p.name)
        if entry is None:
            entry = dict(BLANK_ENTRY)
            entry.update({"sha256": sha, "duration_sec": round(probe_sec(p), 1),
                          "bytes": p.stat().st_size, "missing": False})
            tracks[p.name] = entry
            added += 1
            _log(f"added  {p.name}: {entry['duration_sec']:.1f}s / "
                 f"{human_size(entry['bytes'])} - fill in the human fields "
                 "(provider, title, artist, license, source_url).")
            continue
        old_sha = str(entry.get("sha256") or "")
        if old_sha and old_sha != sha:
            print(f"::error::bgm_manifest: {p.name} sha256 CHANGED "
                  f"({old_sha[:12]}... -> {sha[:12]}...) - the file was "
                  "re-downloaded or replaced, so its provenance (title, "
                  "artist, license, claim state) may now be describing a "
                  "DIFFERENT recording. Re-verify before publishing.", flush=True)
        entry["sha256"] = sha
        entry["duration_sec"] = round(probe_sec(p), 1)
        entry["bytes"] = p.stat().st_size
        entry["missing"] = False
        refreshed += 1
        _log(f"kept   {p.name}: machine fields refreshed"
             + ("" if not old_sha or old_sha == sha else " (sha256 CHANGED)"))

    # Never drop an entry whose file vanished: keep the claim record.
    gone = []
    present = {p.name for p in files}
    for name in sorted(set(tracks) - present, key=natural_key):
        if not entry_is_pool_name(name, args.glob):
            continue  # not a pool filename at all; not this tool's business
        if not tracks[name].get("missing"):
            tracks[name]["missing"] = True
            gone.append(name)
            _warn(f"{name}: file is gone; marked missing=true and kept. "
                  "Set claimed=true if this is a Content ID claim, then "
                  "never reuse the filename.")

    save_manifest(manifest_path, manifest)
    _log(f"wrote {manifest_path.as_posix()}: {len(tracks)} track(s) "
         f"({added} added, {refreshed} refreshed, {len(gone)} marked missing)")
    for name in gone:
        _log(f"  missing: {name}")
    print(f"SUMMARY bgm_manifest: {added} added / {refreshed} refreshed / "
          f"{len(gone)} missing of {len(tracks)} tracked")
    return 0


def entry_is_pool_name(name: str, pattern: str) -> bool:
    return re.fullmatch(pattern.replace("*", ".*").replace("?", "."),
                        name) is not None


def audit(args) -> tuple[int, list[str], list[Path], dict]:
    """(drift_count, problems, pool_files, manifest)."""
    pool_dir = Path(args.dir).expanduser()
    manifest_path = (Path(args.manifest).expanduser() if args.manifest
                     else pool_dir / MANIFEST_NAME)
    manifest = load_manifest(manifest_path)
    tracks = manifest["tracks"]
    files = pool_files(pool_dir, args.glob)
    on_disk = {p.name: p for p in files}
    problems: list[str] = []

    for name, p in sorted(on_disk.items(), key=lambda kv: natural_key(kv[0])):
        entry = tracks.get(name)
        if entry is None:
            problems.append(f"{name}: in the pool but MISSING from the "
                            "manifest (run sync, then fill in the human fields)")
            continue
        if str(entry.get("sha256") or "") != sha256_file(p):
            problems.append(f"{name}: sha256 MISMATCH vs the manifest - the "
                            "file changed, so its provenance may be stale")
        if entry.get("missing"):
            problems.append(f"{name}: marked missing=true but the file is "
                            "there (run sync)")
    for name in sorted(set(tracks) - set(on_disk), key=natural_key):
        state = " (already marked missing)" if tracks[name].get("missing") else ""
        problems.append(f"{name}: in the manifest but not in the pool{state}")
    return len(problems), problems, files, manifest


def render_table(files: list[Path], manifest: dict) -> None:
    tracks = manifest["tracks"]
    on_disk = {p.name: p for p in files}
    names = sorted(set(tracks) | set(on_disk), key=natural_key)
    if not names:
        _log(f"no tracks in the pool or the manifest ({args_dir_hint()})")
        return
    head = (f"{'track':<14} {'status':<9} {'dur':>7} {'size':>9}  "
            f"{'provider':<9} {'claimed':<7} title / artist")
    print(head, flush=True)
    print("-" * len(head), flush=True)
    for name in names:
        entry = tracks.get(name) or {}
        p = on_disk.get(name)
        if p is None:
            status = "MISSING"
        elif name not in tracks:
            status = "NEW"
        elif str(entry.get("sha256") or "") != sha256_file(p):
            status = "CHANGED"
        else:
            status = "ok"
        dur = entry.get("duration_sec")
        dur_s = f"{float(dur):.1f}s" if isinstance(dur, (int, float)) else "-"
        size = entry.get("bytes")
        size_s = human_size(size) if isinstance(size, int) else "-"
        prov = str(entry.get("provider") or "")[:9]
        claimed = "YES" if entry.get("claimed") or entry.get("disabled") else "-"
        who = " - ".join(str(entry.get(k) or "").strip()
                        for k in ("title", "artist") if entry.get(k))
        if entry.get("notes"):
            who = f"{who}  [{entry['notes']}]"
        print(f"{name:<14} {status:<9} {dur_s:>7} {size_s:>9}  "
              f"{prov:<9} {claimed:<7} {who}", flush=True)


def args_dir_hint() -> str:
    return f"{DEFAULT_DIR} / {DEFAULT_GLOB}"


def cmd_check(args) -> int:
    drift, problems, files, manifest = audit(args)
    render_table(files, manifest)
    if problems:
        print("", flush=True)
        for line in problems:
            _warn(line)
    print(f"SUMMARY bgm_manifest: {drift} problem(s), {len(files)} file(s), "
          f"{len(manifest['tracks'])} tracked", flush=True)
    return 1 if drift else 0


def cmd_show(args) -> int:
    pool_dir = Path(args.dir).expanduser()
    manifest_path = (Path(args.manifest).expanduser() if args.manifest
                     else pool_dir / MANIFEST_NAME)
    manifest = load_manifest(manifest_path)
    render_table(pool_files(pool_dir, args.glob), manifest)
    return 0


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=DEFAULT_DIR,
                        help=f"pool directory (default {DEFAULT_DIR})")
    common.add_argument("--glob", default=DEFAULT_GLOB,
                        help=f"pool file pattern (default {DEFAULT_GLOB})")
    common.add_argument("--manifest", default=None,
                        help=f"manifest path (default <pool dir>/{MANIFEST_NAME})")

    ap = argparse.ArgumentParser(
        prog="bgm_manifest",
        description="Maintain the BGM pool provenance manifest "
                    "(sha256 + hand-kept license/claim fields).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", parents=[common],
                   help="merge the pool into the manifest, refreshing only "
                        "the machine fields (human fields are preserved)")
    sub.add_parser("check", parents=[common],
                   help="read-only audit of pool vs manifest "
                        "(nonzero exit on drift)")
    sub.add_parser("show", parents=[common],
                   help="print the track table only")
    args = ap.parse_args(argv)
    return {"sync": cmd_sync, "check": cmd_check, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())