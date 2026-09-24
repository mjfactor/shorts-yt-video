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
            name = open_font(str(path), 40).getname()
        except Exception:
            continue
        # Tolerant unpack: getname() is usually (family, subfamily) but
        # some files yield 1- or 3-tuples. Matching always runs against
        # the COMBINED "family subfamily" string (never family[0] alone),
        # so multi-token queries like ("Montserrat", "ExtraBold") resolve
        # to the right cut instead of any same-family font.
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
    """Filled circle + white check (drawn, no emoji)."""
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
    """Subtle satire icon row: orange upvote, red heart, comment, blue share, bookmark."""
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
    handle_size = 34
    handle_font = load_font(handle_size, bold=True, theme=th)
    handle_y = av_y + (av_size - handle_size) // 2
    d.text((hx, handle_y), handle, font=handle_font, fill=th["handle_color"])
    bx = hx + text_width(d, handle, handle_font) + 14
    draw_verified_badge(d, bx + 12, handle_y + handle_size // 2 + 2, 13,
                        color=th["badge_color"])

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
    "body_min_size": 32,
    "body_max_lines": 3,
    "icon_color": (150, 150, 155, 255),
    "heart_color": (235, 32, 60, 255),    # Vibrant Red
    "share_color": (29, 155, 240, 255),   # Twitter/Verified Blue
    "upvote_color": (255, 69, 0, 255),    # Reddit Orange-Red
    "comment_color": (16, 185, 129, 255), # Emerald Green
    "bookmark_color": (245, 158, 11, 255),# Amber Gold
    "icon_spacing": 82,
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

    Capacity (measured Montserrat ExtraBold, 684px body budget):
    comfortable max at full 64px is ~13 words (~85 chars, 3 lines);
    shrink runs 64px down to the 32px floor, which holds worst-case
    8x12-char words in exactly 3 lines (measured: 4 lines at 36px,
    3 lines at 32px — the floor exists for this case). 32px is the
    legibility floor; beyond 3 lines there the card GROWS taller
    (never clipped). validate_extra enforces the <=8-word card cap.
    """
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
    """Smooth vector heart (~4r wide, ~4r high), grey, no numbers."""
    w = 4 * r
    h = int(w * 0.95)
    cx = x + w / 2.0
    cy = y + h / 2.0
    pts = _heart_points(cx, cy, float(w), float(h), num_points=120)
    draw.polygon(pts, fill=color)


def _draw_spec_share(draw: ImageDraw.ImageDraw, x: int, y: int,
                     color: tuple, size: int = 46) -> None:
    """Modern 3-node share icon: root on left, two branches on right."""
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


def _paste_custom_icon(img: Image.Image, icon_path: str | Path | None,
                       x: int, y: int, size: int) -> bool:
    """Attempt to load and paste a custom PNG/image icon onto img at (x, y).

    Returns True if an image was successfully loaded and pasted, False otherwise.
    """
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
                     width: int = 900) -> Path:
    """Spec-exact opening card: transparent RGBA ~900px wide, radius 48.

    Top row: ~120px circular avatar (ellipse mask; solid circle + handle
    first letter when the file is missing), bold handle + small blue
    circle with a line-drawn white check (no emoji). Body: bold ~64px
    wrapped word-by-word (highlight in #E00000 red, case-insensitive),
    shrunk on overflow via textbbox measurement. Bottom row: vibrant
    satire action buttons (orange upvote, red heart, green comment,
    blue share, amber bookmark, or custom image icons if provided).
    Height is dynamic but canvas-bounded.
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
    height = top_h + body_h + 28 + 48 + pad
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

    handle_h = int(th.get("handle_size", 44))
    handle_font = load_font(handle_h, bold=True, theme=th)
    # Vertically align handle with avatar midpoint instead of hugging the top edge
    handle_y = pad + max(0, (av_size - handle_h) // 2)
    d.text((hx, handle_y), handle, font=handle_font,
           fill=th["handle_color"])
    badge_cx = hx + text_width(d, handle, handle_font) + 26
    badge_cy = handle_y + handle_h // 2 + 3
    draw_verified_badge(d, badge_cx, badge_cy,
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
    spacing = int(th.get("icon_spacing", 82))
    cur_x = hx

    # 1. Orange Upvote (Reddit satire)
    upvote_col = th.get("upvote_color", (255, 69, 0, 255))
    if not _paste_custom_icon(img, th.get("upvote_icon_path"), cur_x, iy, 44):
        _draw_spec_upvote(d, cur_x, iy, color=upvote_col, size=44)
    cur_x += spacing

    # 2. Red Heart (Like)
    heart_col = th.get("heart_color", (235, 32, 60, 255))
    if not _paste_custom_icon(img, th.get("heart_icon_path"), cur_x, iy, 48):
        _draw_spec_heart(d, cur_x, iy, color=heart_col, r=13)
    cur_x += spacing

    # 3. Satire Comment Bubble
    comment_col = th.get("comment_color", (16, 185, 129, 255))
    if not _paste_custom_icon(img, th.get("comment_icon_path"), cur_x, iy + 2, 42):
        _draw_spec_comment(d, cur_x, iy + 2, color=comment_col, size=42)
    cur_x += spacing

    # 4. Blue Share
    share_col = th.get("share_color", (29, 155, 240, 255))
    if not _paste_custom_icon(img, th.get("share_icon_path"), cur_x, iy + 2, 44):
        _draw_spec_share(d, cur_x, iy + 2, color=share_col, size=44)
    cur_x += spacing

    # 5. Satire Bookmark
    bookmark_col = th.get("bookmark_color", (245, 158, 11, 255))
    if not _paste_custom_icon(img, th.get("bookmark_icon_path"), cur_x, iy + 2, 40):
        _draw_spec_bookmark(d, cur_x, iy + 2, color=bookmark_col, size=40)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out
