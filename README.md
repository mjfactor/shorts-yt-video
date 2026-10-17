# youtube-story — $0 Automated YouTube Shorts

Multi-niche faceless Shorts pipeline. Gemini writes the script, TTS narrates,
local background footage (or offline `testsrc` stand-ins in the demo) supplies
visuals, FFmpeg renders vertical video, GitHub Actions uploads to YouTube 2x/day.
No server, no credit card.

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

`engine/pexels.py` still ships in the toolbox, but its only callers are the
**deprecated** niches in `deprecated_niches/` — the story niche embeds no
stock footage and never reads `PEXELS_API_KEY`.

## Niche contract

Every niche in `niches/*` implements `niches.base.Niche`:

| Method | Input → Output |
|---|---|
| `generate_content(history, cfg)` | history + config → `Content` (title, narration_text, description, tags, extras with beats/queries) |
| `narrate(content, cfg, workdir)` | script → `NarrationResult` (audio path, duration, word count, timed words) |
| `build_visuals(content, narration, cfg, workdir)` | script + audio → `VisualsResult` (mp4, srt, credits, used clip ids — story leaves the last two empty, no stock is embedded) |
| `build_metadata(content, ...)` | → upload dict (title/description/tags; default impl passes content through) |
| `sample_content()` | → offline `Content` for demos/tests (zero API keys) |

