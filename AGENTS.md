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
- New env vars must be wired through workflow `env:` — secrets/vars do not flow automatically. Unset there (code defaults win): `GEMINI_MODELS`, `GEMINI_TTS_MODELS`, `MIN/MAX_AUDIO_SEC`, `STILLS_PER_BEAT`, `PEXELS_MIN_WIDTH`
- Empty-string env counts as unset in code (`(os.getenv(...) or "").strip() or DEFAULT`), so CI's empty `GEMINI_TTS_STYLE` safely falls back to the default; only a non-blank value wins and skips tone alternation

## Config (all env, see `.env.example`; never hardcode/commit)

- Required for the YouTube path: `GEMINI_API_KEY` (+ `GEMINI_API_KEY_2..10` failover — code supports up to 10, docs show 2..4; comma-separated keys in one var also work). `_fetch_gemini` sticks with the working key; 429 backs off with jitter on the SAME key, other 4xx/timeout fail over immediately; 2 keys failing with only 5xx/timeout trips a breaker that skips the rest
- `VISIBILITY` default `private` — keep until the OAuth app passes YouTube audit
- `GEMINI_MODELS` tried in order, first win; same pattern for `GEMINI_TTS_MODELS`
- `MIN_AUDIO_SEC=10`/`MAX_AUDIO_SEC=180`; `STILLS_PER_BEAT` default 3 (not 2); `PEXELS_MIN_WIDTH` default 1080 (1:1 with canvas)
- `VOICE` (`en-US-ChristopherNeural`) hardcoded in `core.py`; only `TTS_RATE` (`-5%`) / `TTS_PITCH` (`-10Hz`) / `TTS_VOLUME` (`+0%`) are env-tunable. No custom SSML (Microsoft rejects non-Edge SSML) — voice/punctuation are the expressiveness levers
- `TTS_ENGINE=gemini` default chain `gemini` → `edge`. `edge` = one edge-tts call, word-level SRT. `gemini` = per-beat synth, beat-level SRT (audio-only, no word timings), single voice per run (default `Charon` = no voice alternation; a multi-voice `GEMINI_TTS_VOICES` pool alternates by `history_len % len(pool)`). Body tone alternates energetic/deep by history parity unless `GEMINI_TTS_STYLE` is explicitly set; hook beat 0 always gets the super-energetic prefix. `run()` never passes `topic`, so `pick_tts_style(topic,text)` never fires in the run path
- `OPENROUTER_API_KEY` is wired but reserved — no engine reads it; do not remove
- `history.json` + root `bgm*.mp3` are committed (bed pool, CI-visible) — never gitignore them, never store secrets in history. `out/`, `assets/`, `.env` stay uncommitted
- Bed rotation is strict, not random: `pool[history_len % len(pool)]` over the sorted pool (random only when history is unreadable). Repo root wins for CI, `assets/` is the local fallback
- TikTok via Buffer (`core.buffer_tiktok_upload`, free plan): needs `BUFFER_API_KEY` only. Cloudinary host hardcoded (`epuxrxa5`/`buffer_shorts`, unsigned preset, not sensitive; env overrides win). Buffer has no upload endpoint — the mp4 needs a public URL live until publish. `BUFFER_TIKTOK_CHANNEL_ID` optional (auto-picks `service==tiktok`)
- Buffer caption cut to 2200 UTF-16 units. User text via `$input` GraphQL variables, never f-string interpolation (control chars/emoji → API 500 `Syntax Error`). Never fails the run; post id recorded as `tiktokBufferPostId`. Do not add cookie/`sessionid` uploaders or a self-registered TikTok app (SELF_ONLY until audit)

## Graceful degradation (do not break)

- No Gemini key/fail → offline fallback rotation (3 hard-coded variants: animals/waterfalls/buildings), least-recent-first, never repeating the last video back to back. `_validate_script` raises `ValueError` on bad output; `items` passes through via `validate_extra`
- Beats with no stock render a Pillow `beat_card` (warn; the only visual fallback — no AI imagery). `pexels_clip` returns `None` on no key/fail (warn; partial downloads deleted)
- No YouTube creds → skip upload, keep video in `out/`
- TTS: missing keys skip with warn; any engine failure warns and falls back to edge for the whole narration. Fail-fast on first bad beat — never per-beat retries. `gen_audio()` returns `(mp3, srt, beat_durations|None)`; `run()` rescales measured durations to the muxed total, else word-count weighting
- `notify_failure()` never raises (keep every send wrapped in try). `make_bed()` fallback is pool → generated drone only — don't add raising paths
- `retry()` retries 429/5xx only; other 4xx fail fast. `RefreshError` is fatal → re-run `get_refresh_token.py`
- `warn()`/`err()` emit `::warning::`/`::error::` annotations (degraded/fatal paths); `log()` for normal flow. `run()` logs non-secret config + mp3/srt sizes first — check there when debugging. All console output goes through `_print` (survives Windows cp1252 against Gemini emoji)

## Pipeline gotchas

