# AGENTS.md (project init)

YouTube Shorts automation. $0 budget, no credit card, CPU-only.

## Stack

- Python 3.11, FFmpeg via apt, GitHub Actions `ubuntu-latest`
- Gemini free API (script), edge-tts (narration, no key),
  Pexels free API with Pillow fallback (visuals),
  YouTube Data API v3 with OAuth refresh token (upload)

## Commands

- Local test (needs ffmpeg + env vars): `python pipeline.py`
- Syntax check: `python -m py_compile pipeline.py get_refresh_token.py`
- OAuth helper (local only, never CI): `python get_refresh_token.py`

## Conventions

- All config via env vars; never hardcode keys. See `.env.example`.
- `pipeline.py` must degrade gracefully: no Gemini key → template script;
  no Pexels key/fail → Pillow slides; no YouTube creds → keep file in `out/`, skip upload.
- Keep runs CPU-only and under 30 min (job `timeout-minutes: 30`).
- Artifacts: metadata JSON always 90d; mp4 only on failure 3d (500 MB free cap).
- Niche default: scary true horror stories with cliffhanger, vertical 1080x1920.
- Visuals are beat-aligned: meta["beats"] = [{text,query}], per beat:
  Cloudflare Workers AI SDXL-Lightning still ($0/step, native 1080x1920,
  CF_ACCOUNT_ID + CF_API_TOKEN secrets) -> Pollinations FLUX backup
  (pollen, first beats only) -> Pexels clip -> title card. Ken Burns zoom
  on stills (cover-crop, any input aspect). Screen time weighted by
  beat word count.
- Series continuity: series_state.json (arc_title/episode/recap), 5 eps per arc.
  Workflow needs contents:write + concurrency group to commit state back.
- Never commit secrets, `out/`, or `assets/` binaries.
