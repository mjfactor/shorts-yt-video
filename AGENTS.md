# AGENTS.md

Multi-file Shorts automation. `pipeline.py` is a thin entry (niche registry
+ `main()` → `core.run(niche)`); `core.py` owns all shared machinery
(Gemini/TTS/Pexels/FFmpeg/upload/history); `niche_ranking.py` and
`niche_horror.py` are self-contained niches behind a documented interface
(see the interface block at the top of `core.py`). Imports flow one way
(niches → core); core never imports a niche. To delete a niche, delete its
`niche_*.py` and remove its `NICHES` line in `pipeline.py`.
Run: `python pipeline.py` → Gemini script (with beats) → edge-tts mp3+srt →
visuals → FFmpeg 1080x1920 → YouTube upload.
Output `out/<stamp>_short.{mp4,mp3,srt,json}` (stamp `%Y-%m-%d_%H%M%S`).
Niche switch: `NICHE_MODE` (`ranking` default = Top-5 countdowns, `horror` =
scary stories) — prompt, pools, fallback, visuals, and history shape all branch on it.

## Commands

- Full run (needs `ffmpeg` + `ffprobe` on PATH): `python pipeline.py`
- Syntax check (no tests/lint/typecheck in repo): `python -m py_compile pipeline.py core.py niche_ranking.py niche_horror.py get_refresh_token.py`
- Deps: `pip install -r requirements.txt`
- OAuth helper, local-only with browser, never CI: `python get_refresh_token.py` (needs `YT_CLIENT_ID`/`YT_CLIENT_SECRET`)

## CI (`.github/workflows/daily.yml` is source of truth)

- `ubuntu-latest`, Python `3.11`, `timeout-minutes: 30`, schedule `0 8 * * *` (1x daily) + `workflow_dispatch`
- `concurrency.group: shorts-single`, `cancel-in-progress: false`; `permissions.contents: write` — each run commits `history.json` back, so runs must stay serial (no state-commit race)
- Commit-back step after pipeline: `git add history.json` + commit `[skip ci]` + push (no-op when unchanged/failed). Workflow triggers only on schedule/dispatch, so the push never loops
- Artifacts: `out/*.json` on `always()` 90d; `out/*.mp4`+`*.mp3`+`*.srt` on `failure()` only 3d (YouTube is permanent storage)
- New env vars must be wired through workflow `env:` — secrets/vars do not flow automatically. `GEMINI_MODELS`, `MIN/MAX_AUDIO_SEC`, `CF_IMAGE_MODEL`, `CF_NUM_STEPS`, `STILLS_PER_BEAT` are unset there (code defaults win); `TTS_*`/`VISIBILITY`/`NICHE_MODE` are wired via `vars.*` with code-default fallback

## Config (all env, see `.env.example`; never hardcode/commit)

