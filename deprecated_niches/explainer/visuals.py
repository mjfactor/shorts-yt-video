"""Explainer stage 3: diagrams/stock -> 1080x1920 mp4 + line-caption SRT.

Ports explainer_visuals.build_scene/build_all_scenes (Pillow codegen) and
the explainer stock path (diagram first, exact-query Pexels, text_card,
beat_card). Captions are line-based SRT->ASS (yellow bold, top-anchored),
never word-pop. Assembly (concat/mux/subs burn) included; BGM bed mixing
stays with the caller.
"""

import math
import os
import random
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]

from engine.ffmpeg_utils import (escape_filter_path, ffprobe_duration,
                                 find_font, has_ffmpeg_filter, log,
                                 run_ffmpeg, subtitles_filter, warn)
from engine.gemini_client import (DEFAULT_SAFETY_SETTINGS, GeminiClient,
                                  parse_keys)
from niches.base import Content, NarrationResult, NicheConfig, VisualsResult

W, H = 1080, 1920
CACHE = Path("assets")  # shared Pexels cache (content-addressed by id)
CACHE.mkdir(exist_ok=True)

_QUERY_STOPWORDS = frozenset({
    "a", "an", "the", "in", "on", "of", "at", "to", "for",
    "with", "from", "and", "or", "is", "are", "its", "it",
    "cinematic", "close", "creeping", "slowly", "swimming",
    "underwater", "slithering",
})

