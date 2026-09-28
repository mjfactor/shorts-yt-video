# AGENTS.md

Multi-niche Shorts automation (`story` default, `explainer` + `ranking` live). Layout: `pipeline.py` thin entry (`NICHE` env → `niches.load_niche()` → `core.run(niche)`); `core/` orchestration (stage chain + upload + history, imports only `niches.base` contracts); `engine/` policy-free toolbox (TTS/Pexels/FFmpeg/captions — never imports core or any niche); `niches/<name>/` 3 stages (`content.generate_content` → `narration.narrate` → `visuals.build_visuals`, engine helpers only). Imports flow one way (niche → engine/base); core never imports a niche.
Run: `NICHE=story python pipeline.py` → Gemini script → TTS audio+timings → visuals → FFmpeg 1080x1920 → YouTube (+ optional TikTok via Buffer). Output `out/<stamp>_short.{mp4,mp3,srt,json}` (stamp `%Y-%m-%d_%H%M%S`).

## Commands

- Full run per niche (needs `ffmpeg` + `ffprobe` on PATH): `NICHE=story python pipeline.py` / `NICHE=explainer python pipeline.py` / `NICHE=ranking python pipeline.py` (bare `python pipeline.py` = story default)
- Offline story demo (zero API keys, synthesizes its own testsrc background, `out/demo_story.mp4` 1080x1920, never uploads): `python demo.py` — story ONLY, there is no `--niche` flag (it would error); the only flags are `--tts {auto,gemini,edge}` and `--words N` (e.g. `python demo.py --tts edge --words 120`)
- Unit tests (all niches): `python -m unittest discover -s tests` (legacy single file: `python -m unittest tests/test_explainer.py`)
- Dry-run order: tests → `python demo.py` (story only) → `NICHE=<name> python pipeline.py`
- Syntax check (Python `3.11` per CI): `python -m py_compile pipeline.py core.py demo.py get_refresh_token.py` (+ any `niches/<name>/*.py`, `engine/*.py` you touch)
- Deps: `pip install -r requirements.txt` (must include `edge-tts`, `Pillow`, `requests`; no local transcription models)
- OAuth helper, local-only with browser, never CI: `python get_refresh_token.py` (needs `YT_CLIENT_ID`/`YT_CLIENT_SECRET`)
- Standalone Shorts-thumbnail API diagnostic, NOT part of the pipeline (never uploads/edits a video, never writes `history.json`; it DOES overwrite one Short's thumbnail with a magenta test card): `python tools/thumb_probe.py` → `out/thumb_probe.{json,jpg}`

## New-niche guide (zero core edits)

- Copy `niches/_template/` → `niches/<name>/` (skipped by discovery while prefixed `_`); rename `NAME`/`LABEL`, implement `content.py` / `narration.py` / `visuals.py` + `<Name>Config.from_env()` in `config.py` using `niches.base.env_*` (`<PREFIX>_` wins, bare name falls back); expose module-level `niche = <Name>Niche()`
- Registry auto-discovery (`niches/__init__.py`): no core/pipeline edits — `available_niches()` picks up the subpackage, `load_niche()` serves it via `NICHE=<name>`; unknown names raise `ImportError` listing available niches
- Stage rules: `generate_content` returns `Content` (beats + queries in extras, joined beat texts == narration); `narrate` returns `NarrationResult` (audio + duration + timed `words`); `build_visuals` returns `VisualsResult` (mp4 + srt + credits/used_ids); call `engine/*` helpers only, never import core; keep all knobs in config (never hardcode)
- Visuals sources are optional per niche, not required deps: `engine.pexels` returns `None` on no key/fail (deprecated explainer/ranking are its only current users — story uses local clips), and the demo synthesizes its own background. A new niche may skip Pexels entirely; it never may hardcode a key
- Wire-through: document vars in `.env.example` (generic + `<PREFIX>_` section), add them to `daily.yml` `env:`, add `sample_content()` coverage in `tests/` + verify `NICHE=<name> python pipeline.py` (`demo.py` is story-only, so it cannot smoke-test a new niche)

## CI (`.github/workflows/daily.yml` is source of truth)

- `ubuntu-latest`, Python `3.11`, `timeout-minutes: 30`, schedule `0 8,20 * * *` + `workflow_dispatch`
- `concurrency.group: shorts-single`, `cancel-in-progress: false`; `permissions.contents: write` — each run commits `history.json` back, so runs must stay serial
- `NICHE: ${{ vars.NICHE || 'story' }}` selects the niche (single-niche default; fan out with a `strategy.matrix.niche` + `NICHE: ${{ matrix.niche }}` for multi-niche); per-niche tuning via `STORY_*` / `EXPLAINER_*` / `RANKING_*` vars in the same `env:` block
- Commit-back: `git add history.json` + `chore: record short history [skip ci]` + push (no-op when unchanged). Schedule/dispatch triggers only, so the push never loops
- Artifacts: `out/*.json` on `always()` 90d; `out/*.mp4`+`*.mp3`+`*.srt` on `failure()` only 3d (YouTube is permanent storage)
- `.github/workflows/thumb_probe.yml` is a separate MANUAL probe (`workflow_dispatch` only, no schedule/push, `permissions: contents: read`, own `concurrency.group: thumb-probe-single`, no ffmpeg, commits nothing) — it must never queue behind or block `shorts-single`
- New env vars must be wired through workflow `env:` — secrets/vars do not flow automatically. Prefixed per-niche vars need explicit entries too (bare fallbacks alone won't pick up a repo Variable set under the prefixed name)

## Config (all env, see `.env.example`; never hardcode/commit)

- `NICHE` (`story` default) — selects the subpackage; registry normalizes case/whitespace
- Generic: `GEMINI_API_KEY` (+ `_2..10` failover, comma-separated ok), `PEXELS_API_KEY` (shared `engine.pexels` toolbox only — the deprecated explainer/ranking niches; story never reads it), `VISIBILITY` (default `private` — keep until OAuth audit), `TTS_RATE`/`TTS_PITCH`/`TTS_VOLUME`/`BGM_VOLUME`, `MIN/MAX_AUDIO_SEC` (guard only), timeouts/tries/backoff
- Per-niche (prefix wins, bare falls back): `STORY_*` (`TTS_MODEL/VOICES`, `NARRATION_SPEED`, `BG_DIR` — the single background source, `BG_SINGLE_SPEED`, `CAPTION_UPPER`, `CARD_SECONDS`, `CHANNEL_HANDLE`, `AVATAR_PATH`, `MAX_SECONDS`, `TARGET_MIN/MAX` 80/85, `STORY_GENRES`, `WHISPER_MODEL`), `EXPLAINER_*` (`VOICE`, `TTS_RATE/PITCH/VOLUME/ENGINE`, `GEMINI_TTS_VOICES`, `STILLS_PER_BEAT`, `MIN/MAX_AUDIO_SEC`, `PEXELS_MIN_WIDTH`), `RANKING_*` (`VOICE/VOICES`, `TTS_RATE/PITCH/VOLUME/STYLE`, `TONE`, `TARGET_WPM`, `STILLS_PER_BEAT`, `MIN/MAX_AUDIO_SEC`, `PEXELS_MIN_WIDTH`, `PEXELS_CACHE_DIR`, `BGM_VOLUME`)
- Story TTS: chunked Gemini synth (60–90w chunks, PCM cache, 80ms crossfade) with duration-fit loop (80–85 proceed; ±5 re-speed from cache; wider one paced re-synth; trim past MAX at sentence end); word timings on the FINAL mix are estimated proportionally (no local models)
- `history.json` + `assets/background-musics/bgm*.mp3` (BGM pool folder; the repo root is still honored as a legacy fallback) + brand `assets/fonts/*.ttf/*.otf` + `assets/avatar.png` are committed (never gitignore, never secrets in history). `out/`, `assets/*` (except brand fonts/avatar, the BGM pool + `.gitkeep`), `assets/pexels_cache/`, `.env` stay uncommitted. Pexels cache (`assets/pexels_cache/`) is gitignored but kept between runs (only the deprecated niches still fill it)
- Branding is config: `CHANNEL_HANDLE`, `AVATAR_PATH` (`assets/avatar.png`; missing file → drawn-initials fallback), `assets/fonts/` (brand ttf → DejaVu CI / Arial local → Pillow default). Card theme consts live in niche `visuals.py` — edit only for real theme changes
- TikTok via Buffer: `BUFFER_API_KEY` only (+ optional `BUFFER_TIKTOK_CHANNEL_ID`); Cloudinary host has working defaults (env overrides win); never fails the run (`tiktokBufferPostId` recorded). Do not add cookie/`sessionid` uploaders

## History (per-niche) + migration

- `history.json` is committed in the GROUPED shape `{"<niche>": [entry, ...]}` — `save_history` always writes it through `migrate_history`; `load_history` accepts grouped (via `flatten_history`) or a legacy flat list, and migrates the flat list to grouped on load (best-effort, silent). Trimmed to the last `HISTORY_KEEP` = 110 entries across all niches. Never hand-reformat the file
- Entry fields actually written by `run()`: `date`, `title`, `niche`, `tags` (≤10), `narration`, `videoId`, `tiktokBufferPostId` (may be `null`), `used_ids`, plus `topic`/`topic_source` copied from slots and `narratorGender` when set. `used_ids` is the stock-footage id list and is now always `[]` for story (single local take embeds nothing) — the reader was removed with the Pexels path, so the field is contract-only; entries written before that still carry real ids. `genre`/`items` are NOT recorded: `history_fields()` in `niches/story/content.py` is dead code (nothing calls it; only the deprecated explainer calls its own)
- Dedup is per-niche and reads the flat view: story = exact title/premise + >35% word overlap. `narratorGender` is recorded for reference only — `_narrator_gender` picks the voice from the UTC half-day clock and deliberately ignores history (history-gating had frozen the voice once on a failed CI push-back)
- `niches/history_migrate.py` (`migrate_history` flat→grouped, `flatten_history` back sorted by date, `duplicate_reason` per-niche title/topic check) is pure/lossless and is the ONLY sanctioned way to change the on-disk shape
- `run()` records the entry after successful render even if upload skipped; render failure records nothing; `load/save_history` must never fail the run

## Graceful degradation (do not break)

- No Gemini key/fail → offline fallback variants (least-recent-first, never repeat last); validators raise `ValueError` on bad output; similarity regens best-effort accept (leaks have aired — don't trust prompt bans)
- Story background: local clips are REQUIRED — an empty `STORY_BG_DIR` is a hard failure (`RuntimeError`), and a clip that will not render is skipped with `::warning::` while the next one is tried; when every clip fails the run stops hard (there is no stock-footage fallback). No BGM pool → synthesized drone bed. `engine.pexels` (deprecated niches only) returns `None` on no key/fail (partial downloads deleted)
- No YouTube creds → skip upload, keep video in `out/`; `notify_failure()` never raises
- `retry()` retries 429/5xx only, other 4xx fail fast; `RefreshError` is fatal → re-run `get_refresh_token.py`
- `warn()`/`err()` emit `::warning::`/`::error::`; `log()` normal flow; all output via `_print` (survives Windows cp1252 vs emoji)

## Gotchas (trust code/CI over docs)

- Captions: burn with plain `subtitles='...'` / `ass_cwd_args` (cwd-relative, no drive-colon issues) and NO `force_style` (commas yield zero pixels on some builds); fallback to no-subs render with `::warning::`
- Story planner is pure: `total = narration.duration + 0.4`; card overlay `max(CARD_SECONDS, card_line_end + pad)` with 0.2s fade pop
- Background source: `STORY_BG_DIR` (`assets/backgrounds`, default) is the ONLY source — committed `bg1.mp4 … bg10.mp4` (any `.mp4/.mov/.webm/.mkv`), then played as ONE continuous take at `STORY_BG_SINGLE_SPEED` (1.8x) cut from a random start offset (short sources loop to cover). Which clip is picked is `STORY_BG_SELECT`: `rotate` (default) walks the pool in NATURAL filename order keyed on `video_number` (`len(same-niche history)`), so consecutive uploads use consecutive clips and wrap; `random` shuffles. Natural order matters — plain `sorted()` puts `bg10` before `bg2`. The order is the whole pool rotated (not just the head), so a broken clip still falls through to the next; the start offset inside the clip stays random under both modes. There is exactly one clip per video, so there are no per-beat queries, no segment/timelapse planner, no min_clips warning and no credits/used_ids (no stock footage is embedded). The folder is the ONLY thing CI relies on for visuals — there is no background-fetch step, so the committed clips are what a runner gets
- edge-tts needs `--flag=value` form — values like `-10%` misparse as new flags as separate args
- Shorts ceiling: verify current YouTube max before raising `TARGET_MAX`/`MAX_SECONDS` (don't assume ≤60s either — only `MIN/MAX_AUDIO_SEC` is enforced)
- Stale-prose rule: when docs disagree, code + `daily.yml` win — then fix the docs (these files), never the reverse
