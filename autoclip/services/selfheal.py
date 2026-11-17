"""Self-healing: retry what failed for a passing reason, and tell the owner about work that is stuck.

Failures caused by the network, rate limits or a platform hiccup used to stay "failed" until someone
re-queued them by hand. This runs every 30 minutes, re-queues those with growing back-off (at most
MAX_REQUEUES times each), and alerts once a day per stuck item so problems surface without asking.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.telegram import Notifier
from autoclip.models import Account, DecisionLog, Post, SourceVideo
from autoclip.pipeline.decide import Bandit, reslot
from autoclip.services.settings_store import get_setting, set_setting

MODULE = "selfheal"
MAX_REQUEUES = 3
BASE_BACKOFF = timedelta(minutes=30)  # 30 min, 1 h, 2 h
STUCK_AFTER = timedelta(hours=6)
# Reasons that say "try again later", not "this can never work".
TRANSIENT = re.compile(
    r"HTTPSConnection|Unable to download (JSON metadata|API page|webpage)|ConnectTimeout|ConnectError|ReadTimeout|"
    r"timed out|Timeout|Network is unreachable|RemoteProtocolError|Connection (reset|refused|aborted)|"
    r"HTTP Error 429|Too Many Requests|HTTP 5\d\d|HTTP Error 5\d\d|ProcessingFailedError|request failed|"
    r"Temporary failure in name resolution|Got error|"
    r"unable to download video data: HTTP Error 403|"  # YouTube media URLs 403 for a while, then work
    r"confirm you.re not a bot", re.I)  # YouTube's bot check on one video at a time; it passes later
WORKING = ("ingested", "transcribed", "signals", "highlighted", "gated")


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _requeues(session: Session, subject: str) -> int:
    return session.scalar(select(func.count(DecisionLog.id)).where(
        DecisionLog.module == MODULE, DecisionLog.action == "requeued", DecisionLog.subject == subject)) or 0


def _last(session: Session, subject: str, *, action: str | None = None) -> DecisionLog | None:
    q = select(DecisionLog).where(DecisionLog.subject == subject)
    q = q.where(DecisionLog.action != "alerted")  # our own alert is not progress on the item
    if action:
        q = q.where(DecisionLog.action == action)
    return session.scalars(q.order_by(DecisionLog.id.desc())).first()


def requeue_videos(session: Session, now: datetime) -> list[str]:
    out = []
    for v in session.scalars(select(SourceVideo).where(SourceVideo.status == "failed")):
        subject = f"{v.platform}:{v.video_id}"
        fail = _last(session, subject, action="failed")
        if fail is None or not TRANSIENT.search(fail.reason or ""):
            continue
        n = _requeues(session, subject)
        if n >= MAX_REQUEUES or now - _aware(fail.created_at) < BASE_BACKOFF * 2 ** n:
            continue
        v.status = "discovered"
        log_decision(session, MODULE, "requeued", f"retry {n + 1} of {MAX_REQUEUES}: last failure looked temporary "
                                                  f"({fail.reason[:120]})", subject=subject)
        out.append(subject)
    session.flush()
    return out


def requeue_posts(session: Session, now: datetime, away: tuple[int, int] | None = None,
                  quiet: tuple[int, int] | None = None) -> list[str]:
    out, accounts = [], set()
    for p in session.scalars(select(Post).where(Post.status == "failed", Post.posted_at.is_(None))):
        subject = f"post:{p.id}"
        if not TRANSIENT.search(p.status_reason or ""):
            continue
        fail = _last(session, subject, action="failed")
        n = _requeues(session, subject)
        failed_at = _aware(fail.created_at) if fail else now
        if n >= MAX_REQUEUES or now - failed_at < BASE_BACKOFF * 2 ** n:
            continue
        p.status, p.attempts, p.scheduled_at = "scheduled", 0, now
        log_decision(session, MODULE, "requeued", f"retry {n + 1} of {MAX_REQUEUES}: {p.status_reason[:120]}",
                     subject=subject)
        out.append(subject)
        accounts.add(p.account_id)
    session.flush()
    for acc_id in accounts:  # find each a proper slot (cap, gap, owner's away hours)
        reslot(session, session.get(Account, acc_id), Bandit(session), now, away=away, quiet=quiet)
    return out


FRESH_FOR = timedelta(days=2)  # a clip older than this is not worth a late slot on another platform


def backfill_platforms(session: Session, now: datetime, away: tuple[int, int] | None = None,
                       quiet=None) -> list[str]:
    """A clip made while one platform's queue was full (YouTube takes 3 a day, Instagram 8) only got a post
    on the other platform. Once a slot opens, give it the missing one, while the clip is still fresh."""
    from autoclip.models import Campaign, Clip, Highlight
    from autoclip.pipeline.decide import next_slot
    out = []
    accounts = session.scalars(select(Account).where(Account.active.is_(True))).all()
    clips = session.scalars(select(Clip).where(Clip.status.in_(("scheduled", "posted")),
                                               Clip.created_at >= now - FRESH_FOR)).all()
    for clip in clips:
        posts = session.scalars(select(Post).where(Post.clip_id == clip.id)).all()
        live = [p for p in posts if p.status in ("scheduled", "posted", "held", "manual")]
        if not live:
            continue
        h = session.get(Highlight, clip.highlight_id)
        video = session.get(SourceVideo, h.source_video_id) if h else None
        camp = session.get(Campaign, clip.campaign_id) if clip.campaign_id else None
        allowed = (camp.brief or {}).get("platforms") if camp else None
        for acc in accounts:
            if any(p.platform == acc.platform for p in posts):
                continue  # already has (or had) a post there: never twice, never a removed one revived
            if allowed and acc.platform not in allowed:
                continue
            niche = (video.profile if video else None) or ""
            if niche and niche not in [n.strip() for n in acc.niche.split(",")]:
                continue
            slot = next_slot(session, acc, Bandit(session), now, campaign=bool(camp), away=away, quiet=quiet,
                             campaign_name=camp.name if camp else None)
            if slot is None or slot[0] - _aware(clip.created_at) > FRESH_FOR + timedelta(days=1):
                continue
            post = Post(clip_id=clip.id, account_id=acc.id, platform=acc.platform, scheduled_at=slot[0],
                        status="scheduled", arms={**(live[0].arms or {}), "post_hour_bucket": slot[1]})
            session.add(post)
            session.flush()
            log_decision(session, MODULE, "backfilled", f"{acc.handle} slot at {slot[0]:%Y-%m-%d %H:%M} UTC: "
                         f"this clip had no {acc.platform} post (that queue was full when it was made)",
                         subject=f"post:{post.id}")
            out.append(f"post:{post.id}")
    return out


def stuck_items(session: Session, now: datetime, running: str | None = None) -> list[tuple[str, str]]:
    """(subject, message) for work that has not moved in STUCK_AFTER, or failed for good."""
    items = []
    for v in session.scalars(select(SourceVideo).where(SourceVideo.status.in_(WORKING))):
        subject = f"{v.platform}:{v.video_id}"
        if subject == running:
            continue
        last = _last(session, subject)
        if last and now - _aware(last.created_at) > STUCK_AFTER:
            items.append((subject, f"'{v.title[:60]}' has been at '{v.status}' for "
                                   f"{(now - _aware(last.created_at)).total_seconds() / 3600:.0f} h: {last.reason[:120]}"))
    for v in session.scalars(select(SourceVideo).where(SourceVideo.status == "failed")):
        subject = f"{v.platform}:{v.video_id}"
        fail = _last(session, subject, action="failed")
        if fail and (not TRANSIENT.search(fail.reason or "") or _requeues(session, subject) >= MAX_REQUEUES):
            items.append((subject, f"'{v.title[:60]}' failed and will not be retried: {fail.reason[:150]}"))
    for p in session.scalars(select(Post).where(Post.status == "scheduled", Post.scheduled_at < now - timedelta(hours=1))):
        items.append((f"post:{p.id}", f"post {p.id} ({p.platform}) is over an hour late; is the worker running?"))
    for p in session.scalars(select(Post).where(Post.status == "failed", Post.posted_at.is_(None))):
        if not TRANSIENT.search(p.status_reason or "") or _requeues(session, f"post:{p.id}") >= MAX_REQUEUES:
            items.append((f"post:{p.id}", f"post {p.id} ({p.platform}) failed for good: {(p.status_reason or '')[:150]}"))
    return items


def alert_stuck(session: Session, notifier: Notifier, now: datetime, running: str | None = None) -> int:
    """One message listing new stuck items; each item is repeated at most once a day."""
    seen = dict(get_setting(session, "selfheal.alerted", {}) or {})
    fresh = []
    for subject, msg in stuck_items(session, now, running):
        last = seen.get(subject)
        if last and now - datetime.fromisoformat(last) < timedelta(days=1):
            continue
        seen[subject] = now.isoformat()
        fresh.append(msg)
        log_decision(session, MODULE, "alerted", msg, subject=subject)
    if fresh:
        notifier.send("autoclip needs a look:\n- " + "\n- ".join(fresh[:10]) +
                      (f"\n(+{len(fresh) - 10} more on the dashboard)" if len(fresh) > 10 else ""))
        set_setting(session, "selfheal.alerted", seen)
    return len(fresh)


def heal(session: Session, notifier: Notifier, now: datetime | None = None, *,
         away: tuple[int, int] | None = None, running: str | None = None,
         quiet: tuple[int, int] | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    videos = requeue_videos(session, now)
    posts = requeue_posts(session, now, away, quiet)
    added = backfill_platforms(session, now, away, quiet)
    alerts = alert_stuck(session, notifier, now, running)
    return (f"{len(videos)} videos and {len(posts)} posts re-queued, {len(added)} missing platform posts added, "
            f"{alerts} alerts")
