"""Template stage 1: script Content via engine.gemini_client.

Real niche: build prompt (history dedup) -> gemini_client.generate ->
validate -> Content(title, narration_text, description, tags, extras).
"""

from niches.base import Content, NicheConfig


def generate_content(history: list[dict], cfg: NicheConfig) -> Content:
    """Skeleton: swap with engine.gemini_client call + fallback."""
    # e.g. from engine import gemini_client
    #      data = gemini_client.generate(prompt, ...)
    _ = (history, cfg)
    return Content(title="Sample", narration_text="Sample narration.",
                   description="", tags=[], extras={"offline": True})


def sample_content() -> Content:
    """Offline sample for demos/tests."""
    return Content(title="Sample", narration_text="Sample narration.",
                   extras={"offline": True})
