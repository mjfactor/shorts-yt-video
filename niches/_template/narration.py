"""Template stage 2: narration via engine.tts_cache / engine.align.

Real niche: tts_cache.synthesize(content.narration_text, voice, workdir)
-> align timings -> NarrationResult(audio, duration, words, ...).
"""

from pathlib import Path

from niches.base import Content, NarrationResult, NicheConfig


def narrate(content: Content, cfg: NicheConfig,
            workdir: Path) -> NarrationResult:
    """Skeleton: swap with real engine.tts_cache call."""
    # e.g. from engine import tts_cache, align
    audio = Path(workdir) / "narration.mp3"
    words = len(content.narration_text.split())
    return NarrationResult(audio=audio, duration=0.0, words=words,
                           extras={"pending": True})
