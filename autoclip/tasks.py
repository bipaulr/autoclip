"""Celery tasks: wires real clients into the services. Every run_* function accepts mocks for tests."""
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from autoclip.celery_app import celery
from autoclip.config import Settings, get_settings
from autoclip.services.repackage import repackage_campaign
from autoclip.db import get_session_factory
from autoclip.decisions import log_decision
from autoclip.integrations.downloader import YtDlpDownloader, fetch_chat_replay
from autoclip.integrations.feeds import FeedWithFallback, KickPublic, TwitchHelix, YouTubeRSS
from autoclip.integrations.instagram import GraphInstagramAuth
from autoclip.integrations.llm import CachedLLM, FallbackLLM, GeminiLLM, OllamaLLM
from autoclip.integrations.telegram import TelegramNotifier
from autoclip.integrations.transcriber import FasterWhisperTranscriber
from autoclip.integrations.youtube import YouTubeDataAPI
from autoclip.integrations.llm import LLMError
from autoclip.models import Account, Campaign, Post, SourceVideo
from autoclip.pipeline.brief import BriefError, parse_brief
from autoclip.pipeline.decide import Bandit, make_windows, parse_away
from autoclip.progress import RedisProgress
from autoclip.pipeline.profiles import load_profiles
from autoclip.pipeline.render import safe_reencode, trim_clip
from autoclip.publishers.instagram import InstagramPublisher
from autoclip.publishers.youtube import YouTubePublisher
from autoclip.services import analytics, campaign_feed, cleanup, health, portfolio, selfheal, submissions, tuner
from autoclip.services import focus
from autoclip.services.launch import LaunchMode
from autoclip.services.live import live_chunk_ids, recorded_recently
from autoclip.integrations.marketplace import ContentRewardsPublic
from autoclip.services.campaigns import import_campaigns
from autoclip.services.discovery import discover, stream_ended
from autoclip.services.ig_token import refresh_instagram_token
from autoclip.services.pipeline import (Deps, disk_free, next_video, process_video, requeue_moderation_holds,
                                        spend_today)
from autoclip.services.publishing import PublishSettings, publish_due
from autoclip.services.settings_store import get_setting, set_setting
from autoclip.sources import load_sources
from autoclip.token_store import EnvFileTokenStore

IST = ZoneInfo("Asia/Kolkata")


def notifier_for(s: Settings) -> TelegramNotifier:
    return TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)


def _windows(s: Settings):
    return make_windows(s.no_post_ist, s.morning_window_ist, s.morning_campaigns, s.auto_window_ist,
                        s.auto_submit_campaigns, s.campaign_daily_caps)


def build_llm(s: Settings):
    """Provider and model names come from env. Gemini first, local Ollama as the fallback."""
    gemini = GeminiLLM(s.gemini_api_key, s.gemini_model)
    ollama = OllamaLLM(s.ollama_host, s.ollama_model)
    if s.llm_provider == "ollama":
        return ollama
    backup = ollama
    chain = [m.strip() for m in [s.gemini_fallback_model, *s.gemini_extra_models.split(",")] if m.strip()]
    for model in reversed(chain):  # flash -> flash-lite -> gemma -> ollama
        backup = FallbackLLM(GeminiLLM(s.gemini_api_key, model), backup)
    return FallbackLLM(gemini, backup)


def kill_switch_on(session, s: Settings) -> bool:
    return s.kill_switch or bool(get_setting(session, "kill_switch", False))


def in_heavy_hours(s: Settings, now: datetime) -> bool:
    if not s.heavy_hours:
        return True
    start, end = (int(x) for x in s.heavy_hours.split("-"))
    h = now.astimezone(IST).hour
    return start <= h < end if start < end else (h >= start or h < end)


# ---------------------------------------------------------------- discovery
def _keywords(s: Settings) -> list[str]:
    return [k.strip() for k in s.priority_keywords.split(",") if k.strip()]


