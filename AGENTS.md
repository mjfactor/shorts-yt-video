# AGENTS.md

Multi-niche Shorts automation (`story` default, `explainer` + `ranking` live). Layout: `pipeline.py` thin entry (`NICHE` env → `niches.load_niche()` → `core.run(niche)`); `core/` orchestration (stage chain + upload + history, imports only `niches.base` contracts); `engine/` policy-free toolbox (TTS/Pexels/FFmpeg/captions — never imports core or any niche); `niches/<name>/` 3 stages (`content.generate_content` → `narration.narrate` → `visuals.build_visuals`, engine helpers only). Imports flow one way (niche → engine/base); core never imports a niche.
Run: `NICHE=story python pipeline.py` → Gemini script → TTS audio+timings → visuals → FFmpeg 1080x1920 → YouTube (+ optional TikTok via Buffer). Output `out/<stamp>_short.{mp4,mp3,srt,json}` (stamp `%Y-%m-%d_%H%M%S`).

## Commands

- Full run per niche (needs `ffmpeg` + `ffprobe` on PATH): `NICHE=story python pipeline.py` / `NICHE=explainer python pipeline.py` / `NICHE=ranking python pipeline.py` (bare `python pipeline.py` = story default)
- Offline demo per niche (zero API keys, `out/demo_<niche>.mp4` 1080x1920, never uploads): `python demo.py --niche story` (also `explainer`, `ranking`; bare `python demo.py` = story)
- Unit tests (all niches): `python -m unittest discover -s tests` (legacy single file: `python -m unittest tests/test_explainer.py`)
- Dry-run order per niche: tests → `python demo.py --niche <name>` → `NICHE=<name> python pipeline.py`
- Syntax check (Python `3.11` per CI): `python -m py_compile pipeline.py core.py demo.py get_refresh_token.py` (+ any `niches/<name>/*.py`, `engine/*.py` you touch)
- Deps: `pip install -r requirements.txt` (must include `edge-tts`, `Pillow`, `requests`; no local transcription models)
- OAuth helper, local-only with browser, never CI: `python get_refresh_token.py` (needs `YT_CLIENT_ID`/`YT_CLIENT_SECRET`)

## New-niche guide (zero core edits)

- Copy `niches/_template/` → `niches/<name>/` (skipped by discovery while prefixed `_`); rename `NAME`/`LABEL`, implement `content.py` / `narration.py` / `visuals.py` + `<Name>Config.from_env()` in `config.py` using `niches.base.env_*` (`<PREFIX>_` wins, bare name falls back); expose module-level `niche = <Name>Niche()`
- Registry auto-discovery (`niches/__init__.py`): no core/pipeline edits — `available_niches()` picks up the subpackage, `load_niche()` serves it via `NICHE=<name>`; unknown names raise `ImportError` listing available niches
- Stage rules: `generate_content` returns `Content` (beats + queries in extras, joined beat texts == narration); `narrate` returns `NarrationResult` (audio + duration + timed `words`); `build_visuals` returns `VisualsResult` (mp4 + srt + credits/used_ids); call `engine/*` helpers only, never import core; keep all knobs in config (never hardcode)
- Wire-through: document vars in `.env.example` (generic + `<PREFIX>_` section), add them to `daily.yml` `env:`, add `sample_content()` coverage in `tests/` + verify `python demo.py --niche <name>`

## CI (`.github/workflows/daily.yml` is source of truth)

