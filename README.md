# youtube-story — $0 Automated YouTube Shorts

Multi-niche faceless Shorts pipeline. Gemini writes the script, TTS narrates,
Pexels (or offline fallbacks) supplies visuals, FFmpeg renders vertical video,
GitHub Actions uploads to YouTube 2x/day. No server, no credit card.

## Architecture

```
pipeline.py (thin entry: NICHE env -> niches.load_niche() -> core.run(niche))
│
├─ core/          orchestration only: run() chains the 3 stages,
│                 then upload + history. Never imports a niche —
│                 only the shared contracts in niches/base.py.
├─ engine/        policy-free toolbox (no voices, speeds, or queries
│                 baked in; the niche passes those as params):
│                 gemini_client, tts_cache, align (estimated timings),
│                 ass/srt captions, pexels, overlay_card, audio_mix,
│                 ffmpeg_utils. Never imports core or any niche.
└─ niches/<name>/ one subpackage per niche, 3 stages:
                  content.py      generate_content(history, cfg) -> Content
                  narration.py    narrate(content, cfg, workdir) -> NarrationResult
                  visuals.py      build_visuals(content, narration, cfg, workdir)
                                  -> VisualsResult
                  config.py       per-niche knobs with <PREFIX>_ env overrides
```

Imports flow one way: niche → engine, niche → niches.base. Core never
imports a niche; engine never imports core or a niche.

## Niche contract

Every niche in `niches/*` implements `niches.base.Niche`:

| Method | Input → Output |
|---|---|
| `generate_content(history, cfg)` | history + config → `Content` (title, narration_text, description, tags, extras with beats/queries) |
| `narrate(content, cfg, workdir)` | script → `NarrationResult` (audio path, duration, word count, timed words) |
| `build_visuals(content, narration, cfg, workdir)` | script + audio → `VisualsResult` (mp4, srt, credits, used clip ids) |
| `build_metadata(content, ...)` | → upload dict (title/description/tags; default impl passes content through) |
| `sample_content()` | → offline `Content` for demos/tests (zero API keys) |

