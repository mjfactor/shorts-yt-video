# AGENTS.md

Single-file Shorts automation: `pipeline.py` (entry `main()`) does
Gemini script (with beats) → edge-tts mp3+srt → visuals → FFmpeg
1080x1920 → YouTube upload. Output `out/<stamp>_short.{mp4,srt,json}`.

## Commands

- Full run (needs `ffmpeg` + `ffprobe` on PATH): `python pipeline.py`
- Syntax check: `python -m py_compile pipeline.py get_refresh_token.py`
- Deps: `pip install -r requirements.txt` (edge-tts, requests, Pillow, google-api clients)
- OAuth helper, local-only never CI: `python get_refresh_token.py`
- No tests / lint / typecheck in repo.

## CI (`.github/workflows/daily.yml` is source of truth)

- `ubuntu-latest`, Python `3.11`, `apt install ffmpeg`, `timeout-minutes: 30`
- Schedule `0 2,8,14,20 * * *` (4x daily) + `workflow_dispatch`
- `concurrency.group: series-state`, `cancel-in-progress: false` — never parallelize state advances
- `permissions.contents: write` + commit step (`git add series_state.json`, diff-guard no-op, `[skip ci]`, `pull --rebase --autostash`) required for continuity
- Artifacts: `out/*.json` on `always()` 90d; `out/*.mp4` on `failure()` only 3d (500 MB cap; YouTube is permanent storage)

## Config (all env, see `.env.example`; never hardcode/commit)

- Required: `GEMINI_API_KEY`, `PEXELS_API_KEY`, `YT_CLIENT_ID`/`YT_CLIENT_SECRET`/`YT_REFRESH_TOKEN`
- Optional: `VISIBILITY` (default `private`), `CF_ACCOUNT_ID`/`CF_API_TOKEN`, `POLLINATIONS_API_KEY` (`sk_...` header, never log), `GEMINI_MODELS` (tried in order), `CF_IMAGE_MODEL` (default `@cf/bytedance/stable-diffusion-xl-lightning`), `MIN_AUDIO_SEC=10`/`MAX_AUDIO_SEC=180`, `DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
- `NICHE` / `VOICE` (`en-US-ChristopherNeural`) are hardcoded at top of `pipeline.py` — change code, not env
- `.gitignore`: never commit `out/`, `assets/`, `.env`, secrets

## Graceful degradation (do not break)

- No Gemini key/fail → template standalone script, `state_update=None` (must NOT advance arc)
- No CF creds → skip silently; Pollinations HTTP 402 → log + fall back to stock; `pexels_clip` returns `None` on no key/fail; partial Pexels downloads are deleted
- No YouTube creds → skip upload, keep video in `out/`
- `make_bed()` and `notify_failure()` must never fail the run
- `retry()` retries 429/5xx only; other 4xx fail fast

## Pipeline gotchas

- Beats: `meta["beats"]=[{text,query}]`; per beat in order: Cloudflare still → Pollinations still (first `AI_STILLS_PER_VIDEO=2` beats only; anon = 1 req/15s pacing, may watermark) → Pexels clip (`portrait`, `_mood_query` forces horror mood, smallest file ≥720p wide, cached as `assets/pexels_*.mp4`) → Pillow `beat_card`. Stills get Ken Burns zoom with cover-crop; clip durations weighted by beat word count.
- Audio guard `check_audio_duration()` aborts before render if outside MIN–MAX; bed = looped `bgm.mp3` (repo root or `assets/bgm.mp3`) at volume 0.20, else generated brown-noise drone at 0.22 (lowpass 400Hz so phones can reproduce it).
- Captions: burned via `SUB_STYLE` (`DejaVu Sans 32 bold, MarginV=320` to clear Shorts UI); `_subtitles_vf` returns `None` if no SRT.
- Series: `series_state.json` `{arc_title,episode,recap,finale}`, `MAX_EPISODES_PER_ARC=5` (Ep 5 forced finale, Gemini may set `finale:true` earlier); `save_state()` only after successful render+upload inside `main()` `try`; `cleanup_intermediates()` runs in `finally` but keeps Pexels cache.
