"""Story niche: 100-120s Reddit-style stories with BG visuals + card.

Stages: content.generate_content -> narration.narrate ->
visuals.build_visuals. Engine helpers only; never imports core.py.
Exposes ``niche`` (StoryNiche) + NAME/LABEL/MODE.
"""

from pathlib import Path

from niches.base import (Content, NarrationResult, Niche, NicheConfig,
                         VisualsResult)

from . import content as _content
from . import narration as _narration
from . import visuals as _visuals
from .config import MODE, StoryConfig

NAME = "story"
LABEL = "reddit-style stories (100-120s)"


class StoryNiche(Niche):
    """Reddit-style micro-story niche: hook, escalation, twist, payoff."""
    NAME = NAME
    LABEL = LABEL
    MODE = MODE

    def generate_content(self, history: list[dict],
                         cfg: NicheConfig) -> Content:
        """Stage 1: story script Content."""
        assert isinstance(cfg, StoryConfig)
        return _content.generate_content(history, cfg)

    def narrate(self, content: Content, cfg: NicheConfig,
                workdir: Path) -> NarrationResult:
        """Stage 2: chunked TTS mix with duration fit."""
        assert isinstance(cfg, StoryConfig)
        return _narration.narrate(content, cfg, workdir)

    def build_visuals(self, content: Content, narration: NarrationResult,
                      cfg: NicheConfig, workdir: Path) -> VisualsResult:
        """Stage 3: BG track + card + captions single-pass render."""
        assert isinstance(cfg, StoryConfig)
        return _visuals.build_visuals(content, narration, cfg, workdir)

    def sample_content(self) -> Content:
        """Offline sample for demos/tests."""
        return _content.sample_content()


niche = StoryNiche()

__all__ = ["NAME", "LABEL", "MODE", "niche", "StoryNiche", "StoryConfig"]
