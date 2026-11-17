"""All configuration and secrets come from the environment (.env locally)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://autoclip:autoclip@localhost:5432/autoclip"
    redis_url: str = "redis://localhost:6379/0"
    env_file_path: str = ".env"  # where refreshed tokens are written back

    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = ""
    gemini_fallback_model: str = ""  # tried when the main model is overloaded, before local Ollama
    # further Gemini-API models tried in order after the fallback, before Ollama (comma-separated). Gemma has
    # its own free daily limits on the same key, and judges far better than the small local model.
    gemini_extra_models: str = "gemma-4-26b-a4b-it"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = ""

    sources_file: str = "sources.yaml"
    campaigns_file: str = "config/campaigns.yaml"
    accounts_file: str = "config/accounts.yaml"
    media_dir: str = "media"
    backup_dir: str = "backups"

    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    clip_min_seconds: int = 15
    # 30: Instagram refuses Reels over ~33 s from this account (tested 14 Nov 2026: 33 s ok, 34 s+ refused)
    clip_max_seconds: int = 30
    ig_max_seconds: int = 30  # longer clips are cut to this before an Instagram upload; 0 = off
    download_fragments: int = 8  # HLS pieces fetched at once for Twitch/Kick VODs
    highlights_per_video: int = 5
    # Titles matching any of these jump the queue (x PRIORITY_BOOST). GTA 6 launches ~19 Nov 2026.
    priority_keywords: str = "gta 6,gta vi,gta6,gtavi,grand theft auto 6,grand theft auto vi"
    priority_boost: float = 3.0
    campaign_daily_caps: str = ""  # "stream house:1": at most that many posts a day per account for a campaign
    force_style: str = ""  # a style preset every new clip uses (e.g. story_pop); empty = the bandit picks
    priority_campaigns: str = ""  # words matched in campaign names; their videos are processed first
    # Live clipping (live_daemon.py): record allowlisted streams while they run instead of waiting for the VOD
    live_enabled: bool = True
    live_chunk_seconds: int = 600  # each closed chunk is processed as its own short video
    live_max_streams: int = 3  # simultaneous recordings (disk and CPU)
    live_min_free_gb: int = 20  # no new recording below this; cleanup runs first
    live_quality: str = "720p60,720p,best"
    live_segment_threads: int = 6  # parallel segment downloads (Docker caps each connection's speed here)
    live_chat_lag_s: float = 15.0  # chat reacts this long after the moment; spikes are moved back by it
    live_top_n: int = 2  # clips taken from one live chunk
    # GTA 6 launch mode (services/launch.py): until this date (YYYY-MM-DD) live clips post within minutes
    launch_mode_until: str = ""
    launch_gap_minutes: int = 45
    launch_daily_cap: int = 12
    launch_stale_hours: float = 3.0
    live_dry_run: bool = False  # true = live clips are rendered and scheduled as 'held' (test runs), never posted
    live_skip_vod: bool = True  # a stream recorded live is not downloaded again as a VOD
    sfx_level: str = "full"  # story-style sound effects: off, light or full (pipeline/sfx.py)
    clip_layout: str = "gameplay"  # gameplay = full frame over blur; auto = face-tracked crops when suitable

    # guardrails
    kill_switch: bool = False  # also switchable from the dashboard
    youtube_audit_passed: bool = False
    # Campaigns that forbid "AI-generated content": autoclip clips are real footage, AI-selected and
    # AI-captioned. False = hold those clips for your decision; True = you accept the risk and they post.
    allow_ai_assisted: bool = False
    upload_rate_per_hour: int = 6
    daily_spend_cap_usd: float = 0.0  # 0 = free stack only; any recorded spend stops new jobs
    # When the owner is asleep (IST). Campaign posts are kept out of it because their links must be
    # submitted by hand within ~30 min; other posts go out at any hour. Empty = no restriction.
    owner_away_ist: str = "02:00-10:00"
    no_post_ist: str = "01:00-17:00"  # most clips post 17:00-01:00 IST
    morning_window_ist: str = "10:00-13:00"  # ...but clips of MORNING_CAMPAIGNS post here instead
    morning_campaigns: str = ""  # comma-separated words matched in campaign names, e.g. "jason"
    # Auto-submit (integrations/cr_submit.py): campaigns whose links are submitted by a browser while the
    # owner sleeps, and the extra posting window (US prime time) those campaigns may use. Empty = off.
    auto_submit_campaigns: str = ""
    auto_window_ist: str = "05:30-09:30"
    cr_state_path: str = "/app/secrets/cr_state.json"
    auto_submit_dry_run: bool = False
    auto_submit_always: bool = True  # also while the owner is awake (False = only during OWNER_AWAY_IST)
    heavy_hours: str = ""  # e.g. "0-8" (IST) to transcribe only at night; empty = any time
    view_drop_pause: float = 0.8

    twitch_client_id: str = ""
    twitch_client_secret: str = ""

    admin_password: str = ""  # dashboard login; required to change anything from the dashboard

    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_refresh_token: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    ig_app_id: str = ""
    ig_app_secret: str = ""
    ig_user_id: str = ""
    ig_graph_host: str = "graph.facebook.com"
    ig_token_type: str = "user_long_lived"
    ig_refresh_threshold_days: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
