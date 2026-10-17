"""Styled ASS writer for burn-in captions. All style comes from the caller."""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

from .ffmpeg_utils import warn
from .overlay_card import find_bundled_font
from .srt import group_words

# Caption vertical anchor (PlayRes space, 1080x1920). \an5 is middle-center,
# and libass IGNORES MarginV for middle alignment, so height is an explicit
# \pos(). 960 = exact canvas center (visually identical to no \pos at all).
CAPTION_POS_Y = 960
CAPTION_POS_X = 540


def _env_pos_y() -> int:
    """Caption anchor Y from env (STORY_CAPTION_POS_Y wins over bare).

    Read per call, not at import, so a run that exports the var after import
    still lands. Non-numeric/empty falls back to CAPTION_POS_Y.
    """
    for name in ("STORY_CAPTION_POS_Y", "CAPTION_POS_Y"):
        raw = os.getenv(name, "").strip()
        if raw:
            try:
                return int(float(raw))
            except ValueError:
                continue
    return CAPTION_POS_Y


def _env_fontsize() -> int:
    """Caption font size from env (STORY_ASS_FONTSIZE wins over bare).

    Shares the story config's var name so one repo Variable tunes both the
    demo and production. Falls back to WORDPOP_FONTSIZE.
    """
    for name in ("STORY_ASS_FONTSIZE", "ASS_FONTSIZE"):
        raw = os.getenv(name, "").strip()
        if raw:
            try:
                return int(float(raw))
            except ValueError:
                continue
    return WORDPOP_FONTSIZE


def _max_chars_for(size: int, base_size: int = 132,
                   base_chars: int = 14) -> int:
    """Group char cap scaled to font size so captions stay in the safe area.

    14 chars fits the 60px side margins at 132pt. Bigger type needs fewer
    chars per cue or lines run off the 1080-wide canvas.
    """
    if size <= 0:
        return base_chars
    return max(6, int(round(base_chars * base_size / float(size))))


def pop_tag(pos_y: int = CAPTION_POS_Y) -> str:
    """Word-pop override prefix, anchored at (CAPTION_POS_X, pos_y).

    Order matters: \\an5 before \\pos so the anchor is middle-center when the
    position is applied. Single source of truth for BOTH writers (write_ass
    for the demo, write_wordpop_ass for production story) so a demo render can
    validate a production caption change. pos_y defaults to CAPTION_POS_Y,
    overridable at runtime via CAPTION_POS_Y / STORY_CAPTION_POS_Y.
    """
    y = _env_pos_y() if pos_y == CAPTION_POS_Y else int(pos_y)
    return (rf"{{\an5\pos({CAPTION_POS_X},{y})\fscx70\fscy70"
            r"\t(0,90,\fscx112\fscy112)"
            r"\t(90,160,\fscx100\fscy100)}")


DEFAULT_STYLE = {
    "fontname": "Montserrat ExtraBold",
    # 132pt (+10% over orig 120, -8% from 144 which read slightly big):
    # word-pop stays clearly bigger than original on 1080x1920 and still
    # fits 14-char groups inside 60px side margins.
    "fontsize": 132,
    "primary_colour": "&H00FFFFFF",
    "outline_colour": "&HFF000000",
    "back_colour": "&H99000000",
    "bold": -1,
    "outline": 9,  # scaled with fontsize to keep stroke weight proportionate
    "shadow": 3,
    "alignment": 5,
    "margin_v": 200,
    "play_res_x": 1080,
    "play_res_y": 1920,
    "num_color": "{\\c&H0000FF&}",   # digits pop red
    "key_color": "{\\c&H00E5FF&}",   # key words pop gold
    "pop_in": pop_tag(),
}


def ass_ts(sec: float) -> str:
    """Seconds -> ASS timestamp H:MM:SS.cc."""
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - math.floor(sec)) * 100))
    if cs == 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _colorize(word: str, display: str, key_words: set[str], style: dict) -> str:
    """Wrap numbers / key words in ASS color tags."""
    core = word.strip(".,!?\"'").strip()
    reset = "{\\c&H00FFFFFF&}"
    if re.search(r"\d", core):
        return f"{style['num_color']}{display}{reset}"
    if core.lower() in key_words:
        return f"{style['key_color']}{display}{reset}"
    return display


