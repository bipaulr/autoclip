# autoclip: operations guide

How the owner runs the system day to day. For what it is and how it works, see the main [README](../README.md).


Automated short-form clipping on your own PC, at $0 running cost. It finds long videos from
sources you have rights to, picks highlights, renders 9:16 clips with captions and a hook, posts
them to Instagram Reels and YouTube Shorts, tracks results and tunes itself.

## Start it

```
docker compose up -d --build
```

- Dashboard: http://127.0.0.1:3010 (password: `ADMIN_PASSWORD` in `.env`)
- Status page: http://127.0.0.1:8010/status
- Start automatically at logon (once): `powershell -ExecutionPolicy Bypass -File .\scripts\start-on-boot.ps1`
- Stop everything posting instantly: the kill switch on the dashboard's Overview tab, or `KILL_SWITCH=true` in `.env`.

Docker Desktop must be running. Ollama (the free local fallback model) runs outside Docker and
is reached at `host.docker.internal:11434`.

## How it works

Every 20 minutes discovery checks your sources; every 2 minutes it watches streamers and queues a
VOD as soon as a stream ends. The pipeline takes one video at a time through:

discover -> rights gate -> download + audio + chat -> transcribe (faster-whisper, word timings) ->
signals (loudness peaks, chat spikes, shot cuts) -> highlights (Gemini free tier, Ollama fallback) ->
safety gate -> render (face-tracked or gameplay layout, captions, hook, zooms, SFX, -14 LUFS) ->
package (titles, hashtags, thumbnail, duplicate check) -> decide (account, style, time slot) ->
publish -> analytics (1h to 7d) -> daily tuner.

Every decision, with its reason, is in the dashboard's Decisions tab.

## What only you can do

| Task | Where |
|---|---|
| Join a campaign and accept its terms, then click "I joined it" on the Campaigns tab | Content Rewards / Vyro / Clipster |
| Submit each campaign post's link (a Telegram message tells you within seconds) | the campaign's page |
| Enter approved views from the marketplace, for earnings | Analytics tab |
| Approve held clips below an account's auto-approve score | Calendar tab |
| Submit the YouTube API audit (draft answers in the project notes) | Google form |
| Whop/Vyro identity and payout checks | the marketplace |

Until the YouTube audit passes, YouTube posts are held (uploads would be forced private).
After it passes, set `YOUTUBE_AUDIT_PASSED=true` in `.env` and restart.

## Files you edit

- `config/campaigns.yaml`: campaign listings and their source channels.
- `config/accounts.yaml`: your posting accounts and their niches.
- `sources.yaml`: creators who gave written permission (with the permission noted).
- `config/profiles/*.yaml`: what makes a good clip per niche.
- `.env`: keys and settings (never commit or share it).

## Commands

| | Windows | elsewhere |
|---|---|---|
| tests | `.\scripts\autoclip.ps1 test` | `make test` |
| logs | `.\scripts\autoclip.ps1 logs` | `make logs` |
| migrate | `.\scripts\autoclip.ps1 migrate` | `make migrate` |

Backups: a gzipped Postgres dump lands in `backups/` every night (last 14 kept). Jobs survive
sleep and reboots: each video resumes from its last finished stage.

## Honest limits

- No marketplace offers a listings or submission API usable here, so campaign lists live in
  `config/campaigns.yaml` and submission is a one-tap Telegram link.
- On-screen text OCR and a frame-level violence classifier are not built; the gate checks what is
  said instead.
- Kick has no official API; Kick discovery is best-effort and may be blocked.
- Transcribing a 3-hour stream on this PC's CPU takes a while; set `HEAVY_HOURS=0-8` to run long
  jobs at night (stream-ended VODs still go first).