# ---------------------------------------------------------------------------
# Color palettes for the modern explainer aesthetic
# ---------------------------------------------------------------------------
_PALETTES = [
    {"bg_top": (15, 20, 50), "bg_bot": (40, 15, 60),
     "accent": (100, 180, 255), "accent2": (180, 120, 255),
     "text": (255, 255, 255), "muted": (160, 170, 200)},
    {"bg_top": (10, 30, 40), "bg_bot": (15, 15, 45),
     "accent": (0, 220, 200), "accent2": (60, 160, 255),
     "text": (255, 255, 255), "muted": (140, 190, 200)},
    {"bg_top": (25, 25, 30), "bg_bot": (40, 20, 25),
     "accent": (255, 160, 60), "accent2": (255, 100, 100),
     "text": (255, 255, 255), "muted": (200, 180, 160)},
    {"bg_top": (10, 30, 20), "bg_bot": (15, 20, 40),
     "accent": (80, 220, 140), "accent2": (60, 200, 255),
     "text": (255, 255, 255), "muted": (150, 200, 170)},
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


def _font_bold(size: int):
    global _cached_font_path
    if _cached_font_path is None:
        _cached_font_path = _find_font(_FONT_CANDIDATES) or ""
    if _cached_font_path:
        return ImageFont.truetype(_cached_font_path, size)
    return ImageFont.load_default()


def _font_regular(size: int):
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
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = max(1, math.sqrt(dx * dx + dy * dy))
    ux, uy = dx / length, dy / length
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
    bg_color = (palette["bg_top"][0] + 18, palette["bg_top"][1] + 20, palette["bg_top"][2] + 32)
    border_color = (255, 255, 255) if is_active else palette["accent"]
    border_w = 4 if is_active else 2
    _draw_rounded_rect(draw, (x, y, x + w, y + h), radius=20,
                       fill=bg_color, outline=border_color, width=border_w)
    if is_active:
        draw.rectangle([x + 30, y + 2, x + w - 30, y + 6], fill=palette["accent2"])
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
                        font, color: tuple, max_width: int = W - 120):
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
# Generative AI scene renderer (ported from explainer_visuals.py)
# ---------------------------------------------------------------------------
def _generate_pillow_code(beat_idx: int, hint: dict,
                          cfg: NicheConfig | None = None) -> str:
    """Ask an LLM for Pillow render_frame code (OpenRouter first, Gemini next)."""
    import json as _json

    prompt = f"""You are an expert generative artist using Python and Pillow (ImageDraw).
Write a Python function `def render_frame(t: float, W: int, H: int, draw, pal: dict):` that draws a frame of an animation.
`t` goes from 0.0 to 1.0 representing time. W=1080, H=1920.
`pal` is a color palette dict with keys: bg_top, bg_bot, accent, accent2, text, muted (all RGB tuples).
The visual should explain this concept: {_json.dumps(hint)}

CRITICAL LAYOUT RULE: Keep ALL drawing within y=400 to y=1920 (bottom 87% of frame).
The top 400px (y=0 to y=400) is reserved for burned-in subtitles at the top of the screen — do NOT draw anything there.

DO NOT draw the narration text anywhere in the frame. Do NOT render what the narrator is saying as a caption, subtitle, label, or any text block. The subtitle system handles that separately. Focus 100% on diagrams, animations, and visual elements only.

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
    or_key = (os.getenv("OPENROUTER_API_KEY", "") or "").strip()
    if or_key:
        or_model = os.getenv("OPENROUTER_VISUAL_MODEL",
                             "qwen/qwen3.8-27b:free").strip()
        try:
            log(f"Beat {beat_idx + 1}: Generating Pillow code via OpenRouter ({or_model})...")
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {or_key}",
                         "Content-Type": "application/json"},
                json={"model": or_model,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.2,
                      "max_tokens": 8192},
                timeout=40,
            )
            r.raise_for_status()
            code = r.json()["choices"][0]["message"]["content"]
            code = code.strip().removeprefix("```python").removeprefix("```").removesuffix("```").strip()
            if code:
                log(f"Beat {beat_idx + 1}: Pillow code generated via OpenRouter ({or_model}).")
                return code
        except Exception as e:
            warn(f"Beat {beat_idx + 1}: OpenRouter visual failed ({e}), falling back to Gemini...")
    keys = parse_keys()
    if not keys:
        return ""
    models = list(getattr(cfg, "gemini_models", []) or [])
    if not models:
        models = [m.strip() for m in
                  os.getenv("GEMINI_MODELS",
                            "gemini-3.8-flash,gemini-3.7-flash").split(",")
                  if m.strip()]
    client = GeminiClient(keys, timeout=getattr(cfg, "gemini_timeout", 40) if cfg else 40)
    for model in models:
        try:
            code = client.generate_text(
                model, prompt,
                generation_config={"temperature": 0.2, "maxOutputTokens": 8192})
            code = code.strip().removeprefix("```python").removeprefix("```").removesuffix("```").strip()
            log(f"Beat {beat_idx + 1}: Pillow code generated via Gemini ({model}).")
            return code
        except Exception as e:
            warn(f"Beat {beat_idx + 1}: Gemini model {model} failed ({e}).")
    return ""


def build_scene(beat: dict, beat_idx: int, total_beats: int = 6,
                workdir: Path | str = ".", animate: bool = True,
                cfg: NicheConfig | None = None) -> Path | None:
    """Render one beat's visual via AI-generated Pillow code (mp4 loop)."""
    hint = beat.get("visual_hint") or {}
    if not isinstance(hint, dict):
        hint = {}
    vtype = str(hint.get("type", "")).strip().lower()
    text = str(beat.get("text", "")).strip()
    if vtype == "stock_footage" or not vtype or not animate:
        return None
    log(f"Beat {beat_idx + 1}: Generating custom Pillow code for '{hint.get('description', '')}'...")
    code_str = _generate_pillow_code(beat_idx, hint, cfg)
    if not code_str:
        warn(f"Beat {beat_idx + 1}: AI failed to generate Pillow code.")
        return None
    out_mp4 = Path(workdir) / f"explainer_scene_{beat_idx}.mp4"
    pal = _get_palette(beat_idx)
    namespace = globals().copy()
    namespace["math"] = math
    namespace["random"] = random
    try:
        exec(code_str, namespace)
        render_func = namespace.get("render_frame")
        if not render_func:
            raise ValueError("No render_frame function defined.")
    except Exception as e:
        warn(f"Beat {beat_idx + 1}: Failed to parse generated Pillow code:\n{code_str}\nError: {e}")
        return None
    num_frames, fps = 60, 30
    base = Image.new("RGB", (W, H))
    _draw_gradient_bg(base, pal["bg_top"], pal["bg_bot"])
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
           "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(fps),
           "-i", "-", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", str(out_mp4)]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception as e:
        warn(f"Beat {beat_idx + 1}: FFmpeg error: {e}")
        return None
    success = True
    for f in range(num_frames):
        t = f / float(num_frames)
        frame = base.copy()
        draw = ImageDraw.Draw(frame)
        try:
            render_func(t, W, H, draw, pal)
        except Exception as e:
            warn(f"Beat {beat_idx + 1}: Error in generated render_frame at t={t}: {e}")
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


def build_all_scenes(beats: list[dict], workdir: Path | str = ".",
                     animate: bool = True,
                     cfg: NicheConfig | None = None) -> list[Path | None]:
    return [build_scene(b, i, len(beats), workdir, animate, cfg)
            for i, b in enumerate(beats)]


