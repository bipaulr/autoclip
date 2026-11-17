"""Guardrails that watch accounts, plus the daily summary and accounts.yaml sync."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.telegram import Notifier
from autoclip.models import Account, Clip, Cost, DecisionLog, Metric, Post
from autoclip.services.analytics import estimated_earnings

PAUSE_WORDS = ("restricted", "action blocked", "temporarily blocked", "violat", "strike", "spam", "suspended")


def sync_accounts(path: str | Path, session: Session) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    entries = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("accounts") or []
    for e in entries:
        a = session.scalars(select(Account).where(Account.platform == e["platform"],
                                                  Account.handle == e["handle"])).first()
        if a is None:
            a = Account(platform=e["platform"], handle=e["handle"], niche=e["niche"], active=True,
                        style_preset=e.get("style_preset", "clean_white"))
            session.add(a)
        a.niche = e["niche"]
        a.style_preset = e.get("style_preset", a.style_preset)
        a.daily_cap = int(e.get("daily_cap", 3))
    session.flush()
    return len(entries)


def pause_account(session: Session, account: Account, reason: str, notifier: Notifier) -> None:
    if not account.active:
        return
    account.active, account.paused_reason = False, reason
    log_decision(session, "guardrails", "paused", reason, subject=f"account:{account.handle}")
    notifier.send(f"autoclip PAUSED {account.handle} ({account.platform}): {reason}. "
                  "Resume it from the dashboard once you've checked.")


def check_publish_warnings(session: Session, notifier: Notifier, since: datetime) -> int:
    """Auto-pause an account when a platform error looks like a warning, block or strike."""
    n = 0
    for post in session.scalars(select(Post).where(Post.status == "failed", Post.posted_at.is_(None))):
        reason = (post.status_reason or "").lower()
        if any(w in reason for w in PAUSE_WORDS):
            pause_account(session, session.get(Account, post.account_id),
                          f"platform said: {post.status_reason[:150]}", notifier)
            n += 1
    return n


def check_view_drop(session: Session, notifier: Notifier, now: datetime, drop: float = 0.8) -> list[str]:
    """Pause an account whose 7-day average views fell more than 80% versus the 7 days before."""
    paused = []
    for acc in session.scalars(select(Account).where(Account.active.is_(True))):
        def avg(start, end):
            return session.scalar(select(func.avg(Metric.views)).join(Post, Post.id == Metric.post_id).where(
                Post.account_id == acc.id, Metric.age_bucket == "24h", Post.posted_at >= start, Post.posted_at < end))
        recent = avg(now - timedelta(days=7), now)
        before = avg(now - timedelta(days=14), now - timedelta(days=7))
        if recent is not None and before and before >= 100 and recent < before * (1 - drop):
            pause_account(session, acc, f"7-day average views fell {1 - recent / before:.0%} "
                                        f"({before:.0f} -> {recent:.0f})", notifier)
            paused.append(acc.handle)
    return paused


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


BREAKOUT_VIEWS = 1000  # past Instagram's first test audience (most Reels stop at 100-200)


def retention_report(session: Session, now: datetime, days: int = 3) -> list[str]:
    """How well the last few days' posts held viewers: average watch-through, new style vs old, and
    how many broke out of the first test audience. Watch-through is what decides whether a Reel is
    pushed further."""
    from autoclip.pipeline.style import PRESETS
    latest = (select(Metric.post_id, func.max(Metric.id).label("mid")).group_by(Metric.post_id).subquery())
    rows = session.execute(select(Post, Metric, Clip).join(latest, latest.c.post_id == Post.id)
                           .join(Metric, Metric.id == latest.c.mid).join(Clip, Clip.id == Post.clip_id)
                           .where(Post.posted_at >= now - timedelta(days=days))).all()
    rows = [(p, m, c) for p, m, c in rows if m.avg_view_pct is not None]
    if not rows:
        return [f"Watch-through (last {days} days): no readings yet"]

    def avg(rs):
        return sum(m.avg_view_pct for _, m, _ in rs) / len(rs)

    story = [r for r in rows if (PRESETS.get(r[2].style_preset or "") or PRESETS["clean_white"]).story]
    old = [r for r in rows if r not in story]
    out = [f"Watch-through (last {days} days, {len(rows)} posts): {avg(rows):.0f}% average"]
    if story and old:
        out.append(f"  new style {avg(story):.0f}% ({len(story)} posts) vs old {avg(old):.0f}% ({len(old)})")
    broke = sum(1 for _, m, _ in rows if (m.views or 0) >= BREAKOUT_VIEWS)
    out.append(f"  broke out (>{BREAKOUT_VIEWS} views): {broke} of {len(rows)}")
    best = max(rows, key=lambda r: r[1].views or 0)
    out.append(f"  best: {best[1].views} views, {best[1].avg_view_pct:.0f}% watched, "
               f"{round(best[2].duration_s or 0)} s {best[0].url or ''}")
    return out


def daily_summary(session: Session, now: datetime | None = None, *, free_gb: float | None = None,
                  youtube_audit_passed: bool | None = None) -> str:
    """Evening report: what went out, what broke and why, and what is waiting on the owner."""
    from autoclip.models import SourceVideo, Submission
    now = now or datetime.now(timezone.utc)
    day = now - timedelta(days=1)
    made = session.scalar(select(func.count(Clip.id)).where(Clip.created_at >= day)) or 0
    posted_rows = session.scalars(select(Post).where(Post.posted_at >= day).order_by(Post.posted_at)).all()
    views = session.scalar(select(func.coalesce(func.sum(Metric.views), 0)).where(Metric.taken_at >= day)) or 0
    spend = session.scalar(select(func.coalesce(func.sum(Cost.usd), 0.0)).where(Cost.created_at >= day)) or 0
    held = session.scalars(select(Post).where(Post.status == "held")).all()
    paused = [a.handle for a in session.scalars(select(Account).where(Account.active.is_(False)))]
    fail_rows = session.scalars(select(DecisionLog).where(DecisionLog.created_at >= day, DecisionLog.action == "failed")
                                .order_by(DecisionLog.id.desc())).all()
    scheduled = session.scalars(select(Post).where(Post.status == "scheduled", Post.scheduled_at >= now)
                                .order_by(Post.scheduled_at)).all()
    queue = dict(session.execute(select(SourceVideo.status, func.count(SourceVideo.id))
                                 .where(SourceVideo.status.in_(("discovered", "ingested", "transcribed", "signals",
                                                                 "highlighted", "gated")))
                                 .group_by(SourceVideo.status)).all())
    pending = session.scalar(select(func.count(Submission.id)).where(Submission.status == "pending")) or 0

    lines = ["autoclip daily summary", f"Clips made: {made}", f"Posted: {len(posted_rows)}"]
    lines += [f"  {p.platform}: {p.url}" for p in posted_rows[:8] if p.url]
    nxt = ""
    if scheduled:
        ist = _aware(scheduled[0].scheduled_at) + timedelta(hours=5, minutes=30)
        nxt = f" (next: {scheduled[0].platform} at {ist:%d %b %H:%M} IST)"
    lines += [f"Scheduled: {len(scheduled)}{nxt}", f"Views (new readings): {views}",
              f"Estimated campaign earnings to date: ${estimated_earnings(session):.2f}", f"Spend: ${spend:.2f}",
              f"Videos in the queue: {sum(queue.values())} ({', '.join(f'{k} {v}' for k, v in queue.items()) or 'empty'})"]
    lines += retention_report(session, now)
    if held:
        reasons: dict[str, int] = {}
        for p in held:
            key = (p.status_reason or "no reason").split(";")[0][:70]
            reasons[key] = reasons.get(key, 0) + 1
        lines.append(f"Held posts: {len(held)}")
        lines += [f"  {n} x {r}" for r, n in sorted(reasons.items(), key=lambda x: -x[1])[:4]]
    else:
        lines.append("Held posts: 0")
    lines.append(f"Failures: {len(fail_rows)}")
    lines += [f"  {(f.subject or f.module)[:30]}: {f.reason[:90]}" for f in fail_rows[:5]]
    lines.append(f"Campaign links waiting for you to submit: {pending}")
    lines.append(f"Paused accounts: {', '.join(paused) or 'none'}")
    if free_gb is not None:
        lines.append(f"Free disk for media: {free_gb:.0f} GB" + ("  (LOW: pipeline pauses under 15 GB)" if free_gb < 25 else ""))
    if youtube_audit_passed is False:
        lines.append("YouTube: posts held until the API audit passes (then set YOUTUBE_AUDIT_PASSED=true)")
    return "\n".join(lines)


def day_schedule(session: Session, now: datetime, hours: int = 24) -> str:
    """Morning message: every post scheduled for the next `hours`, in IST, campaign posts marked."""
    from autoclip.models import Campaign, Highlight
    ist = timedelta(hours=5, minutes=30)
    posts = session.scalars(select(Post).where(Post.status == "scheduled", Post.scheduled_at >= now,
                                               Post.scheduled_at < now + timedelta(hours=hours))
                            .order_by(Post.scheduled_at)).all()
    if not posts:
        return "Today's schedule: nothing scheduled in the next 24 hours."
    lines = [f"Today's schedule ({len(posts)} posts, IST):"]
    for p in posts:
        at = p.scheduled_at.replace(tzinfo=p.scheduled_at.tzinfo or timezone.utc) + ist
        clip = session.get(Clip, p.clip_id)
        hl = session.get(Highlight, clip.highlight_id) if clip else None
        camp = session.get(Campaign, clip.campaign_id) if clip and clip.campaign_id else None
        tag = f" [campaign: {camp.name}, submit within 30 min]" if camp else ""
        held = " (held until the YouTube audit passes)" if p.platform == "youtube" else ""
        lines.append(f"{at:%a %H:%M} {p.platform}: {(hl.title if hl else 'clip')[:60]}{tag}{held}")
    return "\n".join(lines)
