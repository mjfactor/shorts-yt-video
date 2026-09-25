# Story sample bundle (offline)

For `demo.py --niche story` without API keys. Wiring demo.py itself is out
of scope for this lane (niches/story/* only) — the orchestrator owns it.

Files:
- `story.json` — full `sample_content()` output (title, narration_text,
  description, tags, extras with beats/bg_queries/card/rehooks).
- `pointer.json` — bundled narration pointer: word count, estimated
  duration at 2.9 wps, card/hook/beats/bg_queries for silent-audio timing.
- `narration.txt` — plain narration text (TTS input / timing reference).

Offline render recipe (mirrors demo.py for explainer):
1. narration: silent tone mp3 sized to `est_duration_s` (or edge-tts over
   narration.txt when available).
2. captions: proportional word timings over the narration text.
3. visuals: `StoryNiche().build_visuals()` needs real narration timings,
   so pass a NarrationResult with proportional words + card_end; BG clips
   come from BG_DIR since Pexels is keyless offline.
