"""Campaign submission queue (plan prompt 12).

Content Rewards has no confirmed public submission API (Whop's bounty-submission API cannot be used
with account API keys, and Content Rewards campaigns are not documented as bounties), so submission
stays a one-tap link: a Telegram push within seconds of each campaign post, escalated if it is not
marked submitted within 20 minutes.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.telegram import Notifier
from autoclip.models import Campaign, Post, Submission

MODULE = "submissions"
STATUSES = ("pending", "submitted", "approved", "rejected", "paid")
ESCALATE_AFTER = timedelta(minutes=20)


def where_to_submit(campaign: Campaign) -> str:
    url = campaign.url or ""
    if "/c/campaigns/" in url:
        return url
    return f"{url or 'contentrewards.com'} (exact campaign page not known yet: open it from your joined campaigns)"


def buttons(sub: Submission, campaign: Campaign) -> list[list[dict]]:
    row = [{"text": "Submitted", "callback_data": f"sub:{sub.id}"}]
    if "/c/campaigns/" in (campaign.url or ""):
        row.append({"text": "Open campaign", "url": campaign.url})
    return [row]


def queue_submission(session: Session, post: Post, campaign: Campaign, notifier: Notifier,
                     now: datetime | None = None, *, auto: bool = False) -> Submission:
    now = now or datetime.now(timezone.utc)
    minutes = (campaign.brief or {}).get("submit_within_minutes") or 30
    sub = Submission(post_id=post.id, campaign_id=campaign.id, deadline=now + timedelta(minutes=minutes),
                     status="pending", alerted_at=now, updated_at=now)
    session.add(sub)
    session.flush()
    if auto:
        notifier.send(f"Posted for {campaign.name}: {post.url}\nIt will be auto-submitted in 2-6 minutes "
                      f"(submission #{sub.id}); no need to tap anything unless a FAILED message follows.")
        log_decision(session, MODULE, "queued", "campaign clip posted; auto-submit will handle it",
                     subject=post.url, data={"campaign": campaign.name, "submission_id": sub.id})
        return sub
    notifier.send(f"SUBMIT NOW ({minutes} min window): {campaign.name}\n"
                  f"1) Copy your post link: {post.url}\n2) Submit it here: {where_to_submit(campaign)}\n"
                  f"3) Tap Submitted below (submission #{sub.id}).", buttons(sub, campaign))
    log_decision(session, MODULE, "queued", f"campaign clip posted; submit within {minutes} minutes",
                 subject=post.url, data={"campaign": campaign.name, "submission_id": sub.id})
    return sub


def escalate(session: Session, notifier: Notifier, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    count = 0
    for sub in session.scalars(select(Submission).where(Submission.status == "pending",
                                                        Submission.escalated_at.is_(None))):
        alerted = sub.alerted_at.replace(tzinfo=sub.alerted_at.tzinfo or timezone.utc)
        if now - alerted >= ESCALATE_AFTER:
            post, camp = session.get(Post, sub.post_id), session.get(Campaign, sub.campaign_id)
            deadline = sub.deadline.replace(tzinfo=sub.deadline.tzinfo or timezone.utc)
            left = int((deadline - now).total_seconds() // 60)
            notifier.send(f"REMINDER: submission #{sub.id} for {camp.name} is still not marked submitted "
                          f"({'%d min left' % left if left > 0 else 'deadline passed'}).\nPost: {post.url}\nSubmit at: {where_to_submit(camp)}",
                          buttons(sub, camp))
            sub.escalated_at = now
            log_decision(session, MODULE, "escalated", f"not submitted after 20 minutes ({left} min left)",
                         subject=post.url)
            count += 1
    return count


def set_status(session: Session, submission_id: int, status: str) -> Submission:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    sub = session.get(Submission, submission_id)
    if sub is None:
        raise KeyError(submission_id)
    sub.status, sub.updated_at = status, datetime.now(timezone.utc)
    log_decision(session, MODULE, status, f"submission #{submission_id} marked {status}")
    return sub
