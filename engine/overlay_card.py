"""Themeable Pillow overlay card: avatar, handle, highlight text, engagement row.

Renders viral social-proof Reddit/KoalaReads-style cards:
- Header: circular avatar, bold handle, verified check badge, and mini awards strip
- Body: full-width left-aligned text with dynamic wrap and red highlight word
- Bottom: sleek engagement row with outline heart (likes) + share tray (shares)
All branding (handle, avatar, colors, counts) arrives via params/theme/config.
"""
from __future__ import annotations

import math
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
    "card_fill": (255, 255, 255, 255),
    "card_border": (225, 228, 232, 255),
    "radius": 40,
    "handle_color": (20, 23, 26, 255),
    "text_color": (18, 18, 20, 255),
    "highlight_color": (224, 0, 0, 255),
    "badge_color": (29, 155, 240, 255),
    "icon_color": (120, 125, 132, 255),
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
            name = open_font(str(path), 40).getname()
        except Exception:
            continue
        try:
            fam = str(name[0])
            style = " ".join(str(p) for p in name[1:])
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
    """Filled circle + crisp white checkmark (drawn, no emoji)."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    w = max(2, r // 3)
    draw.line([(cx - r * 0.45, cy), (cx - r * 0.1, cy + r * 0.4)], fill="white", width=w)
    draw.line([(cx - r * 0.1, cy + r * 0.4), (cx + r * 0.5, cy - r * 0.4)],
              fill="white", width=w)


def draw_icon_row(draw: ImageDraw.ImageDraw, x0: int, y: int,
                  heart_color: tuple = (235, 32, 60, 255),
                  share_color: tuple = (29, 155, 240, 255),
                  upvote_color: tuple = (255, 69, 0, 255),
                  comment_color: tuple = (16, 185, 129, 255),
                  bookmark_color: tuple = (245, 158, 11, 255)) -> None:
    """Legacy satire icon row."""
    hx = x0
    _draw_spec_upvote(draw, hx, y, color=upvote_color, size=22)
    sx = hx + 36
    _draw_spec_heart(draw, sx, y, color=heart_color, r=6)
    cx = sx + 36
    _draw_spec_comment(draw, cx, y, color=comment_color, size=20)
    shx = cx + 36
    _draw_spec_share(draw, shx, y, color=share_color, size=20)
    bx = shx + 36
    _draw_spec_bookmark(draw, bx, y, color=bookmark_color, size=20)


def render_card(text: str, highlight_word: str, *, handle: str,
                avatar_path: str | Path | None, out_png: str | Path,
                theme: dict | None = None,
                width: int = 1080, height: int = 400) -> Path:
    """Legacy card renderer (caller positions via FFmpeg)."""
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
    handle_size = 34
    handle_font = load_font(handle_size, bold=True, theme=th)
    handle_y = av_y + (av_size - handle_size) // 2
    d.text((hx, handle_y), handle, font=handle_font, fill=th["handle_color"])
    bx = hx + text_width(d, handle, handle_font) + 14
    draw_verified_badge(d, bx + 12, handle_y + handle_size // 2 + 2, 13,
                        color=th["badge_color"])

    words = (text or "").strip().split()
    hl = (highlight_word or "").strip().strip(".,!?\"'").lower()
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

    draw_icon_row(
        d, hx, height - pad - 28,
        heart_color=th.get("heart_color", (235, 32, 60, 255)),
        share_color=th.get("share_color", (29, 155, 240, 255)),
        upvote_color=th.get("upvote_color", (255, 69, 0, 255)),
        comment_color=th.get("comment_color", (16, 185, 129, 255)),
        bookmark_color=th.get("bookmark_color", (245, 158, 11, 255)),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out


# ---------------------------------------------------------------------------
# Spec-exact opening card (story niche): ~900px RGBA, pure white #FFFFFF.
# ---------------------------------------------------------------------------

SPEC_CARD_THEME = {
    "card_fill": (255, 255, 255, 255),     # Clean, modern pure white
    "card_border": (225, 228, 232, 255),   # Subtle border edge
    "font_family": "Montserrat ExtraBold",
    "radius": 40,
    "avatar_size": 96,
    "avatar_bg": (30, 30, 34, 255),
    "handle_color": (20, 23, 26, 255),
    "handle_size": 38,
    "badge_color": (29, 155, 240, 255),
    "badge_radius": 13,
    "text_color": (18, 18, 20, 255),
    "highlight_color": (224, 0, 0, 255),   # Vibrant red #E00000
    "body_size": 54,
    "body_min_size": 32,
    "body_max_lines": 3,
    "icon_color": (120, 125, 132, 255),
    "heart_color": (235, 32, 60, 255),     # Vibrant Red
    "heart_filled": False,
    "share_color": (120, 125, 132, 255),
    "upvote_color": (255, 69, 0, 255),
    "comment_color": (16, 185, 129, 255),
    "bookmark_color": (245, 158, 11, 255),
    "likes_text": "720+",
    "shares_text": "413+",
    "engagement_color": (120, 125, 132, 255),
    "engagement_size": 32,
    "show_awards": True,
    "show_reactions": True,
    "pad": 36,
    "heart_icon_path": "assets/icons/heart.png",
    "share_icon_path": "assets/icons/share.png",
    "upvote_icon_path": "assets/icons/upvote.png",
    "comment_icon_path": "assets/icons/comment.png",
    "bookmark_icon_path": "assets/icons/bookmark.png",
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
    (lines, font, size, line_h); lines hold (word, color) pairs.
    """
    words = (text or "").strip().split()
    hl = (highlight_word or "").strip().strip(".,!?\"'").lower()
    size = int(theme.get("body_size", 54))
    min_size = int(theme.get("body_min_size", 32))
    max_lines = int(theme.get("body_max_lines", 3))
    lines: list = []
    font = load_font(size, bold=True, theme=theme)
    while True:
        font = load_font(size, bold=True, theme=theme)
        space_w = _spec_glyph_width(font, " ")
        lines, cur, cur_w = [], [], 0
        for w in words:
            clean_w = w.strip(".,!?\"'").lower()
            color = (theme["highlight_color"]
                     if hl and clean_w == hl
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
        size = max(min_size, size - 4)
    return lines, font, size, size + 14


def _bezier_point(p0: tuple[float, float], p1: tuple[float, float],
                  p2: tuple[float, float], p3: tuple[float, float],
                  t: float) -> tuple[float, float]:
    mt = 1.0 - t
    return (
        mt * mt * mt * p0[0] + 3.0 * mt * mt * t * p1[0] + 3.0 * mt * t * t * p2[0] + t * t * t * p3[0],
        mt * mt * mt * p0[1] + 3.0 * mt * mt * t * p1[1] + 3.0 * mt * t * t * p2[1] + t * t * t * p3[1],
    )


def _heart_points(cx: float, cy: float, width: float, height: float,
                  num_points: int = 120) -> list[tuple[float, float]]:
    top_cleft = (cx, cy - height * 0.22)
    bottom_pt = (cx, cy + height * 0.50)
    p0 = bottom_pt
    p1 = (cx - width * 0.58, cy + height * 0.12)
    p2 = (cx - width * 0.58, cy - height * 0.48)
    p3 = (cx - width * 0.28, cy - height * 0.50)
    p4 = p3
    p5 = (cx - width * 0.08, cy - height * 0.50)
    p6 = (cx - width * 0.02, cy - height * 0.26)
    p7 = top_cleft

    half = max(10, num_points // 2)
    pts = []
    for i in range(half):
        t = i / half
        pts.append(_bezier_point(p0, p1, p2, p3, t))
    for i in range(half):
        t = i / half
        pts.append(_bezier_point(p4, p5, p6, p7, t))

    right_pts = [(2.0 * cx - px, py) for px, py in reversed(pts)]
    return pts + right_pts


def _draw_spec_heart(draw: ImageDraw.ImageDraw, x: int, y: int,
                     color: tuple, r: int = 16) -> None:
    """Smooth vector heart (~4r wide, ~4r high), grey/color."""
    w = 4 * r
    h = int(w * 0.95)
    cx = x + w / 2.0
    cy = y + h / 2.0
    pts = _heart_points(cx, cy, float(w), float(h), num_points=120)
    draw.polygon(pts, fill=color)


def _draw_heart_outline(draw: ImageDraw.ImageDraw, cx: float, cy: float,
                        size: int = 30, color: tuple = (120, 125, 132, 255),
                        stroke: int = 3) -> None:
    """Draw a smooth, modern hollow heart centered at (cx, cy)."""
    pts = []
    scale = size / 34.0
    for i in range(100):
        t = i * 2.0 * math.pi / 100.0
        x = 16.0 * (math.sin(t) ** 3)
        y = -(13.0 * math.cos(t) - 5.0 * math.cos(2 * t) - 2.0 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((cx + x * scale, cy + (y + 2) * scale))
    pts.append(pts[0])
    draw.line(pts, fill=color, width=stroke, joint="curve")


def _draw_heart_filled(draw: ImageDraw.ImageDraw, cx: float, cy: float,
                       size: int = 30, color: tuple = (235, 32, 60, 255)) -> None:
    """Draw a smooth, modern filled heart centered at (cx, cy)."""
    pts = []
    scale = size / 34.0
    for i in range(100):
        t = i * 2.0 * math.pi / 100.0
        x = 16.0 * (math.sin(t) ** 3)
        y = -(13.0 * math.cos(t) - 5.0 * math.cos(2 * t) - 2.0 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((cx + x * scale, cy + (y + 2) * scale))
    draw.polygon(pts, fill=color)


def _draw_spec_heart_outline(draw: ImageDraw.ImageDraw, x: int, y: int,
                             color: tuple, r: int = 13,
                             width: int = 4) -> int:
    """Outline heart helper (compatible with legacy tests)."""
    cx = x + 2 * r
    cy = y + 2 * r
    _draw_heart_outline(draw, cx, cy, size=4 * r, color=color, stroke=width)
    return 4 * r


def _draw_share_tray(draw: ImageDraw.ImageDraw, x: int, y: int,
                     size: int = 30, color: tuple = (120, 125, 132, 255),
                     stroke: int = 3) -> None:
    """Draw a modern share/upload tray icon (open box with upward arrow)."""
    w = size * 0.88
    h = size
    tray_top = y + h * 0.45
    tray_bottom = y + h - stroke
    x_left = x + 1
    x_right = x + w - 1
    draw.line([(x_left, tray_top), (x_left, tray_bottom)], fill=color, width=stroke)
    draw.line([(x_left, tray_bottom), (x_right, tray_bottom)], fill=color, width=stroke)
    draw.line([(x_right, tray_bottom), (x_right, tray_top)], fill=color, width=stroke)
    cx = (x_left + x_right) / 2.0
    shaft_top = y + stroke
    shaft_bottom = tray_bottom - stroke * 2
    draw.line([(cx, shaft_bottom), (cx, shaft_top)], fill=color, width=stroke)
    head_w = w * 0.38
    head_h = h * 0.32
    draw.line([(cx - head_w, shaft_top + head_h), (cx, shaft_top), (cx + head_w, shaft_top + head_h)],
              fill=color, width=stroke, joint="curve")


def _draw_mini_awards(draw: ImageDraw.ImageDraw, x: int, y: int,
                      size: int = 24, spacing: int = 6) -> int:
    """Draw a strip of authentic-looking Reddit/social award badges."""
    cur_x = x
    r = size // 2

    # 1. Gold Heart Medal
    cx, cy = cur_x + r, y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(245, 185, 30, 255), outline=(215, 150, 15, 255), width=2)
    _draw_heart_outline(draw, cx, cy, size=14, color=(220, 20, 40, 255), stroke=2)
    draw.polygon([(cx - 4, cy - 2), (cx, cy + 4), (cx + 4, cy - 2)], fill=(220, 20, 40, 255))
    cur_x += size + spacing

    # 2. Emerald Gem Badge
    cx, cy = cur_x + r, y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(16, 185, 129, 255), outline=(5, 150, 105, 255), width=2)
    draw.polygon([(cx, cy - r + 4), (cx + r - 4, cy), (cx, cy + r - 4), (cx - r + 4, cy)], fill=(255, 255, 255, 230))
    cur_x += size + spacing

    # 3. Rocket Award (orange flame)
    cx, cy = cur_x + r, y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(60, 64, 75, 255))
    draw.polygon([(cx, cy - r + 3), (cx + 5, cy + 2), (cx - 5, cy + 2)], fill=(240, 240, 245, 255))
    draw.polygon([(cx - 3, cy + 2), (cx + 3, cy + 2), (cx, cy + r - 2)], fill=(255, 69, 0, 255))
    cur_x += size + spacing

    # 4. Platinum / Skull Badge
    cx, cy = cur_x + r, y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(185, 200, 215, 255), outline=(150, 170, 190, 255), width=2)
    draw.ellipse([cx - 4, cy - 3, cx + 4, cy + 3], fill=(70, 80, 95, 255))
    draw.rectangle([cx - 3, cy + 2, cx + 3, cy + 5], fill=(70, 80, 95, 255))
    cur_x += size + spacing

    # 5. Red Heart Award
    cx, cy = cur_x + r, y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 235, 238, 255), outline=(255, 180, 190, 255), width=1)
    _draw_heart_outline(draw, cx, cy, size=15, color=(235, 32, 60, 255), stroke=2)
    draw.polygon([(cx - 4, cy - 2), (cx, cy + 4), (cx + 4, cy - 2)], fill=(235, 32, 60, 255))
    cur_x += size + spacing

    # 6. Fire Award
    cx, cy = cur_x + r, y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 243, 224, 255), outline=(255, 170, 130, 255), width=1)
    draw.polygon([(cx, cy - r + 3), (cx + 5, cy), (cx + 3, cy + 4), (cx - 3, cy + 4), (cx - 5, cy)], fill=(245, 80, 40, 255))
    cur_x += size + spacing

    # 7. Snoo / Wholesome Seal
    cx, cy = cur_x + r, y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(240, 242, 245, 255), outline=(200, 205, 215, 255), width=2)
    draw.ellipse([cx - 5, cy - 2, cx - 2, cy + 1], fill=(50, 55, 65, 255))
    draw.ellipse([cx + 2, cy - 2, cx + 5, cy + 1], fill=(50, 55, 65, 255))
    draw.line([(cx - 3, cy + 3), (cx, cy + 5), (cx + 3, cy + 3)], fill=(50, 55, 65, 255), width=2)
    cur_x += size + spacing

    # 8. Gold Coin / Star
    cx, cy = cur_x + r, y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 205, 30, 255), outline=(220, 170, 10, 255), width=2)
    draw.polygon([(cx, cy - 5), (cx + 2, cy - 1), (cx + 6, cy - 1), (cx + 3, cy + 2), (cx + 4, cy + 6),
                  (cx, cy + 3), (cx - 4, cy + 6), (cx - 3, cy + 2), (cx - 6, cy - 1), (cx - 2, cy - 1)],
                 fill=(255, 255, 255, 255))
    cur_x += size + spacing

    # 9. Sapphire Diamond
    cx, cy = cur_x + r, y + r
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(29, 155, 240, 255), outline=(15, 125, 200, 255), width=2)
    draw.polygon([(cx, cy - 5), (cx + 5, cy), (cx, cy + 5), (cx - 5, cy)], fill=(255, 255, 255, 255))
    cur_x += size
    return cur_x - x


