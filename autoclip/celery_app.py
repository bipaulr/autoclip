import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready

from autoclip.config import get_settings

_settings = get_settings()

# httpx logs every request URL at INFO, and those URLs carry the Instagram and Telegram tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)
celery = Celery("autoclip", broker=_settings.redis_url, backend=_settings.redis_url, include=["autoclip.tasks"])
celery.conf.update(
    timezone="Asia/Kolkata",
    task_acks_late=True,  # a task interrupted by sleep or reboot is redelivered, and resumes by stage
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 6 * 3600},
    result_expires=86400,
)
PIPELINE_LOCK = "autoclip:pipeline"
# Every lock held only by worker tasks. A worker restart mid-upload left autoclip:publish set for up to an
# hour, so every publish run skipped and a Reel went out late (16 Nov 2026).
WORKER_LOCKS = (PIPELINE_LOCK, "autoclip:pipeline-live", "autoclip:publish")


@worker_ready.connect
def _clear_stale_pipeline_lock(**_):
    """When the worker starts nothing can be running, so a lock left by a killed worker is stale."""
    import redis
    redis.Redis.from_url(_settings.redis_url).delete(*WORKER_LOCKS)


celery.conf.beat_schedule = {
    "discover-sources": {"task": "autoclip.discover_sources", "schedule": crontab(minute="*/20")},
    "stream-watch": {"task": "autoclip.stream_watch", "schedule": crontab(minute="*/2")},
    "process-next-video": {"task": "autoclip.process_next_video", "schedule": crontab(minute="*/10")},
    # safety net for live chunks the live daemon queued (it also triggers this itself)
    "process-live": {"task": "autoclip.process_live", "schedule": crontab(minute="*/2")},
    "cleanup": {"task": "autoclip.cleanup", "schedule": crontab(minute="*/15")},
    # Docker's container network once slowed to ~80 KB/s per connection mid-session: alert on Telegram
    "net-check": {"task": "autoclip.net_check", "schedule": crontab(minute="7-59/15")},
    "publish-due": {"task": "autoclip.publish_due", "schedule": crontab(minute="*/2")},
    "collect-metrics": {"task": "autoclip.collect_metrics", "schedule": crontab(minute="*/30")},
    "tuner": {"task": "autoclip.tuner", "schedule": crontab(hour=3, minute=0)},
    # 21:00 IST: the owner is awake (asleep 02:00-10:00 IST)
    "daily-summary": {"task": "autoclip.daily_summary", "schedule": crontab(hour=21, minute=0)},
    # 10:00 IST: the owner is awake (OWNER_AWAY_IST ends) -> the day's post times
    "morning-schedule": {"task": "autoclip.morning_schedule", "schedule": crontab(hour=10, minute=0)},
    "self-heal": {"task": "autoclip.self_heal", "schedule": crontab(minute="*/30")},
    "auto-submit": {"task": "autoclip.auto_submit", "schedule": crontab(minute="*")},
    # public Discover page (robots.txt allows it; /api/ is never called). GTA 6 launches 19 Nov 2026 and new
    # GTA campaigns fill fast, so it is checked every 10 minutes until the launch rush is over.
    "campaign-scan": {"task": "autoclip.campaign_scan", "schedule": crontab(minute="*/10")},
    "telegram-commands": {"task": "autoclip.telegram_commands", "schedule": crontab(minute="*")},
    "refresh-instagram-token": {"task": "autoclip.refresh_instagram_token", "schedule": crontab(hour=3, minute=17)},
    "backup": {"task": "autoclip.backup", "schedule": crontab(hour=2, minute=30)},
}
