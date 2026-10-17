"""Audition edge-tts narration voices: synth one sample line per voice.

Manual tool (not in requirements, not in CI): renders the bundled story
sample's opening into out/voice_samples/*.mp3 so voices can be compared
by ear before wiring STORY_EDGE_VOICES / STORY_EDGE_VOICE / EDGE_RATE*.

Audio goes through the SAME prod edge path as a real render — one
synth_sync call + _edge_single_surgery (sentence split, silence trim,
CHUNK_PAUSE_SEC) — so the pacing matches the channel. --raw keeps the
untouched synth for an A/B.

Rates mirror the prod defaults: female-side voices +28% (EDGE_RATE_FEMALE),
all others +20% (EDGE_RATE). The two current baselines (Jenny, Andrew)
print first- and second-index so a candidate can be A/B'd against them.

Usage:
    python tools/voice_samples.py
    python tools/voice_samples.py --raw
    python tools/voice_samples.py --pitch +6Hz               # pitch sweep
    python tools/voice_samples.py --words 48
    python tools/voice_samples.py --voices en-US-EmmaMultilingualNeural,en-GB-RyanNeural
    python tools/voice_samples.py --rate +15%          # force one rate for all

Outputs: out/voice_samples/NN_<voice>_p<rate>.mp3 + index.txt (per-voice
status, duration and wps, including any synth failures).
"""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import edge_tts_client as edge_client  # noqa: E402
from engine.ffmpeg_utils import ffprobe_duration  # noqa: E402

# (voice, rate) — current baselines first so differences are obvious.
MALE_RATE = "+20%"    # EDGE_RATE
FEMALE_RATE = "+28%"  # EDGE_RATE_FEMALE
CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("en-US-JennyNeural", FEMALE_RATE, "BASELINE (current female)"),
    ("en-US-AndrewNeural", MALE_RATE, "BASELINE (current male)"),
    ("en-US-AvaMultilingualNeural", FEMALE_RATE, ""),
    ("en-US-EmmaMultilingualNeural", FEMALE_RATE, ""),
    ("en-US-AvaNeural", FEMALE_RATE, ""),
    ("en-US-EmmaNeural", FEMALE_RATE, ""),
    ("en-US-AriaNeural", FEMALE_RATE, "Novel category"),
    ("en-US-MichelleNeural", FEMALE_RATE, "Novel category"),
    ("en-GB-SoniaNeural", FEMALE_RATE, "UK"),
    ("en-IN-NeerjaExpressiveNeural", FEMALE_RATE, "Expressive (avoid: styles unreachable)"),
    ("en-US-AndrewMultilingualNeural", MALE_RATE, ""),
    ("en-US-BrianMultilingualNeural", MALE_RATE, ""),
    ("en-US-ChristopherNeural", MALE_RATE, "Novel category"),
    ("en-US-EricNeural", MALE_RATE, "Novel category"),
    ("en-US-GuyNeural", MALE_RATE, "Novel category"),
    ("en-US-SteffanNeural", MALE_RATE, "Novel category"),
    ("en-GB-RyanNeural", MALE_RATE, "UK"),
    ("en-AU-WilliamMultilingualNeural", MALE_RATE, "AU"),
)

# Heuristic for --voices overrides when a voice is not in CANDIDATES.
_FEMALE_HINTS = ("jenny", "aria", "ava", "emma", "michelle", "sonia",
                 "neerja", "ana", "clara", "emily", "natasha", "libby",
                 "maisie", "leah", "asilia", "ezinne", "rosa", "luna",
                 "imani", "yan", "molly")


def _default_text(n_words: int) -> str:
    """Bundled story sample's opening (the real narration style)."""
    from niches.story import niche as story_niche

    toks = story_niche.sample_content().narration_text.split()
    return " ".join(toks[:max(1, n_words)])


def _rate_for(voice: str, forced: str | None) -> str:
    if forced:
        return forced
    for name, rate, _note in CANDIDATES:
        if name == voice:
            return rate
    return FEMALE_RATE if any(h in voice.lower()
                              for h in _FEMALE_HINTS) else MALE_RATE


