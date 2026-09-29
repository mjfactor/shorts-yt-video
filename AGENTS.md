# AGENTS.md

Single-niche Shorts automation. **`story` is the only live niche** — `explainer`/`ranking` were moved to `deprecated_niches/`, which is orphaned dead code (nothing in `core.py`/`pipeline.py`/`niches/`/`engine/` imports it). `available_niches()` returns `['story']`; `NICHE=explainer` now raises `ImportError` listing available niches.

Layout: `pipeline.py` thin entry (`NICHE` env → `niches.load_niche()` → `core.run(niche)`); **`core.py` is a single root module** (stage chain + upload + thumbnail + history + notify), importing only `niches.base`/`niches.history_migrate`; `engine/` policy-free toolbox (TTS/Pexels/FFmpeg/captions) that never imports `core` or any niche; `niches/<name>/` 3 stages. Imports flow one way (niche → engine/base); `core.py` never imports a concrete niche.

Run: Gemini script → TTS audio+timings → local-clip background → FFmpeg 1080x1920 + burned ASS captions → card thumbnail → YouTube (+ optional TikTok via Buffer). Output `out/<stamp>_short.{mp4,srt,json}` + `out/<stamp>_thumb.jpg` (stamp `%Y-%m-%d_%H%M%S`). **No `.mp3` is written**; `srt` is `None` on the no-captions path.

## Commands (PowerShell — `NICHE=x cmd` does NOT work; use `$env:NICHE`)

