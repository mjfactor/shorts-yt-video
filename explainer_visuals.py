"""Programmatic visual generation for explainer Shorts.

Produces 1080×1920 PNG frames (and short MP4 loops) from semantic storyboard
instructions. Each beat's `visual_hint` is dispatched to a deterministic
renderer — the AI describes WHAT to show, this code decides HOW to render it.

Unknown or invalid visual types fall back safely to a styled text card.
No arbitrary code execution; all rendering is via Pillow ImageDraw.
"""
import math
import os
import random
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]

# Canvas dimensions (vertical Shorts)
W, H = 1080, 1920

# Asset output directory
ASSETS = Path("assets")
ASSETS.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Color palettes for the modern explainer aesthetic
# ---------------------------------------------------------------------------
_PALETTES = [
    # Deep blue → purple gradient
    {"bg_top": (15, 20, 50), "bg_bot": (40, 15, 60),
     "accent": (100, 180, 255), "accent2": (180, 120, 255),
     "text": (255, 255, 255), "muted": (160, 170, 200)},
    # Dark teal → navy
    {"bg_top": (10, 30, 40), "bg_bot": (15, 15, 45),
     "accent": (0, 220, 200), "accent2": (60, 160, 255),
     "text": (255, 255, 255), "muted": (140, 190, 200)},
    # Charcoal → warm dark
    {"bg_top": (25, 25, 30), "bg_bot": (40, 20, 25),
     "accent": (255, 160, 60), "accent2": (255, 100, 100),
     "text": (255, 255, 255), "muted": (200, 180, 160)},
    # Deep green → dark
    {"bg_top": (10, 30, 20), "bg_bot": (15, 20, 40),
     "accent": (80, 220, 140), "accent2": (60, 200, 255),
     "text": (255, 255, 255), "muted": (150, 200, 170)},
    # Purple → magenta
    {"bg_top": (30, 15, 45), "bg_bot": (50, 15, 35),
     "accent": (200, 130, 255), "accent2": (255, 100, 180),
     "text": (255, 255, 255), "muted": (180, 160, 200)},
]


def _get_palette(beat_idx: int) -> dict:
    """Consistent palette per beat index."""
    return _PALETTES[beat_idx % len(_PALETTES)]


# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)
_FONT_REGULAR_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)

_cached_font_path: str | None = None
_cached_font_regular_path: str | None = None