`niches/__init__.py` auto-discovers subpackages (`available_niches()`,
private `_`-prefixed packages like `_template` skipped) and loads the one
named by `NICHE` (`load_niche()`, default `story`; unknown names raise an
`ImportError` listing what's available). Today `story` is the only live
niche, so `available_niches()` returns `['story']`.

| Niche | Format | Signature |
|---|---|---|
| `story` | 100–120s Reddit-style micro-story (hook → escalation → twist → payoff), 1080x1920 with story card + word-pop captions over ONE continuous local background take | re-hooks at 20–30s / 50–60s, word budget from `TARGET_MIN`/`TARGET_MAX` × measured wps |
| `explainer` *(deprecated)* | ~60–75s "how the world around you actually works" single-concept explainer | beats carry `visual_hint`s (diagram / process_flow / stock_footage / text_card) |
| `ranking` *(deprecated)* | ~2-min Top-5 countdown (hook, #5–#1, closer) | 7 beats, leaderboard overlay, mystery #1 blur, ≥2-shared-items dedup |

> **Deprecated:** `explainer` and `ranking` are no longer loadable. They were
> moved out of `niches/` into `deprecated_niches/`, so `NICHE=explainer` /
> `NICHE=ranking` now raise `ImportError` from `load_niche()`. `daily.yml`
> still exports a few `EXPLAINER_*`/`RANKING_*` env entries, but with `story`
> as the only registered niche they are inert. The two packages are kept for
> reference (and they are the only remaining users of `engine/pexels.py` and
> `PEXELS_API_KEY`). The rows above describe what they still contain.

## Story premises and narrator voice

`niches/story/content.py` builds a premise-first Gemini prompt. It asks the
model to compare three different premises privately in the existing story
request, favoring personal stakes, curiosity, and human complications over
recycled fee/theft/revenge plots. No extra Gemini request or model change is
required. It asks for a conversational first-person voice without forced
filler, decorative scene-setting, or guaranteed perfect justice. The JSON,
length, card, caption, safety, and dedup contracts remain unchanged.
Correction requests include the full previous JSON draft so stateless Gemini
calls can repair the existing story instead of inventing a replacement.

Offline prompt regression checks (no API calls, rendering, or uploads):
`python tools/test_story_prompt.py`. These check instructions and formatting
contracts, not the quality of a live model's output.

## Run (story)

```bash
pip install -r requirements.txt
python pipeline.py                  # story (the only live niche)
NICHE=story python pipeline.py       # same thing, explicit
```

`explainer` / `ranking` are deprecated — `NICHE=explainer python pipeline.py`
and `NICHE=ranking python pipeline.py` now fail with `ImportError` (see the
deprecation note above); their code is parked in `deprecated_niches/`.

(Windows PowerShell: `$env:NICHE="story"; python pipeline.py`.)
Needs `ffmpeg` + `ffprobe` on PATH. Output:
`out/<stamp>_short.{mp4,mp3,srt,json}` (stamp `%Y-%m-%d_%H%M%S`).

## Offline demo (zero API keys, never uploads)

`demo.py` is **story-only** — there is no `--niche` flag (passing one errors).

```bash
python demo.py                      # story demo, --tts auto
python demo.py --tts edge           # keyless edge preview voice
python demo.py --tts gemini         # needs GEMINI_API_KEY
python demo.py --words 120          # trim the sample narration for a quick preview
python demo.py --help
```

Real parser: `demo.py [-h] [--tts {auto,gemini,edge}] [--words WORDS]`.

The demo renders through the **same production stages** as a real upload:
narration from the real niche `narrate()` (tone WAV with proportional
timings if edge-tts has no network), then the real visuals stage
`niches.story.visuals.build_visuals()` — same BG track, card, word-pop
captions, BGM bed and encode. Only content generation and upload are
skipped. Backgrounds come from real files in `BG_DIR` / `STORY_BG_DIR`
when present, else generated `testsrc`/`rgbtestsrc`/`smptebars` color
clips. Output: playable 1080x1920 `out/demo_story.mp4` (+
`demo_story.srt` / `demo_story_thumb.jpg` copied from the same render).
The bundled sample is shorter than the 100–120s target window, so the
demo is a timing / plumbing check, not a full-length proof. Requires only
ffmpeg + ffprobe.

## Setup (one time)

1. Push this folder to a **private** GitHub repo.
2. Add Secrets (Settings → Secrets and variables → Actions):
   `GEMINI_API_KEY`, `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`
   (`PEXELS_API_KEY` is **not** needed for story — only the deprecated
   niches read it, and the story workflow does not wire it through.)
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
- `YT_CLIENT_ID` / `YT_CLIENT_SECRET` / `YT_REFRESH_TOKEN`
- `PEXELS_API_KEY` — deprecated niches only; the story niche never reads it
- `VISIBILITY` (default `private`), `TTS_RATE` / `TTS_PITCH` / `TTS_VOLUME`, `BGM_VOLUME`
- `GEMINI_TIMEOUT` / `GEMINI_TTS_TIMEOUT` / `GEMINI_TRIES` / `GEMINI_BACKOFF`

Per-niche (prefix wins over the bare fallback, per `niches/base.py env_*`).
`STORY_*` is the authoritative list from `niches/story/config.py::from_env()`:

- `STORY_TARGET_MIN` / `STORY_TARGET_MAX` (100/120; `..._SECONDS` aliases win),
  `STORY_MAX_SECONDS` (130, narration past it is trimmed at a sentence end),
  `STORY_NARRATION_SPEED`, `STORY_TRIM_CHUNK_SILENCE`, `STORY_CHUNK_PAUSE_SEC`,
  `STORY_LENGTH_OVERSHOOT`
- `STORY_TTS_MODEL` / `STORY_TTS_MODELS`, `STORY_TTS_VOICES`, `STORY_TTS_ENGINE`,
  `STORY_TTS_PROVIDER_ORDER`, `STORY_EDGE_VOICE` / `STORY_EDGE_VOICES`,
  `STORY_EDGE_RATE`, `STORY_EDGE_RATE_FEMALE`, `STORY_EDGE_PITCH`,
  `STORY_EDGE_PITCH_FEMALE`, `STORY_NARRATOR_GENDER_MODE`
- `STORY_MODELS` (script models; bare `GEMINI_MODELS` overrides the whole pool)
- `STORY_BG_DIR` (`assets/backgrounds` — the **only** background source) and
  `STORY_BG_SINGLE_SPEED` (1.8, playback speed of the single continuous take)
- Audio bed: `STORY_BGM_DIR` (`assets/background-musics`), `STORY_BGM_GLOB`
  (`bgm*.mp3`), `STORY_BGM_VOLUME`, `STORY_BED_PATH`, `STORY_MIX_GAIN_DB`
- Captions + card: `STORY_CAPTION_UPPER`, `STORY_ASS_FONTSIZE`,
  `STORY_CARD_SECONDS`, `STORY_THUMB_OFFSET_SECONDS`, `STORY_CARD_LIKES`,
  `STORY_CARD_SHARES`, `STORY_CARD_SHOW_REACTIONS`, `STORY_CHANNEL_HANDLE`,
  `STORY_AVATAR_PATH`
- Content + pacing: `STORY_GENRES`, `STORY_HISTORY_WPS`, `STORY_WHISPER_MODEL`
- Retries/timeouts: `STORY_TIMEOUT`, `STORY_TTS_TIMEOUT`, `STORY_TRIES`,
  `STORY_BACKOFF`, `STORY_LITE_RETRIES`

Deprecated-niche knobs (still read inside `deprecated_niches/`; `daily.yml`
still exports a few of them, but they are inert while `story` is the only
registered niche): `EXPLAINER_*` — `VOICE`, `TTS_RATE/PITCH/VOLUME/ENGINE`,
`GEMINI_TTS_VOICES`, `STILLS_PER_BEAT`, `MIN/MAX_AUDIO_SEC`, `PEXELS_MIN_WIDTH`;
`RANKING_*` — `VOICE/VOICES`, `TTS_RATE/PITCH/VOLUME/STYLE`, `TONE`,
`TARGET_WPM`, `STILLS_PER_BEAT`, `MIN/MAX_AUDIO_SEC`, `PEXELS_MIN_WIDTH`,
`PEXELS_CACHE_DIR`, `BGM_VOLUME`.

New vars must also be wired through `.github/workflows/daily.yml` `env:` —
secrets/vars do not flow into jobs automatically.

## Branding / fonts / backgrounds

- `assets/avatar.png` — channel avatar drawn on the story card. Missing file
  falls back to a drawn-initials circle, so the render never breaks.
- `assets/fonts/` — drop brand `.ttf`s here; the card/caption code prefers
  bundled fonts, then DejaVu (CI) / Arial (local), then the Pillow default.
- `assets/backgrounds/` — the story niche's ONLY background source. One file
  (`.mp4`/`.mov`/`.webm`/`.mkv`) is picked at random per video and played as a
  single continuous take from a random start offset at `STORY_BG_SINGLE_SPEED`
  (short sources loop to cover), cut to the exact video length — no per-beat
  cuts, no queries, no credits. An **empty folder is a hard failure**
  (`RuntimeError`); a clip that will not render is skipped with a
  `::warning::` and the next one is tried. Clips here are committed, and CI
  relies on them directly: the workflow has no background-fetch step, so the
  folder must never be empty on a runner.
- `assets/background-musics/` — committed `bgm*.mp3` BGM pool, one random track
  per video; with no pool the story synth falls back to a drone bed.
- `assets/pexels_cache/` — legacy stock-download cache dir (gitignored,
  `.gitkeep` tracked). Normal story runs never fill it: only the deprecated
  niches download stock, into their own cache dir
  (`RANKING_PEXELS_CACHE_DIR`, default `assets/`). `out/`, `assets/*` and
  `.env` stay uncommitted, except the brand fonts/avatar, the BGM pool and the
  background clips listed above.

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
| Gemini Flash | free tier | a few calls/day |

Story makes **zero** Pexels calls, so Pexels rate limits/quotas do not apply to
the live run (only to the deprecated niches).

## Known limitations

- **No local transcription models** (`engine/align.py`): word timing is
  estimated proportionally (never fatal), and story narration still ships.
- **Pexels rate limits** (429): irrelevant for story (no Pexels calls). The
  deprecated niches' `engine/pexels.py` backs off and retries; sustained
  limiting degrades them to fewer/older cached clips, then Pillow cards.
- **Gemini free-tier quotas**: key rotation + backoff + OpenRouter `:free`
  failover + offline fallback variants keep runs green, but quota exhaustion
  means the fallback script airs — check `topicSource` in `history.json`.
- **Shorts ceiling — verify before raising targets**: Shorts accept ~3 min
  for vertical uploads, but confirm the *current* ceiling in YouTube docs
  before raising `STORY_TARGET_MAX`/`STORY_MAX_SECONDS`. Never assume a ≤60s cap
  anywhere either — `MAX_SECONDS` is only a trim ceiling, not a reject gate.

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

- **Duration guard:** narration is fitted to `TARGET_MIN`–`TARGET_MAX` by the
  duration-fit loop (re-synth/re-pace attempts, ±5s accepted with a warning),
  and audio past `MAX_SECONDS` is trimmed at a sentence end — so broken or
  runaway TTS can't become an over-long video.
- **Dedup:** `history.json` (kept to 110) is quoted into prompts (advisory)
  plus hard gates per niche (same topic/title, shared ranked items,
  >35% story word-overlap). New entries record topic/source/genre/items
  after every successful render, even when upload is skipped.
- **Intermediates cleanup:** working files are deleted after every run
  (`finally` block); the stock download cache is kept.

## Troubleshooting

- Upload skipped but video in `out/` → YouTube secrets missing. Check secret names.
- Videos locked Private → normal for unverified OAuth apps; request YouTube audit or flip `VISIBILITY` later.
- Cron runs late → normal on GitHub free (5–30 min delays).
- Workflow disabled → GitHub auto-disables cron after 60 days of repo inactivity; re-enable under Actions.
- Wrong niche in CI → check the `NICHE` repo Variable (default `story`, the
  only live niche); per-niche tuning needs the prefixed `STORY_` Variable,
  since bare names are only fallbacks.
