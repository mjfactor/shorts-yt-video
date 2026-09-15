# AGENTS.md

Top-5 countdown Shorts automation. `pipeline.py` is a thin entry (`main()` →
`core.run(niche_ranking)`); `core.py` owns all shared machinery
(Gemini/TTS/Pexels/FFmpeg/upload/history); `niche_ranking.py` is the
self-contained niche behind the interface documented at the top of `core.py`.
Imports flow one way (niche → core); core never imports a niche.
Stale `NICHES` registry mentions in `core.py`'s docstring,
`niche_ranking.py`'s docstring, and `README.md` — no such registry exists;
the single niche is wired directly in `pipeline.py`.
Run: `python pipeline.py` → Gemini script (with beats) → TTS mp3+srt →
visuals → FFmpeg 1080x1920 → YouTube (+ optional TikTok via Buffer).
Output `out/<stamp>_short.{mp4,mp3,srt,json}` (stamp `%Y-%m-%d_%H%M%S`).

## Commands

- Full run (needs `ffmpeg` + `ffprobe` on PATH): `python pipeline.py`
- Syntax check (no tests/lint/typecheck in repo, Python `3.11` per CI): `python -m py_compile pipeline.py core.py niche_ranking.py get_refresh_token.py`
- Deps: `pip install -r requirements.txt`
- OAuth helper, local-only with browser, never CI: `python get_refresh_token.py` (needs `YT_CLIENT_ID`/`YT_CLIENT_SECRET`)

## CI (`.github/workflows/daily.yml` is source of truth)

- `ubuntu-latest`, Python `3.11`, `timeout-minutes: 30`, schedule `0 8,20 * * *` (08:00 + 20:00 UTC) + `workflow_dispatch`
- `concurrency.group: shorts-single`, `cancel-in-progress: false`; `permissions.contents: write` — each run commits `history.json` back, so runs must stay serial
- Commit-back: `git add history.json` + `chore: record short history [skip ci]` + push (no-op when unchanged). Triggers are schedule/dispatch only, so the push never loops
- Artifacts: `out/*.json` on `always()` 90d; `out/*.mp4`+`*.mp3`+`*.srt` on `failure()` only 3d (YouTube is permanent storage)
- New env vars must be wired through workflow `env:` — secrets/vars do not flow automatically. Unset there (code defaults win): `GEMINI_MODELS`, `GEMINI_TTS_MODELS`, `MIN/MAX_AUDIO_SEC`, `CF_IMAGE_MODEL`, `CF_NUM_STEPS`, `STILLS_PER_BEAT`. `TTS_*`/`VISIBILITY`/`GEMINI_TTS_VOICES`/`GEMINI_TTS_STYLE` come from `vars.*`, the rest from `secrets.*`
- `run()` calls `gen_audio()` without `topic`, so `pick_tts_style()` is dead code — Gemini TTS always uses `GEMINI_TTS_STYLE`
- Workflow defaults `GEMINI_TTS_STYLE` to `''` when the var is unset, overriding the code default with no style direction in CI

## Config (all env, see `.env.example`; never hardcode/commit)

- Required for the YouTube path: `GEMINI_API_KEY`, `PEXELS_API_KEY`, `YT_CLIENT_ID`/`YT_CLIENT_SECRET`/`YT_REFRESH_TOKEN`
- `VISIBILITY` default `private` — keep until the OAuth app passes YouTube audit
- `GEMINI_MODELS` tried in order, first win; same pattern for `GEMINI_TTS_MODELS`
- `MIN_AUDIO_SEC=10`/`MAX_AUDIO_SEC=180`; `CF_IMAGE_MODEL` default `@cf/bytedance/stable-diffusion-xl-lightning`; `CF_NUM_STEPS` default 8; `STILLS_PER_BEAT` default 2
- `VOICE` (`en-US-ChristopherNeural`) hardcoded in `core.py`; only `TTS_RATE` (`-5%`) / `TTS_PITCH` (`-10Hz`) / `TTS_VOLUME` (`+0%`) are env-tunable. No custom SSML (Microsoft rejects non-Edge SSML) — voice/punctuation are the expressiveness levers
- `TTS_ENGINE=gemini` default chain `gemini` → `edge`. `edge` = edge-tts only, word-level SRT. Gemini = per-beat synth, voices alternate per `GEMINI_TTS_VOICES` (one = single narrator, two = man/woman), beat-level SRT (audio-only, no word timings)
- `OPENROUTER_API_KEY` is wired but reserved — no engine reads it; do not remove
- `history.json` + root `bgm*.mp3` are committed (bed pool, CI-visible) — never gitignore them, never store secrets in history. `out/`, `assets/`, `.env` stay uncommitted
- TikTok via Buffer (`core.buffer_tiktok_upload`, free plan): needs `BUFFER_API_KEY` only. Cloudinary host hardcoded (`epuxrxa5`/`buffer_shorts`, unsigned preset, not sensitive; env overrides win). Buffer has no upload endpoint — the mp4 needs a public URL live until publish. `BUFFER_TIKTOK_CHANNEL_ID` optional (auto-picks `service==tiktok`)
- Buffer caption cut to 2200 UTF-16 units. User text via `$input` GraphQL variables, never f-string interpolation (control chars/emoji → API 500 `Syntax Error`). Never fails the run; post id recorded as `tiktokBufferPostId`. Do not add cookie/`sessionid` uploaders or a self-registered TikTok app (SELF_ONLY until audit)