def _find_font(candidates: tuple[str, ...]) -> str | None:
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _font_bold(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    global _cached_font_path
    if _cached_font_path is None:
        _cached_font_path = _find_font(_FONT_CANDIDATES) or ""
    if _cached_font_path:
        return ImageFont.truetype(_cached_font_path, size)
    return ImageFont.load_default()


def _font_regular(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    global _cached_font_regular_path
    if _cached_font_regular_path is None:
        _cached_font_regular_path = _find_font(_FONT_REGULAR_CANDIDATES) or ""
    if _cached_font_regular_path:
        return ImageFont.truetype(_cached_font_regular_path, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------
def _draw_gradient_bg(img: Image.Image, top: tuple, bot: tuple):
    """Draw a vertical gradient background."""
    draw = ImageDraw.Draw(img)
    for y in range(H):
        ratio = y / H
        r = int(top[0] + (bot[0] - top[0]) * ratio)
        g = int(top[1] + (bot[1] - top[1]) * ratio)
        b = int(top[2] + (bot[2] - top[2]) * ratio)
        draw.line([(0, y), (W, y)], fill=(r, g, b))


def _draw_rounded_rect(draw: ImageDraw.Draw, xy: tuple, radius: int,
                        fill=None, outline=None, width: int = 1):
    """Draw a rounded rectangle."""
    x0, y0, x1, y1 = xy
    r = min(radius, (x1 - x0) // 2, (y1 - y0) // 2)
    draw.rounded_rectangle(xy, radius=r, fill=fill, outline=outline, width=width)


def _draw_arrow(draw: ImageDraw.Draw, start: tuple, end: tuple,
                color: tuple, width: int = 4, head_size: int = 16):
    """Draw a line with an arrowhead."""
    draw.line([start, end], fill=color, width=width)
    # Arrowhead
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = max(1, math.sqrt(dx * dx + dy * dy))
    ux, uy = dx / length, dy / length
    # Perpendicular
    px, py = -uy, ux
    tip = end
    left = (int(tip[0] - head_size * ux + head_size * 0.5 * px),
            int(tip[1] - head_size * uy + head_size * 0.5 * py))
    right = (int(tip[0] - head_size * ux - head_size * 0.5 * px),
             int(tip[1] - head_size * uy - head_size * 0.5 * py))
    draw.polygon([tip, left, right], fill=color)


def _draw_circle_icon(draw: ImageDraw.Draw, center: tuple, radius: int,
                       fill: tuple, outline: tuple | None = None,
                       icon_text: str = "", text_color: tuple = (255, 255, 255)):
    """Draw a circle with optional centered text (icon placeholder)."""
    x, y = center
    draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                 fill=fill, outline=outline, width=3)
    if icon_text:
        font = _font_bold(max(16, radius))
        bbox = draw.textbbox((0, 0), icon_text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((x - tw // 2, y - th // 2), icon_text, fill=text_color, font=font)


def _draw_box_component(draw: ImageDraw.Draw, x: int, y: int,
                         w: int, h: int, label: str, palette: dict,
                         is_active: bool = False):
    """Draw a clean, glassmorphic rounded box component with centered label."""
    # Dark glassmorphic background: always contrasty and readable
    bg_color = (palette["bg_top"][0] + 18, palette["bg_top"][1] + 20, palette["bg_top"][2] + 32)
    border_color = (255, 255, 255) if is_active else palette["accent"]
    border_w = 4 if is_active else 2

    _draw_rounded_rect(draw, (x, y, x + w, y + h), radius=20,
                       fill=bg_color, outline=border_color, width=border_w)

    if is_active:
        # Subtle glowing top accent stripe
        draw.rectangle([x + 30, y + 2, x + w - 30, y + 6], fill=palette["accent2"])

    # Cleanly centered text label without any overlapping 2-letter circles
    font = _font_bold(32) if len(label) < 22 else _font_regular(28)
    lines = textwrap.wrap(label, width=max(1, w // 18))[:2]
    line_h = font.size + 8
    total_text_h = len(lines) * line_h
    start_text_y = y + (h - total_text_h) // 2

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        text_color = (255, 255, 255) if is_active else palette["text"]
        draw.text((x + (w - tw) // 2, start_text_y + i * line_h), line,
                  fill=text_color, font=font)


def _draw_narration_card(draw: ImageDraw.Draw, text: str, palette: dict):
    """Draw a dedicated lower card for spoken narration so it never collides with diagrams."""
    card_x1, card_y1 = 60, 1480
    card_x2, card_y2 = W - 60, 1820
    bg = (palette["bg_bot"][0] // 2, palette["bg_bot"][1] // 2, palette["bg_bot"][2] // 2)
    _draw_rounded_rect(draw, (card_x1, card_y1, card_x2, card_y2), radius=24,
                       fill=bg, outline=(*palette["accent"], 60), width=2)

    font = _font_regular(28)
    lines = textwrap.wrap(text[:260], width=36)[:5]
    total_h = len(lines) * 38
    start_y = card_y1 + (card_y2 - card_y1 - total_h) // 2
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, start_y + i * 38), line, fill=palette["text"], font=font)


def _draw_centered_text(draw: ImageDraw.Draw, text: str, y: int,
                         font: ImageFont.FreeTypeFont, color: tuple,
                         max_width: int = W - 120):
    """Draw centered, word-wrapped text."""
    chars_per_line = max(1, max_width // max(1, (font.size * 6 // 10)))
    lines = textwrap.wrap(text, width=chars_per_line)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(((W - tw) // 2, y), line, fill=color, font=font)
        y += th + 12
    return y


# ---------------------------------------------------------------------------
# Icon symbol map (simple text placeholders for diagram components)
# ---------------------------------------------------------------------------
_ICON_MAP = {
    "phone": "📱", "smartphone": "📱", "browser": "🌐", "laptop": "💻",
    "computer": "💻", "server": "🖥", "cloud": "☁", "database": "🗄",
    "lock": "🔒", "key": "🔑", "gear": "⚙", "signal": "📡",
    "wave": "〰", "waves": "〰", "arrow": "→", "lightning": "⚡",
    "fire": "🔥", "water": "💧", "molecule": "⚛", "atom": "⚛",
    "magnet": "🧲", "battery": "🔋", "bulb": "💡", "lightbulb": "💡",
    "eye": "👁", "brain": "🧠", "heart": "❤", "shield": "🛡",
    "wifi": "📶", "antenna": "📡", "cable": "🔌", "plug": "🔌",
    "money": "💰", "card": "💳", "coin": "🪙", "document": "📄",
    "documents": "📄", "check": "✓", "checkmark": "✓", "cross": "✗",
    "question": "?", "person": "👤", "people": "👥",
    "car": "🚗", "airplane": "✈", "rocket": "🚀", "submarine": "🚢",
    "food": "🍽", "microwave": "📦", "oven": "📦",
    "sun": "☀", "earth": "🌍", "globe": "🌍",
    "ai_model": "🤖", "robot": "🤖",
}


def _icon_for(component: str) -> str:
    """Get a symbol for a component name. Falls back to first letter."""
    c = (component or "").lower().strip().replace("_", " ")
    for key, icon in _ICON_MAP.items():
        if key in c:
            return icon
    return component[:2].upper() if component else "•"


# ---------------------------------------------------------------------------
# Scene renderers
# ---------------------------------------------------------------------------
def _render_text_card(beat_idx: int, text: str, hint: dict) -> Image.Image:
    """Styled text card with gradient background, label, and beat text."""
    pal = _get_palette(beat_idx)
    img = Image.new("RGB", (W, H))
    _draw_gradient_bg(img, pal["bg_top"], pal["bg_bot"])
    draw = ImageDraw.Draw(img)

    label = hint.get("label", "")
    desc = hint.get("description", "")
    display_text = label or desc or text

    # Decorative accent line
    line_y = 700
    draw.rectangle([80, line_y, W - 80, line_y + 4], fill=pal["accent"])

    # Label (large)
    if label:
        font_label = _font_bold(64)
        y = _draw_centered_text(draw, label, line_y + 40, font_label, pal["accent"])
    else:
        y = line_y + 40

    # Description or hint text
    if desc and desc != label:
        font_desc = _font_regular(36)
        y = _draw_centered_text(draw, desc, y + 30, font_desc, pal["muted"])

    # Beat narration text (smaller, below)
    font_text = _font_regular(30)
    _draw_centered_text(draw, text[:300], max(y + 50, 1100), font_text, pal["text"])

    # Decorative dots
    for i in range(3):
        cx = W // 2 + (i - 1) * 30
        draw.ellipse([cx - 5, H - 200, cx + 5, H - 190], fill=pal["accent2"])

    return img


def _render_diagram(beat_idx: int, text: str, hint: dict) -> Image.Image:
    """Labeled diagram with components, connections, and labels."""
    pal = _get_palette(beat_idx)
    img = Image.new("RGB", (W, H))
    _draw_gradient_bg(img, pal["bg_top"], pal["bg_bot"])
    draw = ImageDraw.Draw(img)

    label = hint.get("label", "")
    components = hint.get("components", [])
    desc = hint.get("description", "")

    # Title label at top
    if label:
        font_title = _font_bold(52)
        _draw_centered_text(draw, label, 200, font_title, pal["accent"])

    # Filter out arrows from component list
    real_components = [c for c in components
                       if c.lower().strip() not in ("arrow", "→", "->", ">")]

    if not real_components:
        real_components = ["Component"]

    # Layout components in a vertical flow
    n = len(real_components)
    box_w, box_h = 400, 140
    start_y = 400
    spacing = max(180, (900 - 100) // max(1, n))

    positions = []
    for i, comp in enumerate(real_components):
        cy = start_y + i * spacing
        cx = W // 2
        positions.append((cx, cy))
        # Draw box
        _draw_box_component(draw, cx - box_w // 2, cy - box_h // 2,
                            box_w, box_h, comp.replace("_", " ").title(), pal)

    # Draw arrows between sequential components
    for i in range(len(positions) - 1):
        x1, y1 = positions[i]
        x2, y2 = positions[i + 1]
        _draw_arrow(draw, (x1, y1 + box_h // 2 + 5),
                    (x2, y2 - box_h // 2 - 5),
                    pal["accent2"], width=3, head_size=14)

    # Description pill above narration card
    if desc:
        font_desc = _font_regular(28)
        _draw_centered_text(draw, desc, max(start_y + n * spacing + 40, 1360),
                            font_desc, pal["muted"])

    # Spoken narration in reserved lower third
    _draw_narration_card(draw, text, pal)
    return img


def _render_process_flow(beat_idx: int, text: str, hint: dict) -> Image.Image:
    """Sequential process flow with steps and arrows."""
    pal = _get_palette(beat_idx)
    img = Image.new("RGB", (W, H))
    _draw_gradient_bg(img, pal["bg_top"], pal["bg_bot"])
    draw = ImageDraw.Draw(img)

    label = hint.get("label", "")
    components = hint.get("components", [])
    desc = hint.get("description", "")

    # Title
    if label:
        font_title = _font_bold(52)
        _draw_centered_text(draw, label, 180, font_title, pal["accent"])

    # Filter arrows
    steps = [c for c in components
             if c.lower().strip() not in ("arrow", "→", "->", ">")]
    if not steps:
        steps = ["Step 1", "Step 2", "Step 3"]

    n = len(steps)
    # Horizontal or vertical layout depending on count
    if n <= 3:
        # Horizontal flow
        step_w = min(280, (W - 160) // max(1, n) - 40)
        step_h = 160
        total_w = n * step_w + (n - 1) * 80
        start_x = (W - total_w) // 2
        cy = 650

        for i, step in enumerate(steps):
            cx = start_x + i * (step_w + 80) + step_w // 2
            # Step circle/box
            r = min(step_w, step_h) // 2 - 10
            _draw_circle_icon(draw, (cx, cy), r,
                               fill=(*pal["accent"], 60),
                               outline=pal["accent"],
                               icon_text=_icon_for(step),
                               text_color=pal["text"])
            # Step label below
            font_step = _font_regular(26)
            step_label = step.replace("_", " ").title()
            lines = textwrap.wrap(step_label, width=max(1, step_w // 14))
            ly = cy + r + 15
            for line in lines[:2]:
                bbox = draw.textbbox((0, 0), line, font=font_step)
                tw = bbox[2] - bbox[0]
                draw.text((cx - tw // 2, ly), line,
                          fill=pal["text"], font=font_step)
                ly += 32
            # Arrow to next
            if i < n - 1:
                ax = cx + r + 10
                bx = start_x + (i + 1) * (step_w + 80) + step_w // 2 - r - 10
                _draw_arrow(draw, (ax, cy), (bx, cy),
                            pal["accent2"], width=3, head_size=12)
    else:
        # Vertical flow for 4+ steps
        step_w, step_h = 500, 100
        start_y = 380
        spacing = max(140, 800 // max(1, n))

        for i, step in enumerate(steps):
            cy = start_y + i * spacing
            cx = W // 2
            # Step number circle
            _draw_circle_icon(draw, (cx - step_w // 2 + 30, cy), 25,
                               fill=pal["accent"],
                               icon_text=str(i + 1),
                               text_color=pal["bg_top"])
            # Step label
            font_step = _font_regular(32)
            step_label = step.replace("_", " ").title()
            draw.text((cx - step_w // 2 + 70, cy - 16), step_label,
                      fill=pal["text"], font=font_step)
            # Connecting line
            if i < n - 1:
                line_x = cx - step_w // 2 + 30
                draw.line([(line_x, cy + 28), (line_x, cy + spacing - 28)],
                          fill=pal["accent2"], width=2)

    # Description
    if desc:
        font_desc = _font_regular(28)
        _draw_centered_text(draw, desc, 1360, font_desc, pal["muted"])

    # Spoken narration in reserved lower third
    _draw_narration_card(draw, text, pal)
    return img


def _render_fallback(beat_idx: int, text: str, hint: dict) -> Image.Image:
    """Graceful fallback: gradient background with beat text."""
    pal = _get_palette(beat_idx)
    img = Image.new("RGB", (W, H))
    _draw_gradient_bg(img, pal["bg_top"], pal["bg_bot"])
    draw = ImageDraw.Draw(img)

    # Large centered text
    font = _font_bold(40)
    _draw_centered_text(draw, text[:300], 700, font, pal["text"])

    return img


def _render_animated_molecules(beat_idx: int, text: str, hint: dict, out_mp4: Path,
                               num_frames: int = 60, fps: int = 30) -> Path | None:
    """Template: Molecular physics & friction (spinning water dipoles + heat aura)."""
    pal = _get_palette(beat_idx)
    label = hint.get("label", "Molecular Friction")

    base = Image.new("RGB", (W, H))
    _draw_gradient_bg(base, pal["bg_top"], pal["bg_bot"])
    d_base = ImageDraw.Draw(base)

    # Title zone (y: 120-250)
    font_title = _font_bold(54)
    _draw_centered_text(d_base, label, 160, font_title, pal["accent"])

    # Visual Stage Frame (y: 280-1420)
    stage_x1, stage_y1 = 70, 280
    stage_x2, stage_y2 = W - 70, 1420
    stage_bg = (pal["bg_top"][0] + 12, pal["bg_top"][1] + 14, pal["bg_top"][2] + 24)
    _draw_rounded_rect(d_base, (stage_x1, stage_y1, stage_x2, stage_y2), radius=28,
                       fill=stage_bg, outline=pal["accent"], width=2)

    # Top indicator pill inside stage
    pill_w, pill_h = 560, 54
    px = (W - pill_w) // 2
    py = stage_y1 + 40
    _draw_rounded_rect(d_base, (px, py, px + pill_w, py + pill_h), radius=16,
                       fill=(pal["bg_top"][0] + 25, pal["bg_top"][1] + 25, pal["bg_top"][2] + 40),
                       outline=pal["accent2"], width=2)
    font_pill = _font_bold(26)
    _draw_centered_text(d_base, "⚡ 2.45 GHz MICROWAVE FIELD", py + 12, font_pill, pal["accent2"])

    # Bottom status badge inside stage
    by = stage_y2 - 90
    font_badge = _font_bold(28)
    _draw_centered_text(d_base, "ROTATION CREATES THERMAL FRICTION", by, font_badge, (255, 180, 80))

    # Narration card in reserved lower third (y: 1480-1820)
    _draw_narration_card(d_base, text, pal)

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        str(out_mp4)
    ]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:
        return None

    mol_centers = [(W // 2 - 200, 640), (W // 2 + 200, 640), (W // 2, 940)]

    for f in range(num_frames):
        t = f / float(num_frames)
        frame = base.copy()
        draw = ImageDraw.Draw(frame)

        for m_idx, (mx, my) in enumerate(mol_centers):
            # Oscillation angle back and forth
            angle = math.radians(35 * math.sin(4 * math.pi * t + m_idx * 1.6))

            # Expanding heat friction aura ring
            aura_r = int(60 + 35 * ((t * 2 + m_idx * 0.33) % 1.0))
            draw.ellipse([mx - aura_r, my - aura_r, mx + aura_r, my + aura_r],
                         outline=(255, 130 + int(60 * math.sin(2 * math.pi * t)), 40), width=3)

            # Oxygen atom (red core)
            ox_r = 38
            draw.ellipse([mx - ox_r, my - ox_r, mx + ox_r, my + ox_r], fill=(235, 45, 60))
            font_atom = _font_bold(24)
            draw.text((mx - 10, my - 14), "O", fill=(255, 255, 255), font=font_atom)
            font_charge = _font_regular(20)
            draw.text((mx + 14, my - 34), "δ-", fill=(255, 200, 200), font=font_charge)

            # Two Hydrogen atoms (cyan)
            h_dist = 62
            h_r = 24
            for h_angle_off in (-0.85, 0.85):
                ha = angle + h_angle_off
                hx = int(mx + h_dist * math.sin(ha))
                hy = int(my + h_dist * math.cos(ha))
                draw.line([(mx, my), (hx, hy)], fill=(210, 215, 230), width=6)
                draw.ellipse([hx - h_r, hy - h_r, hx + h_r, hy + h_r], fill=(60, 170, 255))
                font_h = _font_bold(20)
                draw.text((hx - 8, hy - 12), "H", fill=(255, 255, 255), font=font_h)
                draw.text((hx + 10, hy - 26), "δ+", fill=(180, 230, 255), font=font_charge)

            # Friction sparks
            for s_idx in range(4):
                sp_angle = t * 2 * math.pi + s_idx * (math.pi / 2)
                sp_dist = ox_r + 18 + int(8 * math.sin(6 * math.pi * t + s_idx))
                sx = int(mx + sp_dist * math.cos(sp_angle))
                sy = int(my + sp_dist * math.sin(sp_angle))
                draw.ellipse([sx - 3, sy - 3, sx + 3, sy + 3], fill=(255, 230, 100))

        try:
            proc.stdin.write(frame.tobytes())
        except Exception:
            break

    try:
        proc.stdin.close()
        proc.wait()
    except Exception:
        pass

    if out_mp4.exists() and out_mp4.stat().st_size > 1000:
        return out_mp4
    return None


def _render_animated_waves(beat_idx: int, text: str, hint: dict, out_mp4: Path,
                           num_frames: int = 60, fps: int = 30) -> Path | None:
    """Template: Wave radiation & electromagnetic field emission."""
    pal = _get_palette(beat_idx)
    label = hint.get("label", "The Magnetron")

    base = Image.new("RGB", (W, H))
    _draw_gradient_bg(base, pal["bg_top"], pal["bg_bot"])
    d_base = ImageDraw.Draw(base)

    # Title zone
    font_title = _font_bold(54)
    _draw_centered_text(d_base, label, 160, font_title, pal["accent"])

    # Visual Stage (y: 280-1420)
    _draw_box_component(d_base, (W - 540) // 2, 310, 540, 130, "⚡ Magnetron Tube", pal, is_active=True)

    badge_y = 660
    font_badge = _font_bold(26)
    _draw_centered_text(d_base, "INVISIBLE MICROWAVES (2.45 GHz)", badge_y, font_badge, pal["accent2"])

    _draw_box_component(d_base, (W - 540) // 2, 1020, 540, 130, "🍲 Food & Moisture", pal, is_active=False)

    # Narration card
    _draw_narration_card(d_base, text, pal)

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        str(out_mp4)
    ]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:
        return None

    wave_tracks = [500, 760, 890]

    for f in range(num_frames):
        t = f / float(num_frames)
        frame = base.copy()
        draw = ImageDraw.Draw(frame)

        # 3 smooth traveling sine waves
        for w_idx, wy in enumerate(wave_tracks):
            col = pal["accent"] if w_idx % 2 == 0 else pal["accent2"]
            pts = []
            for wx in range(120, W - 120, 10):
                phase = 2 * math.pi * t * 2 + w_idx * 1.2
                y_val = int(wy + 24 * math.sin(wx * 0.035 - phase))
                pts.append((wx, y_val))
            if len(pts) > 1:
                draw.line(pts, fill=col, width=4)

        # Traveling energy pulses downwards
        for p in (0.0, 0.33, 0.66):
            pt = (t + p) % 1.0
            py = int(440 + (1020 - 440) * pt)
            px = W // 2
            draw.ellipse([px - 12, py - 12, px + 12, py + 12], fill=pal["accent"])
            draw.ellipse([px - 6, py - 6, px + 6, py + 6], fill=(255, 255, 255))

        try:
            proc.stdin.write(frame.tobytes())
        except Exception:
            break

    try:
        proc.stdin.close()
        proc.wait()
    except Exception:
        pass

    if out_mp4.exists() and out_mp4.stat().st_size > 1000:
        return out_mp4
    return None


def _render_animated_diagram(beat_idx: int, text: str, hint: dict, out_mp4: Path,
                             num_frames: int = 60, fps: int = 30) -> Path | None:
    """Template: Architectural and system diagram with connected components."""
    pal = _get_palette(beat_idx)
    label = hint.get("label", "")
    components = hint.get("components", [])
    desc = hint.get("description", "")

    real_components = [str(c) for c in components
                       if str(c).lower().strip() not in ("arrow", "→", "->", ">")]
    if not real_components:
        real_components = ["Component 1", "Component 2"]

    n = len(real_components)
    box_w = min(540, W - 160)
    box_h = 120
    start_y = 310
    avail_h = 1100 - box_h
    spacing = avail_h // max(1, n - 1) if n > 1 else 0

    positions = []
    for i, comp in enumerate(real_components):
        cy = start_y + i * spacing if n > 1 else 600
        cx = W // 2
        positions.append((cx, cy, comp))

    base = Image.new("RGB", (W, H))
    _draw_gradient_bg(base, pal["bg_top"], pal["bg_bot"])
    d_base = ImageDraw.Draw(base)

    # Title zone
    if label:
        font_title = _font_bold(52)
        _draw_centered_text(d_base, label, 160, font_title, pal["accent"])

    # Description pill above narration card
    if desc:
        pill_w = min(800, W - 160)
        pill_y = 1380
        _draw_rounded_rect(d_base, ((W - pill_w) // 2, pill_y, (W + pill_w) // 2, pill_y + 48),
                           radius=14, fill=(pal["bg_top"][0] + 15, pal["bg_top"][1] + 15, pal["bg_top"][2] + 25),
                           outline=pal["accent2"], width=1)
        font_desc = _font_regular(24)
        _draw_centered_text(d_base, desc, pill_y + 10, font_desc, pal["muted"])

    # Narration card
    _draw_narration_card(d_base, text, pal)

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        str(out_mp4)
    ]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:
        return None

    for f in range(num_frames):
        t = f / float(num_frames)
        frame = base.copy()
        draw = ImageDraw.Draw(frame)

        # 1. Connecting arrows with traveling pulses
        for i in range(len(positions) - 1):
            x1, y1, _ = positions[i]
            x2, y2, _ = positions[i + 1]
            top_pt = (x1, y1 + box_h // 2 + 6)
            bot_pt = (x2, y2 - box_h // 2 - 6)
            _draw_arrow(draw, top_pt, bot_pt, pal["accent2"], width=3, head_size=14)

            # Traveling light pulses
            for p in (0.0, 0.5):
                pt = (t + p) % 1.0
                px = int(top_pt[0] + (bot_pt[0] - top_pt[0]) * pt)
                py = int(top_pt[1] + (bot_pt[1] - top_pt[1]) * pt)
                draw.ellipse([px - 10, py - 10, px + 10, py + 10], fill=pal["accent"])
                draw.ellipse([px - 5, py - 5, px + 5, py + 5], fill=(255, 255, 255))

        # 2. Draw component boxes with active highlight
        active_step = int(t * n)
        for i, (cx, cy, comp) in enumerate(positions):
            is_active = (i == active_step)
            _draw_box_component(draw, cx - box_w // 2, cy - box_h // 2,
                                box_w, box_h, comp.replace("_", " ").title(),
                                pal, is_active=is_active)

        try:
            proc.stdin.write(frame.tobytes())
        except Exception:
            break

    try:
        proc.stdin.close()
        proc.wait()
    except Exception:
        pass

    if out_mp4.exists() and out_mp4.stat().st_size > 1000:
        return out_mp4
    return None


def _render_animated_process_flow(beat_idx: int, text: str, hint: dict, out_mp4: Path,
                                  num_frames: int = 60, fps: int = 30) -> Path | None:
    """Template: Sequential pipeline process flow."""
    pal = _get_palette(beat_idx)
    label = hint.get("label", "")
    components = hint.get("components", [])
    desc = hint.get("description", "")

    steps = [str(c) for c in components
             if str(c).lower().strip() not in ("arrow", "→", "->", ">")]
    if not steps:
        steps = ["Step 1", "Step 2", "Step 3"]

    n = len(steps)
    base = Image.new("RGB", (W, H))
    _draw_gradient_bg(base, pal["bg_top"], pal["bg_bot"])
    d_base = ImageDraw.Draw(base)

    if label:
        font_title = _font_bold(52)
        _draw_centered_text(d_base, label, 160, font_title, pal["accent"])

    if desc:
        pill_w = min(800, W - 160)
        pill_y = 1380
        _draw_rounded_rect(d_base, ((W - pill_w) // 2, pill_y, (W + pill_w) // 2, pill_y + 48),
                           radius=14, fill=(pal["bg_top"][0] + 15, pal["bg_top"][1] + 15, pal["bg_top"][2] + 25),
                           outline=pal["accent2"], width=1)
        font_desc = _font_regular(24)
        _draw_centered_text(d_base, desc, pill_y + 10, font_desc, pal["muted"])

    _draw_narration_card(d_base, text, pal)

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        str(out_mp4)
    ]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:
        return None

    step_w = min(540, W - 160)
    step_h = 110
    start_y = 310
    avail_h = 1050 - step_h
    spacing = avail_h // max(1, n - 1) if n > 1 else 0

    for f in range(num_frames):
        t = f / float(num_frames)
        frame = base.copy()
        draw = ImageDraw.Draw(frame)
        active_idx = int(t * n)

        for i, step in enumerate(steps):
            cy = start_y + i * spacing if n > 1 else 600
            cx = W // 2
            is_active = (i == active_idx)
            _draw_box_component(draw, cx - step_w // 2, cy - step_h // 2,
                                step_w, step_h, f"Step {i + 1}: {step.replace('_', ' ').title()}",
                                pal, is_active=is_active)

            if i < n - 1:
                y1 = cy + step_h // 2 + 5
                y2 = (start_y + (i + 1) * spacing) - step_h // 2 - 5
                _draw_arrow(draw, (cx, y1), (cx, y2), pal["accent2"], width=3, head_size=12)
                pt = (t * (n - 1) - i) % 1.0
                if 0 <= pt <= 1.0:
                    py = int(y1 + (y2 - y1) * pt)
                    draw.ellipse([cx - 8, py - 8, cx + 8, py + 8], fill=(255, 255, 255))

        try:
            proc.stdin.write(frame.tobytes())
        except Exception:
            break

    try:
        proc.stdin.close()
        proc.wait()
    except Exception:
        pass

    if out_mp4.exists() and out_mp4.stat().st_size > 1000:
        return out_mp4
    return None


# Renderer dispatch table
_RENDERERS = {
    "text_card": _render_text_card,
    "diagram": _render_diagram,
    "svg_diagram": _render_diagram,  # alias
    "process_flow": _render_process_flow,
    "stock_footage": None,  # handled externally via Pexels
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_scene(beat: dict, beat_idx: int, total_beats: int = 6,
                animate: bool = True) -> Path | None:
    """Render a single beat's visual from its visual_hint using specialized templates."""
    hint = beat.get("visual_hint") or {}
    if not isinstance(hint, dict):
        hint = {}

    vtype = str(hint.get("type", "")).strip().lower().replace(" ", "_")
    text = str(beat.get("text", "")).strip()

    if vtype == "stock_footage" or not vtype:
        return None

    # Step 1: Animate if requested
    if animate:
        out_mp4 = ASSETS / f"explainer_scene_{beat_idx}.mp4"
        desc_lower = hint.get("description", "").lower()
        full_text = f"{desc_lower} {text} {' '.join(str(c) for c in hint.get('components', []))}".lower()

        # Dedicated physics & systems templates:
        is_molecule_beat = any(k in full_text for k in ("molecule", "dipole", "water molecule", "rotation", "friction"))
        is_wave_beat = any(k in full_text for k in ("magnetron", "wave", "radiation", "antenna", "frequency", "electromagnetic"))

        if is_molecule_beat:
            res = _render_animated_molecules(beat_idx, text, hint, out_mp4)
            if res and res.exists():
                return res
        elif is_wave_beat:
            res = _render_animated_waves(beat_idx, text, hint, out_mp4)
            if res and res.exists():
                return res
        elif vtype in ("process_flow", "pipeline"):
            res = _render_animated_process_flow(beat_idx, text, hint, out_mp4)
            if res and res.exists():
                return res
        elif vtype in ("diagram", "svg_diagram", "mechanism"):
            res = _render_animated_diagram(beat_idx, text, hint, out_mp4)
            if res and res.exists():
                return res

    # Step 2: Static PNG renderer
    renderer = _RENDERERS.get(vtype)
    if renderer is None:
        renderer = _render_fallback

    try:
        img = renderer(beat_idx, text, hint)
        out_path = ASSETS / f"explainer_scene_{beat_idx}.png"
        img.save(out_path, "PNG")
        return out_path
    except Exception:
        try:
            img = _render_fallback(beat_idx, text, hint)
            out_path = ASSETS / f"explainer_scene_{beat_idx}.png"
            img.save(out_path, "PNG")
            return out_path
        except Exception:
            return None


def build_all_scenes(beats: list[dict], animate: bool = True) -> list[Path | None]:
    """Render visuals for all beats. Returns list of Paths (None = use stock)."""
    return [build_scene(b, i, len(beats), animate=animate) for i, b in enumerate(beats)]

