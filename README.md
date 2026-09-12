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
4. Optional Variables: `NICHE` (default: scary true horror stories with
   cliffhanger), `VOICE` (default: `en-US-AvaNeural`), `VISIBILITY`
   (default: `private` — keep until your OAuth app passes YouTube audit).
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
| Actions minutes | 2,000 min/mo | ~450–900 min/mo |
| Artifact storage | 500 MB | ~180 MB rolling |
| YouTube uploads | 100/day | 3/day |
| Pexels API | 200 req/day | ~12/day |
| Gemini Flash | free tier | 3 calls/day |

## Files

- `pipeline.py` — whole pipeline (script/audio/visuals/render/upload)
- `.github/workflows/daily.yml` — schedule + split artifacts
- `get_refresh_token.py` — one-time OAuth helper (run locally, never in CI)
- `.env.example` — list of secrets/vars to configure

## Troubleshooting

- Upload skipped but video in `out/` → YouTube secrets missing. Check secret names.
- Videos locked Private → normal for unverified OAuth apps; request YouTube audit or flip `VISIBILITY` later.
- Cron runs late → normal on GitHub free (5–30 min delays).
- Workflow disabled → GitHub auto-disables cron after 60 days of repo inactivity; re-enable under Actions.
