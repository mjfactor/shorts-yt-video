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

from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
# Mobile-first opening card: transparent RGBA, themeable social-post layout.
# ---------------------------------------------------------------------------

SPEC_CARD_THEME = {
    "card_fill": (255, 255, 255, 255),     # Clean, modern pure white
    "card_border": (229, 232, 237, 255),
    "font_family": "Montserrat ExtraBold",
    "radius": 32,
    "avatar_size": 76,
    "avatar_bg": (30, 30, 34, 255),
    "handle_color": (20, 23, 26, 255),
    "handle_size": 32,
    "badge_color": (29, 155, 240, 255),
    "badge_radius": 11,
    "text_color": (18, 18, 20, 255),
    "highlight_color": (224, 0, 0, 255),   # Vibrant red #E00000
    "highlight_fill": (255, 237, 240, 255),
    "body_size": 62,
    "body_min_size": 32,
    "body_max_lines": 3,
    "icon_color": (120, 125, 132, 255),
    "heart_color": (235, 32, 60, 255),     # Vibrant Red
    "heart_filled": False,
    "share_color": (120, 125, 132, 255),
    "upvote_color": (255, 69, 0, 255),
    "comment_color": (16, 185, 129, 255),
    "bookmark_color": (245, 158, 11, 255),
    "likes_text": "930+",
    "shares_text": "930+",
    "engagement_color": (120, 125, 132, 255),
    "engagement_size": 24,
    "show_awards": True,
    "show_reactions": True,
    "pad": 32,
    "shadow_color": (16, 24, 40, 58),
    "shadow_blur": 9,
    "shadow_margin": 18,
    "divider_color": (233, 236, 240, 255),
    "control_fill": (245, 246, 248, 255),
    "label_text": "",
    "label_color": (103, 111, 123, 255),
    "label_fill": None,
    "header_fill": None,
    "header_gradient": None,
    "accent_colors": None,
    "highlight_gradient": None,
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
    """Reserve both the text advance and any glyph overhang when laying out."""
    try:
        box = font.getbbox(word)
        ink = max(0, int(box[2]) - int(box[0]))
        return max(ink, math.ceil(font.getlength(word)))
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
        widest_word = 0
        for w in words:
            clean_w = w.strip(".,!?\"'").lower()
            color = (theme["highlight_color"]
                     if hl and clean_w == hl
                     else theme["text_color"])
            ww = _spec_glyph_width(font, w)
            widest_word = max(widest_word, ww)
            if cur and cur_w + space_w + ww > max_w:
                lines.append(cur)
                cur, cur_w = [], 0
            cur.append((w, color))
            cur_w += (space_w if cur_w else 0) + ww
        if cur:
            lines.append(cur)
        if (len(lines) <= max_lines and widest_word <= max_w) or size <= min_size:
            break
        size = max(min_size, size - 2)

    def line_width(line):
        return (sum(_spec_glyph_width(font, word) for word, _ in line)
                + max(0, len(line) - 1) * space_w)

    # Avoid a packed first line followed by a lonely trailing word. Moving
    # words only across adjacent lines preserves the spoken hook verbatim.
    for i in range(len(lines) - 2, -1, -1):
        while len(lines[i]) > 1:
            left, right = lines[i][:-1], [lines[i][-1], *lines[i + 1]]
            old_gap = abs(line_width(lines[i]) - line_width(lines[i + 1]))
            new_gap = abs(line_width(left) - line_width(right))
            if line_width(right) > max_w or new_gap >= old_gap:
                break
            lines[i], lines[i + 1] = left, right
    return lines, font, size, size + 12


def _spec_fit_label(text: str, max_w: int, size: int, theme: dict):
    """Fit secondary text without letting a long handle overlap its badge."""
    min_size = min(size, 20)
    font = load_font(size, bold=True, theme=theme)
    while size > min_size and _spec_glyph_width(font, text) > max_w:
        size -= 1
        font = load_font(size, bold=True, theme=theme)
    if _spec_glyph_width(font, text) > max_w:
        while text and _spec_glyph_width(font, text + "…") > max_w:
            text = text[:-1]
        text = text.rstrip() + "…" if text else ""
    return text, font


def _spec_gradient(size: tuple[int, int], colors) -> Image.Image:
    """Horizontal two-color ramp for the frame and small accent surfaces."""
    ramp = Image.linear_gradient("L").rotate(90).resize(size)
    return Image.composite(Image.new("RGBA", size, colors[1]),
                           Image.new("RGBA", size, colors[0]), ramp)


def _spec_round_mask(size: tuple[int, int], box, radius: int) -> Image.Image:
    """Antialias curved edges without supersampling every text/asset draw."""
    mask = Image.new("L", (size[0] * 2, size[1] * 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        tuple(int(v * 2) for v in box), radius=radius * 2, fill=255)
    return mask.resize(size, Image.Resampling.LANCZOS)


def _spec_surface(width: int, height: int, bottom: int,
                  header_bottom: int, theme: dict) -> Image.Image:
    """Paint the optional accent frame and masthead inside one RGBA canvas."""
    size = (width, height)
    inset, radius = int(theme["shadow_margin"]), int(theme["radius"])
    shadow_color = theme["shadow_color"]
    shadow_mask = Image.new("L", size, 0)
    ImageDraw.Draw(shadow_mask).rounded_rectangle(
        (inset, inset + 7, width - inset, bottom + 7),
        radius=radius, fill=shadow_color[3])
    img = Image.new("RGBA", size, shadow_color[:3] + (0,))
    img.putalpha(shadow_mask.filter(ImageFilter.GaussianBlur(theme["shadow_blur"])))
    bounds = (inset, inset, width - inset, bottom)
    accents = theme.get("accent_colors")
    if accents:
        frame = _spec_gradient(size, accents)
        frame.putalpha(_spec_round_mask(size, bounds, radius))
        img.alpha_composite(frame)
        border = 3
    else:
        ImageDraw.Draw(img).rounded_rectangle(
            bounds, radius=radius, fill=theme["card_border"])
        border = 2
    inner = (inset + border, inset + border,
             width - inset - border, bottom - border)
    inner_mask = _spec_round_mask(size, inner, max(1, radius - border))
    face = Image.new("RGBA", size, theme["card_fill"])
    alpha = face.getchannel("A")
    face.putalpha(Image.composite(alpha, Image.new("L", size, 0), inner_mask))
    img.alpha_composite(face)
    if theme.get("header_fill"):
        header = (_spec_gradient(size, theme["header_gradient"])
                  if theme.get("header_gradient")
                  else Image.new("RGBA", size, theme["header_fill"]))
        header_mask = inner_mask.copy()
        ImageDraw.Draw(header_mask).rectangle(
            (0, header_bottom, width, height), fill=0)
        header.putalpha(header_mask)
        img.alpha_composite(header)
        if accents:
            rule = _spec_gradient((width - 2 * (inset + border), 3), accents)
            img.alpha_composite(rule, (inset + border, header_bottom - 3))
    return img


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
    """Draw a count label (e.g. "930+") vertically centered; returns width."""
    label = str(text or "").strip() or "930+"
    label = "".join(c for c in label if 32 <= ord(c) < 127) or "930+"
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
    """Render a mobile-readable social card with transparent shadow margins.

    The hook stays verbatim; only the handle may shrink or ellipsize.
    Counts remain caller-supplied decorative labels, not live engagement.
    ``show_reactions`` is the compatibility alias for ``show_awards``.
    """
    overrides = theme or {}
    th = {**SPEC_CARD_THEME, **overrides}
    likes_text = str(likes) if likes is not None else str(th["likes_text"])
    shares_text = str(shares) if shares is not None else str(th["shares_text"])
    if show_awards is not None:
        awards = show_awards
    elif show_reactions is not None:
        awards = show_reactions
    else:
        awards = bool(overrides.get("show_awards", overrides.get(
            "show_reactions", th["show_awards"])))

    inset = int(th["shadow_margin"])
    pad = min(int(th["pad"]), width // 12)
    left, right = inset + pad, width - inset - pad
    content_w = right - left
    top = inset + pad
    av_size = min(int(th["avatar_size"]), width // 5)
    hx = left + av_size + 16
    badge_r = int(th["badge_radius"])
    menu_w = 32 if width >= 500 else 0
    handle_text, handle_font = _spec_fit_label(
        str(handle), max(1, right - hx - menu_w - 2 * badge_r - 14),
        int(th["handle_size"]), th)
    label = str(th.get("label_text", "")).strip()
    secondary = bool(label or awards)
    handle_box = handle_font.getbbox(handle_text or "Ag")
    handle_h = handle_box[3] - handle_box[1]
    handle_y = top + (5 if secondary else (av_size - handle_h) // 2)

    lines, body_font, _size, lh = _spec_wrap_text(
        text, highlight_word, content_w - 8, th)
    header_h = max(av_size, handle_h + (38 if secondary else 0))
    header_bottom = top + header_h + 20
    body_y = (header_bottom + 30 if th.get("header_fill")
              else top + header_h + 28)
    body_box = body_font.getbbox("Ag", anchor="ls")
    body_h = max(0, len(lines) - 1) * lh + body_box[3] - body_box[1]
    divider_y = body_y + body_h + 26
    row_top, row_h = divider_y + 17, 46
    height = row_top + row_h + pad + inset + 7
    surface_bottom = height - inset - 7

    # All decoration stays inside the same PNG bounds for both consumers:
    # FFmpeg's opening overlay and the Pillow thumbnail composite.
    img = _spec_surface(width, height, surface_bottom, header_bottom, th)
    d = ImageDraw.Draw(img)
    if th.get("accent_colors"):
        ring_size = av_size + 10
        ring = _spec_gradient((ring_size, ring_size), th["accent_colors"])
        ring.putalpha(_spec_round_mask(
            ring.size, (0, 0, ring_size - 1, ring_size - 1), ring_size // 2))
        img.alpha_composite(ring, (left - 5, top - 5))
    avatar = circular_avatar(avatar_path, av_size, theme={
        **th, "avatar_fallback_text": _spec_handle_letter(handle)})
    img.alpha_composite(avatar, (left, top))
    d.text((hx, handle_y), handle_text, font=handle_font,
           fill=th["handle_color"], anchor="lt")
    badge_cx = hx + _spec_glyph_width(handle_font, handle_text) + badge_r + 8
    draw_verified_badge(d, badge_cx, handle_y + handle_h // 2,
                        badge_r, color=th["badge_color"])
    if menu_w:
        for dx in (0, 8, 16):
            cx, cy = right - 18 + dx, top + 17
            d.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=th["icon_color"])

    secondary_y = handle_y + handle_h + 12
    sx = hx
    if label:
        label_text, label_font = _spec_fit_label(
            label, max(1, right - hx - 16), 18, th)
        label_w = _spec_glyph_width(label_font, label_text) + 18
        d.rounded_rectangle((sx, secondary_y, sx + label_w, secondary_y + 26),
                            radius=8, fill=th.get("label_fill") or th["control_fill"])
        d.text((sx + 9, secondary_y + 5), label_text, font=label_font,
               fill=th["label_color"], anchor="lt")
        sx += label_w + 12
    if awards:
        # A compact three-award crop keeps the familiar social-post cue
        # without the old nine-icon strip competing with the hook.
        awards_w = min(76, max(0, right - sx))
        if awards_w:
            strip = Image.new("RGBA", (awards_w, 24), (0, 0, 0, 0))
            _draw_mini_awards(ImageDraw.Draw(strip), 0, 0, size=22, spacing=5)
            img.alpha_composite(strip, (sx, secondary_y + 1))

    space_w = _spec_glyph_width(body_font, " ")
    baseline = body_y - body_box[1]
    highlighted = (highlight_word or "").strip().strip(".,!?\"'").lower()
    for line in lines:
        x = left + 4
        for word, color in line:
            if (highlighted and word.strip(".,!?\"'").lower() == highlighted
                    and th.get("highlight_fill")):
                box = d.textbbox((x, baseline), word, font=body_font, anchor="ls")
                marker_box = (box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3)
                if th.get("highlight_gradient"):
                    marker_size = (marker_box[2] - marker_box[0] + 1,
                                   marker_box[3] - marker_box[1] + 1)
                    marker = _spec_gradient(marker_size, th["highlight_gradient"])
                    marker.putalpha(_spec_round_mask(
                        marker_size, (0, 0, marker_size[0] - 1, marker_size[1] - 1), 10))
                    img.alpha_composite(marker, marker_box[:2])
                else:
                    d.rounded_rectangle(marker_box, radius=8, fill=th["highlight_fill"])
            d.text((x, baseline), word, font=body_font, fill=color, anchor="ls")
            x += _spec_glyph_width(body_font, word) + space_w
        baseline += lh

    d.line((left, divider_y, right, divider_y), fill=th["divider_color"], width=2)
    labels = ["".join(c for c in value.strip() if 32 <= ord(c) < 127) or "930+"
              for value in (likes_text, shares_text)]
    eng_size = int(th["engagement_size"])
    while True:
        eng_font = load_font(eng_size, bold=False, theme=th)
        item_widths = [_spec_glyph_width(eng_font, value) + 60 for value in labels]
        if sum(item_widths) + 12 <= content_w or eng_size <= 14:
            break
        eng_size -= 1
    cur_x = left
    row_mid = row_top + row_h // 2
    eng_color = th["engagement_color"]
    for i, (value, item_w) in enumerate(zip(labels, item_widths)):
        d.rounded_rectangle((cur_x, row_top, cur_x + item_w, row_top + row_h),
                            radius=row_h // 2, fill=th["control_fill"])
        icon_x = cur_x + 13
        icon_path = th.get("heart_icon_path" if i == 0 else "share_icon_path")
        if not _paste_custom_icon(img, icon_path, icon_x, row_mid - 12, 24):
            if i == 0:
                heart = _draw_heart_filled if th.get("heart_filled") else _draw_heart_outline
                heart(d, icon_x + 12, row_mid, size=24, color=th["heart_color"])
            else:
                _draw_share_tray(d, icon_x, row_mid - 12, size=24,
                                 color=th["share_color"], stroke=2)
        d.text((icon_x + 34, row_mid), value, font=eng_font,
               fill=eng_color, anchor="lm")
        cur_x += item_w + 12
    if right - cur_x >= 38:
        _draw_spec_bookmark(d, right - 21, row_top + 9,
                            color=th["icon_color"], size=27)

    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out
