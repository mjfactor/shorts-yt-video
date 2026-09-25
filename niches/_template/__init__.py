"""Template niche skeleton: copy this package for a new niche.

Stages: content.generate_content -> narration.narrate ->
visuals.build_visuals. Call engine/* helpers only; never import
or edit core.py. Exposes ``niche`` (TemplateNiche) + NAME/LABEL.
"""

from pathlib import Path

from niches.base import (Content, NarrationResult, Niche, NicheConfig,
                         VisualsResult)

from . import content as _content
from . import narration as _narration
from . import visuals as _visuals

NAME = "template"
LABEL = "template niche (copy me)"


class TemplateNiche(Niche):
    """Skeleton Niche; copy and fill in per-niche logic."""
    NAME = NAME
    LABEL = LABEL

    def generate_content(self, history: list[dict],
                         cfg: NicheConfig) -> Content:
        """Stage 1: delegate to content.generate_content."""
        return _content.generate_content(history, cfg)

    def narrate(self, content: Content, cfg: NicheConfig,
                workdir: Path) -> NarrationResult:
        """Stage 2: delegate to narration.narrate."""
        return _narration.narrate(content, cfg, workdir)

    def build_visuals(self, content: Content, narration: NarrationResult,
                      cfg: NicheConfig, workdir: Path) -> VisualsResult:
        """Stage 3: delegate to visuals.build_visuals."""
        return _visuals.build_visuals(content, narration, cfg, workdir)


niche = TemplateNiche()

__all__ = ["NAME", "LABEL", "niche", "TemplateNiche"]