def parse_new_briefs(session, llm) -> int:
    """Brief engine: turns each campaign's rules text into enforceable constraints, once."""
    n = 0
    for c in session.scalars(select(Campaign).where(Campaign.brief.is_(None), Campaign.rules_text.is_not(None))):
        try:
            b = parse_brief(llm, c.rules_text).model_dump(mode="json")
        except (BriefError, LLMError) as e:
            log_decision(session, "brief", "paused", f"could not parse the brief yet: {e}", subject=c.url)
            continue
        if not b.get("platforms") and c.allowed_platforms:
            b["platforms"] = c.allowed_platforms
        if b.get("allowed_sources"):
            # the brief names the channel by handle (youtube.com/@x) while videos carry the channel id: the
            # campaign's own source channels are those same channels, by id
            b["allowed_sources"] += [str(ch["channel_id"]) for ch in c.source_channels or []
                                     if str(ch["channel_id"]) not in b["allowed_sources"]]
        c.brief = b
        log_decision(session, "brief", "parsed", f"{c.name}: platforms {b.get('platforms')}, tags "
                                                 f"{b.get('required_hashtags')}, minimum views "
                                                 f"{b.get('min_views_to_qualify')}", subject=c.url)
        if c.status == "joined":  # clips already made for it must follow the new rules before they post
            repackage_campaign(session, llm, c)
        n += 1
    session.flush()
    return n


def run_discovery(settings: Settings | None = None, *, session_factory=None, youtube=None, rss=None, twitch=None,
                  kick=None, sources=None, now=None, llm=None) -> str:
    s = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    sources = sources or load_sources(s.sources_file)
    session_factory = session_factory or get_session_factory()
    youtube = youtube or YouTubeDataAPI(s.youtube_client_id, s.youtube_client_secret, s.youtube_refresh_token)
    rss = rss or FeedWithFallback(YouTubeRSS(), youtube)
    twitch = twitch or (TwitchHelix(s.twitch_client_id, s.twitch_client_secret) if s.twitch_client_id else None)
    kick = kick or KickPublic()
    with session_factory() as session:
        import_campaigns(s.campaigns_file, session)
        parse_new_briefs(session, CachedLLM(llm or build_llm(s), session))
        # Creative Commons search costs 100 quota units per query, so it runs once a day at most.
        cc_today = get_setting(session, "discovery.cc_date") == now.date().isoformat()
        if cc_today:
            sources.creative_commons.enabled = False
        r = discover(sources, session, youtube=youtube, rss=rss, twitch=twitch, kick=kick, now=now,
                     priority_keywords=_keywords(s), priority_boost=s.priority_boost)
        portfolio.rank(session, _words_of(s.priority_campaigns))  # keep campaign expected values fresh on the dashboard
        if not cc_today and load_sources(s.sources_file).creative_commons.enabled:
            set_setting(session, "discovery.cc_date", now.date().isoformat())
        session.commit()
    out = f"{r.discovered} discovered, {r.rejected} rejected, {r.already_known} known, {r.quota_units} units"
    return out + (f"; paused: {'; '.join(r.paused)}" if r.paused else "")


def run_stream_watch(settings: Settings | None = None, *, session_factory=None, twitch=None, kick=None,
                     youtube=None, now=None) -> str:
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    twitch = twitch or (TwitchHelix(s.twitch_client_id, s.twitch_client_secret) if s.twitch_client_id else None)
    kick = kick or KickPublic()
    sources = load_sources(s.sources_file)
    with session_factory() as session:
        ended = stream_ended(sources, session, twitch=twitch, kick=kick)
        if s.live_skip_vod:
            now_ = now or datetime.now(timezone.utc)
            for c in [c for c in ended if recorded_recently(session, c, now_)]:
                log_decision(session, "live", "skipped",
                             f"'{c.name}' was clipped live, so its full VOD is not downloaded again",
                             subject=f"{c.platform}:{c.channel_id}")
                ended.remove(c)
        if ended:
            discover(sources, session, twitch=twitch, kick=kick, youtube=youtube, only=ended, boost=3.0, now=now,
                     priority_keywords=_keywords(s), priority_boost=s.priority_boost)
        session.commit()
    return f"{len(ended)} stream(s) ended"


# ---------------------------------------------------------------- pipeline
_transcriber = None  # the whisper model is large: load it once per worker process


