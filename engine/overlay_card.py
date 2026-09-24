"""Themeable Pillow overlay card: avatar, handle, highlight text, icon row.

No fake numbers anywhere — the icon row is drawn shapes only.
All branding (handle, avatar, colors) arrives via params/theme.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

DEFAULT_BOLD_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
DEFAULT_REG_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
)

DEFAULT_THEME = {
    "card_fill": (242, 242, 245, 255),
    "radius": 36,
    "handle_color": (20, 20, 22, 255),
    "text_color": (18, 18, 20, 255),
    "highlight_color": (210, 30, 40, 255),
    "badge_color": (29, 155, 240, 255),
    "icon_color": (150, 150, 155, 255),
    "avatar_bg": (30, 30, 34, 255),
    "avatar_fallback_text": "MY",
    "bold_fonts": DEFAULT_BOLD_FONTS,
    "reg_fonts": DEFAULT_REG_FONTS,
    "max_body_lines": 3,
}


def _norm_family(name: str) -> str:
    """Lowercase alphanumeric fold for family-name matching."""
    return "".join(c for c in (name or "").lower() if c.isalnum())


def find_bundled_font(*needles: str, font_dir: str | Path = "assets/fonts",
                      _opener=None) -> Path | None:
    """First .ttf/.otf in font_dir whose INTERNAL family name matches.

    Matches when every needle appears in the getname() "family style"
    string (case/punctuation tolerant). Filename alone never matches —
    a file named Montserrat-ExtraBold.ttf with another internal family
    is skipped. None when nothing matches.
    """
    open_font = _opener or ImageFont.truetype
    try:
        files = sorted(Path(font_dir).glob("*"))
    except Exception:
        return None
    wants = [_norm_family(n) for n in needles if str(n).strip()]
    if not wants:
        return None
    for path in files:
        if path.suffix.lower() not in (".ttf", ".otf", ".ttc"):
            continue
        try:
            fam, style = open_font(str(path), 40).getname()
        except Exception:
            continue
        full = _norm_family(f"{fam} {style}")
        if all(w and w in full for w in wants):
            return path
    return None


def load_font(size: int, bold: bool = True, theme: dict | None = None):
    """Bundled internal-name match (theme font_family) first, then theme
    candidates, else Pillow default."""
    th = theme or DEFAULT_THEME
    fam = (th.get("font_family") or "").strip()
    if fam:
        hit = find_bundled_font(*fam.split())
        if hit is not None:
            try:
                return ImageFont.truetype(str(hit), size)
            except Exception:
                pass
    cands = th.get("bold_fonts" if bold else "reg_fonts",
                   DEFAULT_BOLD_FONTS if bold else DEFAULT_REG_FONTS)
    for p in cands or ():
        try:
            if p and Path(p).exists():
                return ImageFont.truetype(str(p), size)
        except Exception:
            continue
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, s: str, font) -> int:
    """Text width in px (textlength with bbox fallback)."""
    try:
        return int(draw.textlength(s, font=font))
    except Exception:
        box = draw.textbbox((0, 0), s, font=font)
        return int(box[2]) - int(box[0])


def circular_avatar(avatar_path: str | Path | None, size: int,
                    theme: dict | None = None) -> Image.Image:
    """Circular avatar crop; drawn-initials circle when the file is missing."""
    th = dict(DEFAULT_THEME)
    th.update(theme or {})
    circ = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
    try:
        p = Path(avatar_path) if avatar_path else None
        if p is None or not p.exists():
            raise FileNotFoundError(avatar_path)
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        im = Image.open(p).convert("RGBA").resize((size, size), resample)
        circ.paste(im, (0, 0), mask)
        return circ
    except Exception:
        d = ImageDraw.Draw(circ)
        d.ellipse([0, 0, size, size], fill=th["avatar_bg"])
        f = load_font(max(12, size // 3), bold=True, theme=th)
        t = th["avatar_fallback_text"]
        box = d.textbbox((0, 0), t, font=f)
        d.text(((size - (box[2] - box[0])) / 2, (size - (box[3] - box[1])) / 2 - 2),
               t, font=f, fill=(255, 255, 255, 255))
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(circ, (0, 0), mask)
        return out


def draw_verified_badge(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int,
                        color: tuple = (29, 155, 240, 255)) -> None:
    """Filled circle + white check (drawn, no emoji)."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    w = max(2, r // 3)
    draw.line([(cx - r * 0.45, cy), (cx - r * 0.1, cy + r * 0.4)], fill="white", width=w)
    draw.line([(cx - r * 0.1, cy + r * 0.4), (cx + r * 0.5, cy - r * 0.4)],
              fill="white", width=w)