# ---------------------------------------------------------------------------
# Stock selection (explainer path: exact query only, video x2 vs best photo)
# ---------------------------------------------------------------------------
def _query_words(q: str) -> list[str]:
    """Lowercase alphanum tokens minus stopwords."""
    return [w for w in re.findall(r"[a-z0-9]+", (q or "").lower())
            if w not in _QUERY_STOPWORDS]


def _query_variants(q: str) -> list[str]:
    """Full query + drop-last-word backoff until 2 words remain (max 3)."""
    full = (q or "").strip()
    if not full:
        return []
    words = full.split()
    if len(words) <= 2:
        return [full]
    variants = [full]
    while len(words) > 2 and len(variants) < 3:
        words = words[:-1]
        variants.append(" ".join(words))
    seen: list[str] = []
    for v in variants:
        if v not in seen:
            seen.append(v)
    return seen


def _score_candidate(alt: str, url: str, words: list[str]) -> int:
    """Score candidate text against query words (2x alt + url)."""
    h_alt = (alt or "").lower()
    h_url = (url or "").lower()
    tokens_alt = set(re.findall(r"[a-z0-9]+", h_alt))
    tokens_url = set(re.findall(r"[a-z0-9]+", h_url))
    alt_hits = sum(1 for w in words if w and (w in tokens_alt or (len(w) >= 6 and w in h_alt)))
    url_hits = sum(1 for w in words if w and (w in tokens_url or (len(w) >= 6 and w in h_url)))
    return 2 * alt_hits + url_hits


def _download(url: str, dest: Path, tag: str) -> bool:
    """Stream-download; deletes partials. False on failure."""
    try:
        with requests.get(url, stream=True, timeout=120) as d:
            d.raise_for_status()
            with open(dest, "wb") as f:
                shutil.copyfileobj(d.raw, f)
        return True
    except Exception as e:
        if dest.exists():
            dest.unlink()
        warn(f"Pexels {tag} download failed ({e}).")
        return False


def _best_video(query: str, api_key: str, used: set | None,
                min_width: int) -> tuple[Path | None, int, Any]:
    """Best literal-match stock video. Returns (dest|None, score, key|None)."""
    if not api_key:
        return None, 0, None
    try:
        words = _query_words(query)
        threshold = min(2, len(words)) if len(words) > 1 else 1
        best_video, best_pick, best_score = None, None, -1
        for variant in _query_variants(query):
            r = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": api_key},
                params={"query": variant, "per_page": 8,
                        "orientation": "portrait", "size": "medium"},
                timeout=30)
            r.raise_for_status()
            for v in r.json().get("videos", []):
                if used is not None and v.get("id") in used:
                    continue
                url = v.get("url", "")
                v_tags = " ".join(t.get("name", "") if isinstance(t, dict) else str(t)
                                  for t in v.get("tags", []))
                haystack = f"{url} {v_tags}".strip() if v_tags else url
                score = _score_candidate(v_tags or url, url, words)
                if score < threshold:
                    continue
                cands = sorted((f for f in v.get("video_files", []) if f.get("link")),
                               key=lambda f: f.get("width", 0))
                big_enough = [f for f in cands if f.get("width", 0) >= min_width]
                pick = (big_enough or cands)
                if not pick or score <= best_score:
                    continue
                best_score, best_video, best_pick = score, v, pick[0]
            if best_video is not None and best_score >= threshold:
                break
        if best_video is None or best_pick is None:
            return None, 0, None
        dest = CACHE / f"pexels_{best_video['id']}.mp4"
        if not dest.exists() and not _download(best_pick["link"], dest, "video"):
            return None, 0, None
        return dest, best_score, best_video["id"]
    except Exception as e:
        warn(f"Pexels failed for '{query}' ({e}).")
    return None, 0, None


