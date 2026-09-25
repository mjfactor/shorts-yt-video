"""Shared render engine: generic building blocks for Shorts niches.

Each module holds policy-free helpers (no baked-in voices, speeds, or
queries — the niche passes those as params). Never imports core.py or
any niche; niches import from here.
"""

__all__ = [
    "ffmpeg_utils",
    "gemini_client",
    "tts_cache",
    "align",
    "ass",
    "srt",
    "pexels",
    "overlay_card",
    "audio_mix",
]