def write_ass(words: list[dict], out_ass: str | Path, *,
              style: dict | None = None,
              key_words: list[str] | set[str] | None = None,
              upper: bool = False, gapless: bool = True) -> Path:
    """Write word-pop ASS; gapless sets each cue end=next.start."""
    st = dict(DEFAULT_STYLE)
    st.update(style or {})
    # Re-resolve at call time (not import time) so CAPTION_POS_Y env tuning
    # applies to a long-lived process. A caller-supplied pop_in wins.
    st["pop_in"] = (style or {}).get("pop_in") or pop_tag()
    keys = {str(k).lower() for k in (key_words or [])}
    groups = group_words([dict(w) for w in (words or [])])

    events: list[str] = []
    for gi, g in enumerate(groups):
        start = float(g[0].get("start", 0.0))
        if gapless and gi + 1 < len(groups):
            end = float(groups[gi + 1][0].get("start", g[-1].get("end", start)))
        else:
            end = float(g[-1].get("end", start + 0.4))
        if end <= start:
            end = start + 0.2
        parts = []
        for item in g:
            w = str(item.get("word", ""))
            disp = w.upper() if upper else w
            disp = disp.replace("{", "(").replace("}", ")")
            parts.append(_colorize(w, disp, keys, st))
        text = " ".join(parts)
        events.append(
            f"Dialogue: 0,{ass_ts(start)},{ass_ts(end)},Pop,,0,0,0,,"
            f"{st['pop_in']}{text}")

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {st['play_res_x']}\n"
        f"PlayResY: {st['play_res_y']}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Pop,{st['fontname']},{st['fontsize']},{st['primary_colour']},"
        f"&H000019FF,{st['outline_colour']},{st['back_colour']},{st['bold']},"
        f"0,0,0,100,100,0,0,1,{st['outline']},{st['shadow']},"
        f"{st['alignment']},10,10,{st['margin_v']},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    out = Path(out_ass)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Exact-spec word-pop writer (story niche burn-in).
# ---------------------------------------------------------------------------

WORDPOP_FONTNAME = "Montserrat ExtraBold"
# Caption size nudged 158 -> 145 (read slightly big); the story niche
# overrides it with cfg.ass_fontsize (currently 135), so this stays the
# fallback for callers that pass no size. Group width scales
# with size via _max_chars_for(), so 60px side margins stay in the safe
# area; outline 9 / shadow 3 keep the stroke proportionate.
WORDPOP_FONTSIZE = 145
# Import-time snapshot of pop_tag() (env as of import). Kept for callers that
# reference the constant; the writers themselves call pop_tag() at write time.
WORDPOP_POP = pop_tag()
WORDPOP_YELLOW = r"{\c&H0000FFFF&}"
WORDPOP_RESET = r"{\r}"

_WORDPOP_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "PlayResX: 1080\n"
    "PlayResY: 1920\n"
    "WrapStyle: 2\n"
    "ScaledBorderAndShadow: yes\n"
    "\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
    "Alignment, MarginL, MarginR, MarginV, Encoding\n"
    f"Style: Pop,{WORDPOP_FONTNAME},{WORDPOP_FONTSIZE},&H00FFFFFF,"
    "&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,9,3,5,60,60,0,1\n"
    "\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
    "Effect, Text\n"
)

_WORDPOP_FONT_WARNED = False


def warn_if_caption_font(fontname: str = WORDPOP_FONTNAME) -> bool:
    """One-time check for the caption family in assets/fonts/.

    Returns True when found. Otherwise warns once (renderers substitute,
    typically Arial Black/Impact) — the ASS header stays spec-exact.
    """
    global _WORDPOP_FONT_WARNED
    if _WORDPOP_FONT_WARNED:
        return False
    _WORDPOP_FONT_WARNED = True
    try:
        from PIL import ImageFont

        want = fontname.strip().lower().split()
        for path in sorted(Path("assets/fonts").glob("*")):
            if path.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            try:
                fam, style = ImageFont.truetype(str(path), 40).getname()
            except Exception:
                continue
            full = f"{fam} {style}".lower()
            if all(tok in full for tok in want):
                return True
    except Exception:
        pass
    warn(f"Caption font '{fontname}' not in assets/fonts/; renderer will "
         f"substitute (Arial Black/Impact typical).")
    return False


