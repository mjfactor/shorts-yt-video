# AGENTS.md

Top-5 ranking Shorts automation. `pipeline.py` is a thin entry (`main()` →
`core.run(niche_ranking)`); `core.py` owns all shared machinery
(Gemini/TTS/Pexels/FFmpeg/upload/history); `niche_ranking.py` is the
self-contained niche behind the interface documented at the top of
`core.py`. Imports flow one way (niche → core); core never imports a niche.
Run: `python pipeline.py` → Gemini script (with beats) → edge-tts mp3+srt →
visuals → FFmpeg 1080x1920 → YouTube upload.
Output `out/<stamp>_short.{mp4,mp3,srt,json}` (stamp `%Y-%m-%d_%H%M%S`).

## Commands

- Full run (needs `ffmpeg` + `ffprobe` on PATH): `python pipeline.py`
- Syntax check (no tests/lint/typecheck in repo, Python `3.11` per CI): `python -m py_compile pipeline.py core.py niche_ranking.py get_refresh_token.py`
- Deps: `pip install -r requirements.txt`
- OAuth helper, local-only with browser, never CI: `python get_refresh_token.py` (needs `YT_CLIENT_ID`/`YT_CLIENT_SECRET`)

## CI (`.github/workflows/daily.yml` is source of truth)

- `ubuntu-latest`, Python `3.11`, `timeout-minutes: 30`, schedule `0 8,20 * * *` (2x daily: 08:00 + 20:00 UTC) + `workflow_dispatch`
- `concurrency.group: shorts-single`, `cancel-in-progress: false`; `permissions.contents: write` — each run commits `history.json` back, so runs must stay serial (no state-commit race)
- Commit-back after pipeline: `git add history.json` + commit `[skip ci]` + push (no-op when unchanged). Workflow triggers only on schedule/dispatch, so the push never loops
- Artifacts: `out/*.json` on `always()` 90d; `out/*.mp4`+`*.mp3`+`*.srt` on `failure()` only 3d (YouTube is permanent storage)
- New env vars must be wired through workflow `env:` — secrets/vars do not flow automatically. `GEMINI_MODELS`, `MIN/MAX_AUDIO_SEC`, `CF_IMAGE_MODEL`, `CF_NUM_STEPS`, `STILLS_PER_BEAT`, `BUFFER_TIKTOK_CHANNEL_ID` are unset there (code defaults/auto-pick win); `TTS_*`/`VISIBILITY` are wired via `vars.*` with code-default fallback

## Config (all env, see `.env.example`; never hardcode/commit)

