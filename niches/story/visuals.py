"""Story stage 3: BG-driven 1080x1920 render with card + word-pop captions.

Pipeline: gather BG clips (Pexels → BG_DIR fallback) → pure planner
(total = narration + 0.4s) → per-seg transcode → concat → card overlay +
ASS burn + mixed audio in one final pass (crf20/medium/AAC-192k).
"""
from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from engine import overlay_card as _card
from engine import pexels as _px
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

EVERGREEN_QUERIES = (
    "city night lights", "rain window night", "empty street dusk",
    "coffee shop morning", "highway cars night", "forest fog moody",
    "ocean waves dark", "office desk lamp",
)
GENRE_EVERGREEN = {
    "workplace": ("office desk night", "empty office hallway", "city office windows"),
    "revenge": ("apartment building night", "door knock night", "city street night"),
    "scary": ("dark hallway shadows", "night forest fog", "empty store night"),
    "relationship": ("couple silhouette sunset", "rain window night", "city park dusk"),
    "AITA": ("family dinner table", "living room argument", "front door house"),
    "never-again": ("hospital hallway night", "car road night", "storm clouds dark"),
}


def _pick_window(rng: random.Random, clip_dur: float, window: float,
                 used: list[tuple[float, float]], tries: int = 8) -> float:
    """Random src_start with src_start+window inside the clip and clear of
    already-used windows; relaxes to any in-bounds start on exhaustion."""
    latest = max(0.0, float(clip_dur) - float(window))
    for _ in range(max(1, tries)):
        s = round(rng.uniform(0.0, latest), 3) if latest > 0 else 0.0
        if all(s + window <= us or s >= ue for us, ue in used):
            return s
    return round(rng.uniform(0.0, latest), 3) if latest > 0 else 0.0