- Required: `GEMINI_API_KEY`, `PEXELS_API_KEY`, `YT_CLIENT_ID`/`YT_CLIENT_SECRET`/`YT_REFRESH_TOKEN`
- Optional: `VISIBILITY` (default `private`; keep until OAuth app passes YouTube audit), `CF_ACCOUNT_ID`/`CF_API_TOKEN`, `GEMINI_MODELS` (tried in order, first win), `CF_IMAGE_MODEL` (default `@cf/bytedance/stable-diffusion-xl-lightning`), `CF_NUM_STEPS` (default 8), `MIN_AUDIO_SEC=10`/`MAX_AUDIO_SEC=180`, `DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
- `NICHE_MODE` (`ranking` default; `horror` restores scary stories) is env-tunable with code default — change via `vars.NICHE_MODE`, not code. `VOICE` (`en-US-ChristopherNeural`) is hardcoded in `core.py` config. Prosody is env-tunable: `TTS_RATE` (`-5%`) / `TTS_PITCH` (`-10Hz`) / `TTS_VOLUME`
- `history.json` (repo root) is committed dedup memory — never gitignore it, never store secrets in it. `out/`, `assets/`, `.env` stay uncommitted (see `.gitignore`)

## Graceful degradation (do not break)

- No Gemini key/fail → template standalone script (~290 words; ranking = Top-5 largest animals, horror = twist-ending story; slots avoid recent history); strict `_validate_script` (in `core.py`) raises `ValueError` on bad Gemini output (passes optional `items` through for ranking)
- No CF creds → skip with `warn()`; `pexels_clip` returns `None` on no key/fail (warn, partial downloads deleted)
- No YouTube creds → skip upload, keep video in `out/`
- `make_bed()` and `notify_failure()` must never fail the run
- `retry()` retries 429/5xx only; other 4xx fail fast (`RefreshError` is fatal → re-run `get_refresh_token.py`)
- `warn()`/`err()` emit `::warning::`/`::error::` annotations (use for degraded/fatal paths, `log()` for normal flow); `main()` dumps non-secret config + mp3/srt sizes first — check there when debugging

## Pipeline gotchas

- edge-tts needs `--flag=value` form (e.g. `f"--rate={TTS_RATE}"`): values like `-10%` are misparsed as new flags when passed as separate args
- Beats: `meta["beats"]=[{text,query}]`, 7–8 beats, joined texts == full narration (~280–300 words/~120s, still a YouTube Short). Ranking beats also carry `label` (rank digit `"5"`–`"1"`, `""` for hook/closer; `_validate_script` keeps digits only). Per beat in order (`STILLS_PER_BEAT`=2): Pexels clip (ranking = beat's OWN query, literal item match, no mood forcing; horror = ambient B-roll from `niche_horror.AMBIENT_QUERIES`, deliberately not story-matched; `portrait`, smallest file ≥720p, cached `assets/pexels_*.mp4`, per-run `used` set skips repeats, 2nd-half cuts seek into different footage with `-stream_loop` guard) → Cloudflare still → Pillow `beat_card`
- CF prompt is each niche's `image_prompt` (ranking = vibrant daylight subject photo; horror = dark still + beat-0 continuity anchor; half-beat text via `_split_beat`); `_image_prompt` in `core.py` is dead code, do not rewire. `_clean_beat_text` strips CTA filler. Stills get Ken Burns zoom; screen time is word-count weighted per beat, split evenly across its cuts. Leaderboard: `niche_ranking.board_filter(beats, items, durations)` builds 15 timed `drawtext`s (dim `N.` slots always on, white `N. Name` fill from reveal to end, red highlight during the beat; `enable='between(t,a,b)'`) applied at the FINAL mux only (per-segment clocks restart at 0) — needs a font (`_RANK_FONTS` in `core.py`: DejaVu Bold on CI, Arial Bold locally) and the `drawtext` filter, else skipped with `::warning::`; mux falls back to no-board render on drawtext error (never fails the run). Names are ASCII-sanitized (`_clean_board_text`, ≤14 chars) and filter-escaped (`_escape_drawtext`: single-font drawtext has no emoji)
- `check_audio_duration()` aborts before render if outside MIN–MAX; bed = looped `bgm*.mp3` pool at volume 0.20 (repo-root wins for CI, `assets/` is local fallback, random pick per run), else generated drone bed
- Captions: `_srt_to_ass` → styled ASS with `Alignment=8`, `FontSize=64`, `MarginV=720` (never `Alignment=5`, which re-centers per cue). Burn with plain `subtitles='...'` and NO `force_style` (commas silently yield zero pixels on some FFmpeg/libass builds). Mux falls back to no-subs render with `::warning::` on filter error
- Dedup memory: `history.json` (kept to 50, last 20 quoted in prompt). Niche `build_prompt(history)` forces slots via `_pick_avoiding` (excludes recent names/jobs/locations/rules) + `history_block` bans recent titles/combos; `similar_to_history` retries similar drafts (`SIMILARITY_RETRIES`=2, best-effort accept). `gen_script()` returns `(meta, slots)`; `main()` records the entry after render (even if upload skipped). `load/save_history` must never fail the run; widen pools instead of adding new state files. Ranking branch: `build_prompt` draws from `TOPICS`, Gemini returns `items` (5 names, #5→#1) used for item-overlap checks; horror ruts still banned via `BANNED_DEFAULTS` (Mark, morgue, hospital)
- `cleanup_intermediates()` runs in `finally`, deletes `INTERMEDIATE_PATTERNS` only, keeps Pexels cache. Upload uses `categoryId=22`, resumable `next_chunk()` with retry