def wordpop_ts(sec: float) -> str:
    """Seconds -> ASS timestamp H:MM:SS.cc (3.2 -> 0:00:03.20)."""
    total = max(0, int(round(float(sec) * 100)))
    h, rem = divmod(total, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _wordpop_escape(text: str) -> str:
    """Escape for ASS dialogue text: backslashes, braces, newlines."""
    return (text.replace("\\", "\\\\").replace("{", "\\{")
                .replace("}", "\\}").replace("\r\n", "\\N")
                .replace("\r", "\\N").replace("\n", "\\N"))


def _wordpop_short(word: str, short_len: int = 4) -> bool:
    """Short word (punctuation stripped) for grouping purposes."""
    return len(word.strip(".,!?\"'").strip()) <= short_len


def _wordpop_groups(words: list[dict], short_len: int = 4,
                    max_group: int = 3, max_chars: int = 14) -> list[list[dict]]:
    """Group short words (1-3 per group, joined display ~14 chars max).

    Long words always stand alone; a lone short word pairs forward
    (legacy parity); the char cap is best-effort within short runs.
    """
    groups: list[list[dict]] = []
    words = [dict(w) for w in (words or [])]
    i = 0
    while i < len(words):
        w = str(words[i].get("word", ""))
        if _wordpop_short(w, short_len) and i + 1 < len(words):
            cur, total, j = [words[i]], len(w), i + 1
            while (j < len(words) and len(cur) < max_group
                   and _wordpop_short(str(words[j].get("word", "")),
                                      short_len)
                   and total + 1 + len(str(words[j].get("word", "")))
                   <= max_chars):
                total += 1 + len(str(words[j].get("word", "")))
                cur.append(words[j])
                j += 1
            if len(cur) == 1:
                cur.append(words[i + 1])
                j = i + 2
            groups.append(cur)
            i = j
        else:
            groups.append([words[i]])
            i += 1
    return groups


def _wordpop_core(word: str) -> str:
    """Lowercased core for highlight matching (punctuation stripped)."""
    return word.strip(".,!?\"'").strip().lower()


def _wordpop_needs_yellow(word: str, key_words: set[str]) -> bool:
    """Yellow on numbers, ALL-CAPS, or key words (case-insensitive)."""
    core = word.strip(".,!?\"'").strip()
    if not core:
        return False
    if re.search(r"\d", core):
        return True
    if any(c.isalpha() for c in core) and core.upper() == core:
        return True
    return core.lower() in key_words


def _wordpop_colorize(word: str, display: str, key_words: set[str]) -> str:
    """Wrap highlighted words in yellow + reset tag."""
    if _wordpop_needs_yellow(word, key_words):
        return f"{WORDPOP_YELLOW}{display}{WORDPOP_RESET}"
    return display


def _wordpop_events(words: list[dict], *, key_words: set[str],
                    upper: bool, start_after: float,
                    max_chars: int = 14) -> list[tuple]:
    """Filter/shift to start_after, group, then cap each event.

    Each event holds at most 0.15s past its word end (gaps stay gaps
    instead of stretching the prior caption across them); the last
    event ends at word end + 0.15.
    """
    shifted: list[dict] = []
    for w in words or []:
        try:
            s, e = float(w.get("start", 0.0)), float(w.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if e <= start_after:
            continue  # drop: fully hidden behind the card
        if s < start_after:
            s = start_after  # shift: starts under the card
        if e <= s:
            e = s + 0.2
        shifted.append({"word": str(w.get("word", "")), "start": s,
                        "end": e})
    events: list[tuple] = []
    groups = _wordpop_groups(shifted, max_chars=max_chars)
    for gi, g in enumerate(groups):
        start = float(g[0].get("start", 0.0))
        word_end = float(g[-1].get("end", start + 0.4))
        cap = word_end + 0.15
        if gi + 1 < len(groups):
            end = float(groups[gi + 1][0].get("start", start))
            if end > cap:
                end = cap  # hold at most 0.15s; gaps stay visible
            if end <= start:
                end = start + 0.2  # never overlap
        else:
            end = cap
        parts = []
        for item in g:
            w = str(item.get("word", ""))
            disp = _wordpop_escape(w.upper() if upper else w)
            parts.append(_wordpop_colorize(w, disp, key_words))
        events.append((start, end, " ".join(parts)))
    return events


def _stage_bundled_font(out_ass: str | Path, fontname: str) -> None:
    """Copy the bundled .ttf beside the ASS so libass sees it.

    ass_cwd_args passes fontsdir=<ass folder> to the burn; a family the
    renderer only knows from assets/fonts/ would otherwise be invisible
    to ffmpeg. No-op when absent (renderer substitutes + warns).
    """
    try:
        src = find_bundled_font(*fontname.split())
    except Exception:
        return
    if src is None:
        return
    try:
        dest = Path(out_ass).parent / src.name
        if not dest.exists():
            import shutil

            shutil.copyfile(src, dest)
    except Exception:
        pass


def write_wordpop_ass(words: list[dict], out_ass: str | Path, *,
                      key_words: list[str] | set[str] | None = None,
                      upper: bool = False, start_after: float = 0.0,
                      fontname: str = WORDPOP_FONTNAME,
                      fontsize: int | None = None) -> Path:
    """Exact-spec center word-pop ASS (story burn-in).

    Byte-exact header (spec style block); every Dialogue carries the pop
    override prefix. Captions start AFTER start_after (pass card_end).
    fontsize overrides WORDPOP_FONTSIZE (story cfg.ass_fontsize wiring);
    None keeps the spec default. Burn stays cwd-relative via ass_cwd_args
    (no force_style) — see niches/story/visuals.py Stage 6.
    """
    _ = warn_if_caption_font(fontname)
    out = Path(out_ass)
    out.parent.mkdir(parents=True, exist_ok=True)
    _stage_bundled_font(out, fontname)
    keys = {_wordpop_core(str(k)) for k in (key_words or []) if str(k).strip()}
    # Explicit arg > env > built-in default.
    size = int(fontsize) if fontsize else _env_fontsize()
    header = _WORDPOP_HEADER.replace(
        f"Style: Pop,{WORDPOP_FONTNAME},{WORDPOP_FONTSIZE},",
        f"Style: Pop,{fontname},{size},", 1)
    events = _wordpop_events(words, key_words=keys, upper=upper,
                             start_after=max(0.0, float(start_after)),
                             max_chars=_max_chars_for(size))
    lines = [f"Dialogue: 0,{wordpop_ts(s)},{wordpop_ts(e)},Pop,,0,0,0,,"
             f"{pop_tag()}{t}" for s, e, t in events]
    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return out