def plan(total_dur: float, *, seg_min: float = 2.5, seg_max: float = 4.5,
         n_clips: int = 1, seed: int | None = None,
         min_segments: int = 25, max_segments: int = 40,
         speed_min: float = 1.15, speed_max: float = 1.5,
         recent_ids: tuple = (),
         clips: list[dict] | None = None) -> list[dict]:
    """Pure planner: segments summing to total_dur exactly (within 1 frame).

    Rules: every out_duration in [SEG_MIN, SEG_MAX] (duration bounds win
    over the segment-count range for tiny totals); last segment trims the
    rounding drift; no clip in 4 consecutive segments; no overlapping
    source window per video id; speeds in [1.15, 1.5]; each source window
    (out_dur*speed) fits inside its clip. Aims for >=12 distinct clips
    (logs when fewer are available). clips=[{id, duration}] overrides the
    legacy n_clips count (which assumes 30s clips).
    """
    rng = random.Random(seed)
    total = max(0.0, float(total_dur))
    if total <= 0:
        return []
    lo = min(float(seg_min), float(seg_max))
    hi = max(float(seg_min), float(seg_max))
    smin = min(float(speed_min), float(speed_max))
    smax = max(float(speed_min), float(speed_max))
    if clips:
        table = [{"id": str(c.get("id", i)),
                  "duration": max(0.1, float(c.get("duration", 30.0) or 30.0))}
                 for i, c in enumerate(clips)]
    else:
        n = max(1, int(n_clips or 1))
        table = [{"id": f"clip{i}", "duration": 30.0} for i in range(n)]
    n = len(table)
    count = min(max_segments, max(min_segments, round(total / ((lo + hi) / 2))))
    if total / count < lo:
        count = max(1, min(max_segments, int(total // lo) or 1))
    elif total / count > hi:
        count = max(1, min(max_segments, int(total / hi) + 1))
    base = total / count
    durs = [round(base, 3)] * count
    durs[-1] = round(total - sum(durs[:-1]), 3)
    recent = {str(r) for r in (recent_ids or ())}
    segs: list[dict] = []
    prev: list[int] = []
    uses = dict.fromkeys(range(n), 0)
    used_windows: dict[int, list[tuple[float, float]]] = {}
    cursor: dict[int, float] = {}
    for dur in durs:
        eligible = [i for i in range(n) if i not in prev[-4:]] or list(range(n))
        fresh = ([i for i in eligible if table[i]["id"] not in recent]
                 or eligible)
        least = min(uses[i] for i in fresh)
        idx = rng.choice([i for i in fresh if uses[i] == least])
        speed = round(rng.uniform(smin, smax), 3)
        window = round(dur * max(0.2, speed), 3)
        cdur = table[idx]["duration"]
        cur = cursor.get(idx, 0.0)
        if cur + window <= cdur and all(
                cur + window <= us or cur >= ue
                for us, ue in used_windows.get(idx, [])):
            start = cur  # tile forward: never overlaps while room lasts
        else:
            start = _pick_window(rng, cdur, window,
                                 used_windows.get(idx, []))
        used_windows.setdefault(idx, []).append((start, start + window))
        cursor[idx] = max(cursor.get(idx, 0.0), start + window)
        uses[idx] += 1
        prev.append(idx)
        segs.append({
            "clip_idx": idx,
            "clip_id": table[idx]["id"],
            "start_offset": start,
            "dur": dur,
            "speed": speed,
            "window": window,
        })
    distinct = sum(1 for v in uses.values() if v)
    if distinct < min(12, n):
        log(f"Story plan: only {distinct} distinct clips "
            f"(<{min(12, n)} target); reuse spacing relaxed.")
    return segs


def _fetch_one(query: str, api_key: str, cache_dir: Path,
               used: set[str]) -> tuple[Path | None, str, str]:
    """Fetch one clip for a query. Returns (path|None, video_id, credit)."""
    try:
        for v in _px.search_videos(query, api_key, per_page=30,
                                   orientation="portrait", size="medium"):
            vid = str(v.get("id", ""))
            if not vid or vid in used:
                continue
            picked = _px.pick_video_file(v.get("video_files", []),
                                         target_w=1080, target_h=1920)
            if not picked:
                continue
            dest = _px.cache_path(cache_dir, vid)
            if not dest.exists():
                _px.download(picked["link"], dest)
            used.add(vid)
            credit = _px.parse_photographer(v) or "Pexels"
            return dest, vid, f"Pexels: {credit}"
    except Exception as e:
        warn(f"Pexels query failed '{query}' ({e}).")
    return None, "", ""


def gather_clips(bg_queries: list[str], genre: str, cfg: StoryConfig,
                 recent_ids: tuple = ()) -> tuple[list[Path], list[str], list[str]]:
    """BG clips: Pexels (prefetched) → BG_DIR fallback. Clear error if none."""
    cache_dir = Path(cfg.pexels_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    queries: list[str] = list(dict.fromkeys([*(bg_queries or []),
                                  *GENRE_EVERGREEN.get((genre or "").lower(), ()),
                                  *EVERGREEN_QUERIES]))
    clips: list[Path] = []
    credits: list[str] = []
    used: set[str] = {str(r) for r in (recent_ids or ())}
    used_ids: list[str] = []
    if cfg.pexels_key:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(_fetch_one, q, cfg.pexels_key, cache_dir, used): q
                    for q in queries}
            by_query: dict[str, tuple] = {}
            for fut in as_completed(futs):
                by_query[futs[fut]] = fut.result()
            for q in queries:  # keep query priority order, not finish order
                res = by_query.get(q)
                path, vid, credit = res if res is not None else (None, "", "")
                if path is not None:
                    clips.append(path)
                    credits.append(credit)
                    used_ids.append(vid)
        log(f"Story BG: {len(clips)} Pexels clips for {len(queries)} queries.")
    else:
        log("Story BG: no PEXELS key, using BG_DIR only.")
    bg_dir = Path(cfg.bg_dir)
    if bg_dir.exists():
        local = sorted(p for p in bg_dir.iterdir()
                       if p.suffix.lower() in BG_EXTS and p.is_file())
        for p in local:
            if str(p) not in {str(c) for c in clips}:
                clips.append(p)
        if local:
            log(f"Story BG: +{len(local)} local clips from {bg_dir}.")
    if not clips:
        raise RuntimeError(
            f"No background clips: Pexels returned nothing for {len(queries)} "
            f"queries and BG_DIR '{cfg.bg_dir}' is empty. Add API key or "
            "drop portrait mp4s into assets/backgrounds/.")
    if len(clips) < cfg.min_clips:
        warn(f"Story BG: only {len(clips)} clips (<{cfg.min_clips} target); "
             "reuse spacing relaxed.")
    return clips, credits, used_ids


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


def _transcode_segments(clips: list[Path], segs: list[dict],
                        workdir: Path) -> list[Path]:
    """One silent 1080x1920 mp4 per planned segment (offsets clamped).

    Per-seg temp files + concat-demuxer -c copy (instead of one giant
    filter_complex): per-seg speed/crop stays simple, a failed segment
    retries alone, and concat never re-encodes (memory stays flat for
    25-40 segments).
    """
    out: list[Path] = []
    for i, s in enumerate(segs):
        src = clips[s["clip_idx"] % len(clips)]
        dur = max(0.1, float(s["dur"]))
        speed = float(s["speed"])
        try:
            clip_len = ffprobe_duration(src)
        except Exception:
            clip_len = 0.0
        need = dur * max(0.2, speed)
        offset = min(float(s["start_offset"]),
                     max(0.0, clip_len - need)) if clip_len > need else 0.0
        seg = workdir / f"bg_{i:03d}.mp4"
        run_ffmpeg(["ffmpeg", "-y",
                    "-ss", f"{offset:.3f}", "-i", str(src),
                    "-t", f"{dur:.3f}",
                    "-vf", _seg_filter(speed),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-an", str(seg)], f"bg-seg-{i}")
        out.append(seg)
        if (i + 1) % 10 == 0 or i + 1 == len(segs):
            log(f"Story BG: {i + 1}/{len(segs)} segments transcoded.")
    return out


def _concat(segs: list[Path], workdir: Path) -> Path:
    """Concat segments into one silent BG track (stream copy, no re-encode)."""
    lst = workdir / "bg_concat.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in segs))
    out = workdir / "bg_track.mp4"
    run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(lst), "-c", "copy", str(out)], "bg-concat")
    return out


def _mix_filter(total: float) -> str:
    """Voice-forward mix chain (inputs: 0=BG video, 1=narration, 2=bed).

    Ducked bed under untouched narration, 2s outro fade. Spec-exact.
    """
    fade_st = max(0.0, float(total) - 2.0)
    return ("[2:a]volume=0.12[b];"
            "[b][1:a]sidechaincompress=threshold=0.05:ratio=8:"
            "attack=20:release=300[duck];"
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


def build_visuals(content: Content, narration: NarrationResult,
                  cfg: StoryConfig, workdir: Path,
                  no_card: bool = False) -> VisualsResult:
    """Render the story Short: BG track + card + burned captions + mix.

    no_card skips the card overlay (preview tools); everything else —
    planner, transcodes, captions, mix, encode — is the SAME render.
    """
    t_start = time.monotonic()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    words = narration.extras.get("words") or []
    genre = content.extras.get("genre", "")
    total = narration.duration + 0.4
    log(f"Story visuals: {narration.duration:.1f}s narration → {total:.1f}s video.")

    # Stage 1: clips.
    clips, credits, used_ids = gather_clips(
        list(content.extras.get("bg_queries", [])) or
        list(narration.extras.get("bg_queries", [])),
        genre, cfg)
    log(f"Stage 1/6 clips: {len(clips)} BG sources, {len(used_ids)} Pexels ids.")

    # Stage 2: plan + transcode (planner gets real clip durations for
    # exact source-window math).
    clip_infos = []
    for i, c in enumerate(clips):
        try:
            c_dur = ffprobe_duration(c)
        except Exception:
            c_dur = 30.0
        cid = used_ids[i] if i < len(used_ids) else Path(c).stem
        clip_infos.append({"id": str(cid), "duration": c_dur})
    segs = plan(total, seg_min=cfg.seg_min, seg_max=cfg.seg_max,
                n_clips=len(clips), speed_min=cfg.bg_speed_min,
                speed_max=cfg.bg_speed_max,
                min_segments=cfg.min_segments, max_segments=cfg.max_segments,
                clips=clip_infos)
    log(f"Stage 2/6 plan: {len(segs)} segments "
        f"({segs[0]['dur']:.2f}s avg, speeds {segs[0]['speed']}).")
    seg_files = _transcode_segments(clips, segs, workdir)

    # Stage 3: concat BG track.
    bg_track = _concat(seg_files, workdir)
    log(f"Stage 3/6 BG track: {bg_track.name} "
        f"({ffprobe_duration(bg_track):.1f}s).")

    # Stage 4: card + captions.
    card_end = float(narration.extras.get("card_end", cfg.card_seconds))
    card_dur = max(cfg.card_seconds, card_end + cfg.card_pad)
    card_png = _card.render_spec_card(
        content.extras.get("card_text", content.title),
        content.extras.get("card_highlight_word", ""),
        handle=cfg.channel_handle, avatar_path=cfg.avatar_path,
        out_png=workdir / "card.png", theme=_card.SPEC_CARD_THEME)
    log(f"Stage 4/6 card: {card_png.name} visible 0-{card_dur:.1f}s "
        f"(line end {card_end:.1f}s).")
    # Burned captions start AFTER the card line ends (the card text
    # serves as the on-screen visual for the opening narration); the
    # writer drops/shifts earlier words itself.
    ass_path = write_wordpop_ass(
        words, workdir / "captions.ass",
        key_words=set(content.extras.get("key_words", [])),
        upper=cfg.caption_upper, start_after=card_end)
    # Upload SRT keeps all words (YouTube captions aren't on the card).
    out_srt = write_upload_srt(words, workdir / "captions.srt")
    log(f"Stage 4/6 captions: {len(words)} timed words → ASS burn "
        f"(after {card_end:.1f}s) + SRT upload.")

    # Stage 5: BGM bed loop (mixed with voice in the final graph).
    bed_src = Path(cfg.bed_path) if cfg.bed_path else None
    if bed_src is not None and not bed_src.exists():
        warn(f"Story bed file missing ({bed_src}); synthesizing drone.")
        bed_src = None
    try:
        bed = loop_bed(bed_src or synth_drone(total, workdir / "bed_raw.mp3"),
                       total, workdir / "bed.mp3")
    except Exception as e:
        warn(f"Story bed failed ({e}); using silent bed.")
        silent = workdir / "bed_silent.mp3"
        run_ffmpeg(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"anullsrc=r=44100:cl=mono:d={total:.3f}",
                    "-c:a", "libmp3lame", str(silent)], "bed-silent")
        bed = silent
    log("Stage 5/6 audio: bed loop ready (ducked under voice in render).")

    # Stage 6: single-pass final render (card + captions + voice mix).
    require_ass_filter()
    vf_args, cwd = ass_cwd_args(ass_path)
    subs = vf_args[1]  # "ass=<rel>:fontsdir=<dir>" for filter_complex use
    mix_fc = _mix_filter(total)
    out_mp4 = workdir / "short.mp4"
    t_render = time.monotonic()
    if no_card:
        run_ffmpeg([
            "ffmpeg", "-y", "-i", str(bg_track), "-i", str(narration.audio),
            "-i", str(bed),
            "-filter_complex",
            f"{mix_fc};[0:v]{subs},format=yuv420p[vout]",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-movflags", "+faststart", "-threads", "0",
            "-t", f"{total:.3f}", str(out_mp4)],
            "final-render", cwd=cwd)
    else:
        card_args = _card_input_args(card_png.resolve(), card_dur)
        card_fc = _card_filter(card_dur)
        run_ffmpeg([
            "ffmpeg", "-y", "-i", str(bg_track), "-i", str(narration.audio),
            "-i", str(bed), *card_args,
            "-filter_complex",
            f"{mix_fc};[0:v]null[base];[3:v]null[card];{card_fc};"
            f"[v1]{subs},format=yuv420p[vout]",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
            "-movflags", "+faststart", "-threads", "0",
            "-t", f"{total:.3f}", str(out_mp4)],
            "final-render", cwd=cwd)
    size_mb = out_mp4.stat().st_size / (1024 * 1024)
    elapsed = time.monotonic() - t_start
    log(f"Stage 6/6 done: {out_mp4.name} ({size_mb:.1f} MB, "
        f"{ffprobe_duration(out_mp4):.1f}s video, {elapsed:.0f}s render).")
    if size_mb >= 100:
        warn(f"Story video {size_mb:.1f} MB >= 100 MB budget.")
    return VisualsResult(mp4=out_mp4, srt=out_srt, credits=credits,
                         used_ids=used_ids,
                         extras={"segments": len(segs), "card_dur": card_dur,
                                 "card_end": card_end, "genre": genre,
                                 "size_mb": round(size_mb, 1)})