- Required (YouTube path): `GEMINI_API_KEY`, `PEXELS_API_KEY`, `YT_CLIENT_ID`/`YT_CLIENT_SECRET`/`YT_REFRESH_TOKEN`
- Optional: `VISIBILITY` (default `private`; keep until OAuth app passes YouTube audit), `CF_ACCOUNT_ID`/`CF_API_TOKEN`, `GEMINI_MODELS` (tried in order, first win), `CF_IMAGE_MODEL` (default `@cf/bytedance/stable-diffusion-xl-lightning`), `CF_NUM_STEPS` (default 8), `MIN_AUDIO_SEC=10`/`MAX_AUDIO_SEC=180`, `DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
- `VOICE` (`en-US-ChristopherNeural`) is hardcoded in `core.py`. Prosody is env-tunable: `TTS_RATE` (`-5%`) / `TTS_PITCH` (`-10Hz`) / `TTS_VOLUME`
- `history.json` (repo root) is committed dedup memory — never gitignore it, never store secrets in it. `out/`, `assets/`, `.env` stay uncommitted (see `.gitignore`)
- TikTok dual-post via Buffer (`core.buffer_tiktok_upload`, free plan): needs `BUFFER_API_KEY` only — Cloudinary public host is hardcoded in `core.py` (`epuxrxa5`/`buffer_shorts`, unsigned preset, not sensitive; env overrides still win). Buffer has no upload endpoint, asset url must be public HTTPS and stay live until publish. `BUFFER_TIKTOK_CHANNEL_ID` optional (auto-picks `service==tiktok` via organizations→channels). Caption cut to 2200 UTF-16 units. User text travels via `$input` GraphQL variables, never f-string interpolation (raw control chars/emoji break the document → API 500 `Syntax Error`). Never fails the run (warn + `None`); post id recorded as `tiktokBufferPostId` in the same `history.json` entry. Do not add unofficial `sessionid`/cookie uploaders or a self-registered TikTok Direct-Post app (SELF_ONLY/private until audit)

## Graceful degradation (do not break)

- No Gemini key/fail → template standalone script (~200 words, Top-5 largest animals); strict `_validate_script` raises `ValueError` on bad Gemini output (passes optional `items` through)
- No CF creds → skip with `warn()`; `pexels_clip` returns `None` on no key/fail (warn, partial downloads deleted)
- No YouTube creds → skip upload, keep video in `out/`
- `make_bed()` and `notify_failure()` must never fail the run
- `retry()` retries 429/5xx only; other 4xx fail fast (`RefreshError` is fatal → re-run `get_refresh_token.py`)
- `warn()`/`err()` emit `::warning::`/`::error::` annotations (use for degraded/fatal paths, `log()` for normal flow); `run()` logs non-secret config + mp3/srt sizes first — check there when debugging

## Pipeline gotchas

- edge-tts needs `--flag=value` form (e.g. `f"--rate={TTS_RATE}"`): values like `-10%` are misparsed as new flags when passed as separate args
- Beats: `meta["beats"]=[{text,query}]`, 7–8 beats, joined texts == full narration (~200–220 words/~90s, still a YouTube Short). Beats also carry `label` (rank digit `"5"`–`"1"`, `""` for hook/closer; `_validate_script` keeps digits only). Per beat in order (`STILLS_PER_BEAT`=2): Pexels clip (beat's OWN query, literal item match, no mood forcing; `portrait`, smallest file ≥720p, cached `assets/pexels_*.mp4`, per-run `used` set skips repeats, 2nd-half cuts seek into different footage with `-stream_loop` guard) → Cloudflare still → Pillow `beat_card`
- CF prompt is `niche_ranking.image_prompt` (vibrant daylight subject photo + half-beat text via `_split_beat`); `_clean_beat_text` strips CTA filler. Stills get Ken Burns zoom; screen time is word-count weighted per beat, split evenly across its cuts. Leaderboard: `niche_ranking.board_filter(beats, items, durations)` builds 15 timed `drawtext`s (dim `N.` slots always on, white `N. Name` fill from reveal to end, red highlight during the beat; `enable='between(t,a,b)'`) applied at the FINAL mux only (per-segment clocks restart at 0) — needs a font (`_RANK_FONTS` in `core.py`: DejaVu Bold on CI, Arial Bold locally) and the `drawtext` filter, else skipped with `::warning::`; mux falls back to no-board render on drawtext error (never fails the run). Names are ASCII-sanitized (`_clean_board_text`, em-dash to `-`, word-boundary cut) and auto-sized (`_fit_board_label`: 66px down to 40px min, truncate-to-fit only when min still overflows) plus filter-escaped (`_escape_drawtext`: single-font drawtext has no emoji)
- `check_audio_duration()` aborts before render if outside MIN–MAX; bed = looped `bgm*.mp3` pool at volume 0.20 (repo-root wins for CI, `assets/` is local fallback, random pick per run), else generated drone bed
- Captions: `_srt_to_ass` → styled ASS with `Alignment=2`, `FontSize=34`, `MarginV=230` (tiny, bottom-anchored above the Shorts UI, clear of the left-side leaderboard). Burn with plain `subtitles='...'` and NO `force_style` (commas silently yield zero pixels on some FFmpeg/libass builds). Mux falls back to no-subs render with `::warning::` on filter error
- Dedup memory: `history.json` (kept to 50, last 20 quoted in prompt). Niche `build_prompt(history)` forces slots via `_pick_avoiding` (excludes recent topics) + `history_block` bans recent titles/combos; `similarity_reason` (same topic, ≥2 shared items, or banned title hook) retries similar drafts (`SIMILARITY_RETRIES`=2, best-effort accept). `gen_script()` returns `(meta, slots)`; `main()` records the entry after render (even if upload skipped). `load/save_history` must never fail the run; widen pools instead of adding new state files
- `cleanup_intermediates()` runs in `finally`, deletes `INTERMEDIATE_PATTERNS` only, keeps Pexels cache. Upload uses `categoryId=22`, resumable `next_chunk()` with retry
