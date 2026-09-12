# youtube-story — $0 Automated YouTube Shorts

Faceless horror Shorts pipeline. Gemini writes the script, edge-tts narrates,
Pexels (or Pillow fallback) provides visuals, FFmpeg renders vertical video,
GitHub Actions uploads to YouTube 3x/day. No server, no credit card.

## How it works

```
cron (08:00, 16:00, 23:00 UTC)
 -> pipeline.py: Gemini script -> edge-tts mp3+srt -> Pexels clips
 -> FFmpeg 1080x1920 + burned subtitles -> YouTube Data API upload
```

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
4. Optional: set `VISIBILITY` repo Variable (default `private` — keep until
   your OAuth app passes YouTube audit). Niche and voice are hardcoded at
   the top of `pipeline.py`.
5. Actions tab → `daily-short` → **Run workflow** to test one Short.

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

| Item | Free allowance | Our usage (3x/day) |
|---|---|---|
| Actions minutes | 2,000 min/mo | ~1,200–1,800 min/mo |
| Artifact storage | 500 MB | ~180 MB rolling |
| YouTube uploads | 100/day | 4/day |
| Pexels API | 200 req/day | ~12/day |
| Gemini Flash | free tier | 3 calls/day |

## Files

- `pipeline.py` — whole pipeline (script/audio/visuals/render/upload)
- `.github/workflows/daily.yml` — schedule + split artifacts
- `get_refresh_token.py` — one-time OAuth helper (run locally, never in CI)
- `.env.example` — list of secrets/vars to configure

## Series continuity (connected episodes)

Videos form story arcs of up to 5 episodes, not one-offs. `series_state.json`
in the repo holds the current arc (title, episode number, recap, finale flag).
Each run feeds it to Gemini ("continue Episode N, start with a one-line
'previously' hook, end on a new cliffhanger"), then commits the updated state
back so the next run continues the story.

- Gemini may end an arc early by setting `finale: true` (proper ending at
  Ep 3/4); Episode 5 is always the forced finale. Next run starts a new arc.
- Titles always carry the episode marker and descriptions always name the
  series/episode — the validator auto-fixes them if Gemini omits them.

- Fallback runs (Gemini down) post standalone stories and do **not**
  advance the arc — the commit step no-ops when the state is unchanged.
- State advances only after successful render+upload, so a failed run never
  skips an episode publicly.
- Runs are serialized (`concurrency: series-state`) so two runs can't fork
  the story; the job needs `contents: write` for the state commit.

## Visuals (beat-aligned, literal AI stills)

Gemini splits each narration into 3–4 beats, each with its own concrete
query. Per beat, in story order: **Cloudflare Workers AI still**
(SDXL-Lightning, listed at $0.00/step, native 1080x1920, no card — free-plan
hard-stop at cap so billing is impossible) → **Pollinations FLUX still**
(first beats only, pollen budget) → **Pexels clip** (real motion) →
**title card** with the beat's text. Stills get a **Ken Burns slow zoom**
(alternating in/out) so they feel like motion. Screen time is weighted by
beat word count.

Cloudflare setup: free account at dash.cloudflare.com (email only, no card)
→ copy Account ID (dashboard sidebar) → My Profile → API Tokens → create
token with the Workers AI template → secrets `CF_ACCOUNT_ID` + `CF_API_TOKEN`.

Notes: anonymous Pollinations = 1 request/15s (built-in pacing) and free
images may carry a small watermark. With an account key (secret
`POLLINATIONS_API_KEY`, an `sk_...` key from enter.pollinations.ai/keys),
stills are watermark-free with faster pacing. The key travels as an HTTP
header and is never printed in logs.

Pollen budget: only the first `AI_STILLS_PER_VIDEO` beats (default 2: the
hook + follow-through) get AI stills; later beats use beat-matched stock.
That's ~180 stills/month ≈ 0.36 pollen at schnell rates. If pollen runs dry
(HTTP 402), runs automatically fall back to stock and log it — top up via
quests or lower `AI_STILLS_PER_VIDEO`.

## Captions + music

- **Captions:** burned in with libass `force_style` — big bold white with
  black outline, bottom-center lifted 220px above the Shorts UI overlay.
  Tweak `SUB_STYLE` at the top of `pipeline.py`.
- **Music:** a dark drone bed is generated per run (brown noise → lowpass →
  slow swell at ~−20dB under the voice). Generated = zero copyright-strike
  risk and nothing to download. To use your own track instead, commit a
  loopable `bgm.mp3` to the repo root (YouTube Audio Library is a safe
  source) — the pipeline loops it under the narration automatically.

## Failure alerts (optional, free)

If a run crashes, the pipeline pings you instead of failing silently.
Set either or both as repo Secrets (both unset = no alerts, runs still work):

- **Discord:** your server → Server Settings → Integrations → Webhooks →
  New Webhook → Copy Webhook URL → secret `DISCORD_WEBHOOK_URL`
- **Telegram:** chat with `@BotFather` → `/newbot` → copy token to secret
  `TELEGRAM_BOT_TOKEN`; get your numeric id from `@userinfobot` → secret
  `TELEGRAM_CHAT_ID`

The message includes the error and a link to the failed run. Alert delivery
never breaks the run — if the webhook itself fails, it's logged as non-fatal.

## Safety gates

- **Duration guard:** narration audio outside `MIN_AUDIO_SEC`–`MAX_AUDIO_SEC`
  (defaults 10–180s) aborts the run before rendering, so a broken TTS
  output can never become an uploaded video.
- **Intermediates cleanup:** `assets/` working files are deleted after every
  run (`finally` block); the Pexels clip cache is kept to save re-downloads.

## Troubleshooting

- Upload skipped but video in `out/` → YouTube secrets missing. Check secret names.
- Videos locked Private → normal for unverified OAuth apps; request YouTube audit or flip `VISIBILITY` later.
- Cron runs late → normal on GitHub free (5–30 min delays).
- Workflow disabled → GitHub auto-disables cron after 60 days of repo inactivity; re-enable under Actions.
