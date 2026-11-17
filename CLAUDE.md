You are building "autoclip", a fully automated short-form clipping system that must cost $0 to run.
Goal: discover long videos from an allowlist of paid campaigns and permissioned creators, find highlights, render 9:16 clips with captions and an original value-add layer, publish to YouTube Shorts and Instagram Reels (no TikTok: banned in India, where this runs), track analytics, and self-tune.
Stack (all free, runs on my own PC): Python 3.12 (not 3.13, MediaPipe lacks wheels), FastAPI, Celery + Redis, Postgres via SQLAlchemy 2 + Alembic, all in Docker Compose; ffmpeg; faster-whisper locally for transcription; Gemini API free tier for LLM calls with a local Ollama model as fallback; Next.js 15 dashboard on localhost; Telegram bot for alerts. No paid services, no cloud storage: upload from local files.
Rules:
- Only process sources in sources.yaml or active campaigns. Never bypass the rights gate.
- Respect free-tier quotas: batch LLM requests, cache results, back off on 429, and fall back to Ollama when the daily quota is used up.
- Every external API behind an interface with a mock, so tests run offline.
- Config and secrets via env only. LLM provider and model names from env.
- Write tests for every module and run them before saying a task is done. Show me the test output.
- Log every decision (clip chosen, skipped, posted, paused) with a reason to the DB.
- Never post the same clip to more than one account per platform.