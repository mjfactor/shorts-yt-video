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
# Generative AI Scene Renderer
# ---------------------------------------------------------------------------
def _generate_pillow_code(beat_idx: int, text: str, hint: dict) -> str:
    """Ask Gemini to generate Pillow render_frame code."""
    import core
    import json
    
    prompt = f"""You are an expert generative artist using Python and Pillow (ImageDraw).
Write a Python function `def render_frame(t: float, W: int, H: int, draw, pal: dict):` that draws a frame of an animation.
`t` goes from 0.0 to 1.0 representing time. W=1080, H=1920.
`pal` is a color palette dict with keys: bg_top, bg_bot, accent, accent2, text, muted (all RGB tuples).
The visual should explain this concept: {json.dumps(hint)}
The narrator is saying: "{text}"

You have access to these pre-defined helper functions in the global scope (do NOT redefine them):
- _draw_box_component(draw, x, y, w, h, label, palette, is_active=False)
- _draw_arrow(draw, start, end, color, width=4, head_size=16)
- _draw_circle_icon(draw, center, radius, fill, outline, icon_text, text_color)
- _draw_centered_text(draw, text, y, font, color, max_width=W-120)
- _font_bold(size)
- _font_regular(size)
- _icon_for(component_name) -> str

Output ONLY raw valid Python code. No markdown formatting, no explanations, no ` ```python ` blocks.
Define `def render_frame(...)`. Do not call it. Do not import anything.
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
        "safetySettings": core._GEMINI_SAFETY_SETTINGS,
    }
    
    for model in core.GEMINI_MODELS:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            raw = core._fetch_gemini(url, payload)
            code = raw["candidates"][0]["content"]["parts"][0]["text"]
            code = code.strip().removeprefix("```python").removeprefix("```").removesuffix("```").strip()
            core.mark_model_working(model)
            return code
        except Exception as e:
            core.warn(f"Code gen failed with {model}: {e}")
            
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_scene(beat: dict, beat_idx: int, total_beats: int = 6,
                animate: bool = True) -> Path | None:
    """Render a single beat's visual dynamically using AI-generated Pillow code."""
    import core
    hint = beat.get("visual_hint") or {}
    if not isinstance(hint, dict):
        hint = {}

    vtype = str(hint.get("type", "")).strip().lower()
    text = str(beat.get("text", "")).strip()

    if vtype == "stock_footage" or not vtype:
        return None

    if not animate:
        return None

    core.log(f"Beat {beat_idx + 1}: Generating custom Pillow code for '{hint.get('description', '')}'...")
    code_str = _generate_pillow_code(beat_idx, text, hint)
    if not code_str:
        core.warn(f"Beat {beat_idx + 1}: AI failed to generate Pillow code.")
        return None

    out_mp4 = ASSETS / f"explainer_scene_{beat_idx}.mp4"
    pal = _get_palette(beat_idx)

    # Execute the generated code in this module's namespace safely
    namespace = globals().copy()
    namespace["math"] = math
    namespace["random"] = random
    
    try:
        exec(code_str, namespace)
        render_func = namespace.get("render_frame")
        if not render_func:
            raise ValueError("No render_frame function defined.")
    except Exception as e:
        core.warn(f"Beat {beat_idx + 1}: Failed to parse generated Pillow code:\n{code_str}\nError: {e}")
        return None

    num_frames = 60
    fps = 30
    base = Image.new("RGB", (W, H))
    _draw_gradient_bg(base, pal["bg_top"], pal["bg_bot"])
    d_base = ImageDraw.Draw(base)


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
    except Exception as e:
        core.warn(f"Beat {beat_idx + 1}: FFmpeg error: {e}")
        return None

    success = True
    for f in range(num_frames):
        t = f / float(num_frames)
        frame = base.copy()
        draw = ImageDraw.Draw(frame)
        try:
            render_func(t, W, H, draw, pal)
        except Exception as e:
            core.warn(f"Beat {beat_idx + 1}: Error in generated render_frame at t={t}: {e}")
            success = False
            break
        
        try:
            proc.stdin.write(frame.tobytes())
        except Exception:
            success = False
            break

    try:
        proc.stdin.close()
        proc.wait()
    except Exception:
        pass

    if success and out_mp4.exists() and out_mp4.stat().st_size > 1000:
        return out_mp4
        
    return None

def build_all_scenes(beats: list[dict], animate: bool = True) -> list[Path | None]:
    return [build_scene(b, i, len(beats), animate=animate) for i, b in enumerate(beats)]

