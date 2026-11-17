"""Publishes due posts, with every guardrail checked right before each upload."""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.telegram import Notifier
from autoclip.models import Account, Campaign, Clip, Highlight, Post, SourceVideo
from autoclip.pipeline.brief import Brief
from autoclip.pipeline.gate import check_brief, check_source
from autoclip.publishers import Publisher, PublishError, PublishRequest, QuotaFull
from autoclip.services import focus
from autoclip.services.launch import LaunchMode
from autoclip.services.submissions import queue_submission

MODULE = "publish"
MAX_ATTEMPTS = 3


@dataclass
class PublishSettings:
    kill_switch: bool = False
    youtube_audit_passed: bool = False
    upload_rate_per_hour: int = 6  # global limiter across all accounts
    allow_ai_assisted: bool = False
    auto_submit_keywords: tuple[str, ...] = ()  # campaigns whose links autosubmit.py submits
    launch: LaunchMode = field(default_factory=LaunchMode)
    focus_on: bool = False  # GTA focus: non-GTA posts are held until it ends
    priority_keywords: tuple[str, ...] = ()


YOUTUBE_LINK = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)([\w-]{11})")


def manual_buttons(post: Post) -> list[list[dict]]:
    return [[{"text": "I posted it: add link", "callback_data": f"ytl:{post.id}"}]]


def record_manual_post(session: Session, post_id: int, url: str, notifier: Notifier,
                       auto_submit_keywords: tuple[str, ...] = (), now: datetime | None = None) -> str:
    """The owner uploaded a 'manual' YouTube post by hand and sent its link: the post becomes 'posted'
    (so analytics and the one-account rules see it) and a campaign clip is queued for submission."""
    now = now or datetime.now(timezone.utc)
    m = YOUTUBE_LINK.search(url or "")
    if not m:
        return "That doesn't look like a YouTube link. Send the Short's link, like https://youtube.com/shorts/..."
    post = session.get(Post, post_id)
    if post is None or post.platform != "youtube":
        return f"No YouTube post #{post_id}."
    if post.status == "posted":
        return f"Post #{post.id} is already recorded: {post.url}"
    if post.status != "manual":
        return f"Post #{post.id} is {post.status}, not waiting for a manual upload."
    clip, account = session.get(Clip, post.clip_id), session.get(Account, post.account_id)
    campaign = session.get(Campaign, clip.campaign_id) if clip.campaign_id else None
    post.external_id, post.url = m.group(1), f"https://youtube.com/shorts/{m.group(1)}"
    post.status, post.posted_at, post.privacy, post.status_reason = "posted", now, "public", "uploaded by hand"
    clip.status = "posted"
    log_decision(session, MODULE, "posted", f"owner uploaded it by hand to {account.handle}: {post.url}",
                 subject=f"post:{post.id}", data={"arms": post.arms, "manual": True})
    if campaign is None:
        return f"Recorded post #{post.id}: {post.url}"
    auto = any(k == "*" or k in campaign.name.lower() for k in auto_submit_keywords)
    queue_submission(session, post, campaign, notifier, now, auto=auto)
    return f"Recorded post #{post.id}: {post.url}" + (". Auto-submit follows in 2-6 minutes." if auto else "")


def _caption(clip: Clip, platform: str) -> tuple[str, str, list[str]]:
    p = (clip.package or {}).get(platform, {})
    return p.get("title", ""), p.get("description", ""), list(p.get("hashtags", []))


