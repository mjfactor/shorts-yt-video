"""Narration + BGM mix: loop, sidechain ducking, fades, LUFS target."""
from __future__ import annotations

from pathlib import Path

from .ffmpeg_utils import ffprobe_duration, log, run_ffmpeg


def loop_bed(src: str | Path, duration: float, out_mp3: str | Path,
             volume: float = 0.08) -> Path:
    """Loop a music track to `duration` at linear `volume`."""
    out = Path(out_mp3)
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src),
                "-t", f"{max(0.1, float(duration)):.3f}",
                "-af", f"volume={float(volume):.3f}",
                "-c:a", "libmp3lame", str(out)], "bed-loop")
    return out


def synth_drone(duration: float, out_mp3: str | Path, *,
                root_hz: float = 55.0, beat_hz: float = 58.27) -> Path:
    """Copyright-safe dark drone bed (brown noise + detuned subs + tremolo)."""
    out = Path(out_mp3)
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, float(duration))
    fc = ("[0:a]lowpass=f=300,volume=0.6[ns];"
          "[1:a]volume=0.25[s1];[2:a]volume=0.25[s2];"
          "[ns][s1][s2]amix=inputs=3:normalize=1,"
          "tremolo=f=0.2:d=0.7,volume=0.5,"
          "aecho=0.8:0.5:50:0.3[aout]")
    run_ffmpeg(["ffmpeg", "-y",
                "-f", "lavfi", "-i",
                f"anoisesrc=color=brown:sample_rate=44100:duration={dur}:seed=7",
                "-f", "lavfi", "-i",
                f"sine=frequency={root_hz}:sample_rate=44100:duration={dur}",
                "-f", "lavfi", "-i",
                f"sine=frequency={beat_hz}:sample_rate=44100:duration={dur}",
                "-filter_complex", fc, "-map", "[aout]",
                "-t", f"{dur:.3f}",
                "-c:a", "libmp3lame", str(out)], "bed-drone")
    return out


def mix(narration: str | Path, bed: str | Path | None, out_mp3: str | Path, *,
        bed_volume: float = 0.08, duck_threshold: float = 0.02,
        duck_ratio: float = 8.0, fade_in: float = 0.3, fade_out: float = 0.8,
        target_lufs: float = -14.0) -> Path:
    """Duck the bed under narration, loudness-normalize, fade ends.

    No bed (None/missing) mixes narration alone with the same mastering.
    """
    out = Path(out_mp3)
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = ffprobe_duration(narration)
    fade_out_start = max(0.0, dur - max(0.0, fade_out))
    tail = (f"loudnorm=I={float(target_lufs)}:TP=-1.5:LRA=11,"
            f"afade=t=in:st=0:d={max(0.0, fade_in):.2f},"
            f"afade=t=out:st={fade_out_start:.2f}:d={max(0.0, fade_out):.2f}")
    if bed is not None and Path(bed).exists():
        fc = (f"[1:a]volume={float(bed_volume):.3f}[bed];"
              f"[bed][0:a]sidechaincompress=threshold={float(duck_threshold)}:"
              f"ratio={float(duck_ratio)}:attack=20:release=400:makeup=1[duck];"
              f"[0:a][duck]amix=inputs=2:normalize=0:duration=first[m];"
              f"[m]{tail}[out]")
        inputs = ["-i", str(narration), "-i", str(bed)]
    else:
        log("Mix: no bed, mastering narration alone.")
        fc = f"[0:a]{tail}[out]"
        inputs = ["-i", str(narration)]
    run_ffmpeg(["ffmpeg", "-y", *inputs,
                "-filter_complex", fc, "-map", "[out]",
                "-t", f"{dur:.3f}",
                "-c:a", "libmp3lame", str(out)], "audio-mix")
    return out
