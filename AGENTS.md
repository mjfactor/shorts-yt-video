# AGENTS.md

Single-file Shorts automation: `pipeline.py` (entry `main()`) does
Gemini script (with beats) → edge-tts mp3+srt → visuals → FFmpeg
1080x1920 → YouTube upload. Output `out/<stamp>_short.{mp4,srt,json}`
(stamp `%Y-%m-%d_%H%M%S`).

## Commands

- Full run (needs `ffmpeg` + `ffprobe` on PATH; CI `apt install ffmpeg` covers both): `python pipeline.py`
- Syntax check (no tests / lint / typecheck in repo): `python -m py_compile pipeline.py get_refresh_token.py`
- Deps: `pip install -r requirements.txt` (edge-tts, requests, Pillow, google-api clients)
- OAuth helper, local-only with browser, never CI: `python get_refresh_token.py` (needs `YT_CLIENT_ID`/`YT_CLIENT_SECRET` env)

## CI (`.github/workflows/daily.yml` is source of truth)

- `ubuntu-latest`, Python `3.11`, `timeout-minutes: 30`
- Schedule `0 8,16,23 * * *` (3x daily) + `workflow_dispatch`
- `concurrency.group: shorts-single`, `cancel-in-progress: false` (no shared state; serial only to avoid quota spikes)
- `permissions.contents: read` — no state commit; every run is standalone
- Artifacts: `out/*.json` on `always()` 90d; `out/*.mp4`+`*.mp3`+`*.srt` on `failure()` only 3d (YouTube is permanent storage)
- New env vars must be wired through the workflow `env:` block — secrets do not flow automatically. `GEMINI_MODELS`, `MIN/MAX_AUDIO_SEC`, `CF_IMAGE_MODEL`, `CF_NUM_STEPS`, `STILLS_PER_BEAT` are currently unset there, so CI uses code defaults. `TTS_*` and `VISIBILITY` are wired via `vars.*` with code-default fallback, so CI respects repo Variables.

## Config (all env, see `.env.example`; never hardcode/commit)

- Required: `GEMINI_API_KEY`, `PEXELS_API_KEY`, `YT_CLIENT_ID`/`YT_CLIENT_SECRET`/`YT_REFRESH_TOKEN`
- Optional: `VISIBILITY` (workflow vars fallback `private`; keep private until OAuth app passes YouTube audit), `CF_ACCOUNT_ID`/`CF_API_TOKEN` (fallback stills only), `GEMINI_MODELS` (tried in order, first win), `CF_IMAGE_MODEL` (default `@cf/bytedance/stable-diffusion-xl-lightning`), `CF_NUM_STEPS` (default 8; Lightning adheres weakly past ~20 steps — prefer FLUX-schnell over more steps), `MIN_AUDIO_SEC=10`/`MAX_AUDIO_SEC=180`, `DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
- `NICHE` / `VOICE` (`en-US-ChristopherNeural`) are hardcoded at top of `pipeline.py` — change code, not env; TTS prosody is env-tunable via `TTS_RATE` (default `-5%`) / `TTS_PITCH` (default `-10Hz`) / `TTS_VOLUME`
- `.gitignore`: never commit `out/`, `assets/`, `.env`, secrets

## Graceful degradation (do not break)

- No Gemini key/fail → template standalone script (~200 words, complete twist ending)
- No CF creds → skip with `warn()`; `pexels_clip` returns `None` on no key/fail (warn); partial Pexels downloads are deleted
- No YouTube creds → skip upload, keep video in `out/`
- `make_bed()` and `notify_failure()` must never fail the run
- `retry()` retries 429/5xx only; other 4xx fail fast (`RefreshError` is fatal for uploads → re-run `get_refresh_token.py`)
- Observability: `warn()`/`err()` emit `::warning::`/`::error::` annotations — use them for degraded/fatal paths, `log()` for normal flow; `main()` dumps non-secret config + mp3/srt sizes first, check there when debugging

## Pipeline gotchas

- edge-tts must use `--flag=value` form (e.g. `f"--rate={TTS_RATE}"`): values like `-10%` start with a dash and are misparsed as new flags when passed as separate args.
- Beats: `meta["beats"]=[{text,query}]` (5–6 beats, joined texts == full narration, ~200 words for ~90s). Per beat in order (`STILLS_PER_BEAT`=2 cuts/beat, ~10–12 clips/video): Pexels stock (`RANDOM_HORROR_QUERIES` ambient B-roll, deliberately not story-matched; `portrait`, `_mood_query` forces horror mood, smallest file ≥720p wide, cached as `assets/pexels_*.mp4`, per-run `used` set skips repeats, 2nd-half cuts `_stock_offset`-seek into different footage with `-stream_loop` guard) → Cloudflare still fallback → Pillow `beat_card`.
- CF fallback uses `_cloudflare_prompt` (query-anchored single moment + beat-0 continuity anchor, half-beat text per cut via `_split_beat`); `_image_prompt` is dead code, do not wire it back. `_clean_beat_text` strips CTA filler before prompting. Stills get Ken Burns zoom with cover-crop; screen time is word-count weighted per beat, split evenly across its cuts. Strict `_validate_script` raises `ValueError` on bad Gemini output.
- Audio guard `check_audio_duration()` aborts before render if outside MIN–MAX; bed = looped custom track at volume 0.20 (repo-root `bgm*.mp3` pool wins for CI; `assets/bgm*.mp3` is local-only fallback; one picked at random per run — stateless, so random not strict rotation), else generated 3-layer drone (brown-noise texture lowpassed for phone speakers + 55Hz sub root + 58.27Hz dissonant voice for ~3.3Hz beating dread, slow tremolo swell + short echo).
- Captions: `_srt_to_ass` converts edge-tts SRT to styled ASS (top-center `Alignment=8`, `FontSize=64`, `MarginV=720` — first line pinned just above middle so 1-line vs 3-line cues can't jump; never `Alignment=5`, which re-centers each cue's block); `_subtitles_vf` burns the ASS with plain `subtitles='...'` and NO `force_style` (comma-bearing `force_style` silently yields zero pixels on some FFmpeg/libass builds); returns `None` if no SRT, falls back to raw SRT if ASS conversion fails; mux falls back to no-subs render with `::warning::` if the filter errors.
- Standalone: every run is one complete story (no arcs; root `series_state.json` is a dead leftover `pipeline.py` never reads — do not wire it back); `gen_script()` returns `(meta, None)`. `cleanup_intermediates()` runs in `finally`, deletes `INTERMEDIATE_PATTERNS` only, keeps Pexels cache. Upload uses `categoryId=22`, resumable `next_chunk()` with retry.
