# youtube-story — $0 Automated YouTube Shorts

Faceless Top-5 countdown Shorts pipeline. Gemini writes the ranking script,
edge-tts narrates, Pexels (or AI-still fallback) provides matched visuals,
FFmpeg renders vertical video, GitHub Actions uploads to YouTube 2x/day.
No server, no credit card.

## How it works

```
cron (08:00 + 20:00 UTC daily)
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
   your OAuth app passes YouTube audit).
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

| Item | Free allowance | Our usage (2x/day) |
|---|---|---|
| Actions minutes | 2,000 min/mo | ~800–1,200 min/mo |
| Artifact storage | 500 MB | ~120 MB rolling |
| YouTube uploads | 100/day | 2/day |
| Pexels API | 200 req/hour + 20k/mo | ~28/day |
| Gemini Flash | free tier | 2 calls/day |

## Files

- `pipeline.py` — thin entry (`NICHES` registry + `main()` → `core.run`)
- `core.py` — shared machinery (script/audio/visuals/render/upload/history)
- `niche_ranking.py` — Top-5 countdown niche (prompt, topics, board overlay)
- `.github/workflows/daily.yml` — schedule + split artifacts + history commit-back
- `history.json` — committed dedup memory (recent topics/titles/ranked items); the pipeline quotes it into the Gemini prompt so countdowns never repeat
- `get_refresh_token.py` — one-time OAuth helper (run locally, never in CI)
- `.env.example` — list of secrets/vars to configure

## Standalone countdowns (~90s each)

Every run is one complete Top-5 countdown with its own topic — no arcs,
no episode markers, no "part 2". Each video is evergreen: a stranger can
watch any upload with zero context. Gemini is prompted for 200–220 words
(~90s of narration: hook teasing #1, countdown #5→#1 with 1–2 concrete
facts each, comment-bait closer), 7 beats → 14 Pexels cuts, rounded
approximate numbers only (never invented precise stats). Gemini also adds
fitting emojis to the title (1) and description (1-2) — metadata only, since
the on-screen banner sanitizer strips any emoji that slips through. Fallback runs
(Gemini down) use a built-in ~200-word Top-5 largest animals countdown.

- Runs are serialized (`concurrency: shorts-single`) so the `history.json`
   commit-back can't race; every run records its topic + ranked items and
   the next run is forced onto a fresh topic (Gemini proposes one, validated;
       `TOPICS`, 123 evergreen categories, is the backstop). Same-topic and ≥2 shared items trigger a regeneration.

## Visuals (item-matched stock)

Gemini splits each countdown into 7 beats (hook, #5–#1, closer). Per beat,
in video order: **Pexels clip** searched with the beat's OWN query — literal
matches (`lion walking savanna` for the lion entry), no mood forcing — else
a **title card** with the beat's text (stock-footage-or-text-card only, no AI
imagery). A persistent leaderboard (slots 1–5,
left side) fills in with each item's name the moment its rank is revealed —
current rank highlighted red — burned at the final mux via timed FFmpeg
`drawtext`, so entries populate exactly in sync with the narration. Screen time
is weighted by beat word count.

## Captions + music

- **Captions:** the TTS SRT (word-level from edge-tts, or one cue per beat
  from Gemini TTS, the default engine) is converted to a styled ASS
  and burned in with the libass `subtitles` filter. They are tiny and bottom-anchored
  (above the Shorts UI), clear of the left-side leaderboard. The pipeline
  avoids comma-heavy `force_style` options because
  some Windows builds silently render zero caption pixels.
- **Music:** a dark drone bed is generated per run (brown noise → lowpass →
  slow swell at ~−20dB under the voice). Generated = zero copyright-strike
  risk and nothing to download. To use your own track instead, commit a
  loopable `bgm.mp3` (or `bgm1.mp3`, `bgm2.mp3`, ... for a randomly rotated
  pool) to the repo root (YouTube Audio Library is a safe
  source) — the pipeline loops it under the narration automatically.

## TikTok dual-post (optional, free via Buffer)

YouTube stays exactly as-is — only TikTok goes through Buffer (Buffer's
audited TikTok app posts publicly, so no TikTok audit of your own needed):

1. `buffer.com` → connect **TikTok only** → `publish.buffer.com/settings/api` → API key → secret `BUFFER_API_KEY`
2. Nothing else — the Cloudinary public host is already hardcoded (`epuxrxa5` / `buffer_shorts`, unsigned preset, not sensitive). Override via `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_UPLOAD_PRESET` only if you use your own Cloudinary account (Buffer has no upload endpoint; the mp4 needs a public URL)
3. Optional secret `BUFFER_TIKTOK_CHANNEL_ID` — auto-picked when empty

Missing creds = TikTok skipped with a warning, YouTube unaffected. The Buffer post id is recorded as `tiktokBufferPostId` in the same `history.json` entry.

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

The script prompt is designed around the proven ranked-list pattern:

```
0-3s    hook teasing #1 without naming it ("...and number 1 is NOT what you think")
#5→#2   one payoff per item (name + 1-2 concrete facts, rounded numbers)
#1      biggest payoff, held to the end
final   comment-bait closer ("Disagree? Comment your ranking")
```

Each ranked item is a mini open loop that resets attention, and the closer
drives comments. Scripts use simple words, short sentences, one idea per
sentence. When reviewing YouTube Analytics, prioritize **Viewed vs
swiped away**, audience retention, average percentage viewed, completion,
rewatches, shares, and comments. Test different hooks and topics while
keeping the countdown format consistent.

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
