"""Template stage 3: visuals via engine.pexels/overlay_card/ffmpeg.

Real niche: pexels.fetch per beat -> overlay_card captions ->
ffmpeg_utils.concat/mux -> VisualsResult(mp4, srt, credits, used_ids).
"""

from pathlib import Path

from niches.base import Content, NarrationResult, NicheConfig, VisualsResult


def build_visuals(content: Content, narration: NarrationResult,
                  cfg: NicheConfig, workdir: Path) -> VisualsResult:
    """Skeleton: swap with real engine.pexels + ffmpeg_utils calls."""
    # e.g. from engine import pexels, overlay_card, ffmpeg_utils, srt
    _ = (content, narration, cfg)
    return VisualsResult(mp4=Path(workdir) / "short.mp4", extras={"pending": True})
