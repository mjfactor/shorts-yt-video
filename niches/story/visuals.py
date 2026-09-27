"""Story stage 3: BG-driven 1080x1920 render with card + word-pop captions.

Pipeline: pick one clip from the local assets/backgrounds folder → one
continuous single-take BG track (total = narration + 0.4s) → card overlay
+ ASS burn + mixed audio in one final pass (crf20/veryfast/AAC-192k).
Local clips are the ONLY background source: an empty folder is a hard
failure, never a silent fallback.
"""
from __future__ import annotations

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
BG_EXTS = {".mp4", ".mov", ".webm", ".mkv"}

STORY_CARD_THEME = {
    "card_fill": (18, 18, 24, 255),
    "text_color": (245, 245, 247, 255),
    "highlight_color": (255, 210, 63, 255),
    "handle_color": (245, 245, 247, 255),
}


def _pick_bgm(cfg: StoryConfig) -> Path | None:
    """Bed source priority: explicit STORY_BED_PATH file → one random
    match for bgm_glob from cfg.bgm_dir → the same pattern at the repo
    root (legacy layout) → None (caller synthesizes the drone).

    Pool is sorted then random.choice per video and logged with the file
    name and pool size. Both layouts work, so moving the tracks between
    them never silences the bed.
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
                pick = random.choice(pool)
                log(f"Story BGM pool: picked {pick.name} "
                    f"({len(pool)} files) from {base.as_posix() or '.'}.")
                return pick
    # Legacy layout: pool at the repo root.
    pool = sorted(Path().glob(pattern))
    if pool:
        pick = random.choice(pool)
        log(f"Story BGM pool: picked {pick.name} ({len(pool)} files) "
            "from the repo root.")
        return pick
    log(f"Story BGM pool: no {pattern} match in "
        f"{getattr(cfg, 'bgm_dir', '') or '.'} or the repo root; "
        "using the synthesized drone.")
    return None


def _seg_filter(speed: float) -> str:
    """Scale-to-cover + center-crop, then speed, fps, pixfmt.

    NOTE: scale's 3rd positional arg is sws FLAGS, not the aspect-ratio
    mode — `scale=1080:1920:increase` fails on real FFmpeg ("Unable to
    parse sws_flags"). The named force_original_aspect_ratio form is
    required.
    """
    sp = max(0.2, float(speed))
    return (f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,"
            f"crop={CANVAS_W}:{CANVAS_H},"
            f"setpts=PTS/{sp:.3f},fps=30,format=yuv420p")


def _build_single_clip_track(clip: Path, total: float, speed: float,
                            workdir: Path) -> tuple[Path, float, float]:
    """One continuous background take from a local clip.

    Picks a random start offset so repeated use of the same file looks
    different, cuts exactly `total` seconds of output, and covers the
    whole span even when the source is shorter than needed (looped input
    with -stream_loop -1, truncated to the exact length). Returns
    (track_path, start_offset, speed).
    """
    sp = max(0.2, float(speed))
    try:
        src_dur = ffprobe_duration(clip)
    except Exception:
        src_dur = 0.0
    need = total * sp  # source seconds required at this speed
    if src_dur > need:
        offset = random.uniform(0.0, max(0.0, src_dur - need))
    else:
        # Short source: loop it and keep playing from 0.
        offset = 0.0
    track = workdir / "bg_single.mp4"
    args = ["ffmpeg", "-y"]
    if src_dur <= need:
        args += ["-stream_loop", "-1"]
    args += ["-ss", f"{offset:.3f}", "-i", str(clip),
             "-t", f"{total:.3f}", "-vf", _seg_filter(sp),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-an", str(track)]
    run_ffmpeg(args, "bg-single-clip")
    log(f"Story BG single-clip: {clip.name} @ {sp:.2f}x, start {offset:.1f}s, "
        f"need {need:.1f}s of {src_dur:.1f}s source"
        f"{' (looped to cover)' if src_dur <= need else ''} "
        f"-> {total:.1f}s track.")
    return track, offset, sp


def _mix_filter(total: float, bed_volume: float = 0.12) -> str:
    """Voice-forward mix chain (inputs: 0=BG video, 1=narration, 2=bed).

    Looser ducking (threshold 0.15, ratio 3, release 180) so the bed sits
    higher between words; narration is mixed at full untouched volume, so
    voice clarity never depends on the duck. 2s outro fade, normalize=0.
    """
    fade_st = max(0.0, float(total) - 2.0)
    return (f"[2:a]volume={max(0.0, float(bed_volume)):.2f}[b];"
            "[b][1:a]sidechaincompress=threshold=0.15:ratio=3:"
            "attack=20:release=180[duck];"
            "[1:a][duck]amix=inputs=2:duration=first:normalize=0,"
            f"afade=t=out:st={fade_st:.2f}:d=2[aout]")


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
        f"[base][c]overlay=x=(W-w)/2:y=H*0.30:"
        f"enable='between(t,0,{dur:.2f})'[v1]"
    )


def _extract_thumb(mp4: Path, card_dur: float, cfg: StoryConfig,
                   workdir: Path) -> Path | None:
    """Grab one 1080x1920 JPG frame for the YouTube thumbnail (warn-only).

    Offset defaults to ~1.0s (inside the card-visible 0-card_dur window)
    via STORY_THUMB_OFFSET_SECONDS, clamped inside card_dur. Quality is
    tuned (-q:v 4, fallback 10) to stay under YouTube's 2MB thumbnail
    limit. Never raises: returns None on any failure.
    """
    try:
        raw = float(getattr(cfg, "thumb_offset_seconds", 1.0) or 1.0)
        hi = max(0.1, float(card_dur) - 0.1)
        offset = min(max(0.0, raw), hi)
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


def _composite_thumb(card_png_path: Path, bg_clip_path: Path,
                     output_thumb_path: Path) -> Path | None:
    """Generate a thumbnail by compositing the card PNG onto the first frame
    of the first BG clip at the same y=H*0.30 position used in the video.

    Extracts frame 0 from bg_clip_path via FFmpeg, then uses Pillow to
    composite the RGBA card PNG centered horizontally at y = H * 0.30.
    Exports as JPEG quality=92 at 1080x1920. Returns output path or None.
    """
    try:
        frame_path = output_thumb_path.with_suffix(".frame.png")
        run_ffmpeg(["ffmpeg", "-y", "-i", str(Path(bg_clip_path).resolve()),
                    "-vframes", "1", "-vf", f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,"
                    f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2",
                    str(frame_path)], "thumb-frame-extract")
        if not frame_path.exists():
            warn("Story thumb composite: frame extraction produced no file.")
            return None

        bg = Image.open(frame_path).convert("RGBA")
        if bg.size != (CANVAS_W, CANVAS_H):
            bg = bg.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)

        card = Image.open(card_png_path).convert("RGBA")
        card_x = (CANVAS_W - card.width) // 2
        card_y = int(CANVAS_H * 0.30)
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
                f"({size_kb:.0f} KB, card at y={int(CANVAS_H * 0.30)}).")
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
    the single BG take, captions, mix, encode — is the SAME render.
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

    # Stage 2: one continuous take from a single local clip (the whole
    # video is one BG track, never a cut-up). Selection rotates through the
    # pool by video_number (STORY_BG_SELECT=rotate, default) so consecutive
    # uploads use consecutive clips; set it to "random" to shuffle instead.
    # The start offset INSIDE the chosen clip stays random either way, so
    # the same clip never looks identical on its next turn. A broken clip
    # warns and the next in the order is tried; when EVERY clip fails the
    # run stops hard — there is no other background source.
    speed = float(getattr(cfg, "bg_single_speed", 1.8) or 1.8)
    video_number = int(content.extras.get("video_number") or 0)
    select = str(getattr(cfg, "bg_select", "rotate") or "rotate")
    order = _bg_order(clips, video_number, select)
    log(f"Stage 2/6 BG pick: video #{video_number} -> {order[0].name} "
        f"({select} over {len(clips)} clips: "
        f"{', '.join(c.name for c in order[:6])}"
        f"{'...' if len(order) > 6 else ''}).")
    bg_track: Path | None = None
    for cand in order:
        try:
            bg_track, _off, _sp = _build_single_clip_track(
                cand, total, speed, workdir)
            log(f"Stage 2/6 single-clip mode: {bg_track.name} from "
                f"{cand.name} @ {speed:.2f}x.")
            break
        except Exception as e:
            warn(f"Story BG: local clip {cand.name} failed "
                 f"({type(e).__name__}: {e}); trying another local clip...")
    if bg_track is None:
        raise RuntimeError(
            f"Story BG: all {len(order)} local clips in '{cfg.bg_dir}' "
            f"failed to render; no other background source exists.")
    log(f"Stage 3/6 BG track: {bg_track.name} "
        f"({ffprobe_duration(bg_track):.1f}s continuous).")
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
        theme={**_card.SPEC_CARD_THEME,
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
        f"(after {card_end:.1f}s) + SRT upload.")

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
    # bg_single.mp4/bed.mp3/etc and libass spams "Error opening memory font"
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
    mix_fc = _mix_filter(total, getattr(cfg, "bgm_volume", 0.12))
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
    # One continuous BG take: there are no segments to report.
    extras = {"segments": 1, "card_dur": card_dur,
              "card_end": card_end, "genre": genre,
              "size_mb": round(size_mb, 1)}
    thumb = _composite_thumb(workdir / "card.png", clips[0] if clips else out_mp4,
                              workdir / "thumb.jpg")
    if thumb is None:
        warn("Story thumb composite failed; falling back to frame extraction.")
        thumb = _extract_thumb(out_mp4, card_dur, cfg, workdir)
    if thumb is not None:
        extras["thumb"] = str(thumb)
    return VisualsResult(mp4=out_mp4, srt=out_srt, credits=credits,
                         used_ids=used_ids, extras=extras)