## Graceful degradation (do not break)

- No Gemini key/fail → offline fallback script (~200 words, largest-animals countdown). `_validate_script` raises `ValueError` on bad output; `items` passes through via `validate_extra`
- No CF creds → `cloudflare_image` returns `None` → `beat_card`. `pexels_clip` returns `None` on no key/fail (warn; partial downloads deleted)
- No YouTube creds → skip upload, keep video in `out/`
- TTS: missing keys skip with warn; any engine failure warns and falls back to edge for the whole narration. Fail-fast on first bad beat — never per-beat retries. `gen_audio()` returns `(mp3, srt, beat_durations|None)`; `run()` rescales measured durations to the muxed total, else word-count weighting
- `notify_failure()` never raises (keep every send wrapped in try). `make_bed()` fallback is pool → generated drone only — don't add raising paths
- `retry()` retries 429/5xx only; other 4xx fail fast. `RefreshError` is fatal → re-run `get_refresh_token.py`
- `warn()`/`err()` emit `::warning::`/`::error::` annotations (degraded/fatal paths); `log()` for normal flow. `run()` logs non-secret config + mp3/srt sizes first — check there when debugging. All console output goes through `_print` (survives Windows cp1252 against Gemini emoji)

## Pipeline gotchas

- edge-tts needs `--flag=value` form (`f"--rate={TTS_RATE}"`) — values like `-10%` misparse as new flags when passed as separate args
- Beats: `meta["beats"]=[{text,query,label}]`, 7 in order (hook, #5–#1, closer); validator caps at 8. `label` is rank digit `"5"`–`"1"` or `""` (digits only). Joined texts == narration (~200–220 words)
- Visuals per beat in order: Pexels clip (beat's OWN query, literal match, `USE_MOOD=False`; `portrait`, smallest file ≥720p, cached `assets/pexels_*.mp4`, per-run `used` set skips repeats; 2nd-half cuts seek via `_stock_offset` with `-stream_loop` guard) → Cloudflare still → Pillow `beat_card`. `MOOD_WORDS`/`_mood_query`/`AMBIENT_QUERIES` are dead horror remnants for this niche — do not pass `mood=True`
- CF prompt is `niche_ranking.image_prompt` (daylight subject photo + cleaned half-beat text; `_split_beat` gives mid-beat cuts different moments, `_clean_beat_text` strips CTA filler). Stills get Ken Burns zoom. Screen time is word-count weighted per beat, split evenly across its cuts. Known conflict: shared CF `negative_prompt` bans `daytime, sunlight, cheerful colors` (`core.py`) while the niche prompt asks for `natural daylight or golden-hour` — check both sides before touching either
- Leaderboard (`niche_ranking.board_filter`) applies at the FINAL mux only (per-segment clocks restart at 0): dim `N.` slots always on, white `N. Name` fill from reveal to end, red highlight during the beat (`enable='between(t,a,b)'`). Needs a font (`_RANK_FONTS`: DejaVu Bold on CI, Arial Bold locally) + `drawtext`, else skipped with `::warning::`; mux falls back to no-board render on drawtext error. Names are ASCII-sanitized, auto-shrunk 66px→40px min, filter-escaped (single-font drawtext has no emoji)
- Captions: `_srt_to_ass` → ASS (`Alignment=8`, `FontSize=34`, `MarginV=320` — tiny, top-anchored just below the title, above the leaderboard). Burn with plain `subtitles='...'` and NO `force_style` (commas silently yield zero pixels on some builds). Mux falls back to no-subs render with `::warning::`
- `check_audio_duration()` aborts before render if outside MIN–MAX. Bed = looped `bgm*.mp3` pool at volume 0.20 (repo root wins for CI, `assets/` local fallback, random pick) else generated drone
- Dedup: `history.json` kept to 50, last 20 quoted in prompt. `build_prompt` forces fresh slots (`_pick_avoiding`) + `history_block` bans recents; `similarity_reason` (same topic, ≥2 shared items, banned hook in `BANNED_TITLE_HOOKS`) retries (`SIMILARITY_RETRIES`=2, best-effort accept — `history.json` proves leaks aired, so don't trust prompt bans). `sanitize_title` runs AFTER the similarity verdict and rebuilds non-`5 ...` titles from the topic
- `run()` records the history entry after render even if upload skipped. `load/save_history` must never fail the run; widen pools instead of new state files
- `cleanup_intermediates()` runs in `finally`, deletes `INTERMEDIATE_PATTERNS` only, keeps Pexels cache. Upload uses `categoryId=22`, resumable `next_chunk()` with retry
- Stale prose — trust code/CI over docs: `README` still says edge-tts narrates (default is Gemini) and frames committed `bgm*.mp3` as hypothetical; `.env.example` omits `GEMINI_TTS_MODELS` (code default wins silently); duration prose conflicts (`~90s` vs `~120s`) — the only enforced guard is `MIN/MAX_AUDIO_SEC`
