"""Explainer config: env-overridable knobs (EXPLAINER_ prefix wins)."""

from dataclasses import dataclass, field

from niches.base import NicheConfig, env_bool, env_float, env_int, env_str

_DEFAULT_MODELS = ("gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,"
                   "gemini-3.5-flash,gemini-3.5-flash-lite,"
                   "gemini-3.1-flash-lite,gemini-2.5-flash,"
                   "gemini-2.5-flash-lite")
_DEFAULT_TTS_MODELS = ("gemini-3.1-flash-tts-preview,"
                       "gemini-2.5-flash-preview-tts")
_DEFAULT_OPENROUTER_MODELS = ("meta-llama/llama-3.3-70b-instruct:free,"
                              "openai/gpt-oss-120b:free")


def _env_list(name: str, default: str, prefix: str = "EXPLAINER_") -> list[str]:
    """Comma-separated env list; prefixed name wins over bare name."""
    raw = env_str(name, "", prefix)
    if not raw.strip():
        raw = default
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class ExplainerConfig(NicheConfig):
    """Explainer knobs; defaults mirror niche_explainer.py + core.py."""
    voice: str = "en-US-ChristopherNeural"
    tts_rate: str = "-5%"
    tts_pitch: str = "-10Hz"
    tts_volume: str = "+0%"
    tts_engine: str = "gemini"
    gemini_models: list[str] = field(default_factory=list)
    gemini_tts_models: list[str] = field(default_factory=list)
    gemini_voices: list[str] = field(default_factory=lambda: ["Charon"])
    gemini_target_wpm: int = 145
    gemini_tts_pace_sec: int = 15
    gemini_timeout: int = 40
    gemini_tts_timeout: int = 90
    ai_topics: bool = True
    ai_topic_tries: int = 2
    stills_per_beat: int = 2
    min_audio_sec: float = 10.0
    max_audio_sec: float = 180.0
    pexels_min_width: int = 1080
    history_keep: int = 110
    history_prompt_n: int = 20
    dedup_window: int = 110
    similarity_retries: int = 2
    openrouter_models: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, niche_name: str = "explainer") -> "ExplainerConfig":
        """Build from env (EXPLAINER_ prefix wins, bare names fallback)."""
        p = "EXPLAINER_"
        return cls(
            niche_name=niche_name or env_str("NAME", "explainer", p),
            voice=env_str("VOICE", "en-US-ChristopherNeural", p),
            tts_rate=env_str("TTS_RATE", "-5%", p),
            tts_pitch=env_str("TTS_PITCH", "-10Hz", p),
            tts_volume=env_str("TTS_VOLUME", "+0%", p),
            tts_engine=env_str("TTS_ENGINE", "gemini", p).strip().lower(),
            gemini_models=_env_list("GEMINI_MODELS", _DEFAULT_MODELS, p),
            gemini_tts_models=_env_list("GEMINI_TTS_MODELS",
                                        _DEFAULT_TTS_MODELS, p),
            gemini_voices=_env_list("GEMINI_TTS_VOICES", "Charon", p),
            gemini_target_wpm=env_int("GEMINI_TTS_TARGET_WPM", 145, p),
            gemini_tts_pace_sec=env_int("GEMINI_TTS_PACE_SEC", 15, p),
            gemini_timeout=env_int("GEMINI_TIMEOUT", 40, p),
            gemini_tts_timeout=env_int("GEMINI_TTS_TIMEOUT", 90, p),
            ai_topics=env_str("AI_TOPICS", "on", p).strip().lower()
            not in ("0", "off", "false", "no"),
            ai_topic_tries=env_int("AI_TOPIC_TRIES", 2, p),
            stills_per_beat=env_int("STILLS_PER_BEAT", 2, p),
            min_audio_sec=env_float("MIN_AUDIO_SEC", 10.0, p),
            max_audio_sec=env_float("MAX_AUDIO_SEC", 180.0, p),
            pexels_min_width=env_int("PEXELS_MIN_WIDTH", 1080, p),
            history_keep=env_int("HISTORY_KEEP", 110, p),
            history_prompt_n=env_int("HISTORY_PROMPT_N", 20, p),
            dedup_window=env_int("DEDUP_WINDOW", 110, p),
            similarity_retries=env_int("SIMILARITY_RETRIES", 2, p),
            openrouter_models=_env_list("OPENROUTER_MODELS",
                                        _DEFAULT_OPENROUTER_MODELS, p),
        )
