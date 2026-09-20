# AGENTS.md

Top-5 countdown Shorts automation. `pipeline.py` thin entry (`main()` → `core.run(niche_ranking)`); `core.py` owns shared machinery (Gemini/TTS/Pexels/FFmpeg/upload/history); `niche_ranking.py` is the self-contained niche behind the interface at top of `core.py`. Imports flow one way (niche → core); core never imports a niche.
Run: `python pipeline.py` → Gemini script (beats) → TTS mp3+srt → visuals → FFmpeg 1080x1920 → YouTube (+ optional TikTok via Buffer). Output `out/<stamp>_short.{mp4,mp3,srt,json}` (stamp `%Y-%m-%d_%H%M%S`).

## Commands

- Full run (needs `ffmpeg` + `ffprobe` on PATH): `python pipeline.py`
- Syntax check (no tests/lint/typecheck in repo, Python `3.11` per CI): `python -m py_compile pipeline.py core.py niche_ranking.py get_refresh_token.py`
- Deps: `pip install -r requirements.txt`
- OAuth helper, local-only with browser, never CI: `python get_refresh_token.py` (needs `YT_CLIENT_ID`/`YT_CLIENT_SECRET`)

## CI (`.github/workflows/daily.yml` is source of truth)

- `ubuntu-latest`, Python `3.11`, `timeout-minutes: 30`, schedule `0 8,20 * * *` + `workflow_dispatch`
- `concurrency.group: shorts-single`, `cancel-in-progress: false`; `permissions.contents: write` — each run commits `history.json` back, so runs must stay serial
- Commit-back: `git add history.json` + `chore: record short history [skip ci]` + push (no-op when unchanged). Schedule/dispatch triggers only, so the push never loops
- Artifacts: `out/*.json` on `always()` 90d; `out/*.mp4`+`*.mp3`+`*.srt` on `failure()` only 3d (YouTube is permanent storage)
- New env vars must be wired through workflow `env:` — secrets/vars do not flow automatically. Several code defaults are unset there and win silently (`GEMINI_MODELS`, `GEMINI_TTS_MODELS`, `MIN/MAX_AUDIO_SEC`, `STILLS_PER_BEAT`, `PEXELS_MIN_WIDTH`, …)
- Empty-string env counts as unset in code (`(os.getenv(...) or "").strip() or DEFAULT`), so CI's empty `GEMINI_TTS_STYLE` falls back safely; only non-blank wins and skips tone alternation

## Config (all env, see `.env.example`; never hardcode/commit)

- `GEMINI_API_KEY` (+ `GEMINI_API_KEY_2..10` failover; comma-separated keys in one var also work). Sticks with working key; 429 backs off with jitter on SAME key, other 4xx/timeout fail over immediately; HTTP 5xx is per-project overload so all keys are tried (never trips the breaker); 2 keys failing with only timeouts/network errors trips a breaker skipping the rest
- `VISIBILITY` default `private` — keep until OAuth app passes YouTube audit
- `GEMINI_MODELS` / `GEMINI_TTS_MODELS` tried in order, first win
- `MIN_AUDIO_SEC=10` / `MAX_AUDIO_SEC=180`; `STILLS_PER_BEAT` default 3 (`.env.example` comment saying 2 is stale); `PEXELS_MIN_WIDTH` default 1080 (1:1 with canvas)
- `VOICE` (`en-US-ChristopherNeural`) hardcoded; only `TTS_RATE` (`-5%`) / `TTS_PITCH` (`-10Hz`) / `TTS_VOLUME` (`+0%`) / `BGM_VOLUME` (`0.08`) are env-tunable. No custom SSML (Microsoft rejects non-Edge SSML)
- `TTS_ENGINE=gemini` default chain `gemini` → `edge`. `edge` = one edge-tts call, word-level SRT. `gemini` = per-beat synth, beat-level SRT, one voice per run (`Charon` default = no alternation; `GEMINI_TTS_VOICES` pool rotates by `history_len % len(pool)`). Delivery adapts per beat role + content mood (~145 WPM target via `GEMINI_TTS_TARGET_WPM`, atempo-calibrated). Explicit non-blank `GEMINI_TTS_STYLE` overrides.
- `OPENROUTER_API_KEY` wired but reserved — no engine reads it; do not remove
- `history.json` + root `bgm*.mp3` are committed (never gitignore, never store secrets in history). `out/`, `assets/`, `.env` stay uncommitted (per `.gitignore`)
- Bed rotation is strict `pool[history_len % len(pool)]` over sorted pool (random only when history unreadable). Repo root wins for CI, `assets/` is local fallback
- TikTok via Buffer (`core.buffer_tiktok_upload`): needs `BUFFER_API_KEY` only. Cloudinary host hardcoded (`epuxrxa5`/`buffer_shorts`, unsigned; env overrides win). Buffer has no upload endpoint — mp4 needs a public URL live until publish. `BUFFER_TIKTOK_CHANNEL_ID` optional (auto-picks `service==tiktok`)
- Buffer caption cut to 2200 UTF-16 units. User text via `$input` GraphQL variables, never f-string interpolation (emoji/control chars → API 500). Never fails the run; post id recorded as `tiktokBufferPostId`. Do not add cookie/`sessionid` uploaders or self-registered TikTok app

