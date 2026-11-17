"""Submits campaign posts automatically while the owner is asleep (see integrations/cr_submit.py).

Picks pending submissions that are at least a couple of minutes old (a human does not submit within
seconds), still inside the 30-minute window, posted during the owner's away hours, for a campaign in
AUTO_SUBMIT_CAMPAIGNS. Each is tried once; a failure falls back to the normal Telegram alert.
"""
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.cr_submit import ClipSubmitter
from autoclip.integrations.telegram import Notifier
from autoclip.models import Campaign, Post, Submission
from autoclip.pipeline.decide import owner_away
from autoclip.services.campaigns import NOTICE_TAG
from autoclip.services.settings_store import get_setting, set_setting
from autoclip.services.submissions import set_status

MODULE = "autosubmit"
MIN_AGE = timedelta(minutes=2)
TRIED_KEY = "autosubmit.tried"
LOGIN_ALERT_KEY = "autosubmit.login_alerted"


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=t.tzinfo or timezone.utc)




def remember_notice(session: Session, campaign: Campaign, notice: str, notifier: Notifier | None = None) -> bool:
    """A campaign's "Before you submit" rules (only visible to joined members) are added to its rules text
    and the brief is re-parsed, so the next captions follow them. Returns True when they were new."""
    notice = " ".join((notice or "").split())
    if not notice or notice in (campaign.rules_text or ""):
        return False
    campaign.rules_text = f"{campaign.rules_text or ''}\n\n{NOTICE_TAG} {notice}".strip()
    campaign.brief = None  # parse_new_briefs re-reads the rules on its next run
    log_decision(session, MODULE, "rules_updated", f"{campaign.name}: {notice}", subject=campaign.url)
    if notifier is not None:
        notifier.send(f"{campaign.name} has submission rules: {notice}\nNew captions will follow them.")
    return True


def eligible(campaign: Campaign | None, keywords: tuple[str, ...]) -> bool:
    name = (campaign.name if campaign else "").lower()
    return bool(name) and any(k == "*" or k in name for k in keywords)


def run(session: Session, submitter: ClipSubmitter, notifier: Notifier, now: datetime, *,
        keywords: tuple[str, ...], away: tuple[int, int] | None, rng: random.Random | None = None,
        always: bool = False) -> list[str]:
    if not keywords:
        return []
    rng = rng or random.Random()
    tried = set(get_setting(session, TRIED_KEY, []))
    out = []
    for sub in session.scalars(select(Submission).where(Submission.status == "pending").order_by(Submission.id)):
        if sub.id in tried:
            continue
        alerted, deadline = _utc(sub.alerted_at), _utc(sub.deadline)
        # a little human-like variety: wait 2-6 minutes after posting
        if now - alerted < MIN_AGE + timedelta(seconds=rng.randint(0, 240)) or now >= deadline:
            continue
        camp, post = session.get(Campaign, sub.campaign_id), session.get(Post, sub.post_id)
        if not eligible(camp, keywords) or not (always or owner_away(alerted, away)):
            continue  # the owner is awake for this one: the Telegram button stays theirs
        tried.add(sub.id)
        set_setting(session, TRIED_KEY, sorted(tried)[-500:])
        session.commit()  # never try the same submission twice, even if the browser crashes
        result = submitter.submit(camp.url or "", post.url or "")
        if getattr(result, "notice", None):
            remember_notice(session, camp, result.notice, notifier)
        if result.ok:
            set_status(session, sub.id, "submitted")
            log_decision(session, MODULE, "submitted", f"auto-submitted to {camp.name}: {result.reason}",
                         subject=post.url, data={"submission_id": sub.id})
            notifier.send(f"Auto-submitted #{sub.id} to {camp.name}: {post.url}")
        else:
            log_decision(session, MODULE, "failed", f"auto-submit failed for {camp.name}: {result.reason}",
                         subject=post.url, data={"submission_id": sub.id})
            notifier.send(f"Auto-submit FAILED #{sub.id} ({camp.name}): {result.reason}.\n"
                          f"Submit by hand if you can: {post.url}")
            if result.logged_out and get_setting(session, LOGIN_ALERT_KEY) != now.date().isoformat():
                set_setting(session, LOGIN_ALERT_KEY, now.date().isoformat())
                notifier.send("Content Rewards login for auto-submit needs renewing: "
                              "run  py -3.12 scripts/cr_login.py  on the PC.")
        session.commit()
        out.append(f"#{sub.id} {'ok' if result.ok else 'failed'}: {result.reason}")
    return out