def publish_due(session: Session, publishers: dict[str, Publisher], notifier: Notifier, settings: PublishSettings,
                now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    out = []
    if settings.kill_switch:
        log_decision(session, MODULE, "paused", "KILL_SWITCH is on; nothing will be published")
        return ["kill switch on"]
    hour_ago = now - timedelta(hours=1)
    recent = len(session.scalars(select(Post.id).where(Post.posted_at >= hour_ago)).all())
    due = session.scalars(select(Post).where(Post.status == "scheduled", Post.scheduled_at <= now)
                          .order_by(Post.scheduled_at)).all()
    for post in due:
        if recent >= settings.upload_rate_per_hour:
            log_decision(session, MODULE, "deferred", f"global upload limit of {settings.upload_rate_per_hour}/hour reached")
            break
        out.append(_publish_one(session, post, publishers, notifier, settings, now))
        recent += post.status == "posted"
        session.commit()  # save each result at once: a later failure must not undo a finished upload
    return out


def _publish_one(session: Session, post: Post, publishers: dict[str, Publisher], notifier: Notifier,
                 settings: PublishSettings, now: datetime) -> str:
    clip = session.get(Clip, post.clip_id)
    hl = session.get(Highlight, clip.highlight_id)
    video = session.get(SourceVideo, hl.source_video_id)
    account = session.get(Account, post.account_id)
    campaign = session.get(Campaign, clip.campaign_id) if clip.campaign_id else None
    subject = f"post:{post.id}"

    def stop(status: str, reason: str) -> str:
        post.status, post.status_reason = status, reason
        log_decision(session, MODULE, status, reason, subject=subject)
        return f"{subject} {status}: {reason}"

    if not account.active:
        return stop("held", f"account {account.handle} is paused: {account.paused_reason}")
    if settings.focus_on and not focus.is_gta_video(video, settings.priority_keywords or focus.DEFAULT_KEYWORDS):
        return stop("held", focus.FOCUS_HOLD_REASON)
    # Gate, second pass: rights, campaign brief (platform, length, caption) right before upload.
    src = check_source(video, campaign, now.date())
    if not src.ok:
        return stop("blocked", src.reason)
    launch = settings.launch.active(now)
    created = clip.created_at if clip.created_at.tzinfo else clip.created_at.replace(tzinfo=timezone.utc)
    if launch and (video.video_id or "").startswith("live_") and now - created > settings.launch.stale:
        return stop("expired", f"launch mode: live clip is {(now - created).total_seconds() / 3600:.1f} h old; "
                               "the moment is stale, so it is not posted")
    title, desc, tags = _caption(clip, post.platform)
    brief = Brief.model_validate(campaign.brief) if campaign and campaign.brief else None
    b = check_brief(brief, video=video, length_s=clip.duration_s, platform=post.platform,
                    caption=" ".join([title, desc, " ".join(tags)]),
                    allow_ai_assisted=settings.allow_ai_assisted)
    if not b.ok:
        return stop("held" if b.hold else "blocked", b.reason)
    if post.platform == "youtube" and not settings.youtube_audit_passed and launch:
        # API uploads would be forced private: hand the finished clip to the owner for a manual upload.
        send = getattr(notifier, "send_video", None)
        note = (f"\n\nCampaign: {campaign.name}. Its 30-minute submit window starts when you post."
                if campaign else "")
        after = ("Once it is up, tap the button below and paste the Short's link"
                 + (": it gets submitted to the campaign for you." if campaign else "."))
        caption = (f"POST TO YOUTUBE SHORTS ({account.handle}) - post #{post.id}\n\n{title}\n\n{desc}\n"
                   f"{' '.join(tags)}{note}\n\n{after}")
        if send and send(Path(clip.path), caption, manual_buttons(post)):
            return stop("manual", "sent to Telegram for a manual YouTube upload (API audit pending)")
        log_decision(session, MODULE, "retry", "could not send the clip to Telegram; trying again next run",
                     subject=subject)
        post.scheduled_at = now + timedelta(minutes=5)
        return f"{subject} retry: Telegram send failed"
    if post.platform == "youtube" and not settings.youtube_audit_passed:
        return stop("held", "YouTube API audit not passed yet: uploads would be locked private; held until "
                            "YOUTUBE_AUDIT_PASSED=true")

    publisher = publishers(account) if callable(publishers) else publishers.get(post.platform)
    if publisher is None:
        return stop("held", f"no publisher configured for {post.platform}")
    post.attempts += 1
    try:
        res = publisher.publish(PublishRequest(Path(clip.path), title, desc, tags, privacy="public",
                                               thumbnail=Path(clip.thumbnail_path) if clip.thumbnail_path else None,
                                               duration_s=clip.duration_s))
    except QuotaFull as e:
        post.scheduled_at = now + timedelta(hours=1)
        log_decision(session, MODULE, "deferred", f"{e}; retrying in an hour", subject=subject)
        return f"{subject} deferred: {e}"
    except PublishError as e:
        if e.retryable and post.attempts < MAX_ATTEMPTS:
            post.scheduled_at = now + timedelta(minutes=10 * post.attempts)
            log_decision(session, MODULE, "retry", f"{e}; attempt {post.attempts} of {MAX_ATTEMPTS}", subject=subject)
            return f"{subject} retry: {e}"
        notifier.send(f"autoclip: publishing failed for {account.handle} ({post.platform}): {e}")
        return stop("failed", str(e))

    post.external_id, post.url, post.privacy = res.external_id, res.url, res.privacy
    post.responses = (post.responses or []) + res.responses
    if res.status == "locked_private":
        notifier.send("autoclip: YouTube forced an upload to private. The project needs YouTube's free API "
                      "compliance audit before uploads can be public. YouTube posts are now held; Instagram "
                      "keeps running.")
        return stop("held", "YouTube locked the upload to private (audit required)")
    post.status, post.posted_at = "posted", now
    clip.status = "posted"
    log_decision(session, MODULE, "posted", f"posted to {account.handle} on {post.platform}: {res.url}",
                 subject=subject, data={"arms": post.arms})
    if campaign is not None:
        auto = any(k == "*" or k in campaign.name.lower() for k in settings.auto_submit_keywords)
        queue_submission(session, post, campaign, notifier, now, auto=auto)
    return f"{subject} posted: {res.url}"