def _render_one(text: str, voice: str, rate: str, dest: Path,
                raw: bool, pitch: str | None = None) -> float:
    """Synth one sample to dest via the prod edge path; return duration.

    raw=False (default) runs the audio through the prod sentence-gap
    surgery (trim + CHUNK_PAUSE_SEC) so pacing matches a real render.
    pitch=None keeps the prod per-voice default (EDGE_PITCH /
    EDGE_PITCH_FEMALE).
    """
    from niches.story.config import StoryConfig
    from niches.story.narration import (_edge_pitch_for, _edge_single_surgery,
                                        _map_edge_words)

    cfg = StoryConfig.from_env()
    pitch_eff = pitch or _edge_pitch_for(cfg, voice)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        raw_mp3 = work / "raw.mp3"
        events = edge_client.synth_sync(text, voice, rate, raw_mp3,
                                        pitch=pitch_eff)
        if raw:
            src = raw_mp3
        else:
            words = _map_edge_words(events, text)
            src, _words = _edge_single_surgery(raw_mp3, words, text, cfg,
                                               work, "sample")
        shutil.copy(src, dest)
    return ffprobe_duration(dest)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Edge-tts voice audition pack (sample line per voice).")
    ap.add_argument("--voices", default=None,
                    help="comma-separated ShortNames (default: candidate set)")
    ap.add_argument("--rate", default=None,
                    help="force one rate for all voices (e.g. +24%%)")
    ap.add_argument("--pitch", default=None,
                    help="prosody pitch for all voices, e.g. +6Hz "
                         "(default: cfg EDGE_PITCH / EDGE_PITCH_FEMALE)")
    ap.add_argument("--words", type=int, default=36,
                    help="words of the sample line to speak (default: %(default)s)")
    ap.add_argument("--text-file", default=None,
                    help="custom text file (default: sample narration head)")
    ap.add_argument("--out-dir", default="out/voice_samples",
                    help="output dir (default: %(default)s)")
    ap.add_argument("--raw", action="store_true",
                    help="skip the prod sentence-gap surgery (untouched synth)")
    args = ap.parse_args(argv)

    text = (Path(args.text_file).read_text(encoding="utf-8").strip()
            if args.text_file else _default_text(args.words))
    if not text:
        print("FAILED: empty text")
        sys.exit(1)

    if args.voices:
        voices = [(v.strip(), "") for v in args.voices.split(",") if v.strip()]
    else:
        voices = [(v, note) for v, _r, note in CANDIDATES]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Text ({len(text.split())} words): {text[:80]}...")
    print(f"Pacing: {'raw synth (no surgery)' if args.raw else 'prod edge path (surgery + pause)'}")
    print(f"Output: {out_dir.resolve()}")

    index_lines: list[str] = []
    failures: list[str] = []
    for i, (voice, note) in enumerate(voices, start=1):
        rate = _rate_for(voice, args.rate)
        tag = f"p{rate.lstrip('+').rstrip('%')}"
        if args.pitch:
            tag += f"_hz{args.pitch.lstrip('+').rstrip('Hz%')}"
        mp3 = out_dir / f"{i:02d}_{voice}_{tag}.mp3"
        try:
            dur = _render_one(text, voice, rate, mp3, args.raw, args.pitch)
            size_kb = mp3.stat().st_size // 1024
            wps = len(text.split()) / dur if dur > 0 else 0.0
            pitch_note = f", pitch {args.pitch}" if args.pitch else ""
            status = f"OK ({size_kb} KB, {dur:.1f}s, {wps:.2f} wps{pitch_note})"
            print(f"[{i:02d}] {voice} @ {rate}: {status}")
        except Exception as e:
            status = f"FAILED: {type(e).__name__}: {e}"
            failures.append(voice)
            print(f"[{i:02d}] {voice} @ {rate}: {status}")
        line = f"{mp3.name}\t{voice}\t{rate}\t{status}"
        if note:
            line += f"\t{note}"
        index_lines.append(line)

    (out_dir / "index.txt").write_text(
        "\n".join(index_lines) + "\n", encoding="utf-8")
    print(f"\nDone: {len(voices) - len(failures)}/{len(voices)} rendered.")
    if failures:
        print(f"Failures: {', '.join(failures)}")
    print(f"Listen: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
