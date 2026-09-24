"""Template config: per-niche knobs with NICHE_ env overrides."""

from dataclasses import dataclass

from niches.base import NicheConfig, env_float, env_str


@dataclass
class TemplateConfig(NicheConfig):
    """Example knobs; copy and rename per niche."""
    voice: str = ""
    target_wpm: float = 145.0

    @classmethod
    def from_env(cls, niche_name: str = "template") -> "TemplateConfig":
        """Read NICHE_VOICE / NICHE_TARGET_WPM (bare names as fallback)."""
        return cls(niche_name=niche_name or env_str("NAME", "template"),
                   voice=env_str("VOICE", ""),
                   target_wpm=env_float("TARGET_WPM", 145.0))