def _best_photos(query: str, api_key: str, used: set | None, n: int,
                 min_width: int) -> list[tuple]:
    """Top-N literal-match stock photos. Returns [(dest, score, key, alt)]."""
    if n <= 0 or not api_key:
        return []
    try:
        words = _query_words(query)
        threshold = min(2, len(words)) if len(words) > 1 else 1
        pool: dict = {}
        for variant in _query_variants(query):
            r = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": api_key},
                params={"query": variant, "per_page": 10,
                        "orientation": "portrait", "size": "large"},
                timeout=30)
            r.raise_for_status()
            for p in r.json().get("photos", []):
                pid = p.get("id")
                if pid in pool:
                    continue
                if used is not None and f"photo_{pid}" in used:
                    continue
                alt, url = p.get("alt", ""), p.get("url", "")
                score = _score_candidate(alt, url, words)
                if score < threshold:
                    continue
                w = p.get("width", 0) or 0
                if w and w < min_width:
                    continue
                src = p.get("src", {})
                if not src.get("original"):
                    continue
                h = p.get("height", 0) or 0
                rank = abs(w / h - 9 / 16) if w and h else 1.0
                pool[pid] = (p, score, (w or 0) * (h or 0), rank)
            if len(pool) >= n:
                break
        ranked = sorted(pool.values(), key=lambda t: (-t[1], -t[2], t[3]))
        out = []
        for (p, score, _a, _r) in ranked[:max(1, n)]:
            dest = CACHE / f"pexels_{p['id']}.jpg"
            if not dest.exists():
                if not _download(p["src"]["original"], dest, "photo"):
                    continue
            out.append((dest, score, f"photo_{p['id']}", p.get("alt", "")))
        return out
    except Exception as e:
        warn(f"Pexels photo failed for '{query}' ({e}).")
    return []


def _split_beat(text: str, n: int) -> list[str]:
    """Split beat narration into n word-even parts for mid-beat cuts."""
    words = text.split()
    if n <= 1 or len(words) <= n:
        return [text]
    size = len(words) / n
    parts = [" ".join(words[int(i * size):int((i + 1) * size)]).strip()
             for i in range(n)]
    return [p for p in parts if p] or [text]


def _stock_offset(stock: Path, part_idx: int, parts: int) -> float | None:
    """Seek offset so mid-beat cuts show different stock footage."""
    if part_idx == 0 or parts <= 1:
        return None
    try:
        dur = ffprobe_duration(stock)
    except Exception:
        return None
    return max(0.0, dur * part_idx / parts)


def beat_card(i: int, text: str, workdir: Path | str = ".") -> Path:
    """Pillow gap-filler card carrying the beat's story text."""
    from PIL import Image as _Image, ImageDraw as _Draw  # lazy like core
    colors = [(18, 18, 40), (40, 18, 30), (18, 40, 35), (35, 35, 18)]
    img = _Image.new("RGB", (W, H), colors[i % len(colors)])
    d = _Draw.Draw(img)
    d.multiline_text((80, 800), textwrap.fill(text[:220], width=28),
                     fill=(255, 255, 255), spacing=8)
    p = Path(workdir) / f"slide_beat_{i}.png"
    img.save(p)
    return p


# ---------------------------------------------------------------------------
# Cut planning (mirrors core._visuals_for_explainer caller expectations)
# ---------------------------------------------------------------------------
def _plan_cuts(beats: list[dict], cfg: NicheConfig,
               workdir: Path) -> tuple[list[tuple], list[str], list[str]]:
    """Per beat: diagram -> exact Pexels (video x2 vs photo) -> card.

    Returns (cuts, credits, used_ids); cuts are (clip, seek|None, beat_idx).
    """
    api_key = (os.getenv("PEXELS_API_KEY", "") or "").strip()
    min_width = getattr(cfg, "pexels_min_width", 1080)
    stills_n = max(1, getattr(cfg, "stills_per_beat", 2))
    cuts: list[tuple] = []
    credits: list[str] = []
    used: set = set()
    used_ids: list[str] = []
    n_diagram = n_stock = n_card = 0
    for i, b in enumerate(beats or []):
        parts = _split_beat(b.get("text", ""), stills_n)
        hint = b.get("visual_hint") or {}
        vtype = str(hint.get("type", "")).strip().lower()
        query = b.get("query", "")
        scene = None
        if vtype and vtype != "stock_footage":
            try:
                scene = build_scene(b, i, len(beats), workdir, cfg=cfg)
            except Exception as e:
                warn(f"Explainer visual failed for beat {i + 1} ({e}), trying stock.")
        if scene is not None and scene.exists():
            log(f"Beat {i + 1} ('{query}'): diagram tier=explainer winner={scene.name}.")
            n_diagram += len(parts)
            cuts += [(scene, None, i)] * len(parts)
            continue
        if api_key and query:
            vpath, vraw, vkey = _best_video(query, api_key, used, min_width)
            plist = _best_photos(query, api_key, used, len(parts), min_width)
            if plist and (vpath is None or plist[0][1] > vraw * 2):
                for (_, _, k, _a) in plist:
                    used.add(k)
                    used_ids.append(str(k))
                credits.append(f"Pexels photo: {query}")
                n_stock += len(parts)
                log(f"Beat {i + 1} ('{query}'): {len(parts)}xphoto tier=exact.")
                for j in range(len(parts)):
                    clip = plist[j][0] if j < len(plist) else plist[0][0]
                    cuts.append((clip, None, i))
                continue
            if vpath is not None:
                used.add(vkey)
                used_ids.append(str(vkey))
                credits.append(f"Pexels video: {query}")
                n_stock += len(parts)
                log(f"Beat {i + 1} ('{query}'): video tier=exact.")
                for j in range(len(parts)):
                    cuts.append((vpath, _stock_offset(vpath, j, len(parts)), i))
                continue
        fb_done = False
        try:
            fb = {"text": b.get("text", ""), "query": query,
                  "visual_hint": {"type": "text_card", "label": "",
                                  "description": b.get("text", "")[:200]}}
            scene = build_scene(fb, i, len(beats), workdir, cfg=cfg)
            if scene is not None and scene.exists():
                n_diagram += len(parts)
                log(f"Beat {i + 1} ('{query}'): text_card fallback.")
                cuts += [(scene, None, i)] * len(parts)
                fb_done = True
        except Exception:
            pass
        if fb_done:
            continue
        n_card += len(parts)
        warn(f"Beat {i + 1} ('{query}'): emergency beat_card fallback.")
        for j, part in enumerate(parts):
            cuts.append((beat_card(i * stills_n + j, part, workdir), None, i))
    log(f"Explainer visuals: {len(cuts)} cuts for {len(beats or [])} beats "
        f"({n_diagram} diagram, {n_stock} stock, {n_card} card).")
    return cuts, credits, used_ids