- edge-tts needs `--flag=value` form (`f"--rate={TTS_RATE}"`) — values like `-10%` misparse as new flags when passed as separate args
- Beats: `meta["beats"]=[{text,query,label}]`, 7 in order (hook, #5–#1, closer); validator caps at 8 (`beats[:8]`). `label` is rank digit `"5"`–`"1"` or `""` (digits-only, `[:2]`). The prompt demands joined texts == narration (~200–220 words) but the validator only checks ≥20 words — don't rely on equality
- Visuals per beat in order: Pexels video vs photos compete head-to-head (video raw score×2 vs best-photo score, ties go video; `portrait`, smallest file ≥`PEXELS_MIN_WIDTH`, cached `assets/pexels_*.mp4`, per-run `used` set skips repeats; 2nd-half cuts seek via `_stock_offset` with `-stream_loop` guard) → Pillow `beat_card`. Photo win emits one DISTINCT photo per cut (Ken Burns'd). Beat's OWN query, literal match, `USE_MOOD=False`. Beat queries are short (2-4 words, MUST start with the exact ranked item name + at most one habitat noun); labeled beats (#5..#1 zipped positionally with `items`, like `board_filter`) search in tiers — exact (beat query + distinctive item word: long ≥6-char word, else any word) → family (item's last word as family guess, e.g. 'Green Anaconda'→'anaconda', gated on that word — no species dictionary, works for any topic) → topic (topic string's generic plural noun threaded from `slots`, e.g. 'snakes', gated both spellings; skipped when the topic has no plural) → `beat_card` last resort. Wrong-family stock never airs at any tier; fallback tiers run only on a miss (bounded extra Pexels calls), and the per-beat log carries `tier=<exact|family|topic|card>` + `winner=<video_id|photo_<id>>` + 60-char photo alt snippet for CI diagnosis. `MOOD_WORDS`/`_mood_query`/`AMBIENT_QUERIES` are dead horror remnants — do not pass `mood=True`
- `_split_beat` gives mid-beat cuts different moments (stock seeks + card text splits). Screen time is word-count weighted per beat, split evenly across its cuts with a `max(2.0s)` floor per segment
- Leaderboard (`niche_ranking.board_filter`) applies at the FINAL mux only (per-segment clocks restart at 0): dim `N.` slots always on, white `N. Name` fill from reveal to end, red highlight during the beat (`enable='between(t,a,b)'`). Needs a font (`_RANK_FONTS`: DejaVu Bold on CI, Arial Bold locally) + `drawtext`, else skipped with `::warning::`; mux falls back to no-board render on drawtext error. Names are ASCII-sanitized, auto-shrunk 66px→40px min, filter-escaped (single-font drawtext has no emoji)
- Mystery #1 blur: the #1 beat stays blurred (`boxblur`) for `min(2s, half the beat)` then snaps clear at the FINAL mux; mux falls back to no-blur render on filter error
- Captions: `_srt_to_ass` → ASS (yellow bold DejaVu Sans, `Alignment=8`, `FontSize=34`, `MarginV=320` — tiny, top-anchored just below the title, above the leaderboard). Burn with plain `subtitles='...'` and NO `force_style` (commas silently yield zero pixels on some builds). Mux falls back to no-subs render with `::warning::`
- `check_audio_duration()` aborts before render if outside MIN–MAX. Bed = looped `bgm*.mp3` pool at volume 0.20 else generated drone
- Dedup: `history.json` kept to 110 (100 videos + headroom), last 20 quoted in prompt (advisory bans only — token sanity). Hard gates: same-topic/title vs full 110 (`DEDUP_WINDOW`), ≥2 shared items vs last 30 (`DEDUP_ITEM_WINDOW`, common animals would false-positive across 110), story word-overlap >35% vs last 30. Hybrid topics: `propose_topic()` lets Gemini suggest a fresh topic (validated: fresh vs full window, sane shape, 5 filmable subjects, subjects pre-checked vs last 30; `AI_TOPICS=off` kills it), pool draw is the backstop (LRU on exhaustion), source recorded as `topicSource`. `_pick_avoiding` casefolds both sides. Offline fallback rotates 3 hard-coded variants least-recent-first, never repeating the last video back to back. `build_prompt` forces fresh slots (`_pick_avoiding`) + `history_block` bans recents; `similarity_reason` (same topic, ≥2 shared items, banned hook in `BANNED_TITLE_HOOKS`) retries (`SIMILARITY_RETRIES`=2, best-effort accept — `history.json` proves leaks aired, so don't trust prompt bans). `sanitize_title` runs AFTER the similarity verdict and rebuilds non-`5 ...` titles from the topic
- `run()` records the history entry (date/title/niche/tags/narration/bed/videoId/tiktokBufferPostId + topic/topicSource/items) after successful render even if upload skipped; render failure records nothing. `load/save_history` must never fail the run; widen pools instead of new state files
- `cleanup_intermediates()` runs in `finally`, deletes `INTERMEDIATE_PATTERNS` only, keeps Pexels cache. Upload uses `categoryId=22`, resumable `next_chunk()` with retry
- Stale prose — trust code/CI over docs: `README` still says edge-tts narrates (default is Gemini) and frames committed `bgm*.mp3` as hypothetical; `.env.example` omits `GEMINI_TTS_MODELS` (code default wins silently); duration prose conflicts (`~90s` vs `~120s`) — the only enforced guard is `MIN/MAX_AUDIO_SEC`