- Full run (story default): `python pipeline.py` (needs `ffmpeg` + `ffprobe` on PATH)
- Offline demo, zero API keys, never uploads → `out/demo_story.mp4`: `python demo.py`; only flags are `--tts {auto,gemini,edge}` and `--words N` (there is **no** `--niche`; it errors). It uses real clips from `STORY_BG_DIR` when present and synthesizes a testsrc 1080x1920 card only when the dir is empty/missing.
- **The test suite is `python deprecated_niches/test_explainer.py`** (runnable standalone via its own `sys.path` bootstrap). There is **no `tests/` dir** — `python -m unittest discover -s tests` is dead. The file is not collected by discovery (wrong name/location) and only exercises the deprecated explainer. **CI runs no tests and no lint at all.**
- Syntax check: `python -m py_compile pipeline.py core.py demo.py get_refresh_token.py` (+ any `niches/<name>/*.py`, `engine/*.py` you touch)
- Deps: `pip install -r requirements.txt` (`edge-tts`, `requests`, `Pillow`, `google-api-python-client`, `google-auth`, `google-auth-oauthlib`; no local transcription models)
- OAuth helper, local browser only, never CI: `python get_refresh_token.py` (needs `YT_CLIENT_ID`/`YT_CLIENT_SECRET`)
- Background prep, **manual only** (not in requirements, not in any workflow) — normalizes footage into `assets/backgrounds/bgN.mp4` (1080x1920, 30fps, cover-crop): `python tools/prep_backgrounds.py --source-folder <dir>`; `--url` uses yt-dlp (explicitly not a dependency), `--section START-END`, `--speed` (1.8), `--list` dry-run, `--force` restarts numbering
- Keyless edge-TTS render smoke through the **real** story pipeline → `out/edge_preview.mp4`: `python tools/test_edge_preview.py --card "hook line"`
- Thumbnail API diagnostic, **not** part of the pipeline (never edits a video, never writes `history.json`; default mode DOES overwrite one Short's thumbnail with a magenta test card): `python tools/thumb_probe.py` → `out/thumb_probe.json` + `out/thumb_probe_src.jpg`; `--readback` (read-only cache-busted `?cb=` fetch, classifies pixels) and `--verify-only` (read-only `hasCustomThumbnail` re-read) upload nothing. Modes are mutually exclusive.

## New-niche guide (zero core edits)

- Copy `niches/_template/` → `niches/<name>/` (discovery skips `_`-prefixed dirs) and expose a module-level `niche = <Name>Niche()` — the registry does `getattr(mod, "niche", mod)`, so the module itself is the fallback. **The template is a skeleton that has bit-rotted**: its `narrate` returns `duration=0.0` with `extras={"pending":True}`, its `build_visuals` returns an unwritten `workdir/short.mp4` and no srt, and it has no `load_config`/`Config`. Treat it as a shape reference, not working code.
- Exact contracts (`niches/base.py`): `Content(title, narration_text, description, tags, extras)` — **there is no `beats` field**; beats and queries are an `extras` convention. `NarrationResult(audio, duration, words: int, voice, style, extras)` — **`words` is an int count, not timed words**; the timed list lives in `extras["words"]` and is what `core.py` reads for captions. `VisualsResult(mp4, srt, credits, used_ids, extras)`. Optional hooks: `build_metadata(...)` and `sample_content()`, plus `load_config(name)` which `core.py` checks before falling back to a `NicheConfig` subclass scan.
- Env helpers `env_str/env_int/env_float/env_bool(name, default, prefix="STORY_")`: prefixed non-empty → bare non-empty → default, and **an empty string counts as unset**.
- Call `engine/*` helpers only, never import `core.py`; keep all knobs in config. A new niche may skip Pexels entirely and must never hardcode a key.
- Wire-through: document vars in `.env.example` (generic + `<PREFIX>_` section), add them to `daily.yml` `env:`, and verify `NICHE=<name> python pipeline.py` — `demo.py` is story-only and cannot smoke-test a new niche.

## CI (three workflows; `daily.yml` is the pipeline)

`daily.yml` — `ubuntu-latest`, Python `3.11`, `timeout-minutes: 30`, cron **`0 6,18 * * *`** (06:00/18:00 PHT = 22:00/10:00 UTC, *not* 08:00/20:00) + `workflow_dispatch`; `concurrency.group: shorts-single`, `cancel-in-progress: false`, `permissions: contents: write` because each run commits `history.json` back, so runs must stay serial. Steps: checkout default branch → setup-python → install ffmpeg → `pip install -r requirements.txt` → `python pipeline.py` → commit history → artifacts. Commit-back is `if: always()`, `git add history.json` + `chore: record short history [skip ci]` + push, no-op when unchanged. Artifacts: `out/*.json` on `always()` 90d; `out/*.mp4`+`*.mp3`+`*.srt` on `failure()` 3d only. `NICHE: ${{ vars.NICHE || 'story' }}`; per-niche tuning via `STORY_*` in the same `env:` block.

`thumb_probe.yml` (name `thumbnail-probe`) and `edge-tts-smoke.yml` are both **manual `workflow_dispatch` only** — no schedule, no commits, `permissions: contents: read`, own concurrency group, 15min timeouts. `edge-tts-smoke.yml` runs `tools/test_edge_preview.py` (ffmpeg check, no secrets) and uploads the preview as a 7d artifact. Neither may queue behind or block `shorts-single`.

Secrets/vars do **not** flow automatically — every new env var needs an explicit `env:` entry, including prefixed `STORY_*` ones (bare fallbacks won't pick up a repo Variable set under the prefixed name).

## Config (all env, see `.env.example`; never hardcode or commit)

Story is the only consumer. `STORY_*` prefix wins, bare name falls back. Notable: `TTS_ENGINE` (default **`edge`**, not gemini), `EDGE_RATE` `+20%`/`EDGE_RATE_FEMALE` `+28%`, `TARGET_MIN`/`TARGET_MAX` **100/120s** (`*_SECONDS` aliases win) with `MAX_SECONDS` 130, `NARRATION_SPEED` 1.15, `CARD_SECONDS` 3.0, `BG_DIR` `assets/backgrounds`, `BG_SINGLE_SPEED` 1.8, `BG_SELECT` `rotate`, `BGM_DIR` `assets/background-musics`, `BGM_VOLUME` 0.20, `NARRATOR_GENDER_MODE` `auto|male|female`, `LITE_RETRIES` 5, `CARD_LIKES`/`CARD_SHARES`/`CARD_SHOW_REACTIONS`. `VISIBILITY` defaults to `private` — keep it there until the OAuth audit.

- `GEMINI_API_KEY` failover supports `_2.._11` (`parse_keys(extra_slots=10)`), comma-separated allowed; CI only wires `_2.._5`.
- **Dead for story — do not wire them expecting an effect:** `MIN/MAX_AUDIO_SEC`, `OPENROUTER_API_KEY`, `AI_TOPICS`, `GEMINI_TTS_STYLE`, and `WHISPER_MODEL` (it feeds an accepted-but-ignored kwarg; `engine/align.py` is proportional-fallback only, no local model). They are referenced only by `deprecated_niches/` or by CI passthrough. Story's real guards are `TARGET_MIN/MAX` (fit loop) and `MAX_SECONDS` (trim).
- Committed and must never be gitignored: `history.json`, `assets/backgrounds/bg*.mp4` (**9 clips, bg1–bg9**), `assets/background-musics/bgm*.mp3`, `assets/fonts/` (currently one `Montserrat-ExtraBold.ttf`), `assets/avatar.png`. `.gitignore` ignores `assets/*` wholesale then re-includes those; `assets/pexels_cache/` is gitignored except its tracked `.gitkeep` and is only filled by the deprecated niches.
- Branding is config: `CHANNEL_HANDLE`, `AVATAR_PATH` (missing file → drawn-initials circle), font order = bundled brand ttf → DejaVu (CI) / Arial (local) → Pillow default. Card theme consts live in story `visuals.py`.

## Story pipeline internals

- **TTS is chunked (60–90 words) with a PCM cache** (`workdir/tts_cache`, content-addressed on text+voice+style) and joined **gaplessly** — `XFADE_SEC = 0.0`, not a crossfade; boundaries are handled by a `-50dB/20ms` silence trim plus `CHUNK_PAUSE_SEC` (0.10). Duration-fit loop: proceed at 100–120s; outside ±5 re-speed from cached PCM (factor clamped 1.05–1.25); still outside → one paced re-synth with a ±15% delivery direction; past `MAX_SECONDS` → trim at the last sentence end.
- **Word timings differ by engine**: the edge path uses real `WordBoundary` service events (`_map_edge_words`); only the gemini/final-mix path falls back to proportional estimation. Do not describe both as estimated.
- The planner is pure: `total = narration.duration + 0.4`; card overlay `max(card_seconds, card_line_end + card_pad)`, fade in 0.2s / out **0.25s**. `card_pad` (0.4) is a dataclass field with **no env key**.
- **Background is one continuous local take per video**: `BG_DIR` is the only source, played at `BG_SINGLE_SPEED` from a random start offset (short sources loop). `BG_SELECT=rotate` (default) walks the pool in **natural** filename order keyed on `video_number` = `len(same-niche history)`, so consecutive uploads use consecutive clips and wrap; `random` shuffles. Natural order matters because plain `sorted()` puts `bg10` before `bg2`. The whole pool is rotated, not just the head, so a broken clip still falls through. One clip per video means no per-beat queries, no segment planner, no min_clips warning, and `credits`/`used_ids` are always `[]` — `used_ids` is contract-only. **The committed folder is the only thing CI relies on** (no fetch step).
- Empty `BG_DIR` is a hard `RuntimeError`; a clip that will not render is skipped with `::warning::` and the next is tried; when every clip fails the run stops hard. There is no stock-footage fallback.
- Captions burn via `ass_cwd_args(ass_path, fontsdir=...)` with `cwd=` — passing a bare filename is what avoids the Windows drive-colon, which FFmpeg parses as a filter option separator. **Never add `force_style`** (commas render as zero pixels on some builds), and note there is **no no-subs fallback on the story path** — `require_ass_filter()` raises if libass is missing. The warn-and-retry-without-subs pattern only survives in the deprecated niches.
- Dedup is per-niche: exact casefolded title or premise, else Jaccard word overlap `> 0.35`. No Gemini key → offline fallback variants ordered least-recently-used, never repeating the last. Validators raise `ValueError`; similarity regens are best-effort and accept-with-warning (leaks have aired — don't trust prompt bans).
- `_narrator_gender` deliberately **ignores history** and picks from the UTC half-day clock; history-gating once froze the voice after a failed CI push-back.
- "Lite" is not a mode: `_model_attempts` gives any model name containing `lite` `LITE_RETRIES` drafts vs 2 for others.

## Upload, thumbnail, history

- **Upload failure is fatal, but missing credentials are not** — no `YT_*` creds warns and skips upload (video stays in `out/`, history is still recorded), yet a real upload exception propagates and records **no** history entry. `RefreshError` is fatal → re-run `get_refresh_token.py`. Only the thumbnail sequence and TikTok are warn-only.
- **The pipeline sets a custom thumbnail on every upload** (~260 lines in `core.py`): it validates `out/<stamp>_thumb.jpg` exists / is JPEG / is <2MB, polls `processingStatus == succeeded` (180s) because an immediate `thumbnails().set` can 200-accept-then-discard, retries the set 3×, verifies `hasCustomThumbnail`, then deletes the local file. Tune with `YT_THUMB_WAIT_TIMEOUT_SEC` / `_POLL_SEC` / `_VERIFY_DELAY_SEC` / `_SET_RETRIES`. All warn-only.
- TikTok via Buffer (`BUFFER_API_KEY`, optional `BUFFER_TIKTOK_CHANNEL_ID`; Cloudinary host has working defaults) can never fail the run — the id is recorded as `tiktokBufferPostId`. Do not add cookie/`sessionid` uploaders.
- `history.json` is committed in the GROUPED shape `{"story": [...]}`; `save_history` always writes through `migrate_history` and `load_history` accepts a legacy flat list, migrating on load best-effort and silently. `HISTORY_KEEP = 110` applied as a **global flat cap** on both load and save, then regrouped. Never hand-reformat the file.
- Entry keys actually written: `date`, `title`, `niche`, `tags` (≤10), `narration`, `videoId`, `tiktokBufferPostId` (may be `null`), `used_ids`, plus `topic`, `topic_source`, `topicSource`, `category` and `narratorGender` when present in the slots. `genre`/`items` are never written — `history_fields()` in `niches/story/content.py` is **dead code** (nothing calls it).
- `niches/history_migrate.py` (`migrate_history`, `flatten_history`, `duplicate_reason`) is pure and is the only sanctioned way to change the on-disk shape — but its own module docstring claiming "not wired into core yet" is stale; `core.py` imports and uses it.

## Graceful degradation (do not break)

- No YouTube creds → skip upload, keep the video in `out/`. `notify_failure()` never raises.
- `retry()` (`core.py`, used by upload/Buffer) retries 429/408/5xx and no-status, other 4xx fail fast. A second `retry` lives in `engine/ffmpeg_utils.py` — don't confuse them.
- `engine.pexels` returns `None` on no key or any failure and deletes partial downloads (deprecated niches only).
- No BGM pool → synthesized drone bed (priority: explicit `BED_PATH` → pool dir → repo-root legacy glob → drone).
- `log()`/`warn()`/`err()` emit `::warning::`/`::error::` and all route through `_print`, which survives Windows cp1252 vs emoji.

## Gotchas

- The edge-TTS CLI `--flag=value` rule (bare `-10%` misparses as a new flag) is **stale for story** — it uses the `edge_tts` library, not the CLI. That form only survives in the deprecated niches.
- Before raising `TARGET_MAX`/`MAX_SECONDS`, verify the current YouTube Shorts ceiling; don't assume it's ≤60s.
- Docstrings and comments here have bit-rotted in several places (`pipeline.py`, `history_migrate.py`, `niches/base.py` vs the old prose). **Trust code + `daily.yml` over prose** — then fix the prose, never the reverse.