def run_process_next(settings: Settings | None = None, *, session_factory=None, deps: Deps | None = None,
                     video_id: str | None = None, now=None) -> str:
    global _transcriber
    s = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    session_factory = session_factory or get_session_factory()
    with session_factory() as session:
        if s.daily_spend_cap_usd >= 0 and spend_today(session, now) > s.daily_spend_cap_usd:
            log_decision(session, "guardrails", "paused", f"daily spend cap ${s.daily_spend_cap_usd} reached")
            session.commit()
            return "spend cap reached"
        if video_id:
            video = session.scalars(select(SourceVideo).where(SourceVideo.video_id == video_id)).first()
        else:
            requeue_moderation_holds(session, now)
            video = next_video(session, _words_of(s.priority_campaigns))
        if video is None:
            return "nothing to process"
        if (video.status == "discovered" and not video.video_id.startswith("live_")
                and focus.active(session, now, _keywords(s)) and not focus.is_gta_video(video, _keywords(s))):
            return f"GTA focus mode: new non-GTA video {video.platform}:{video.video_id} waits"
        if video.status == "discovered" and not in_heavy_hours(s, now) and (video.rank_score or 0) < 3:
            return f"outside heavy hours ({s.heavy_hours} IST); waiting"
        if deps is None:
            _transcriber = _transcriber or FasterWhisperTranscriber(s.whisper_model, s.whisper_device,
                                                                    s.whisper_compute_type)
            # Chat replay is off: the chat-downloader library no longer works with Twitch and its retries
            # stalled each Twitch video for hours. Twitch offers no official chat-replay API.
            deps = Deps(downloader=YtDlpDownloader(s.media_dir, fragments=s.download_fragments),
                        transcriber=_transcriber, llm=CachedLLM(build_llm(s), session),
                        sources=load_sources(s.sources_file), media_dir=Path(s.media_dir), bandit=Bandit(session),
                        allow_ai_assisted=s.allow_ai_assisted, owner_away=parse_away(s.owner_away_ist), quiet=_windows(s),
                        layout=None if s.clip_layout == "auto" else s.clip_layout,
                        min_s=s.clip_min_seconds, max_s=s.clip_max_seconds, top_n=s.highlights_per_video,
                        live_top_n=s.live_top_n, live_dry_run=s.live_dry_run, sfx_level=s.sfx_level, force_style=s.force_style,
                        launch=LaunchMode.from_settings(s), priority_keywords=tuple(_keywords(s)),
                        progress=RedisProgress(s.redis_url, {
                            "platform": video.platform, "video_id": video.video_id, "title": video.title,
                            "channel": video.channel_title, "minutes": video.duration_s // 60}))
        cleanup.run_cleanup(session, Path(s.media_dir), deps.min_free_bytes, now)  # posted footage out, then process
        deps.focus_on = focus.active(session, now, _keywords(s))  # GTA focus: GTA clips take the fast lane
        health.sync_accounts(s.accounts_file, session)
        try:
            out = process_video(video, session, deps, now)
        except Exception as e:
            if isinstance(deps.progress, RedisProgress):
                deps.progress.finish(f"crashed: {type(e).__name__}: {str(e)[:150]}")
            raise
        if isinstance(deps.progress, RedisProgress):
            deps.progress.finish(out)
        session.commit()
        return f"{video.platform}:{video.video_id}: {out}"


# ---------------------------------------------------------------- publishing
def publisher_factory(s: Settings):
    store = EnvFileTokenStore(s.env_file_path)

    def get(account: Account):
        if account.platform == "instagram":
            token, uid = store.get("IG_ACCESS_TOKEN"), store.get("IG_USER_ID")
            return InstagramPublisher(uid, token, s.ig_graph_host, reencode=safe_reencode,
                                      max_seconds=s.ig_max_seconds, trim=trim_clip) if token and uid else None
        if account.platform == "youtube" and s.youtube_refresh_token:
            return YouTubePublisher(s.youtube_client_id, s.youtube_client_secret, s.youtube_refresh_token)
        return None
    return get


def release_focus_holds(session, s: Settings, now: datetime) -> int:
    """GTA focus is over: non-GTA posts it held go back to the schedule, re-spaced per account."""
    from autoclip.pipeline.decide import reslot
    held = session.scalars(select(Post).where(Post.status == "held",
                                              Post.status_reason == focus.FOCUS_HOLD_REASON)).all()
    for p in held:
        p.status, p.status_reason, p.scheduled_at = "scheduled", None, now
    session.flush()
    for acc_id in {p.account_id for p in held}:
        reslot(session, session.get(Account, acc_id), Bandit(session), now, away=parse_away(s.owner_away_ist),
               quiet=_windows(s))
    if held:
        log_decision(session, "focus", "released", f"GTA focus over: {len(held)} held post(s) re-scheduled")
    return len(held)


def run_publish(settings: Settings | None = None, *, session_factory=None, publishers=None, notifier=None,
                now=None) -> str:
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    notifier = notifier or notifier_for(s)
    with session_factory() as session:
        now_ = now or datetime.now(timezone.utc)
        focus_on = focus.active(session, now_, _keywords(s))
        if not focus_on:
            release_focus_holds(session, s, now_)
        out = publish_due(session, publishers or publisher_factory(s), notifier,
                          PublishSettings(kill_switch=kill_switch_on(session, s),
                                          youtube_audit_passed=s.youtube_audit_passed,
                                          upload_rate_per_hour=s.upload_rate_per_hour,
                                          allow_ai_assisted=s.allow_ai_assisted,
                                          auto_submit_keywords=_auto_keywords(s),
                                          launch=LaunchMode.from_settings(s), focus_on=focus_on,
                                          priority_keywords=tuple(_keywords(s))), now)
        submissions.escalate(session, notifier, now)
        health.check_publish_warnings(session, notifier, now or datetime.now(timezone.utc))
        session.commit()
    return "; ".join(out) or "nothing due"


# ---------------------------------------------------------------- analytics, tuning, reports
def run_metrics(settings: Settings | None = None, *, session_factory=None, sources=None, notifier=None, now=None) -> str:
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    now = now or datetime.now(timezone.utc)
    if sources is None:
        store = EnvFileTokenStore(s.env_file_path)
        sources = {"instagram": analytics.InstagramInsights(store.get("IG_ACCESS_TOKEN") or "", s.ig_graph_host)}
        if s.youtube_refresh_token:
            yt = YouTubePublisher(s.youtube_client_id, s.youtube_client_secret, s.youtube_refresh_token)
            sources["youtube"] = analytics.YouTubeAnalytics(yt._token)
    with session_factory() as session:
        n = analytics.collect(session, sources, now)
        health.check_view_drop(session, notifier or notifier_for(s), now, s.view_drop_pause)
        session.commit()
    return f"{n} metric readings"


def run_tuner(settings: Settings | None = None, *, session_factory=None, now=None) -> dict:
    session_factory = session_factory or get_session_factory()
    with session_factory() as session:
        out = tuner.run_daily(session, Bandit(session), list(load_profiles()), now)
        portfolio.rank(session, _words_of(get_settings().priority_campaigns))
        session.commit()
    return out


def run_summary(settings: Settings | None = None, *, session_factory=None, notifier=None, now=None) -> str:
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    with session_factory() as session:
        free = disk_free(Path(s.media_dir))
        text = health.daily_summary(session, now, free_gb=free / 1e9 if free is not None else None,
                                    youtube_audit_passed=s.youtube_audit_passed)
    (notifier or notifier_for(s)).send(text)
    return text


def _running_video(s: Settings) -> str | None:
    """The video the worker is on right now (from the live-progress key), so it is never called stuck."""
    try:
        import json
        import redis
        raw = redis.Redis.from_url(s.redis_url, socket_timeout=3).get("autoclip:progress")
        d = json.loads(raw) if raw else {}
        return f"{d['video']['platform']}:{d['video']['video_id']}" if d.get("running") else None
    except Exception:  # noqa: BLE001 - progress is a nicety; healing must still run
        return None


def run_self_heal(settings: Settings | None = None, *, session_factory=None, notifier=None, now=None,
                  running: str | None = None) -> str:
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    with session_factory() as session:
        out = selfheal.heal(session, notifier or notifier_for(s), now, away=parse_away(s.owner_away_ist), quiet=_windows(s),
                            running=running if running is not None else _running_video(s))
        session.commit()
    return out


def run_campaign_scan(settings: Settings | None = None, *, session_factory=None, market=None, notifier=None,
                      now=None) -> str:
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    with session_factory() as session:
        out = campaign_feed.scan(session, market or ContentRewardsPublic(), notifier or notifier_for(s), now,
                                 priority_keywords=_keywords(s))
        session.commit()
    return out


def run_telegram_commands(settings: Settings | None = None, *, session_factory=None, bot=None) -> str:
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    with session_factory() as session:
        n = campaign_feed.poll_commands(session, bot or notifier_for(s), s.telegram_chat_id, _auto_keywords(s))
        session.commit()
    return f"{n} command(s)"


def run_ig_refresh(settings: Settings | None = None, *, auth=None, store=None, notifier=None,
                   session_factory=None, now=None) -> str:
    s = settings or get_settings()
    auth = auth or GraphInstagramAuth(s.ig_app_id, s.ig_app_secret, s.ig_graph_host)
    store = store or EnvFileTokenStore(s.env_file_path)
    notifier = notifier or notifier_for(s)
    session_factory = session_factory or get_session_factory()
    with session_factory() as session:
        result = refresh_instagram_token(auth, store, notifier, session, now=now,
                                         threshold_days=s.ig_refresh_threshold_days)
        session.commit()
    return f"{result.status}: {result.reason}"


def run_backup(settings: Settings | None = None, *, run=subprocess.run, now=None) -> str:
    """Nightly pg_dump into the backups folder; keeps the last 14."""
    s = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    folder = Path(s.backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"autoclip_{now:%Y%m%d}.sql.gz"
    url = s.database_url.replace("postgresql+psycopg://", "postgresql://")
    p = run(["sh", "-c", f'pg_dump "$DUMP_URL" | gzip > "{target}"'], capture_output=True, text=True,
            env={**os.environ, "DUMP_URL": url})
    if p.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {p.stderr[-300:]}")
    for old in sorted(folder.glob("autoclip_*.sql.gz"))[:-14]:
        old.unlink()
    return str(target)


# ---------------------------------------------------------------- Celery wrappers
def _guarded(name: str, fn, *args, **kwargs):
    """Dead-letter handling: Celery retries with backoff; the final failure is logged and alerted."""
    try:
        return fn(*args, **kwargs)
    except SoftTimeLimitExceeded:
        raise
    except Exception as e:  # noqa: BLE001 - anything else is a failure to record, not to hide
        s = get_settings()
        with get_session_factory()() as session:
            log_decision(session, "orchestrator", "failed", f"{name} failed: {type(e).__name__}: {str(e)[:300]}")
            session.commit()
        notifier_for(s).send(f"autoclip: task {name} failed: {type(e).__name__}: {str(e)[:200]}")
        raise


RETRY = dict(autoretry_for=(ConnectionError, TimeoutError, OSError), retry_backoff=60, retry_backoff_max=900,
             max_retries=3, acks_late=True)


@celery.task(name="autoclip.discover_sources", **RETRY)
def discover_sources_task() -> str:
    return _guarded("discovery", run_discovery)


@celery.task(name="autoclip.stream_watch", **RETRY)
def stream_watch_task() -> str:
    return _guarded("stream watch", run_stream_watch)


@celery.task(name="autoclip.process_next_video", acks_late=True, soft_time_limit=4 * 3600)
def process_next_video_task() -> str:
    import redis
    lock = redis.Redis.from_url(get_settings().redis_url).lock("autoclip:pipeline", timeout=5 * 3600)
    if not lock.acquire(blocking=False):
        return "another video is already being processed"
    try:
        return _guarded("pipeline", run_process_next)
    finally:
        try:
            lock.release()
        except Exception:  # noqa: BLE001 - lock may have expired
            pass


def run_live_chunks(settings: Settings | None = None, *, session_factory=None, process=None, limit: int = 20) -> str:
    """Processes queued live chunks one after another (oldest first), until none are left."""
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    process = process or (lambda vid: run_process_next(s, session_factory=session_factory, video_id=vid))
    done = []
    for _ in range(limit):
        with session_factory() as session:
            ids = live_chunk_ids(session)
        if not ids:
            break
        done.append(process(ids[0]))  # GTA chunks first, then oldest first
    return f"{len(done)} live chunk(s) processed"


@celery.task(name="autoclip.process_live", acks_late=True, soft_time_limit=4 * 3600)
def process_live_task() -> str:
    import redis
    lock = redis.Redis.from_url(get_settings().redis_url).lock("autoclip:pipeline-live", timeout=5 * 3600)
    if not lock.acquire(blocking=False):
        return "live chunks are already being processed"
    try:
        return _guarded("live chunks", run_live_chunks)
    finally:
        try:
            lock.release()
        except Exception:  # noqa: BLE001
            pass


def run_cleanup_task(settings: Settings | None = None, *, session_factory=None, now=None) -> str:
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    with session_factory() as session:
        freed = cleanup.run_cleanup(session, Path(s.media_dir), s.live_min_free_gb * 10**9, now)
        session.commit()
    return f"{freed / 1e9:.2f} GB freed"


def run_net_check(settings: Settings | None = None, *, session_factory=None, notifier=None, speed=None,
                  now=None) -> str:
    from autoclip.services import netcheck
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    bps = netcheck.measure() if speed is None else speed
    with session_factory() as session:
        shown = netcheck.check(session, notifier or notifier_for(s), now or datetime.now(timezone.utc), bps)
        session.commit()
    return f"download speed inside Docker: {shown}"


@celery.task(name="autoclip.net_check")
def net_check_task() -> str:
    return _guarded("network check", run_net_check)


@celery.task(name="autoclip.cleanup")
def cleanup_task() -> str:
    return _guarded("cleanup", run_cleanup_task)


@celery.task(name="autoclip.publish_due", **RETRY)
def publish_due_task() -> str:
    # One publish run at a time: an upload can outlast the 2-minute schedule, and a second run would
    # see the same post still 'scheduled' and upload it again (it happened: one clip posted twice).
    import redis
    lock = redis.Redis.from_url(get_settings().redis_url).lock("autoclip:publish", timeout=3600)
    if not lock.acquire(blocking=False):
        return "another publish run is still uploading"
    try:
        return _guarded("publish", run_publish)
    finally:
        try:
            lock.release()
        except Exception:  # noqa: BLE001 - lock may have expired
            pass


@celery.task(name="autoclip.collect_metrics", **RETRY)
def collect_metrics_task() -> str:
    return _guarded("analytics", run_metrics)


@celery.task(name="autoclip.tuner")
def tuner_task() -> dict:
    return _guarded("tuner", run_tuner)


def _words_of(text: str) -> tuple[str, ...]:
    from autoclip.pipeline.decide import _words
    return _words(text)


def _auto_keywords(s: Settings) -> tuple[str, ...]:
    from autoclip.pipeline.decide import _words
    return _words(s.auto_submit_campaigns)


def run_auto_submit(settings: Settings | None = None, *, session_factory=None, submitter=None, notifier=None,
                    now=None) -> str:
    from autoclip.integrations.cr_submit import BrowserSubmitter
    from autoclip.pipeline.decide import _words
    from autoclip.services import autosubmit
    s = settings or get_settings()
    keywords = _words(s.auto_submit_campaigns)
    if not keywords:
        return "auto-submit is off"
    session_factory = session_factory or get_session_factory()
    with session_factory() as session:
        out = autosubmit.run(session, submitter or BrowserSubmitter(s.cr_state_path, dry_run=s.auto_submit_dry_run),
                             notifier or notifier_for(s), now or datetime.now(timezone.utc),
                             keywords=keywords, away=parse_away(s.owner_away_ist),
                             always=s.auto_submit_always)
    return "; ".join(out) or "nothing to submit"


@celery.task(name="autoclip.auto_submit")
def auto_submit_task() -> str:
    return _guarded("auto submit", run_auto_submit)


def run_morning_schedule(settings: Settings | None = None, *, session_factory=None, notifier=None, now=None) -> str:
    s = settings or get_settings()
    session_factory = session_factory or get_session_factory()
    with session_factory() as session:
        text = health.day_schedule(session, now or datetime.now(timezone.utc))
    (notifier or notifier_for(s)).send(text)
    return text


@celery.task(name="autoclip.morning_schedule")
def morning_schedule_task() -> str:
    return _guarded("morning schedule", run_morning_schedule)


@celery.task(name="autoclip.daily_summary")
def daily_summary_task() -> str:
    return _guarded("daily summary", run_summary)


@celery.task(name="autoclip.self_heal")
def self_heal_task() -> str:
    return _guarded("self-heal", run_self_heal)


@celery.task(name="autoclip.campaign_scan", **RETRY)
def campaign_scan_task() -> str:
    return _guarded("campaign scan", run_campaign_scan)


@celery.task(name="autoclip.telegram_commands")
def telegram_commands_task() -> str:
    return run_telegram_commands()  # runs every minute: a failure here is not worth an alert each time


@celery.task(name="autoclip.refresh_instagram_token")
def refresh_instagram_token_task() -> str:
    return _guarded("instagram token refresh", run_ig_refresh)


@celery.task(name="autoclip.backup")
def backup_task() -> str:
    return _guarded("backup", run_backup)