- `ubuntu-latest`, Python `3.11`, `timeout-minutes: 30`, schedule `0 8,20 * * *` + `workflow_dispatch`
- `concurrency.group: shorts-single`, `cancel-in-progress: false`; `permissions.contents: write` — each run commits `history.json` back, so runs must stay serial
- `NICHE: ${{ vars.NICHE || 'story' }}` selects the niche (single-niche default; fan out with a `strategy.matrix.niche` + `NICHE: ${{ matrix.niche }}` for multi-niche); per-niche tuning via `STORY_*` / `EXPLAINER_*` / `RANKING_*` vars in the same `env:` block
- Commit-back: `git add history.json` + `chore: record short history [skip ci]` + push (no-op when unchanged). Schedule/dispatch triggers only, so the push never loops
- Artifacts: `out/*.json` on `always()` 90d; `out/*.mp4`+`*.mp3`+`*.srt` on `failure()` only 3d (YouTube is permanent storage)
- New env vars must be wired through workflow `env:` — secrets/vars do not flow automatically. Prefixed per-niche vars need explicit entries too (bare fallbacks alone won't pick up a repo Variable set under the prefixed name)

## Config (all env, see `.env.example`; never hardcode/commit)

- `NICHE` (`story` default) — selects the subpackage; registry normalizes case/whitespace
- Generic: `GEMINI_API_KEY` (+ `_2..10` failover, comma-separated ok), `PEXELS_API_KEY`, `VISIBILITY` (default `private` — keep until OAuth audit), `TTS_RATE`/`TTS_PITCH`/`TTS_VOLUME`/`BGM_VOLUME`, `MIN/MAX_AUDIO_SEC` (guard only), timeouts/tries/backoff
- Per-niche (prefix wins, bare falls back): `STORY_*` (`TTS_MODEL/VOICES`, `NARRATION_SPEED`, `SEG_MIN/MAX`, `BG_SPEED_MIN/MAX`, `CAPTION_UPPER`, `CARD_SECONDS`, `CHANNEL_HANDLE`, `AVATAR_PATH`, `BG_DIR`, `MAX_SECONDS`, `TARGET_MIN/MAX` 80/85, `STORY_GENRES`, `WHISPER_MODEL`, `PEXELS_API_KEY`), `EXPLAINER_*` (`VOICE`, `TTS_RATE/PITCH/VOLUME/ENGINE`, `GEMINI_TTS_VOICES`, `STILLS_PER_BEAT`, `MIN/MAX_AUDIO_SEC`, `PEXELS_MIN_WIDTH`), `RANKING_*` (`VOICE/VOICES`, `TTS_RATE/PITCH/VOLUME/STYLE`, `TONE`, `TARGET_WPM`, `STILLS_PER_BEAT`, `MIN/MAX_AUDIO_SEC`, `PEXELS_MIN_WIDTH`, `PEXELS_CACHE_DIR`, `BGM_VOLUME`)
- Story TTS: chunked Gemini synth (60–90w chunks, PCM cache, 80ms crossfade) with duration-fit loop (80–85 proceed; ±5 re-speed from cache; wider one paced re-synth; trim past MAX at sentence end); word timings on the FINAL mix are estimated proportionally (no local models)
- `history.json` + `assets/background-musics/bgm*.mp3` (BGM pool folder; the repo root is still honored as a legacy fallback) + brand `assets/fonts/*.ttf/*.otf` + `assets/avatar.png` are committed (never gitignore, never secrets in history). `out/`, `assets/*` (except brand fonts/avatar, the BGM pool + `.gitkeep`), `assets/pexels_cache/`, `.env` stay uncommitted. Pexels cache (`assets/pexels_cache/`) is gitignored but kept between runs
- Branding is config: `CHANNEL_HANDLE`, `AVATAR_PATH` (`assets/avatar.png`; missing file → drawn-initials fallback), `assets/fonts/` (brand ttf → DejaVu CI / Arial local → Pillow default). Card theme consts live in niche `visuals.py` — edit only for real theme changes
- TikTok via Buffer: `BUFFER_API_KEY` only (+ optional `BUFFER_TIKTOK_CHANNEL_ID`); Cloudinary host has working defaults (env overrides win); never fails the run (`tiktokBufferPostId` recorded). Do not add cookie/`sessionid` uploaders

## History (per-niche) + migration

- `history.json` stays a flat committed list; entries carry `niche` (+ `topic`/`topicSource`/`genre`/`items`) and dedup is per-niche (story: title/premise + >35% word-overlap; ranking: topic + ≥2 shared items; explainer: topic/title)
- `niches/history_migrate.py` (`migrate_history` flat→grouped, `flatten_history` back sorted by date, `duplicate_reason` per-niche title/topic check) is pure/lossless and the migration path for any grouped-history future — do not reformat `history.json` in place
- `run()` records the entry after successful render even if upload skipped; render failure records nothing; `load/save_history` must never fail the run

## Graceful degradation (do not break)

- No Gemini key/fail → offline fallback variants (least-recent-first, never repeat last); validators raise `ValueError` on bad output; similarity regens best-effort accept (leaks have aired — don't trust prompt bans)
- Beats/clips with no stock → Pillow card / testsrc (warn only); `pexels` fetch returns `None` on no key/fail (partial downloads deleted)
- No YouTube creds → skip upload, keep video in `out/`; `notify_failure()` never raises
- `retry()` retries 429/5xx only, other 4xx fail fast; `RefreshError` is fatal → re-run `get_refresh_token.py`
- `warn()`/`err()` emit `::warning::`/`::error::`; `log()` normal flow; all output via `_print` (survives Windows cp1252 vs emoji)

## Gotchas (trust code/CI over docs)

- Captions: burn with plain `subtitles='...'` / `ass_cwd_args` (cwd-relative, no drive-colon issues) and NO `force_style` (commas yield zero pixels on some builds); fallback to no-subs render with `::warning::`
- Story planner is pure: `total = narration.duration + 0.4`, 25–40 segs, 12+ clips, no repeat within 4; card overlay `max(CARD_SECONDS, card_line_end + pad)` with 0.2s fade pop
- edge-tts needs `--flag=value` form — values like `-10%` misparse as new flags as separate args
- Shorts ceiling: verify current YouTube max before raising `TARGET_MAX`/`MAX_SECONDS` (don't assume ≤60s either — only `MIN/MAX_AUDIO_SEC` is enforced)
- Stale-prose rule: when docs disagree, code + `daily.yml` win — then fix the docs (these files), never the reverse
