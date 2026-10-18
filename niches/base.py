"""Shared niche contracts: Niche ABC + stage result dataclasses.

Stage split: generate_content (script) -> narrate (audio) ->
build_visuals (mp4/srt). Engine helpers live in engine/*; core
is never imported here.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Content", "NarrationResult", "VisualsResult", "NicheConfig",
    "Niche", "env_str", "env_int", "env_float", "env_bool",
]


def env_str(name: str, default: str = "", prefix: str = "NICHE_") -> str:
    """Prefixed non-empty wins, then bare non-empty, else default.

    Empty-string exports (e.g. an unset GitHub vars entry exported as "")
    are treated as unset so bare fallbacks and defaults still apply.
    """
    prefixed = os.getenv(f"{prefix}{name}", "")
    if prefixed != "":
        return prefixed
    bare = os.getenv(name, "")
    if bare != "":
        return bare
    return default


def env_int(name: str, default: int = 0, prefix: str = "NICHE_") -> int:
    """Int env with NICHE_ prefix override; falls back on bad values."""
    try:
        return int(env_str(name, str(default), prefix))
    except ValueError:
        return default


def env_float(name: str, default: float = 0.0, prefix: str = "NICHE_") -> float:
    """Float env with NICHE_ prefix override; falls back on bad values."""
    try:
        return float(env_str(name, str(default), prefix))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False, prefix: str = "NICHE_") -> bool:
    """Bool env (1/on/true/yes); NICHE_ prefix wins."""
    return env_str(name, "", prefix).strip().lower() in ("1", "on", "true", "yes") \
        if (os.getenv(f"{prefix}{name}", os.getenv(name, "")) != "") else default


@dataclass
class Content:
    """Stage 1 output: script + metadata."""
    title: str
    narration_text: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)


@dataclass
class NarrationResult:
    """Stage 2 output: spoken audio + timing."""
    audio: Path
    duration: float
    words: int
    voice: str = ""
    style: str = ""
    extras: dict = field(default_factory=dict)


@dataclass
class VisualsResult:
    """Stage 3 output: final video + captions + provenance."""
    mp4: Path
    srt: Path | None = None
    credits: list[str] = field(default_factory=list)
    used_ids: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)


@dataclass
class NicheConfig:
    """Base per-niche config; extend per niche with from_env()."""
    niche_name: str = ""

    @classmethod
    def from_env(cls, niche_name: str = ""):
        """Build from env (NICHE_ prefix wins over bare names)."""
        return cls(niche_name=niche_name or env_str("NAME", ""))


class Niche(ABC):
    """Interface every niches/* subpackage implements."""
    NAME: str = ""
    LABEL: str = ""

    @abstractmethod
    def generate_content(self, history: list[dict], cfg: NicheConfig) -> Content:
        """Build script Content (may call engine.gemini_client)."""

    @abstractmethod
    def narrate(self, content: Content, cfg: NicheConfig,
                workdir: Path) -> NarrationResult:
        """Synthesize narration (engine.tts_cache / engine.align)."""

    @abstractmethod
    def build_visuals(self, content: Content, narration: NarrationResult,
                      cfg: NicheConfig, workdir: Path) -> VisualsResult:
        """Render mp4+srt (overlay_card / ffmpeg_utils)."""

    def build_metadata(self, content: Content,
                       narration: NarrationResult | None = None,
                       visuals: VisualsResult | None = None) -> dict:
        """Upload metadata dict (title/description/tags)."""
        return {"title": content.title, "description": content.description,
                "tags": list(content.tags)}

    def sample_content(self) -> Content:
        """Offline/sample Content for demos and tests."""
        return Content(title="Sample", narration_text="Sample narration.",
                       extras={"offline": True})
