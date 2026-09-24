"""Word timestamps via proportional fallback (no local models).

No model downloads, no GPU/CPU transcription: timings are estimated.
Always returns [{word, start, end}] so callers never branch on the source.
"""
from __future__ import annotations

from pathlib import Path

from .ffmpeg_utils import ffprobe_duration, log, warn

Word = dict  # {word: str, start: float, end: float}


def proportional_fallback(transcript: str | list[str],
                          total_dur: float) -> list[Word]:
    """Spread words over total_dur weighted by chars (space counts 1)."""
    words = transcript.split() if isinstance(transcript, str) else [str(w) for w in transcript]
    words = [w for w in words if w]
    if not words or total_dur <= 0:
        return []
    weights = [len(w) + 1 for w in words]
    unit = float(total_dur) / sum(weights)
    out: list[Word] = []
    t = 0.0
    for i, w in enumerate(words):
        end = float(total_dur) if i == len(words) - 1 else t + weights[i] * unit
        out.append({"word": w, "start": round(t, 3), "end": round(end, 3)})
        t = end
    return out


def align_or_fallback(audio_path: str | Path, transcript: str | list[str], *,
                      total_dur: float | None = None,
                      model_size: str = "base", device: str = "cpu",
                      compute_type: str = "int8", language: str = "en") -> list[Word]:
    """Estimated word timings via proportional fallback (never raises).

    Extra kwargs (model_size/device/...) are accepted for API stability
    and ignored: there is no local transcription model anymore.
    """
    _ = (model_size, device, compute_type, language)
    log("Word timings are estimated (proportional fallback, no local model).")
    if total_dur is None:
        try:
            total_dur = ffprobe_duration(audio_path)
        except Exception as e:
            warn(f"Duration probe failed ({e}); no timings available.")
            return []
    return proportional_fallback(transcript, total_dur)