def draw_icon_row(draw: ImageDraw.ImageDraw, x0: int, y: int,
                  color: tuple = (150, 150, 155, 255)) -> None:
    """Subtle like/share/comment shapes only (no counts, ever)."""
    hx = x0
    draw.ellipse([hx, y, hx + 14, y + 14], outline=color, width=2)
    draw.ellipse([hx + 14, y, hx + 28, y + 14], outline=color, width=2)
    draw.polygon([(hx + 2, y + 8), (hx + 26, y + 8), (hx + 14, y + 26)], outline=color)
    sx = hx + 56
    for nx, ny in ((sx, y + 4), (sx + 22, y + 12), (sx, y + 20)):
        draw.ellipse([nx - 4, ny - 4, nx + 4, ny + 4], outline=color, width=2)
    draw.line([(sx, y + 4), (sx + 22, y + 12)], fill=color, width=2)
    draw.line([(sx, y + 20), (sx + 22, y + 12)], fill=color, width=2)
    bx = sx + 52
    draw.rounded_rectangle([bx, y, bx + 30, y + 20], radius=6, outline=color, width=2)
    draw.polygon([(bx + 8, y + 20), (bx + 8, y + 27), (bx + 16, y + 20)], outline=color)


def render_card(text: str, highlight_word: str, *, handle: str,
                avatar_path: str | Path | None, out_png: str | Path,
                theme: dict | None = None,
                width: int = 1080, height: int = 400) -> Path:
    """Render a transparent story card PNG (caller positions it via FFmpeg)."""
    th = dict(DEFAULT_THEME)
    th.update(theme or {})
    out = Path(out_png)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 28
    d.rounded_rectangle([4, 4, width - 4, height - 4],
                        radius=th["radius"], fill=th["card_fill"])

    av_size = 84
    av_x, av_y = pad + 8, pad + 6
    avatar = circular_avatar(avatar_path, av_size, theme=th)
    img.paste(avatar, (av_x, av_y), avatar)

    hx = av_x + av_size + 18
    handle_font = load_font(34, bold=True, theme=th)
    d.text((hx, av_y + 6), handle, font=handle_font, fill=th["handle_color"])
    bx = hx + text_width(d, handle, handle_font) + 14
    draw_verified_badge(d, bx + 12, av_y + 6 + 20, 13, color=th["badge_color"])

    words = (text or "").strip().split()
    hl = (highlight_word or "").strip().lower()
    max_w = width - hx - pad - 8
    body_size = 40
    lines: list[list[tuple[str, tuple]]] = []
    body_font = load_font(body_size, bold=True, theme=th)
    space_w = text_width(d, " ", body_font)
    max_lines = int(th.get("max_body_lines", 3))
    while body_size >= 24:
        body_font = load_font(body_size, bold=True, theme=th)
        space_w = text_width(d, " ", body_font)
        lines, cur, cur_w = [], [], 0
        for w in words:
            color = th["highlight_color"] if hl and w.strip(".,!?\"'").lower() == hl else th["text_color"]
            ww = text_width(d, w, body_font)
            if cur and cur_w + space_w + ww > max_w:
                lines.append(cur)
                cur, cur_w = [], 0
            cur.append((w, color))
            cur_w += (space_w if cur_w else 0) + ww
        if cur:
            lines.append(cur)
        if len(lines) <= max_lines:
            break
        body_size -= 4
    y = av_y + av_size + 18
    lh = body_size + 10
    for line in lines[:max_lines]:
        x = hx
        for w, color in line:
            d.text((x, y), w, font=body_font, fill=color)
            x += text_width(d, w, body_font) + space_w
        y += lh

    draw_icon_row(d, hx, height - pad - 28, color=th["icon_color"])
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out


# ---------------------------------------------------------------------------
# Spec-exact opening card (story niche): ~900px RGBA, radius 48, #E6E6E6.
# ---------------------------------------------------------------------------

SPEC_CARD_THEME = {
    "card_fill": (230, 230, 230, 255),  # #E6E6E6
    "font_family": "Montserrat ExtraBold",
    "radius": 48,
    "avatar_size": 120,
    "avatar_bg": (30, 30, 34, 255),
    "handle_color": (20, 20, 22, 255),
    "handle_size": 44,
    "badge_color": (29, 155, 240, 255),
    "badge_radius": 14,
    "text_color": (18, 18, 20, 255),
    "highlight_color": (224, 0, 0, 255),  # #E00000
    "body_size": 64,
    "body_min_size": 36,
    "body_max_lines": 3,
    "icon_color": (150, 150, 155, 255),
    "pad": 36,
    "bold_fonts": DEFAULT_BOLD_FONTS,
    "reg_fonts": DEFAULT_REG_FONTS,
}


def _spec_handle_letter(handle: str) -> str:
    """First alphanumeric of the handle, uppercased (avatar fallback)."""
    for c in (handle or ""):
        if c.isalnum():
            return c.upper()
    return "S"


def _spec_glyph_width(font, word: str) -> int:
    """Word width via textbbox measurement (font.getbbox, no Draw needed)."""
    try:
        box = font.getbbox(word)
        return max(0, int(box[2]) - int(box[0]))
    except Exception:
        return len(word) * int(getattr(font, "size", 20)) // 2


