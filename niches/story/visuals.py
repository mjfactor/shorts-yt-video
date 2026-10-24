"""Story stage 3: BG-driven 1080x1920 render with card + word-pop captions.

Pipeline: pick clips from the local assets/backgrounds folder → one BG
track (total = narration + 0.4s), either ONE continuous single take
(STORY_BG_SEGMENTS 0/1, the default) or N segments from different clips
concatenated back into a single track → card overlay + ASS burn + mixed
audio in one final pass (crf20/veryfast/AAC-192k).
Local clips are the ONLY background source: an empty folder is a hard
failure, never a silent fallback.
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

from PIL import Image

from engine import overlay_card as _card
from engine.ass import write_wordpop_ass
from engine.audio_mix import loop_bed, synth_drone
from engine.ffmpeg_utils import (ass_cwd_args, ffprobe_duration, log,
                                 require_ass_filter, run_ffmpeg, warn)
from engine.srt import write_upload_srt
from niches.base import Content, NarrationResult, VisualsResult

from .config import StoryConfig

CANVAS_W, CANVAS_H = 1080, 1920
CARD_Y_OFFSET = -280
# Card top as a fraction of the canvas height. Single source for the y
# used by BOTH the video overlay (_card_filter) and the composite
# thumbnail, so the two can never drift apart.
CARD_Y_FRAC = 0.30
BG_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
# Provenance record for the BGM pool, kept by tools/bgm_manifest.py next to
# the tracks. Purely advisory: a bad manifest must never silence the bed.
BGM_MANIFEST_NAME = "bgm_manifest.json"

STORY_CARD_THEME = {
    "card_fill": (251, 251, 255, 255),
    "card_border": (100, 92, 180, 255),
    "header_fill": (15, 20, 37, 255),
    "header_gradient": ((13, 23, 39, 255), (35, 20, 49, 255)),
    "accent_colors": ((27, 222, 241, 255), (238, 57, 205, 255)),
    "text_color": (21, 24, 40, 255),
    "highlight_color": (186, 28, 117, 255),  # magenta punchline word, no fill
    "highlight_fill": None,
    "highlight_gradient": None,
    "handle_color": (248, 249, 255, 255),
    "handle_size": 33,
    "avatar_size": 84,
    "badge_color": (34, 185, 235, 255),
    "body_size": 66,
    "body_min_size": 34,
    "radius": 36,
    "pad": 36,
    "shadow_margin": 26,
    "shadow_blur": 12,
    "shadow_color": (13, 15, 33, 110),
    "label_text": "STORYTIME",
    "label_fill": (42, 43, 66, 255),
    "label_color": (210, 218, 238, 255),
    "control_fill": (237, 239, 248, 255),
    "engagement_color": (76, 85, 111, 255),
    "engagement_size": 26,
    "heart_color": (193, 34, 122, 255),
    "heart_filled": True,
    "share_color": (31, 125, 154, 255),
    "icon_color": (145, 152, 180, 255),
    "divider_color": (228, 231, 242, 255),
}


def _load_bgm_manifest(base: Path) -> dict[str, dict]:
    """Read bgm_manifest.json beside the pool. Never raises.

    Missing / unreadable / malformed / non-dict all degrade to `{}` with a
    single log line, which means "no manifest, no blacklist" — the same
    behavior as the unmanifested pool the pipeline had before.
    """
    path = base / BGM_MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log(f"Story BGM manifest unreadable ({path.name}: {e}); ignoring it.")
        return {}
    tracks = raw.get("tracks") if isinstance(raw, dict) else None
    if not isinstance(tracks, dict):
        log(f"Story BGM manifest malformed ({path.name}); ignoring it.")
        return {}
    return {str(k): v for k, v in tracks.items() if isinstance(v, dict)}


def _bgm_blocked(entry: dict | None) -> bool:
    """True when a track is blacklisted. `claimed` is canonical; `disabled`
    is an accepted synonym."""
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("claimed") or entry.get("disabled"))


def _bgm_provenance(entry: dict | None) -> str:
    """Provenance suffix for the CI log line, '' when nothing is known."""
    if not isinstance(entry, dict):
        return ""
    bits = [str(entry.get("provider") or "").strip(),
            str(entry.get("title") or "").strip(),
            str(entry.get("artist") or "").strip()]
    bits = [b for b in bits if b]
    if _bgm_blocked(entry):
        bits.append("FLAGGED")
    return f" [{' - '.join(bits)}]" if bits else ""


def _choose_bgm(pool: list[Path], base: Path) -> tuple[Path, str, str]:
    """Manifest-aware pick from one pool (never empty).

    Returns (pick, count_phrase, provenance_suffix). A pool file absent
    from the manifest is still a candidate (the pool predates the
    manifest); entries flagged claimed/disabled are dropped; if the
    manifest would exclude everything the blacklist is ignored with a
    warning — a bad manifest must never silently mute the bed.
    """
    manifest = _load_bgm_manifest(base)
    eligible = [p for p in pool if not _bgm_blocked(manifest.get(p.name))]
    if manifest and not eligible:
        where = base.as_posix() or "."
        warn(f"Story BGM manifest excluded every track in {where}; ignoring "
             "the blacklist so the bed keeps playing.")
        eligible = list(pool)
    count = (f"{len(eligible)} files" if len(eligible) == len(pool)
             else f"{len(eligible)} of {len(pool)} files")
    pick = random.choice(eligible)
    return pick, count, _bgm_provenance(manifest.get(pick.name))


def _pick_bgm(cfg: StoryConfig) -> Path | None:
    """Bed source priority: explicit STORY_BED_PATH file → one random
    match for bgm_glob from cfg.bgm_dir → the same pattern at the repo
    root (legacy layout) → None (caller synthesizes the drone).

    Pool is sorted then random.choice per video and logged with the file
    name and pool size. Both layouts work, so moving the tracks between
    them never silences the bed. When the pool has a bgm_manifest.json,
    tracks flagged claimed/disabled are skipped and the picked one is
    logged with its provider/artist (see _choose_bgm).
    """
    if cfg.bed_path:
        explicit = Path(cfg.bed_path)
        if explicit.exists():
            return explicit
        warn(f"Story bed file missing ({explicit}); trying bgm pool.")
    pattern = cfg.bgm_glob or "bgm*.mp3"
    candidates = [Path(getattr(cfg, "bgm_dir", "") or "")]
    for base in candidates:
        if base.is_dir():
            pool = sorted(base.glob(pattern))
            if pool:
                pick, count, prov = _choose_bgm(pool, base)
                log(f"Story BGM pool: picked {pick.name} "
                    f"({count}) from {base.as_posix() or '.'}.{prov}")
                return pick
    # Legacy layout: pool at the repo root.
    pool = sorted(Path().glob(pattern))
    if pool:
        pick, count, prov = _choose_bgm(pool, Path())
        log(f"Story BGM pool: picked {pick.name} ({count}) "
            f"from the repo root.{prov}")
        return pick
    log(f"Story BGM pool: no {pattern} match in "
        f"{getattr(cfg, 'bgm_dir', '') or '.'} or the repo root; "
        "using the synthesized drone.")
    return None


def _cover_filter() -> str:
    """Scale-to-cover + center-crop onto the 1080x1920 canvas.

    Full-bleed, NO letterbox bars. Shared by the video BG track
    (_seg_filter) and the composite thumbnail, so the thumbnail frame is
    geometrically identical to the video's background layer.

    NOTE: scale's 3rd positional arg is sws FLAGS, not the aspect-ratio
    mode — `scale=1080:1920:increase` fails on real FFmpeg ("Unable to
    parse sws_flags"). The named force_original_aspect_ratio form is
    required.
    """
    return (f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,"
            f"crop={CANVAS_W}:{CANVAS_H}")


def _seg_filter(speed: float) -> str:
    """Cover-crop (see _cover_filter), then speed, fps, pixfmt."""
    sp = max(0.2, float(speed))
    return f"{_cover_filter()},setpts=PTS/{sp:.3f},fps=30,format=yuv420p"


def _render_bg_segment(clip: Path, duration: float, speed: float,
                       out_path: Path, frames: int | None = None) -> float:
    """Cut `duration` seconds of `clip` into `out_path`; return the start offset.

    The reusable per-segment renderer: one continuous take from one local
    clip. Picks a random start offset so repeated use of the same file looks
    different, cuts exactly `duration` seconds of output, and covers the
    whole span even when the source is shorter than needed (looped input
    with -stream_loop -1, truncated to the exact length).

    `frames` (30fps) is supplied by the segmented path so N segments
    concatenate to EXACTLY the same length; when it is None the output is
    cut with -t exactly as the single-take path has always done.
    """
    sp = max(0.2, float(speed))
    dur = max(0.0, float(duration))
    try:
        src_dur = ffprobe_duration(clip)
    except Exception:
        src_dur = 0.0
    need = dur * sp  # source seconds required at this speed
    if src_dur > need:
        offset = random.uniform(0.0, max(0.0, src_dur - need))
    else:
        # Short source: loop it and keep playing from 0.
        offset = 0.0
    args = ["ffmpeg", "-y"]
    if src_dur <= need:
        args += ["-stream_loop", "-1"]
    args += ["-ss", f"{offset:.3f}", "-i", str(clip)]
    if frames and frames > 0:
        args += ["-frames:v", str(int(frames))]
    else:
        args += ["-t", f"{dur:.3f}"]
    args += ["-vf", _seg_filter(sp),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-an", str(out_path)]
    run_ffmpeg(args, "bg-segment")
    log(f"Story BG segment: {clip.name} @ {sp:.2f}x, start {offset:.1f}s, "
        f"need {need:.1f}s of {src_dur:.1f}s source"
        f"{' (looped to cover)' if src_dur <= need else ''} "
        f"-> {dur:.1f}s {out_path.name}.")
    return offset


def _segment_frame_counts(total: float, segments: int,
                          fps: int = 30) -> tuple[list[int], int]:
    """Frame counts per segment that sum EXACTLY to round(total*fps).

    Flooring every segment would shorten the track by up to `segments` frames
    and desync the 2s outro fade in _mix_filter (keyed off `total`), so the
    remainder is handed out one frame at a time instead.
    """
    n = max(1, int(segments or 1))
    total_frames = max(n, int(round(float(total) * fps)))
    base, rem = divmod(total_frames, n)
    return [base + (1 if i < rem else 0) for i in range(n)], total_frames


def _concat_segments(paths: list[Path], out_path: Path,
                     workdir: Path, total: float) -> None:
    """Stitch rendered segments into one track (stream copy, else re-encode).

    Every segment was encoded with the same _seg_filter params (1080x1920,
    30fps, yuv420p, cover-crop, same speed) so -c copy is normally exact;
    the duration is verified anyway and a re-encode is the fallback if the
    copy path drifts. Runs with cwd=workdir so the list file holds bare
    relative names (absolute Windows paths need escaping and -safe 1
    rejects drive letters anyway).
    """
    list_path = workdir / "bg_seg_list.txt"
    list_path.write_text(
        "".join(f"file '{p.name}'\n" for p in paths), encoding="utf-8")
    out_abs = str(Path(out_path).resolve())
    run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "1",
                "-i", list_path.name, "-c", "copy", out_abs],
               "bg-concat", cwd=workdir)
    try:
        got = ffprobe_duration(out_path)
    except Exception:
        got = -1.0
    if abs(got - float(total)) <= 0.1:
        return
    warn(f"Story BG concat: stream-copy gave {got:.2f}s, expected "
         f"{total:.2f}s; re-encoding instead.")
    run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "1",
                "-i", list_path.name,
                "-vf", f"fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-an", out_abs],
               "bg-concat-reencode", cwd=workdir)
    got = ffprobe_duration(out_path)
    log(f"Story BG concat: re-encoded to {got:.2f}s (target {total:.2f}s).")


def _build_bg_track(order: list[Path], total: float, speed: float,
                    segments: int, workdir: Path,
                    bg_dir: str) -> tuple[Path, list[Path]]:
    """Render the background track; return (track_path, clips_used).

    segments <= 1 is exactly the historical single continuous take. N >= 2
    cuts the same total into N segments, each from a DIFFERENT clip in
    `order` (cycling, so the whole pool is used and wraps), concatenated
    back into one track. Every segment gets its own random start offset.
    A failing clip warns and the next in `order` is tried; when EVERY clip
    fails the run stops hard — there is no other background source.
    """
    track = workdir / "bg_track.mp4"
    n = max(1, int(segments or 1))
    if n <= 1 or len(order) < 2:
        for cand in order:
            try:
                _render_bg_segment(cand, total, speed, track)
                return track, [cand]
            except Exception as e:
                warn(f"Story BG: local clip {cand.name} failed "
                     f"({type(e).__name__}: {e}); trying another local clip...")
        raise RuntimeError(
            f"Story BG: all {len(order)} local clips in '{bg_dir}' "
            f"failed to render; no other background source exists.")

    frames, _total_frames = _segment_frame_counts(total, n)
    seg_paths: list[Path] = []
    used: list[Path] = []
    for i, count in enumerate(frames):
        seg_path = workdir / f"bg_seg_{i:02d}.mp4"
        start = i % len(order)
        for k in range(len(order)):
            cand = order[(start + k) % len(order)]
            try:
                _render_bg_segment(cand, count / 30.0, speed, seg_path,
                                   frames=count)
                seg_paths.append(seg_path)
                used.append(cand)
                break
            except Exception as e:
                warn(f"Story BG: local clip {cand.name} failed "
                     f"({type(e).__name__}: {e}); trying another local clip...")
        else:
            raise RuntimeError(
                f"Story BG: all {len(order)} local clips in '{bg_dir}' "
                f"failed to render segment {i + 1}/{n}; no other background "
                f"source exists.")
    _concat_segments(seg_paths, track, workdir, total)
    return track, used


def _mix_filter(
    total: float,
    bed_volume: float = 0.05,
    *,
    duck_threshold: float = 0.05,
    duck_ratio: float = 8.0,
    duck_attack_ms: int = 10,
    duck_release_ms: int = 400,
    highpass_hz: int = 180,
    lowpass_hz: int = 9000,
    output_gain_db: float = 7.0,
) -> str:
    """Voice-forward mix chain (inputs: 0=BG video, 1=narration, 2=bed).

    Measured: the old loose duck (threshold 0.15, ratio 3, attack 20,
    release 180) at bed_volume 0.15 left the loudest pool track only ~6 dB
    under the voice — not enough for narration intelligibility. The target
    is ~-20 dB under the voice, reached two ways:

      * the duck now bites: threshold 0.05 (a linear amplitude above typical
        speech RMS, so the bed is pulled down for the whole sentence) at
        ratio 8 (~18 dB of gain reduction) with a 10 ms attack and a 400 ms
        release, which keeps the bed from pumping between words;
      * the bed is band-limited before the duck (highpass 180 Hz, lowpass
        9 kHz), so the music energy that competes with the 200 Hz-4 kHz
        speech band is not in the mix to begin with.

    bed_volume only sets the level BETWEEN sentences (the floor the duck
    rides down from). highpass_hz/lowpass_hz <= 0 OMIT that filter — never
    emit f=0, which is invalid. Stream indices are fixed by the story's
    input order (0=BG, 1=narration, 2=bed); do not renumber.
    2s outro fade, normalize=0, narration mixed at full untouched volume.

    Output stage: output_gain_db of make-up gain on the mixed bus, then
    alimiter pinned at -2 dBFS. Untouched, the mix measures ~-22 LUFS -
    ~8 dB under the ~-14 LUFS YouTube plays loud uploads at (and YouTube
    does NOT turn quiet uploads up), so phone playback read weak; +7 dB
    lands ~-16 LUFS with only the loudest peaks caught by the limiter.
    level=disabled is required: this ffmpeg's alimiter defaults to
    auto-level ON, which re-normalizes output to 0 dBTP and silently
    defeats the ceiling (verified by measurement).
    """
    gain = max(0.0, float(bed_volume))
    # Band limit first, so the duck measures (and is heard against) a bed
    # that has already been cleaned of the speech-competing bands. A
    # disabled (<= 0) filter is dropped from the chain entirely.
    bed_chain = [f"volume={gain:.2f}"]
    if float(highpass_hz) > 0:
        bed_chain.append(f"highpass=f={float(highpass_hz):.0f}")
    if float(lowpass_hz) > 0:
        bed_chain.append(f"lowpass=f={float(lowpass_hz):.0f}")
    fade_st = max(0.0, float(total) - 2.0)
    # -2.0 dBFS limiter ceiling: measured output true-peak tracks it
    # exactly (-2.0 dBTP), leaving AAC-encode/upload headroom.
    ceiling = 10 ** (-2.0 / 20)
    return (f"[2:a]{','.join(bed_chain)}"
            "[b];"
            "[b][1:a]"
            f"sidechaincompress=threshold={float(duck_threshold):g}"
            f":ratio={float(duck_ratio):g}"
            f":attack={int(duck_attack_ms)}"
            f":release={int(duck_release_ms)}[duck];"
            "[1:a][duck]amix=inputs=2:duration=first:normalize=0,"
            f"afade=t=out:st={fade_st:.2f}:d=2,"
            f"volume={float(output_gain_db):g}dB,"
            f"alimiter=limit={ceiling:.3f}:level=disabled[aout]")


def _card_input_args(card_png: Path, card_dur: float) -> list[str]:
    """Looped card-still input lasting exactly card_dur (input index 2)."""
    return ["-loop", "1", "-t", f"{max(0.1, float(card_dur)):.2f}",
            "-i", str(card_png)]


def _card_filter(card_dur: float) -> str:
    """Spec-exact composite: rgba + fade in/out, overlaid at y=H*0.30.

    Y mapping: the old (H-h)/2-280 put a 1080x400 card top at 480;
    the spec y=H*0.30 puts the ~900px card top at 576 (card center
    ~836, still above canvas center 960). Expects inputs aliased as
    [base] (BG video) and [card] (looped still); emits [v1].
    """
    dur = max(0.1, float(card_dur))
    out = max(0.1, dur - 0.25)
    return (
        f"[card]format=rgba,"
        f"fade=t=in:st=0:d=0.2:alpha=1,"
        f"fade=t=out:st={out:.2f}:d=0.25:alpha=1[c];"
        f"[base][c]overlay=x=(W-w)/2:y=H*{CARD_Y_FRAC}:"
        f"enable='between(t,0,{dur:.2f})'[v1]"
    )


def _thumb_offset(cfg: StoryConfig, card_dur: float) -> float:
    """STORY_THUMB_OFFSET_SECONDS clamped into the card window.

    Single source for the offset used by BOTH thumbnail paths, so the
    primary composite and the fallback extractor always agree. The card
    is visible 0-card_dur, so the default 1.0s lands on the card.
    An explicit 0.0 is honored (not treated as "unset", which a
    `or 1.0` fallback would do).
    """
    raw = getattr(cfg, "thumb_offset_seconds", 1.0)
    raw = 1.0 if raw is None else float(raw)
    hi = max(0.1, float(card_dur) - 0.1)
    return min(max(0.0, raw), hi)


def _extract_thumb(mp4: Path, card_dur: float, cfg: StoryConfig,
                   workdir: Path) -> Path | None:
    """Grab one 1080x1920 JPG frame from the FINAL mp4 (warn-only).

    FALLBACK path only: _composite_thumb (card over a real BG frame) is
    the primary thumbnail. Offset comes from STORY_THUMB_OFFSET_SECONDS
    via _thumb_offset (clamped inside card_dur). Quality is tuned
    (-q:v 4, fallback 10) to stay under YouTube's 2MB thumbnail limit.
    Never raises: returns None on any failure.
    """
    try:
        offset = _thumb_offset(cfg, card_dur)
        thumb = Path(workdir) / "thumb.jpg"
        run_ffmpeg(["ffmpeg", "-y", "-ss", f"{offset:.2f}",
                    "-i", str(Path(mp4).resolve()),
                    "-vframes", "1", "-q:v", "4", str(thumb)],
                   "thumb-extract")
        limit = 2 * 1024 * 1024
        if thumb.exists() and thumb.stat().st_size >= limit:
            run_ffmpeg(["ffmpeg", "-y", "-i", str(thumb),
                        "-q:v", "10", str(thumb)], "thumb-shrink")
        if not thumb.exists():
            warn("Story thumb: extraction produced no file; skipping.")
            return None
        size_kb = thumb.stat().st_size / 1024
        if thumb.stat().st_size >= limit:
            warn(f"Story thumb still {size_kb:.0f} KB >= 2MB limit; "
                 "upload will skip it.")
        else:
            log(f"Story thumb: {thumb.name} at {offset:.2f}s "
                f"({size_kb:.0f} KB).")
        return thumb
    except Exception as e:
        warn(f"Story thumb extraction skipped ({e}).")
        return None


def _composite_thumb(card_png_path: Path, bg_track: Path,
                     output_thumb_path: Path, offset: float) -> Path | None:
    """Thumbnail = the real BG frame + the card, at the video's card y.

    `bg_track` is the ALREADY-RENDERED background track (the winning clip
    from the pool — or, with STORY_BG_SEGMENTS on, the first of the
    concatenated segments — already cut from its random start offset and
    already speed-adjusted), NOT a raw source clip. Sourcing the frame from
    is what makes the thumbnail match the video: same clip, same offset,
    same speed, and the same full-bleed cover crop via _cover_filter()
    (which _seg_filter also uses for the video path). Reading the raw
    clip here would show second 0 at 1x on the naturally-first pool
    entry — a background the video does not use, and, after a broken-clip
    fallback, the clip that FAILED.

    `offset` (STORY_THUMB_OFFSET_SECONDS, clamped by _thumb_offset) is
    seconds into the BG track, which is exactly seconds into the video's
    background layer since the track starts where the video does.

    Pillow composites the RGBA card PNG centered horizontally at
    y = H * CARD_Y_FRAC — the same y the video overlay uses. Exports JPEG
    quality=92 at 1080x1920. Warn-only: never raises, returns None on any
    failure (caller falls back to _extract_thumb).
    """
    try:
        frame_path = output_thumb_path.with_suffix(".frame.png")
        run_ffmpeg(["ffmpeg", "-y", "-ss", f"{max(0.0, float(offset)):.2f}",
                    "-i", str(Path(bg_track).resolve()),
                    "-vframes", "1", "-vf", _cover_filter(),
                    str(frame_path)], "thumb-frame-extract")
        if not frame_path.exists():
            warn("Story thumb composite: frame extraction produced no file.")
            return None

        bg = Image.open(frame_path).convert("RGBA")
        if bg.size != (CANVAS_W, CANVAS_H):
            bg = bg.resize((CANVAS_W, CANVAS_H), Image.Resampling.LANCZOS)

        card = Image.open(card_png_path).convert("RGBA")
        card_x = (CANVAS_W - card.width) // 2
        card_y = int(CANVAS_H * CARD_Y_FRAC)
        bg.paste(card, (card_x, card_y), card)

        result = bg.convert("RGB")
        result.save(str(output_thumb_path), "JPEG", quality=92,
                    optimize=True, progressive=True)

        # Clean up temp frame
        try:
            frame_path.unlink(missing_ok=True)
        except Exception:
            pass

        limit = 2 * 1024 * 1024
        size_kb = output_thumb_path.stat().st_size / 1024
        if output_thumb_path.stat().st_size >= limit:
            warn(f"Story thumb composite {size_kb:.0f} KB >= 2MB limit; "
                 "upload will skip it.")
        else:
            log(f"Story thumb composite: {output_thumb_path.name} "
                f"({size_kb:.0f} KB, {bg_track.name} @ "
                f"{offset:.2f}s, card at y={int(CANVAS_H * CARD_Y_FRAC)}).")
        return output_thumb_path
    except Exception as e:
        warn(f"Story thumb composite skipped ({e}).")
        return None


def _bg_sort_key(p: Path) -> tuple[int, str]:
    """Natural order for background filenames: bg2 before bg10.

    Plain sorted() is lexicographic, so 'bg10.mp4' would sort before
    'bg2.mp4' and break the rotation order as soon as the pool passes
    nine clips. Trailing number wins; names without one fall back to
    name order.
    """
    nums = re.findall(r"\d+", p.stem)
    return (int(nums[-1]) if nums else 0, p.stem.lower())


def _bg_order(clips: list[Path], video_number: int, mode: str) -> list[Path]:
    """Clip preference order for this video.

    rotate (default): deterministic round-robin keyed on video_number, so
    consecutive uploads walk bg1 -> bg2 -> ... -> bgN -> bg1. Predictable
    and easy to reason about ("this video is bg3"). Returns the whole pool
    rotated, NOT just the head, so a broken preferred clip still falls
    through to the next ones.
    random: legacy shuffle, for when you want a surprise background.
    """
    if len(clips) < 2:
        return list(clips)
    if str(mode or "rotate").strip().lower() == "random":
        order = list(clips)
        random.shuffle(order)
        return order
    start = int(video_number or 0) % len(clips)
    return list(clips[start:]) + list(clips[:start])


def build_visuals(content: Content, narration: NarrationResult,
                  cfg: StoryConfig, workdir: Path,
                  no_card: bool = False) -> VisualsResult:
    """Render the story Short: BG track + card + burned captions + mix.

    no_card skips the card overlay (preview tools); everything else —
    the BG track, captions, mix, encode — is the SAME render.
    """
    t_start = time.monotonic()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    words = narration.extras.get("words") or []
    genre = content.extras.get("genre", "")
    total = narration.duration + 0.4
    log(f"Story visuals: {narration.duration:.1f}s narration → {total:.1f}s video.")

    def local_clips(cfg: StoryConfig) -> list[Path]:
        """Every usable video in bg_dir (the only background source)."""
        bg_dir = Path(cfg.bg_dir)
        found = []
        if bg_dir.is_dir():
            found = sorted((p for p in bg_dir.iterdir()
                            if p.suffix.lower() in BG_EXTS and p.is_file()),
                           key=_bg_sort_key)
        if not found:
            raise RuntimeError(
                f"No background clips: '{cfg.bg_dir}' holds no "
                f"{sorted(BG_EXTS)} video. Drop portrait mp4s there.")
        return found

    # Stage 1: the local assets/backgrounds pool is the only source.
    clips = local_clips(cfg)
    log(f"Stage 1/6 clips: {len(clips)} local BG source(s) in "
        f"{Path(cfg.bg_dir).as_posix()}.")

    # Stage 2: background track from the local pool. Selection rotates
    # through the pool by video_number (STORY_BG_SELECT=rotate, default) so
    # consecutive uploads use consecutive clips; set it to "random" to
    # shuffle instead. The start offset INSIDE each clip stays random either
    # way, so the same clip never looks identical on its next turn.
    # STORY_BG_SEGMENTS (default 0 = off) then cuts the same total: 0/1 =
    # ONE continuous take (historical behavior), N>=2 = N segments, each
    # from a different clip in that same order (cycling, so the whole pool
    # is used), concatenated back into a single track. A broken clip warns
    # and the next in the order is tried; when EVERY clip fails the run
    # stops hard — there is no other background source.
    speed = float(getattr(cfg, "bg_single_speed", 1.8) or 1.8)
    video_number = int(content.extras.get("video_number") or 0)
    select = str(getattr(cfg, "bg_select", "rotate") or "rotate")
    segments = max(0, int(getattr(cfg, "bg_segments", 0) or 0))
    order = _bg_order(clips, video_number, select)
    log(f"Stage 2/6 BG pick: video #{video_number} -> {order[0].name} "
        f"({select} over {len(clips)} clips: "
        f"{', '.join(c.name for c in order[:6])}"
        f"{'...' if len(order) > 6 else ''}).")
    bg_track, bg_clips = _build_bg_track(order, total, speed, segments,
                                         workdir, cfg.bg_dir)
    # The clips actually used ARE the segments actually rendered (one clip
    # per segment), so len(bg_clips) is the truthful count even when the
    # pool holds a single clip and segmentation could not be honored.
    n_segments = len(bg_clips)
    names = [c.name for c in bg_clips]
    if segments >= 2 and len(order) < 2:
        warn(f"Story BG: only {len(order)} local clip available; "
             f"STORY_BG_SEGMENTS={segments} needs at least 2, so the video "
             f"keeps one continuous take.")
    shown = ",".join(names[:12])
    log(f"Stage 2/6 {'single-clip' if n_segments == 1 else 'segmented'} "
        f"mode: {bg_track.name} from {n_segments} segment(s) @ "
        f"{speed:.2f}x: {shown}{'...' if len(names) > 12 else ''}.")
    log(f"Stage 3/6 BG track: {bg_track.name} "
        f"({ffprobe_duration(bg_track):.1f}s, {n_segments} segment"
        f"{'s' if n_segments > 1 else ''}: "
        f"{shown}{'...' if len(names) > 12 else ''}).")
    # No stock footage is embedded, so nothing to attribute or dedup.
    credits: list[str] = []
    used_ids: list[str] = []

    # Stage 4: card + captions.
    card_end = float(narration.extras.get("card_end", cfg.card_seconds))
    card_dur = max(cfg.card_seconds, card_end + cfg.card_pad)
    card_png = _card.render_spec_card(
        content.extras.get("card_text", content.title),
        content.extras.get("card_highlight_word", ""),
        handle=cfg.channel_handle, avatar_path=cfg.avatar_path,
        out_png=workdir / "card.png",
        theme={**STORY_CARD_THEME,
               "likes_text": cfg.card_likes,
               "shares_text": cfg.card_shares,
               "show_reactions": cfg.card_show_reactions},
        likes=cfg.card_likes, shares=cfg.card_shares,
        show_reactions=cfg.card_show_reactions)
    log(f"Stage 4/6 card: {card_png.name} visible 0-{card_dur:.1f}s "
        f"(line end {card_end:.1f}s).")
    # Burned captions start AFTER the card line ends (the card text
    # serves as the on-screen visual for the opening narration); the
    # writer drops/shifts earlier words itself.
    ass_path = write_wordpop_ass(
        words, workdir / "captions.ass",
        key_words=set(content.extras.get("key_words", [])),
        upper=cfg.caption_upper, start_after=card_end,
        fontsize=cfg.ass_fontsize)
    # Upload SRT keeps all words (YouTube captions aren't on the card).
    out_srt = write_upload_srt(words, workdir / "captions.srt")
    log(f"Stage 4/6 captions: {len(words)} timed words → ASS burn "
        f"@{cfg.ass_fontsize}px (after {card_end:.1f}s) + SRT upload.")

    # Stage 5: BGM bed (explicit file → random bgm pool → drone → silent).
    # Loop the bed at UNITY gain: loop_bed's own default (0.08) used to stack
    # with cfg.bgm_volume in _mix_filter, so the effective bed gain was
    # bgm_volume * 0.08 (~-58 dB under the voice, inaudible). _mix_filter is
    # now the single place the bed level is set, and the drone fallback goes
    # through the same unity loop so it is equally loud.
    try:
        picked = _pick_bgm(cfg)
        bed = loop_bed(picked or synth_drone(total, workdir / "bed_raw.mp3"),
                       total, workdir / "bed.mp3", volume=1.0)
    except Exception as e:
        warn(f"Story bed failed ({e}); using silent bed.")
        silent = workdir / "bed_silent.mp3"
        run_ffmpeg(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"anullsrc=r=44100:cl=mono:d={total:.3f}",
                    "-c:a", "libmp3lame", str(silent)], "bed-silent")
        bed = silent
    log("Stage 5/6 audio: bed loop ready (ducked under voice in render).")

    # Stage 6: single-pass final render (card + captions + voice mix).
    # veryfast preset: medium-preset 1080x1920 encodes were silently
    # killed on CI runners (suspected OOM); veryfast halves peak RAM.
    final_preset = "veryfast"
    require_ass_filter()
    # fontsdir must be the real font dir, never workdir (workdir holds
    # bg_track.mp4/bed.mp3/etc and libass spams "Error opening memory font"
    # for every media file). Ensure the dir exists and always pass it:
    # an empty scan means no spam, and libass still uses fontconfig
    # system fonts (DejaVu on CI).
    _fonts_dir = Path(__file__).resolve().parents[2] / "assets" / "fonts"
    try:
        _fonts_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # still pass the path: missing dir scans nothing (≠ workdir)
    vf_args, cwd = ass_cwd_args(ass_path, fontsdir=_fonts_dir.resolve())
    subs = vf_args[1]  # "ass=<rel>:fontsdir=<dir>" for filter_complex use
    mix_fc = _mix_filter(
        total, getattr(cfg, "bgm_volume", 0.10),
        duck_threshold=getattr(cfg, "bgm_duck_threshold", 0.05),
        duck_ratio=getattr(cfg, "bgm_duck_ratio", 8.0),
        duck_attack_ms=getattr(cfg, "bgm_duck_attack_ms", 10),
        duck_release_ms=getattr(cfg, "bgm_duck_release_ms", 400),
        highpass_hz=getattr(cfg, "bgm_highpass_hz", 180),
        lowpass_hz=getattr(cfg, "bgm_lowpass_hz", 9000),
        output_gain_db=getattr(cfg, "mix_gain_db", 7.0))
    out_mp4 = workdir / "short.mp4"
    # ffmpeg runs with cwd=workdir (for the cwd-relative ASS filter), so
    # every -i input and the output must be absolute; repo-relative
    # "out/<stamp>_work/..." paths would resolve to "<workdir>/out/..."
    # and fail with "[in#0] No such file".
    bg_abs = str(Path(bg_track).resolve())
    narr_abs = str(Path(narration.audio).resolve())
    bed_abs = str(Path(bed).resolve())
    card_abs = str(Path(card_png).resolve())
    out_abs = str(Path(out_mp4).resolve())
    render_inputs = [bg_abs, narr_abs, bed_abs]
    if not no_card:
        render_inputs.append(card_abs)
    log(f"Stage 6/6 render: {total:.1f}s total, preset {final_preset}, "
        f"inputs={render_inputs}.")
    t_render = time.monotonic()
    if no_card:
        run_ffmpeg([
            "ffmpeg", "-y", "-i", bg_abs, "-i", narr_abs,
            "-i", bed_abs,
            "-filter_complex",
            f"{mix_fc};[0:v]{subs},format=yuv420p[vout]",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", final_preset, "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-movflags", "+faststart", "-threads", "0",
            "-t", f"{total:.3f}", out_abs],
            "final-render", cwd=cwd)
    else:
        card_args = _card_input_args(Path(card_abs), card_dur)
        card_fc = _card_filter(card_dur)
        run_ffmpeg([
            "ffmpeg", "-y", "-i", bg_abs, "-i", narr_abs,
            "-i", bed_abs, *card_args,
            "-filter_complex",
            f"{mix_fc};[0:v]null[base];[3:v]null[card];{card_fc};"
            f"[v1]{subs},format=yuv420p[vout]",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", final_preset, "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-movflags", "+faststart", "-threads", "0",
            "-t", f"{total:.3f}", out_abs],
            "final-render", cwd=cwd)
    size_mb = out_mp4.stat().st_size / (1024 * 1024)
    elapsed = time.monotonic() - t_start
    log(f"Stage 6/6 done: {out_mp4.name} ({size_mb:.1f} MB, "
        f"{ffprobe_duration(out_mp4):.1f}s video, {elapsed:.0f}s render, "
        f"preset {final_preset}).")
    if size_mb >= 100:
        warn(f"Story video {size_mb:.1f} MB >= 100 MB budget.")
    # Real segment count: 1 for the single continuous take, STORY_BG_SEGMENTS
    # when the timeline is cut up. Machine-readable proof in out/*.json.
    extras = {"segments": n_segments, "bg_clips": names,
              "card_dur": card_dur,
              "card_end": card_end, "genre": genre,
              "size_mb": round(size_mb, 1)}
    # Thumbnail: card over a frame of the SAME rendered BG track the video
    # uses (same clip, random start offset, speed and cover crop already
    # baked in), so the composite matches the video's background. With
    # segmentation on, STORY_THUMB_OFFSET_SECONDS lands in the FIRST
    # segment (the default 1.0s is inside it) — always a freshly rendered
    # frame, never a stale one. Falls back to a frame of the final mp4.
    # Both paths are warn-only.
    log(f"Stage 6/6 thumb source: bg_track {bg_track.name} from "
        f"{', '.join(names) if names else bg_track.name}.")
    thumb = _composite_thumb(workdir / "card.png", bg_track,
                             workdir / "thumb.jpg",
                             _thumb_offset(cfg, card_dur))
    if thumb is None:
        log("Story thumb composite unavailable; falling back to frame "
            "extraction from the final render.")
        thumb = _extract_thumb(out_mp4, card_dur, cfg, workdir)
    if thumb is not None:
        extras["thumb"] = str(thumb)
    return VisualsResult(mp4=out_mp4, srt=out_srt, credits=credits,
                         used_ids=used_ids, extras=extras)