## Graceful degradation (do not break)

- No Gemini key/fail → offline fallback rotation (3 hard-coded variants), least-recent-first, never repeating last video. `_validate_script` raises `ValueError` on bad output; `items` passes through via `validate_extra`
- Beats with no stock render Pillow `beat_card` (warn; only visual fallback — no AI imagery). `pexels_clip` returns `None` on no key/fail (warn; partial downloads deleted)
- No YouTube creds → skip upload, keep video in `out/`
- TTS: missing keys skip with warn; any engine failure warns and falls back to edge for whole narration. Fail-fast on first bad beat, never per-beat retries. `gen_audio()` returns `(mp3, srt, beat_durations|None)`; `run()` rescales measured durations to muxed total, else word-count weighting
- `notify_failure()` never raises (keep every send wrapped in try). `make_bed()` fallback is pool → generated drone only
- `retry()` retries 429/5xx only; other 4xx fail fast. `RefreshError` is fatal → re-run `get_refresh_token.py`
- `warn()`/`err()` emit `::warning::`/`::error::` annotations; `log()` for normal flow. `run()` logs non-secret config + mp3/srt sizes first. All output via `_print` (survives Windows cp1252 vs Gemini emoji)

## Pipeline gotchas

- edge-tts needs `--flag=value` form (`f"--rate={TTS_RATE}"`) — values like `-10%` misparse as new flags as separate args
- Beats: `meta["beats"]=[{text,query,label}]`, 7 in order (hook, #5–#1, closer); validator caps at 8. `label` is rank digit `"5"`–`"1"` or `""` (digits-only, `[:2]`). Prompt demands joined texts == narration (~195–205 words) but validator only checks ≥20 words
- Visuals per beat: Pexels video vs photos compete head-to-head (video score×2 vs best photo, ties go video; `portrait`, smallest file ≥`PEXELS_MIN_WIDTH`, cached `assets/pexels_*.mp4`, per-run `used` set skips repeats; 2nd-half cuts seek via `_stock_offset`) → Pillow `beat_card`. Photo win emits one DISTINCT photo per cut (Ken Burns). Beat's OWN query, literal match, `USE_MOOD=False`
- Beat queries are 1-3 words; labeled beats (#5..#1 zipped positionally with `items`) search tiers exact → family → topic → `beat_card`, gated by item + domain keywords against homonyms. Per-beat log carries `tier=` + `winner=` + 60-char alt snippet for CI diagnosis. `MOOD_WORDS`/`_mood_query`/`AMBIENT_QUERIES` are dead — do not pass `mood=True`
- `_split_beat` gives mid-beat cuts different moments (stock seeks + card text splits). Screen time is word-count weighted, split evenly with `max(2.0s)` floor per segment
- Leaderboard (`niche_ranking.board_filter`) applies at FINAL mux only (segment clocks restart at 0): dim `N.` slots always on, white fill from reveal to end, red highlight during beat. Needs font (DejaVu Bold CI / Arial Bold local) + `drawtext`, else skip with `::warning::`; mux falls back to no-board render on error. Names ASCII-sanitized, auto-shrunk 66px→40px, filter-escaped (no emoji)
- Mystery #1 blur: #1 beat `boxblur` for `min(2s, half beat)` then snaps clear at FINAL mux; fallback to no-blur render on filter error
- Captions: SRT → ASS (yellow bold, top-anchored below title). Burn with plain `subtitles='...'` and NO `force_style` (commas yield zero pixels on some builds). Fallback to no-subs render with `::warning::`
- `check_audio_duration()` aborts before render if outside MIN–MAX. Bed = looped pool at `BGM_VOLUME` else generated drone
- Dedup: `history.json` kept to 110; last 20 quoted in prompt (advisory only). Hard gates: same-topic/title vs full 110 (`DEDUP_WINDOW`), ≥2 shared items vs last 30 (`DEDUP_ITEM_WINDOW`), story word-overlap >35% vs last 30. Hybrid topics: `propose_topic()` suggests fresh topic (validated; `AI_TOPICS=off` kills it), pool draw is backstop (LRU on exhaustion), source recorded as `topicSource`. `similarity_reason` retries (`SIMILARITY_RETRIES`=2, best-effort accept — leaks have aired, don't trust prompt bans). `sanitize_title` runs AFTER verdict and rebuilds non-`5 ...` titles
- `run()` records history entry (date/title/niche/tags/narration/bed/videoId/tiktokBufferPostId + topic/topicSource/items) after successful render even if upload skipped; render failure records nothing. `load/save_history` must never fail the run
- `cleanup_intermediates()` runs in `finally`, deletes `INTERMEDIATE_PATTERNS` only, keeps Pexels cache. Upload uses `categoryId=22`, resumable `next_chunk()` with retry
- Stale prose — trust code/CI over docs: `README` says edge-tts narrates (default is Gemini) and frames committed `bgm*.mp3` as hypothetical; `pipeline.py` no longer has a `NICHES` registry but `core.py`/`niche_ranking.py` docstrings and `README` still mention one; `.env.example` omits `GEMINI_TTS_MODELS`; duration prose conflicts (`~90s` vs `~120s`) — only enforced guard is `MIN/MAX_AUDIO_SEC`
