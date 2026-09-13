# youtube-story — $0 Automated YouTube Shorts

Faceless horror Shorts pipeline. Gemini writes the script, edge-tts narrates,
Pexels (or Pillow fallback) provides visuals, FFmpeg renders vertical video,
GitHub Actions uploads to YouTube 1x/day. No server, no credit card.

## How it works

```
cron (08:00 UTC daily)
 -> pipeline.py: Gemini script -> edge-tts mp3+srt -> Pexels clips
 -> FFmpeg 1080x1920 + burned subtitles -> YouTube Data API upload
 -> history.json committed back (dedup memory so stories never repeat)
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

| Item | Free allowance | Our usage (1x/day) |
|---|---|---|
| Actions minutes | 2,000 min/mo | ~400–600 min/mo |
| Artifact storage | 500 MB | ~60 MB rolling |
| YouTube uploads | 100/day | 1/day |
| Pexels API | 200 req/hour + 20k/mo | ~14/day |
| Gemini Flash | free tier | 1 call/day |

## Files

- `pipeline.py` — whole pipeline (script/audio/visuals/render/upload)
- `.github/workflows/daily.yml` — schedule + split artifacts + history commit-back
- `history.json` — committed dedup memory (recent titles/names/settings/rules); the pipeline quotes it into the Gemini prompt so stories never repeat
- `get_refresh_token.py` — one-time OAuth helper (run locally, never in CI)
- `.env.example` — list of secrets/vars to configure

## Standalone stories (~2 min each)

Every run is one complete horror story with its own twist ending — no arcs,
no episode markers, no "part 2". Each video is evergreen: a stranger can
watch any upload with zero context. Gemini is prompted for 280–300 words
(~2 min of narration, 7–8 beats → 14–16 Pexels cuts), one named protagonist in
one location, escalating scares, one broken rule. Fallback runs (Gemini down)
use a built-in ~270-word standalone story.

- Runs are serialized (`concurrency: shorts-single`) so the `history.json`
  commit-back can't race; every run records its story (title, protagonist,
  setting, rule, full narration) and the next run is forced onto fresh slots.

## Visuals (ambient horror B-roll)

Gemini splits each narration into 7–8 beats for timing only. Per beat, in
story order: **Pexels clip** (random ambient horror B-roll from
`RANDOM_HORROR_QUERIES` — foggy forest, dark hallway, etc., deliberately not
story-matched, just eye-pleasing) → **Cloudflare Workers AI still** fallback
(SDXL-Lightning, listed at $0.00/step, native 1080x1920 — free-plan
hard-stop at cap so billing is impossible) → **title card** with the beat's
text. Stills get a **Ken Burns slow zoom** (alternating in/out) so they feel
like motion. Screen time is weighted by beat word count.

Cloudflare setup (fallback only): free account at dash.cloudflare.com
(email only, no card) → copy Account ID (dashboard sidebar) → My Profile →
API Tokens → create token with the Workers AI template → secrets
`CF_ACCOUNT_ID` + `CF_API_TOKEN`.

## Captions + music

- **Captions:** the edge-tts SRT is converted to a styled ASS and burned in
  with the libass `subtitles` filter. They appear middle-center, slightly
  smaller, so the Shorts UI never covers them and they don't block the
  footage. The pipeline avoids comma-heavy `force_style` options because
  some Windows builds silently render zero caption pixels.
- **Music:** a dark drone bed is generated per run (brown noise → lowpass →
  slow swell at ~−20dB under the voice). Generated = zero copyright-strike
  risk and nothing to download. To use your own track instead, commit a
  loopable `bgm.mp3` (or `bgm1.mp3`, `bgm2.mp3`, ... for a randomly rotated
  pool) to the repo root (YouTube Audio Library is a safe
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

## Shorts algorithm and retention

YouTube first tests a Short with a small audience. Distribution can expand
when viewers choose to watch instead of swiping away, stay through the video,
rewatch it, share it, comment, like it, or subscribe. Hashtags help classify
the topic, but they cannot compensate for a weak opening or poor retention.

The script prompt is designed around this retention pattern:

```
0-2s    shocking hook
2-8s    unanswered question
8-25s   clues and rising danger
25-35s  twist or reveal
final   strong cliffhanger
```

For readable horror narration, scripts use simple words, short sentences,
one idea per sentence, concrete sensory details, and a new clue or danger
every few sentences. When reviewing YouTube Analytics, prioritize **Viewed vs
swiped away**, audience retention, average percentage viewed, completion,
rewatches, and shares. Test different hooks while keeping the rest of the
story format consistent.

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