# ---------------------------------------------------------------------------
# Line-caption SRT -> ASS (yellow bold, top-anchored below title; not word-pop)
# ---------------------------------------------------------------------------
def _srt_ts(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, rem = divmod(rem, 60)
    s, ms = divmod(rem, 1)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(ms * 1000):03d}"


def _write_line_srt(beats: list[dict], durations: list[float],
                    out_srt: Path) -> None:
    """One cue per beat (wrapped to 2 lines); timings sum to narration."""
    lines = []
    t, n = 0.0, 1
    for b, d in zip(beats, durations):
        cue = "\n".join(textwrap.wrap(" ".join(b.get("text", "").split()),
                                      width=38)[:4])
        lines += [str(n), f"{_srt_ts(t)} --> {_srt_ts(t + d)}", cue, ""]
        t += d
        n += 1
    out_srt.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _srt_to_ass(srt: Path, ass: Path) -> None:
    """Line-caption ASS: yellow bold, top-center below the title zone."""
    import re as _re
    cues = []
    for block in srt.read_text(encoding="utf-8").strip().split("\n\n"):
        rows = block.strip().splitlines()
        if len(rows) < 3:
            continue
        m = _re.match(r"(\d+):(\d+):(\d+),(\d+)\s*-->\s*"
                      r"(\d+):(\d+):(\d+),(\d+)", rows[1])
        if not m:
            continue
        s = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
             + int(m.group(3)) + int(m.group(4)) / 1000.0)
        e = (int(m.group(5)) * 3600 + int(m.group(6)) * 60
             + int(m.group(7)) + int(m.group(8)) / 1000.0)
        text = r"\N".join(rows[2:])
        cues.append((s, e, text))

    def _ts(sec: float) -> str:
        sec = max(0.0, float(sec))
        return f"{int(sec // 3600)}:{int((sec % 3600) // 60):02d}:{int(sec % 60):02d}.{int(round((sec % 1) * 100)):02d}"

    events = [f"Dialogue: 0,{_ts(s)},{_ts(e)},Line,,0,0,0,,{t}"
              for s, e, t in cues]
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\nYCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Line,Arial,60,&H0000FFFF&,&H000019FF,&HFF000000,&H99000000,"
        "-1,0,0,0,100,100,0,0,1,3,1,8,10,10,420,1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Assembly: cuts -> concat -> mux narration -> burn line captions
# ---------------------------------------------------------------------------
def _cut_durations(beats: list[dict], durations: list[float],
                   cuts: list[tuple]) -> list[float]:
    """Split each beat's duration evenly across its cuts (2.0s floor each)."""
    per_beat: dict[int, list[int]] = {}
    for ci, (_c, _s, bi) in enumerate(cuts):
        per_beat.setdefault(bi, []).append(ci)
    out = [0.0] * len(cuts)
    for bi, idxs in per_beat.items():
        share = durations[bi] / len(idxs) if bi < len(durations) else 2.0
        for ci in idxs:
            out[ci] = max(2.0, share) if len(idxs) > 1 else (durations[bi] if bi < len(durations) else 2.0)
    return out


