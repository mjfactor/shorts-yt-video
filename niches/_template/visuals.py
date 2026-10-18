"""Template stage 3: visuals via overlay_card/ffmpeg.

Real niche: fetch/render per beat -> overlay_card captions ->
ffmpeg_utils.concat/mux -> VisualsResult(mp4, srt, credits, used_ids).
"""

from pathlib import Path

from niches.base import Content, NarrationResult, NicheConfig, VisualsResult


def build_visuals(content: Content, narration: NarrationResult,
                  cfg: NicheConfig, workdir: Path) -> VisualsResult:
    """Skeleton: swap with real ffmpeg_utils + overlay_card calls."""
    # e.g. from engine import overlay_card, ffmpeg_utils, srt
    _ = (content, narration, cfg)
    return VisualsResult(mp4=Path(workdir) / "short.mp4", extras={"pending": True})
