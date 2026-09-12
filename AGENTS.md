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
- Never commit secrets, `out/`, or `assets/` binaries.