def build_visuals(content: Content, narration: NarrationResult,
                  cfg: NicheConfig, workdir: Path) -> VisualsResult:
    """Plan cuts, assemble 1080x1920 mp4, mux narration, burn line captions."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    beats = list((content.extras or {}).get("beats") or [])
    if not beats:
        raise ValueError("Explainer visuals need beats in content.extras.")
    cuts, credits, used_ids = _plan_cuts(beats, cfg, workdir)
    beat_durs = list((narration.extras or {}).get("beat_durations") or [])
    if beat_durs and len(beat_durs) == len(beats) and sum(beat_durs) > 0:
        scale = narration.duration / sum(beat_durs)
        durations = [d * scale for d in beat_durs]
    else:
        total_words = sum(len(b.get("text", "").split()) for b in beats) or 1
        durations = [narration.duration * len(b.get("text", "").split()) / total_words
                     for b in beats]
    seg_durs = _cut_durations(beats, durations, cuts)
    cmd: list[str] = []
    filter_parts: list[str] = []
    for ci, ((clip, seek, _bi), dur) in enumerate(zip(cuts, seg_durs)):
        suf = Path(clip).suffix.lower()
        if suf in (".jpg", ".jpeg", ".png"):
            cmd += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(clip)]
            if suf == ".jpg":
                # Gentle Ken Burns push-in for stock photos.
                frames = max(1, int(round(dur * 30)))
                filter_parts.append(
                    f"[{ci}:v]scale=1296:2304,zoompan=z='1+0.08*on/{frames}':"
                    f"d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"s=1080x1920:fps=30,setsar=1,fps=30[v{ci}]")
            else:
                filter_parts.append(
                    f"[{ci}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                    f"crop=1080:1920,setsar=1,fps=30[v{ci}]")
        else:
            if seek:
                cmd += ["-ss", f"{seek:.2f}"]
            cmd += ["-t", f"{dur:.3f}", "-i", str(clip)]
            filter_parts.append(
                f"[{ci}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,setsar=1,fps=30[v{ci}]")
    filter_parts.append("".join(f"[v{ci}]" for ci in range(len(cuts)))
                        + f"concat=n={len(cuts)}:v=1:a=0[vcat]")
    total = sum(seg_durs)
    srt = workdir / "captions.srt"
    _write_line_srt(beats, durations, srt)
    ass = workdir / "captions.ass"
    _srt_to_ass(srt, ass)
    mp4 = workdir / "short.mp4"
    audio_idx = len(cuts)
    cmd += ["-i", str(narration.audio)]
    burn_ok = bool(find_font([r"C:\Windows\Fonts\arialbd.ttf",
                              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
                   or True) and has_ffmpeg_filter("subtitles")
    base = (f"{';'.join(filter_parts)}; [vcat]format=yuv420p"
            + (f",{subtitles_filter(ass)}" if burn_ok else "") + "[vout]")
    full = (["ffmpeg", "-y", *cmd,
             "-filter_complex", base,
             "-map", "[vout]", "-map", f"{audio_idx}:a",
             "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast",
             "-c:a", "aac", "-shortest", str(mp4)])
    try:
        run_ffmpeg(full, "explainer-assemble")
    except Exception as e:
        if burn_ok:
            warn(f"Subtitled render failed ({e}), retrying without subs.")
            base = f"{';'.join(filter_parts)}; [vcat]format=yuv420p[vout]"
            run_ffmpeg(["ffmpeg", "-y", *cmd, "-filter_complex", base,
                        "-map", "[vout]", "-map", f"{audio_idx}:a",
                        "-t", f"{total:.3f}", "-c:v", "libx264",
                        "-preset", "veryfast", "-c:a", "aac",
                        "-shortest", str(mp4)], "explainer-assemble-n subs")
        else:
            raise
    if not burn_ok:
        warn("Caption burn skipped (no subtitles filter); muxed without subs.")
    return VisualsResult(mp4=mp4, srt=srt, credits=credits, used_ids=used_ids,
                         extras={"cuts": len(cuts), "beats": len(beats),
                                 "durations": durations,
                                 "subtitles_burned": burn_ok,
                                 "topic": (content.extras or {}).get("topic", "")})
