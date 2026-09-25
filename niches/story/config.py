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

DEFAULT_TEXT_MODELS = ("gemini-3.8-flash", "gemini-3.7-flash",
                       "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                       "gemini-2.5-flash-lite", "gemini-2.5-flash")
DEFAULT_TTS_MODELS = ("gemini-2.5-flash-preview-tts",
                      "gemini-3.1-flash-tts-preview")


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
    # tts_model is kept for backward compat and always equals tts_models[0].
    tts_model: str = "gemini-3.1-flash-tts-preview"
    tts_models: tuple[str, ...] = DEFAULT_TTS_MODELS
    # Gemini text models tried in order for story generation (first win).
    story_models: tuple[str, ...] = DEFAULT_TEXT_MODELS
    tts_voices: tuple[str, ...] = ("Kore", "Charon", "Puck")
    # TTS engine: edge (default, fast keyless single-voice preview
    # with word-boundary timings) or gemini (chunked synth + duration fit).
    tts_engine: str = "edge"
    # Provider order: edge first, gemini fallback when edge exhausts.
    tts_provider_order: str = "edge,gemini"
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
    # Clips fetched per Pexels query (distinct videos). Cap math for a
    # full run: ~20 queries x K=2 x ~10MB worst case ≈ ~400MB transient
    # (typical portrait clips are smaller; the id cache skips re-downloads
    # on retry). Lower to 1 to restore one-clip-per-query behavior.
    clips_per_query: int = 2
    # Optional BGM bed file (preview tools); empty = pool/drone path.
    bed_path: str = ""
    # Repo-root-relative glob for the random per-video BGM pool.
    bgm_glob: str = "bgm*.mp3"
    story_genres: tuple[str, ...] = DEFAULT_GENRES
    # Word-timing model for the FINAL mix only.
    whisper_model: str = "base"
    # Measured speech rate (words/sec); history-calibrated at runtime.
    history_wps: float = 2.9
    # Length overshoot multiplier on the edge-aware word budget: the edge
    # TTS path runs faster than history wps implies, so aim above the bare
    # seconds*wps floor (default 1.08 targets mid-range, not the floor).
    length_overshoot: float = 1.08
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

        def _story_models() -> tuple[str, ...]:
            """Bare GEMINI_MODELS wins when set, else STORY_MODELS, else
            bare MODELS, else DEFAULT_TEXT_MODELS."""
            bare_override = (os.getenv("GEMINI_MODELS") or "").strip()
            if bare_override:
                pool = tuple(g.strip() for g in bare_override.split(",")
                             if g.strip())
                if pool:
                    return pool
            raw = env_str("MODELS", "", "STORY_")
            if raw.strip():
                pool = tuple(g.strip() for g in raw.split(",")
                             if g.strip())
                if pool:
                    return pool
            return DEFAULT_TEXT_MODELS

        def _tts_models() -> tuple[str, ...]:
            """STORY_TTS_MODELS wins, then STORY_TTS_MODEL single, then
            TTS_MODELS bare, then TTS_MODEL bare, else DEFAULT_TTS_MODELS."""
            for key, single in (("STORY_TTS_MODELS", False),
                                ("STORY_TTS_MODEL", True),
                                ("TTS_MODELS", False),
                                ("TTS_MODEL", True)):
                raw = (os.getenv(key) or "").strip()
                if not raw:
                    continue
                if single:
                    return (raw,)
                pool = tuple(g.strip() for g in raw.split(",")
                             if g.strip())
                if pool:
                    return pool
            return DEFAULT_TTS_MODELS

        # TARGET_MIN/MAX_SECONDS aliases win over TARGET_MIN/MAX.
        tmin = env_float("TARGET_MIN_SECONDS",
                         env_float("TARGET_MIN", 100.0, "STORY_"), "STORY_")
        tmax = env_float("TARGET_MAX_SECONDS",
                         env_float("TARGET_MAX", 120.0, "STORY_"), "STORY_")
        tts_pool = _tts_models()

        return cls(
            niche_name=niche_name or MODE,
            target_min=tmin,
            target_max=tmax,
            target_min_seconds=tmin,
            target_max_seconds=tmax,
            max_seconds=env_float("MAX_SECONDS", 130.0, "STORY_"),
            narration_speed=env_float("NARRATION_SPEED", 1.15, "STORY_"),
            tts_model=tts_pool[0],
            tts_models=tts_pool,
            story_models=_story_models(),
            tts_voices=_tuple("TTS_VOICES", ("Kore", "Charon", "Puck")),
            tts_engine=env_str("TTS_ENGINE", "edge", "STORY_").strip().lower() or "edge",
            tts_provider_order=env_str("TTS_PROVIDER_ORDER", "edge,gemini",
                                       "STORY_").strip().lower() or "edge,gemini",
            edge_voice=env_str("EDGE_VOICE", "en-US-AvaNeural", "STORY_") or "en-US-AvaNeural",
            edge_voices=_edge_pool(),
            edge_rate=env_str("EDGE_RATE", "+20%", "STORY_") or "+20%",
            seg_min=env_float("SEG_MIN", 2.5, "STORY_"),
            seg_max=env_float("SEG_MAX", 4.5, "STORY_"),
            bg_speed_min=env_float("BG_SPEED_MIN", 1.15, "STORY_"),
            bg_speed_max=env_float("BG_SPEED_MAX", 1.5, "STORY_"),
            caption_upper=env_bool("CAPTION_UPPER", False, "STORY_"),
            card_seconds=env_float("CARD_SECONDS", 3.0, "STORY_"),
            channel_handle=env_str("CHANNEL_HANDLE", "@UnhingedStories", "STORY_") or "@UnhingedStories",
            avatar_path=env_str("AVATAR_PATH", "assets/avatar.png", "STORY_") or "assets/avatar.png",
            bg_dir=env_str("BG_DIR", "assets/backgrounds", "STORY_") or "assets/backgrounds",
            pexels_key=env_str("PEXELS_API_KEY", "", "STORY_"),
            clips_per_query=env_int("CLIPS_PER_QUERY", 2, "STORY_"),
            bed_path=env_str("BED_PATH", "", "STORY_"),
            bgm_glob=env_str("BGM_GLOB", "bgm*.mp3", "STORY_") or "bgm*.mp3",
            story_genres=_tuple("STORY_GENRES", DEFAULT_GENRES),
            whisper_model=env_str("WHISPER_MODEL", "base", "STORY_") or "base",
            history_wps=env_float("HISTORY_WPS", 2.9, "STORY_"),
            length_overshoot=min(1.20, max(1.0, env_float(
                "LENGTH_OVERSHOOT", 1.08, "STORY_"))),
            timeout=env_int("TIMEOUT", 40, "STORY_"),
            tts_timeout=env_int("TTS_TIMEOUT", 90, "STORY_"),
            tries=env_int("TRIES", 2, "STORY_"),
            backoff=env_int("BACKOFF", 5, "STORY_"),
            gemini_keys=tuple(parse_keys()),
        )
