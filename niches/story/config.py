"""Story config: 100-120s Reddit-story knobs with STORY_ env overrides.

STORY_<name> wins, then bare <name>, else the default below (see
niches.base env_* helpers). Secrets are read at from_env() time and
never hardcoded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from engine.gemini_client import parse_keys
from niches.base import (NicheConfig, env_bool, env_float, env_int,
                         env_str)

MODE = "story"

DEFAULT_GENRES = ("AITA", "revenge", "workplace", "relationship",
                  "scary", "never-again")


@dataclass
class StoryConfig(NicheConfig):
    """Per-run knobs for the story niche."""
    # Duration targets (seconds of final narration).
    target_min: float = 100.0
    target_max: float = 120.0
    # Aliases for the same window (TARGET_MIN/MAX_SECONDS win over
    # TARGET_MIN/MAX; all four fields resolve to identical values).
    target_min_seconds: float = 100.0
    target_max_seconds: float = 120.0
    # YouTube Shorts ceiling confirmed: ~3 minutes for vertical uploads,
    # so 100-120s with 130s trim headroom fits comfortably (well under
    # the 180s guard ceiling). Never assume a <=60s cap anywhere here.
    max_seconds: float = 130.0
    # Narration render speed multiplier.
    narration_speed: float = 1.15
    # Gemini TTS speech model + voice pool (rotated per video).
    tts_model: str = "gemini-3.1-flash-tts-preview"
    tts_voices: tuple[str, ...] = ("Kore", "Charon", "Puck")
    # TTS engine: gemini (default, chunked synth + duration fit) or edge
    # (fast keyless single-voice preview with word-boundary timings).
    tts_engine: str = "gemini"
    # Provider order (edge-only; gemini fallback is NOT implemented yet).
    tts_provider_order: str = "edge"
    edge_voice: str = "en-US-AvaNeural"
    # STORY_EDGE_VOICES (plural) wins over STORY_EDGE_VOICE (singular,
    # kept working as a one-voice fallback); unset means the default pool.
    edge_voices: tuple[str, ...] = ("en-US-AndrewNeural", "en-US-AriaNeural",
                                    "en-US-GuyNeural")
    edge_rate: str = "+20%"
    # BG segment length bounds (seconds) + playback speed range.
    seg_min: float = 2.5
    seg_max: float = 4.5
    bg_speed_min: float = 1.15
    bg_speed_max: float = 1.5
    # Captions: 1/true = uppercase word-pop.
    caption_upper: bool = False
    # Card floor: overlay shows max(card_seconds, card_line_end + pad).
    card_seconds: float = 3.0
    card_pad: float = 0.4
    # Card branding.
    channel_handle: str = "@UnhingedStories"
    avatar_path: str = "assets/avatar.png"
    # Clip sources.
    bg_dir: str = "assets/backgrounds"
    pexels_key: str = ""
    pexels_cache_dir: str = "assets/pexels_cache"
    # Optional BGM bed file (preview tools); empty = synthesized drone.
    bed_path: str = ""
    story_genres: tuple[str, ...] = DEFAULT_GENRES
    # Word-timing model for the FINAL mix only.
    whisper_model: str = "base"
    # Measured speech rate (words/sec); history-calibrated at runtime.
    history_wps: float = 2.9
    # Planner floors.
    min_clips: int = 12
    min_segments: int = 25
    max_segments: int = 40
    # Caption style.
    ass_fontsize: int = 120
    # Network knobs.
    timeout: int = 40
    tts_timeout: int = 90
    tries: int = 2
    backoff: int = 5
    similarity_retries: int = 2
    history_prompt_n: int = 20
    dedup_window: int = 110
    gemini_keys: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, niche_name: str = MODE) -> "StoryConfig":
        """Read STORY_* (bare names as fallback)."""
        def _tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            raw = env_str(name, ",".join(default), "STORY_")
            pool = tuple(g.strip() for g in raw.split(",") if g.strip())
            return pool or default

        def _edge_pool() -> tuple[str, ...]:
            """STORY_EDGE_VOICES wins; explicit STORY_EDGE_VOICE is the
            one-voice fallback; unset means the default pool."""
            plural = tuple(v.strip() for v in
                           env_str("EDGE_VOICES", "", "STORY_").split(",")
                           if v.strip())
            if plural:
                return plural
            if ("STORY_EDGE_VOICE" in os.environ
                    or "EDGE_VOICE" in os.environ):
                single = env_str("EDGE_VOICE", "en-US-AvaNeural",
                                 "STORY_").strip()
                if single:
                    return (single,)
            return ("en-US-AndrewNeural", "en-US-AriaNeural",
                    "en-US-GuyNeural")

        # TARGET_MIN/MAX_SECONDS aliases win over TARGET_MIN/MAX.
        tmin = env_float("TARGET_MIN_SECONDS",
                         env_float("TARGET_MIN", 100.0, "STORY_"), "STORY_")
        tmax = env_float("TARGET_MAX_SECONDS",
                         env_float("TARGET_MAX", 120.0, "STORY_"), "STORY_")

        return cls(
            niche_name=niche_name or MODE,
            target_min=tmin,
            target_max=tmax,
            target_min_seconds=tmin,
            target_max_seconds=tmax,
            max_seconds=env_float("MAX_SECONDS", 130.0, "STORY_"),
            narration_speed=env_float("NARRATION_SPEED", 1.15, "STORY_"),
            tts_model=env_str("TTS_MODEL", "gemini-3.1-flash-tts-preview", "STORY_"),
            tts_voices=_tuple("TTS_VOICES", ("Kore", "Charon", "Puck")),
            tts_engine=env_str("TTS_ENGINE", "gemini", "STORY_").strip().lower() or "gemini",
            tts_provider_order=env_str("TTS_PROVIDER_ORDER", "edge",
                                       "STORY_").strip().lower() or "edge",
            edge_voice=env_str("EDGE_VOICE", "en-US-AvaNeural", "STORY_"),
            edge_voices=_edge_pool(),
            edge_rate=env_str("EDGE_RATE", "+20%", "STORY_"),
            seg_min=env_float("SEG_MIN", 2.5, "STORY_"),
            seg_max=env_float("SEG_MAX", 4.5, "STORY_"),
            bg_speed_min=env_float("BG_SPEED_MIN", 1.15, "STORY_"),
            bg_speed_max=env_float("BG_SPEED_MAX", 1.5, "STORY_"),
            caption_upper=env_bool("CAPTION_UPPER", False, "STORY_"),
            card_seconds=env_float("CARD_SECONDS", 3.0, "STORY_"),
            channel_handle=env_str("CHANNEL_HANDLE", "@UnhingedStories", "STORY_"),
            avatar_path=env_str("AVATAR_PATH", "assets/avatar.png", "STORY_"),
            bg_dir=env_str("BG_DIR", "assets/backgrounds", "STORY_"),
            pexels_key=env_str("PEXELS_API_KEY", "", "STORY_"),
            bed_path=env_str("BED_PATH", "", "STORY_"),
            story_genres=_tuple("STORY_GENRES", DEFAULT_GENRES),
            whisper_model=env_str("WHISPER_MODEL", "base", "STORY_"),
            history_wps=env_float("HISTORY_WPS", 2.9, "STORY_"),
            timeout=env_int("TIMEOUT", 40, "STORY_"),
            tts_timeout=env_int("TTS_TIMEOUT", 90, "STORY_"),
            tries=env_int("TRIES", 2, "STORY_"),
            backoff=env_int("BACKOFF", 5, "STORY_"),
            gemini_keys=tuple(parse_keys()),
        )
