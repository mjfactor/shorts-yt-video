"""Ranking niche: Top-5 countdown Shorts (~2 min, 7 beats).

Stages: content.generate_content -> narration.narrate ->
visuals.build_visuals. Engine helpers only; never imports core.py.
Exposes ``niche`` (RankingNiche) + NAME/LABEL/MODE.
"""

from pathlib import Path

from niches.base import (Content, NarrationResult, Niche, NicheConfig,
                         VisualsResult)

from . import content as _content
from . import narration as _narration
from . import visuals as _visuals
from .config import MODE, RankingConfig

NAME = "ranking"
LABEL = "top-5 rankings and countdowns"


class RankingNiche(Niche):
    """Top-5 countdown niche: hook, #5..#1, closer with leaderboard."""
    NAME = NAME
    LABEL = LABEL
    MODE = MODE

    def generate_content(self, history: list[dict],
                         cfg: NicheConfig) -> Content:
        """Stage 1: countdown script Content."""
        assert isinstance(cfg, RankingConfig)
        return _content.generate_content(history, cfg)

    def narrate(self, content: Content, cfg: NicheConfig,
                workdir: Path) -> NarrationResult:
        """Stage 2: per-beat TTS narration."""
        assert isinstance(cfg, RankingConfig)
        return _narration.narrate(content, cfg, workdir)

    def build_visuals(self, content: Content, narration: NarrationResult,
                      cfg: NicheConfig, workdir: Path) -> VisualsResult:
        """Stage 3: tiered stock + board/blur/captions mux."""
        assert isinstance(cfg, RankingConfig)
        return _visuals.build_visuals(content, narration, cfg, workdir)

    def sample_content(self) -> Content:
        """Offline sample for demos/tests."""
        return _content.sample_content()


niche = RankingNiche()

__all__ = ["NAME", "LABEL", "MODE", "niche", "RankingNiche", "RankingConfig"]