`niches/__init__.py` auto-discovers subpackages (`available_niches()`,
private `_`-prefixed packages like `_template` skipped) and loads the one
named by `NICHE` (`load_niche()`, default `story`; unknown names raise an
`ImportError` listing what's available).

| Niche | Format | Signature |
|---|---|---|
| `story` | 80–85s Reddit-style micro-story (hook → escalation → twist → payoff), BG-driven 1080x1920 with story card + word-pop captions | re-hooks at 20–30s / 50–60s, word budget from `TARGET_*` × measured wps |
| `explainer` | ~60–75s "how the world around you actually works" single-concept explainer | beats carry `visual_hint`s (diagram / process_flow / stock_footage / text_card) |
| `ranking` | ~2-min Top-5 countdown (hook, #5–#1, closer) | 7 beats, leaderboard overlay, mystery #1 blur, ≥2-shared-items dedup |

## Run (per niche)

```bash
pip install -r requirements.txt
NICHE=story     python pipeline.py   # Reddit story (default)
NICHE=explainer python pipeline.py   # how-it-works explainer
NICHE=ranking   python pipeline.py   # Top-5 countdown
```

(Windows PowerShell: `$env:NICHE="story"; python pipeline.py`.)
Needs `ffmpeg` + `ffprobe` on PATH. Output:
`out/<stamp>_short.{mp4,mp3,srt,json}` (stamp `%Y-%m-%d_%H%M%S`).

## Offline demo (zero API keys, never uploads)

```bash
python demo.py                  # defaults to --niche story
python demo.py --niche story
python demo.py --niche explainer
python demo.py --niche ranking
python demo.py --help
```

Each demo uses that niche's `sample_content()` plus offline stand-ins
(story: ~80–85s sine-tone WAV via FFmpeg lavfi; explainer/ranking: tone MP3
+ even SRT, the old fixture path) and local backgrounds (real files from
`BG_DIR` when present, else generated `testsrc`/`rgbtestsrc`/`smptebars`
color clips). Output: playable 1080x1920 `out/demo_<niche>.mp4`.
Requires only ffmpeg + ffprobe.

## Setup (one time)

1. Push this folder to a **private** GitHub repo.
2. Add Secrets (Settings → Secrets and variables → Actions):
   `GEMINI_API_KEY`, `PEXELS_API_KEY`,
   `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`
3. Get the YouTube refresh token (headless runners can't click OAuth):
   - Google Cloud Console → new project → enable **YouTube Data API v3**
   - Create **OAuth Desktop Client**, set `YT_CLIENT_ID` / `YT_CLIENT_SECRET`
   - Locally: `set YT_CLIENT_ID=... & set YT_CLIENT_SECRET=... & python get_refresh_token.py`
   - Log in with your channel account, copy the printed token into `YT_REFRESH_TOKEN`
4. Optional: set `NICHE` + tuning repo Variables (defaults: `NICHE=story`,
   `VISIBILITY=private` — keep private until your OAuth app passes YouTube audit).
5. Actions tab → `daily-short` → **Run workflow** to test one Short.

## Env var reference (see `.env.example`; never hardcode/commit)

Generic ( Secrets for keys, Variables for tuning; bare names are
cross-niche fallbacks):

- `GEMINI_API_KEY` (+ `GEMINI_API_KEY_2..10` failover; comma-separated keys in one var also work)
- `PEXELS_API_KEY`, `YT_CLIENT_ID` / `YT_CLIENT_SECRET` / `YT_REFRESH_TOKEN`
- `VISIBILITY` (default `private`), `TTS_RATE` / `TTS_PITCH` / `TTS_VOLUME`, `BGM_VOLUME`
- `MIN_AUDIO_SEC=10` / `MAX_AUDIO_SEC=180` (duration guard, not a target)
- `GEMINI_TIMEOUT` / `GEMINI_TTS_TIMEOUT` / `GEMINI_TRIES` / `GEMINI_BACKOFF`

Per-niche (prefix wins over the bare fallback, per `niches/base.py env_*`):

- `STORY_*` — `TTS_MODEL`, `TTS_VOICES`, `NARRATION_SPEED`, `SEG_MIN/MAX`,
  `BG_SPEED_MIN/MAX`, `CAPTION_UPPER`, `CARD_SECONDS`, `CHANNEL_HANDLE`,
  `AVATAR_PATH`, `BG_DIR`, `MAX_SECONDS`, `TARGET_MIN/MAX` (80/85),
  `STORY_GENRES`, `WHISPER_MODEL`, `PEXELS_API_KEY`
- `EXPLAINER_*` — `VOICE`, `TTS_RATE/PITCH/VOLUME/ENGINE`,
  `GEMINI_TTS_VOICES`, `STILLS_PER_BEAT`, `MIN/MAX_AUDIO_SEC`, `PEXELS_MIN_WIDTH`
- `RANKING_*` — `VOICE/VOICES`, `TTS_RATE/PITCH/VOLUME/STYLE`, `TONE`,
  `TARGET_WPM`, `STILLS_PER_BEAT`, `MIN/MAX_AUDIO_SEC`, `PEXELS_MIN_WIDTH`,
  `PEXELS_CACHE_DIR`, `BGM_VOLUME`

New vars must also be wired through `.github/workflows/daily.yml` `env:` —
secrets/vars do not flow into jobs automatically.

## Branding / fonts / backgrounds

- `assets/avatar.png` — channel avatar drawn on the story card. Missing file
  falls back to a drawn-initials circle, so the render never breaks.
- `assets/fonts/` — drop brand `.ttf`s here; the card/caption code prefers
  bundled fonts, then DejaVu (CI) / Arial (local), then the Pillow default.
- `assets/backgrounds/` — drop looping portrait clips here for story BGs;
  when empty, Pexels (with key) or generated testsrc color clips (demo) fill in.
- `assets/pexels_cache/` — downloaded Pexels clips, kept between runs to
  save re-downloads. Both dirs are gitignored (only `.gitkeep` placeholders
  are tracked); `out/`, `assets/`, `.env` stay uncommitted.

Card branding (`STORY_CHANNEL_HANDLE`, theme colors) is config, not code —
edit env, not `visuals.py`.

## Synthetic-content disclosure

Everything this pipeline emits is AI-generated: Gemini scripts, synthetic
TTS narration, and (in demos) tone-audio/testsrc stand-ins. When publishing:

- Treat uploads as AI-generated content under each platform's policy
  (e.g. YouTube's altered-content disclosure where it applies).
- Stories are 100% original micro-fiction — never real people, brands, or
  retold Reddit posts (the story prompt + validators enforce this; the demo
  fallback variants are originals too).
- Edge-TTS preview voices come from Microsoft Edge's online TTS service via
  the edge-tts library: a free, keyless, UNOFFICIAL endpoint with no SLA
  (preview path only — never load-bearing). No commercial-rights claim is
  made about these voices; check the service terms for your own use.

## Storage model (why retention is split)

- **YouTube is the permanent storage.** Uploaded videos live there forever free.
- **GitHub artifacts are temp debug copies**, not storage. Free quota is
  only 500 MB, so we split them:
  - `short-metadata` (`out/*.json`, title/script/tags, ~2 KB each):
    kept **always, 90 days**. This is the history of what was posted.
  - `short-video-debug` (`out/*.mp4`, ~20 MB each): kept **only when a
    run fails, 3 days**. Just long enough to debug, then auto-deleted.
- **Retention = auto-delete timer.** `retention-days: 3` means GitHub
  deletes that copy 3 days after the run. Your YouTube video is untouched.

## Limits ($0 budget)

| Item | Free allowance | Our usage (2x/day) |
|---|---|---|
| Actions minutes | 2,000 min/mo | ~800–1,200 min/mo |
| Artifact storage | 500 MB | ~120 MB rolling |
| YouTube uploads | 100/day | 2/day |
| Pexels API | 200 req/hour + 20k/mo | ~28/day |
| Gemini Flash | free tier | a few calls/day |

## Known limitations

- **No local transcription models** (`engine/align.py`): word timing is
  estimated proportionally (never fatal), and story narration still ships.
- **Pexels rate limits** (429): `engine/pexels.py` backs off and retries;
  sustained limiting degrades to fewer/older cached clips, then Pillow cards.
- **Gemini free-tier quotas**: key rotation + backoff + OpenRouter `:free`
  failover + offline fallback variants keep runs green, but quota exhaustion
  means the fallback script airs — check `topicSource` in `history.json`.
- **Shorts ceiling — verify before raising targets**: Shorts accept ~3 min
  for vertical uploads, but confirm the *current* ceiling in YouTube docs
  before raising `STORY_TARGET_MAX`/`MAX_SECONDS`. Never assume a ≤60s cap
  anywhere either — the only enforced guard is `MIN/MAX_AUDIO_SEC`.

## Failure alerts (optional, free)

If a run crashes, the pipeline pings you instead of failing silently.
Set either or both as repo Secrets (both unset = no alerts, runs still work):

- **Discord:** your server → Server Settings → Integrations → Webhooks →
  New Webhook → Copy Webhook URL → secret `DISCORD_WEBHOOK_URL`
- **Telegram:** chat with `@BotFather` → `/newbot` → copy token to secret
  `TELEGRAM_BOT_TOKEN`; get your numeric id from `@userinfobot` → secret
  `TELEGRAM_CHAT_ID`

Alert delivery never breaks the run.

## TikTok dual-post (optional, free via Buffer)

YouTube stays exactly as-is — only TikTok goes through Buffer:

1. `buffer.com` → connect **TikTok only** → `publish.buffer.com/settings/api` → API key → secret `BUFFER_API_KEY`
2. Nothing else — the Cloudinary public host has working defaults; override via `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_UPLOAD_PRESET` only for your own account (Buffer has no upload endpoint; the mp4 needs a public URL)
3. Optional secret `BUFFER_TIKTOK_CHANNEL_ID` — auto-picked when empty

Missing creds = TikTok skipped with a warning, YouTube unaffected.

## Safety gates

- **Duration guard:** narration audio outside `MIN_AUDIO_SEC`–`MAX_AUDIO_SEC`
  aborts the run before rendering, so broken TTS can never become a video.
- **Dedup:** `history.json` (kept to 110) is quoted into prompts (advisory)
  plus hard gates per niche (same topic/title, shared ranked items,
  >35% story word-overlap). New entries record topic/source/genre/items
  after every successful render, even when upload is skipped.
- **Intermediates cleanup:** working files are deleted after every run
  (`finally` block); the Pexels clip cache is kept.

## Troubleshooting

- Upload skipped but video in `out/` → YouTube secrets missing. Check secret names.
- Videos locked Private → normal for unverified OAuth apps; request YouTube audit or flip `VISIBILITY` later.
- Cron runs late → normal on GitHub free (5–30 min delays).
- Workflow disabled → GitHub auto-disables cron after 60 days of repo inactivity; re-enable under Actions.
- Wrong niche in CI → check the `NICHE` repo Variable (default `story`); per-niche tuning needs the prefixed (`STORY_`/`EXPLAINER_`/`RANKING_`) Variable, since bare names are only fallbacks.
