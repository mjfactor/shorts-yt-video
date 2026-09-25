"""Explainer niche: 'How the world around you actually works' Shorts.

Stages: content.generate_content -> narration.narrate ->
visuals.build_visuals. Engine helpers only; never imports core.
"""

from pathlib import Path

from niches.base import (Content, NarrationResult, Niche, NicheConfig,
                         VisualsResult)

from . import content as _content
from . import narration as _narration
from . import visuals as _visuals
from .config import ExplainerConfig
from .content import (MODE, NICHE_LABEL, history_fields, sample_content,
                      sanitize_title, similarity_reason)

NAME = "explainer"
LABEL = "how things actually work"


class ExplainerNiche(Niche):
    """Explainer Shorts niche (ported from niche_explainer.py)."""
    NAME = NAME
    LABEL = LABEL
    MODE = MODE
    NICHE_LABEL = LABEL

    def generate_content(self, history: list[dict],
                         cfg: NicheConfig) -> Content:
        """Stage 1: Gemini script draft -> Content."""
        return _content.generate_content(history, cfg)

    def narrate(self, content: Content, cfg: NicheConfig,
                workdir: Path) -> NarrationResult:
        """Stage 2: calm-voice narration -> NarrationResult."""
        return _narration.narrate(content, cfg, workdir)

    def build_visuals(self, content: Content, narration: NarrationResult,
                      cfg: NicheConfig, workdir: Path) -> VisualsResult:
        """Stage 3: diagrams/stock + line captions -> VisualsResult."""
        return _visuals.build_visuals(content, narration, cfg, workdir)

    def build_metadata(self, content: Content,
                       narration: NarrationResult | None = None,
                       visuals: VisualsResult | None = None) -> dict:
        """Upload metadata + explainer history fields (topic/category)."""
        meta = super().build_metadata(content, narration, visuals)
        meta.update(history_fields((content.extras or {}).get("slots", {}),
                                   {"title": content.title}))
        return meta

    def sample_content(self) -> Content:
        """Offline sample shaped like demo_fixtures/website.json."""
        return sample_content()


niche = ExplainerNiche()

__all__ = ["NAME", "LABEL", "MODE", "NICHE_LABEL", "niche", "ExplainerNiche",
           "ExplainerConfig", "history_fields", "sanitize_title",
           "similarity_reason", "sample_content"]