def _spec_wrap_text(text: str, highlight_word: str, max_w: int,
                    theme: dict) -> tuple[list, Any, int, int]:
    """Wrap body to width with font shrink on overflow. Returns
    (lines, font, size, line_h); lines hold (word, color) pairs."""
    words = (text or "").strip().split()
    hl = (highlight_word or "").strip().lower()
    size = int(theme.get("body_size", 64))
    min_size = int(theme.get("body_min_size", 36))
    max_lines = int(theme.get("body_max_lines", 3))
    lines: list = []
    font = load_font(size, bold=True, theme=theme)
    while True:
        font = load_font(size, bold=True, theme=theme)
        space_w = _spec_glyph_width(font, " ")
        lines, cur, cur_w = [], [], 0
        for w in words:
            color = (theme["highlight_color"]
                     if hl and w.strip(".,!?\"'").lower() == hl
                     else theme["text_color"])
            ww = _spec_glyph_width(font, w)
            if cur and cur_w + space_w + ww > max_w:
                lines.append(cur)
                cur, cur_w = [], 0
            cur.append((w, color))
            cur_w += (space_w if cur_w else 0) + ww
        if cur:
            lines.append(cur)
        if len(lines) <= max_lines or size <= min_size:
            break
        size -= 4
    return lines, font, size, size + 12


def _draw_spec_heart(draw: ImageDraw.ImageDraw, x: int, y: int,
                     color: tuple, r: int = 16) -> None:
    """Filled heart (~4r wide): two lobe circles with a top-center dip,
    tapering to a bottom point. Grey, no numbers.

    The old thin-outline version read as two spectacles at card scale;
    filled lobes + a real taper read as a heart at ~60-80px.
    """
    draw.ellipse([x, y, x + 2 * r, y + 2 * r], fill=color)
    draw.ellipse([x + 2 * r, y, x + 4 * r, y + 2 * r], fill=color)
    draw.polygon([(x + 2, y + r), (x + 4 * r - 2, y + r),
                  (x + 2 * r, y + 4 * r)], fill=color)


def _draw_spec_share(draw: ImageDraw.ImageDraw, x: int, y: int,
                     color: tuple) -> None:
    """Pillow-shape share icon: three nodes + two lines, grey."""
    for nx, ny in ((x, y + 6), (x + 26, y + 16), (x, y + 26)):
        draw.ellipse([nx - 5, ny - 5, nx + 5, ny + 5], outline=color, width=3)
    draw.line([(x, y + 6), (x + 26, y + 16)], fill=color, width=3)
    draw.line([(x, y + 26), (x + 26, y + 16)], fill=color, width=3)


def render_spec_card(text: str, highlight_word: str, *, handle: str,
                     avatar_path: str | Path | None, out_png: str | Path,
                     theme: dict | None = None,
                     width: int = 900) -> Path:
    """Spec-exact opening card: transparent RGBA ~900px wide, radius 48.

    Top row: ~120px circular avatar (ellipse mask; solid circle + handle
    first letter when the file is missing), bold handle + small blue
    circle with a line-drawn white check (no emoji). Body: bold ~64px
    wrapped word-by-word (highlight in #E00000 red, case-insensitive),
    shrunk on overflow via textbbox measurement. Bottom row: grey heart
    + share shapes, no numbers. Height is dynamic but canvas-bounded.
    """
    th = dict(SPEC_CARD_THEME)
    th.update(theme or {})
    pad = int(th.get("pad", 36))
    av_size = int(th.get("avatar_size", 120))
    hx = pad + av_size + 24
    max_w = width - hx - pad  # body starts right of the avatar
    lines, body_font, _size, lh = _spec_wrap_text(
        text, highlight_word, max_w, th)
    top_h = pad + av_size + 28
    body_h = len(lines) * lh
    height = top_h + body_h + 28 + 44 + pad
    out = Path(out_png)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, width - 6, height - 6],
                        radius=int(th.get("radius", 48)),
                        fill=th["card_fill"])

    letter = _spec_handle_letter(handle)
    avatar = circular_avatar(
        avatar_path, av_size,
        theme={**th, "avatar_fallback_text": letter})
    img.paste(avatar, (pad, pad), avatar)

    handle_font = load_font(int(th.get("handle_size", 44)), bold=True,
                            theme=th)
    handle_y = pad + 8
    d.text((hx, handle_y), handle, font=handle_font,
           fill=th["handle_color"])
    badge_cx = hx + text_width(d, handle, handle_font) + 26
    draw_verified_badge(d, badge_cx, handle_y + 24,
                        int(th.get("badge_radius", 14)),
                        color=th["badge_color"])

    space_w = _spec_glyph_width(body_font, " ")
    y = top_h
    for line in lines:
        x = hx
        for w, color in line:
            d.text((x, y), w, font=body_font, fill=color)
            x += _spec_glyph_width(body_font, w) + space_w
        y += lh

    iy = top_h + body_h + 28
    _draw_spec_heart(d, hx, iy, color=th["icon_color"])
    _draw_spec_share(d, hx + 110, iy + 4, color=th["icon_color"])
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out