def _draw_reaction_strip(draw: ImageDraw.ImageDraw, x: int, y_center: float,
                         colors: tuple | list, size: int = 30) -> int:
    """Reaction strip helper (fallback / test compatibility)."""
    return _draw_mini_awards(draw, x, int(y_center) - size // 2, size=size)


def _draw_spec_share(draw: ImageDraw.ImageDraw, x: int, y: int,
                     color: tuple, size: int = 46) -> None:
    """Share icon helper (compatible with legacy tests)."""
    r = max(4.0, size * 0.16)
    w_line = max(3, int(size * 0.10))
    x_left = x + r
    y_mid = y + size / 2.0
    x_right = x + size - r
    y_top = y + r + 2.0
    y_bottom = y + size - r - 2.0
    draw.line([(x_left, y_mid), (x_right, y_top)], fill=color, width=w_line)
    draw.line([(x_left, y_mid), (x_right, y_bottom)], fill=color, width=w_line)
    for nx, ny in ((x_left, y_mid), (x_right, y_top), (x_right, y_bottom)):
        draw.ellipse([nx - r, ny - r, nx + r, ny + r], fill=color)


def _draw_spec_upvote(draw: ImageDraw.ImageDraw, x: int, y: int,
                      color: tuple, size: int = 44) -> None:
    """Reddit-style upvote arrow in vibrant orange."""
    cx = x + size / 2.0
    stem_w = size * 0.38
    head_w = size * 0.88
    head_base_y = y + size * 0.48
    stem_bottom_y = y + size
    pts = [
        (cx, y),
        (cx + head_w / 2.0, head_base_y),
        (cx + stem_w / 2.0, head_base_y),
        (cx + stem_w / 2.0, stem_bottom_y),
        (cx - stem_w / 2.0, stem_bottom_y),
        (cx - stem_w / 2.0, head_base_y),
        (cx - head_w / 2.0, head_base_y),
    ]
    draw.polygon(pts, fill=color)


def _draw_spec_comment(draw: ImageDraw.ImageDraw, x: int, y: int,
                       color: tuple, size: int = 42) -> None:
    """Satire social comment bubble."""
    w = size * 1.05
    h = size * 0.74
    draw.rounded_rectangle([x, y, x + w, y + h], radius=int(size * 0.28), fill=color)
    tail_pts = [
        (x + size * 0.22, y + h - 2),
        (x + size * 0.10, y + size * 0.98),
        (x + size * 0.48, y + h - 2),
    ]
    draw.polygon(tail_pts, fill=color)


def _draw_spec_bookmark(draw: ImageDraw.ImageDraw, x: int, y: int,
                        color: tuple, size: int = 40) -> None:
    """Satire bookmark/save ribbon."""
    w = size * 0.72
    h = size
    pts = [
        (x, y),
        (x + w, y),
        (x + w, y + h),
        (x + w / 2.0, y + h - size * 0.28),
        (x, y + h),
    ]
    draw.polygon(pts, fill=color)


def _draw_engagement_count(draw: ImageDraw.ImageDraw, x: int, y_center: float,
                           text: str, font, color: tuple) -> int:
    """Draw a count label (e.g. "420+") vertically centered; returns width."""
    label = str(text or "").strip() or "420+"
    label = "".join(c for c in label if 32 <= ord(c) < 127) or "420+"
    top = int(y_center - int(getattr(font, "size", 30)) / 2)
    draw.text((x, top), label, font=font, fill=color)
    try:
        return int(draw.textlength(label, font=font))
    except Exception:
        box = draw.textbbox((x, top), label, font=font)
        return int(box[2]) - int(box[0])


def _paste_custom_icon(img: Image.Image, icon_path: str | Path | None,
                       x: int, y: int, size: int) -> bool:
    """Attempt to load and paste a custom PNG/image icon onto img at (x, y)."""
    if not icon_path:
        return False
    try:
        p = Path(icon_path)
        if not p.is_file():
            return False
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        icon_img = Image.open(p).convert("RGBA").resize((size, size), resample)
        img.paste(icon_img, (x, y), icon_img)
        return True
    except Exception:
        return False


def render_spec_card(text: str, highlight_word: str, *, handle: str,
                     avatar_path: str | Path | None, out_png: str | Path,
                     theme: dict | None = None,
                     width: int = 900,
                     likes: str | None = None,
                     shares: str | None = None,
                     show_reactions: bool | None = None,
                     show_awards: bool | None = None) -> Path:
    """Spec-exact viral social card (KoalaReads style): transparent RGBA ~900px wide.

    Header:
    - Circular avatar at top-left
    - Bold handle + Twitter/Instagram verified blue badge
    - Colorful strip of authentic Reddit/social awards directly beneath handle

    Body:
    - Full-width left-aligned text (starts under the avatar header)
    - Dynamic font scaling for punchy 1-3 lines
    - Highlight word rendered in bold red (#E00000)

    Bottom:
    - Clean engagement row with neutral grey hollow heart + like count (e.g. 732+)
    - Modern share tray icon + share count (e.g. 420+)
    - Counts and visibility customizable via kwargs or theme
    """
    th = dict(SPEC_CARD_THEME)
    th.update(theme or {})
    likes_text = (str(likes) if likes is not None
                  else str(th.get("likes_text", "732+")))
    shares_text = (str(shares) if shares is not None
                   else str(th.get("shares_text", "420+")))
    awards = (show_awards if show_awards is not None
              else (show_reactions if show_reactions is not None
                    else bool(th.get("show_awards", th.get("show_reactions", True)))))

    pad = int(th.get("pad", 36))
    av_size = int(th.get("avatar_size", 96))
    hx = pad + av_size + 18

    # Body spans full width of card (starts from left margin under avatar)
    max_body_w = width - 2 * pad - 8
    lines, body_font, _size, lh = _spec_wrap_text(
        text, highlight_word, max_body_w, th)

    header_h = av_size + (24 if awards else 8)
    body_y = pad + header_h + 10
    body_h = len(lines) * lh
    eng_y = body_y + body_h + 18
    height = eng_y + 44 + pad

    out = Path(out_png)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 1. Card Background
    card_fill = th.get("card_fill", (255, 255, 255, 255))
    card_border = th.get("card_border", (225, 228, 232, 255))
    radius = int(th.get("radius", 40))
    d.rounded_rectangle([4, 4, width - 4, height - 4],
                        radius=radius, fill=card_fill,
                        outline=card_border, width=2)

    # 2. Circular Avatar
    letter = _spec_handle_letter(handle)
    avatar = circular_avatar(
        avatar_path, av_size,
        theme={**th, "avatar_fallback_text": letter})
    img.paste(avatar, (pad, pad), avatar)

    # 3. Handle & Verified Badge
    handle_h = int(th.get("handle_size", 38))
    handle_font = load_font(handle_h, bold=True, theme=th)
    handle_y = pad + (10 if awards else max(0, (av_size - handle_h) // 2))
    d.text((hx, handle_y), handle, font=handle_font, fill=th["handle_color"])
    badge_r = int(th.get("badge_radius", 13))
    badge_cx = hx + text_width(d, handle, handle_font) + 20
    badge_cy = handle_y + handle_h // 2 + 1
    draw_verified_badge(d, badge_cx, badge_cy, badge_r,
                        color=th.get("badge_color", (29, 155, 240, 255)))

    # 4. Mini Awards Strip (directly below handle)
    if awards:
        awards_y = handle_y + handle_h + 8
        _draw_mini_awards(d, hx, awards_y, size=24, spacing=6)

    # 5. Body Text (left-aligned under header)
    space_w = _spec_glyph_width(body_font, " ")
    y = body_y
    for line in lines:
        x = pad + 4
        for w, color in line:
            d.text((x, y), w, font=body_font, fill=color)
            x += _spec_glyph_width(body_font, w) + space_w
        y += lh

    # 6. Bottom Engagement Row (KoalaReads style)
    eng_font = load_font(int(th.get("engagement_size", 32)), bold=False, theme=th)
    eng_color = th.get("engagement_color", (120, 125, 132, 255))
    cur_x = pad + 6
    row_mid = eng_y + 16

    # Item 1: Outline / Filled Heart + likes count
    heart_col = th.get("heart_color", (235, 32, 60, 255))
    pasted_heart = _paste_custom_icon(img, th.get("heart_icon_path"), cur_x, row_mid - 15, 30)
    if not pasted_heart:
        if th.get("heart_filled", False):
            _draw_heart_filled(d, cur_x + 15, row_mid, size=30, color=heart_col)
        else:
            _draw_heart_outline(d, cur_x + 15, row_mid, size=30, color=heart_col, stroke=3)
    cur_x += 34 + 10
    cur_x += _draw_engagement_count(d, cur_x, row_mid, likes_text, eng_font, eng_color) + 40

    # Item 2: Share Tray + shares count
    pasted_share = _paste_custom_icon(img, th.get("share_icon_path"), cur_x, row_mid - 15, 30)
    if not pasted_share:
        _draw_share_tray(d, cur_x, row_mid - 15, size=30, color=eng_color, stroke=3)
    cur_x += 32 + 10
    cur_x += _draw_engagement_count(d, cur_x, row_mid, shares_text, eng_font, eng_color)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out
