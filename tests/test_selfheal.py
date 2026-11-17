from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from autoclip.decisions import log_decision
from autoclip.integrations.telegram import MockNotifier
from autoclip.models import Account, Clip, DecisionLog, Highlight, Post, SourceVideo
from autoclip.services import selfheal
from autoclip.services.health import daily_summary

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def video(session, vid, status="failed", title="t"):
    v = SourceVideo(platform="twitch", video_id=vid, channel_id="c", channel_title="c", title=title, duration_s=600,
                    license="", views=1, status=status, rights_basis="x")
    session.add(v)
    session.flush()
    return v


def failed(session, subject, reason, at, action="failed"):
    row = log_decision(session, "pipeline", action, reason, subject=subject)
    row.created_at = at
    session.flush()


def test_transient_video_failures_are_requeued_with_backoff_and_a_limit(session):
    v = video(session, "1")
    failed(session, "twitch:1", "download failed: Unable to download JSON metadata: HTTPSConnection", NOW - timedelta(hours=1))
    perm = video(session, "2")
    failed(session, "twitch:2", "download failed: ERROR: Postprocessing: Conversion failed!", NOW - timedelta(hours=1))
    assert selfheal.requeue_videos(session, NOW) == ["twitch:1"]
    assert v.status == "discovered" and perm.status == "failed"
    assert selfheal.TRANSIENT.search("download failed: DownloadError: ERROR: unable to download video data: "
                                     "HTTP Error 403: Forbidden")

    # fails again right away: the next retry waits 1 h, then 2 h, and stops after 3 retries
    for n, wait in ((1, 60), (2, 120)):
        v.status = "failed"
        failed(session, "twitch:1", "download failed: HTTP Error 429: Too Many Requests", NOW)
        assert selfheal.requeue_videos(session, NOW + timedelta(minutes=wait - 5)) == []
        assert selfheal.requeue_videos(session, NOW + timedelta(minutes=wait + 1)) == ["twitch:1"]
    v.status = "failed"
    failed(session, "twitch:1", "download failed: ConnectTimeout", NOW)
    assert selfheal.requeue_videos(session, NOW + timedelta(days=1)) == []


def _post(session, status="failed", reason="Instagram upload HTTP 400 ProcessingFailedError", when=NOW):
    acc = session.scalars(select(Account)).first()
    if acc is None:
        acc = Account(platform="instagram", handle="@a", niche="x", style_preset="bold_pop", active=True, daily_cap=3)
        session.add(acc)
        session.flush()
    v = video(session, f"p{session.scalar(select(func.count(SourceVideo.id)))}", status="done")
    h = Highlight(source_video_id=v.id, start_s=0, end_s=30, title="t", reason="r", score=60, llm="m", status="rendered")
    session.add(h)
    session.flush()
    c = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="scheduled")
    session.add(c)
    session.flush()
    p = Post(clip_id=c.id, account_id=acc.id, platform="instagram", scheduled_at=when, status=status,
             status_reason=reason, attempts=3)
    session.add(p)
    session.flush()
    return p


def test_transient_post_failures_are_rescheduled_into_a_proper_slot(session):
    p = _post(session)
    failed(session, f"post:{p.id}", p.status_reason, NOW - timedelta(hours=1))
    bad = _post(session, reason="Instagram HTTP 400: (#10) Application does not have permission")
    assert selfheal.requeue_posts(session, NOW) == [f"post:{p.id}"]
    assert p.status == "scheduled" and p.attempts == 0 and p.scheduled_at >= NOW
    assert bad.status == "failed"


def test_stuck_and_permanent_failures_alert_once_a_day(session):
    v = video(session, "s", status="transcribed", title="Long stream")
    failed(session, "twitch:s", "transcribed", NOW - timedelta(hours=8), action="transcribed")
    running = video(session, "r", status="signals")
    failed(session, "twitch:r", "signals", NOW - timedelta(hours=8), action="signals")
    perm = video(session, "x")
    failed(session, "twitch:x", "download failed: Conversion failed!", NOW - timedelta(hours=1))
    late = _post(session, status="scheduled", reason=None, when=NOW - timedelta(hours=2))
    note = MockNotifier()
    assert selfheal.alert_stuck(session, note, NOW, running="twitch:r") == 3
    text = note.sent[0]
    assert "Long stream" in text and "Conversion failed" in text and f"post {late.id}" in text and "twitch:r" not in text
    assert selfheal.alert_stuck(session, note, NOW + timedelta(hours=2), running="twitch:r") == 0  # no repeat
    assert selfheal.alert_stuck(session, note, NOW + timedelta(days=1, hours=1), running="twitch:r") == 3
    assert v and perm


def test_heal_logs_every_requeue(session):
    video(session, "1")
    failed(session, "twitch:1", "download failed: Network is unreachable", NOW - timedelta(hours=1))
    out = selfheal.heal(session, MockNotifier(), NOW)
    assert out.startswith("1 videos and 0 posts re-queued")
    assert any(d.action == "requeued" and d.module == "selfheal" for d in session.scalars(select(DecisionLog)))


def test_daily_summary_reports_links_failures_and_waiting_items(session):
    p = _post(session, status="posted", reason=None, when=NOW - timedelta(hours=3))
    p.posted_at, p.url = NOW - timedelta(hours=3), "https://www.instagram.com/reel/abc/"
    _post(session, status="held", reason="YouTube API audit not passed yet: uploads would be locked private")
    failed(session, "twitch:9", "download failed: Conversion failed!", NOW - timedelta(hours=2))
    text = daily_summary(session, NOW, free_gb=20, youtube_audit_passed=False)
    for part in ("Posted: 1", "reel/abc", "Held posts: 1", "YouTube API audit", "Failures: 1", "Conversion failed",
                 "LOW", "YOUTUBE_AUDIT_PASSED"):
        assert part in text, part


def test_long_streams_get_more_highlights():
    from autoclip.services.pipeline import scaled_candidates, scaled_top_n
    assert scaled_top_n(5, 20 * 60) == 5 and scaled_top_n(5, 3600) == 5
    assert scaled_top_n(5, 4 * 3600) == 14 and scaled_top_n(5, 9 * 3600) == 20
    assert scaled_candidates(1800) == 60 and scaled_candidates(4 * 3600) == 160 and scaled_candidates(9 * 3600) == 180


def test_daily_summary_reports_watch_through_and_breakouts(session):
    from autoclip.models import Metric
    from autoclip.services.health import retention_report
    old = _post(session, status="posted", reason=None, when=NOW - timedelta(hours=30))
    new = _post(session, status="posted", reason=None, when=NOW - timedelta(hours=20))
    for p, style, views, pct in ((old, "bold_pop", 150, 20.0), (new, "story_pop", 1800, 55.0)):
        p.posted_at, p.url = p.scheduled_at, f"https://www.instagram.com/reel/{style}/"
        session.get(Clip, p.clip_id).style_preset = style
        session.add(Metric(post_id=p.id, age_bucket="24h", taken_at=NOW, views=views, avg_view_pct=pct))
    session.flush()
    text = "\n".join(retention_report(session, NOW))
    assert "38% average" in text and "new style 55% (1 posts) vs old 20% (1)" in text
    assert "broke out (>1000 views): 1 of 2" in text and "reel/story_pop" in text
    assert "no readings yet" in retention_report(session, NOW + timedelta(days=10))[0]


def test_youtube_bot_check_is_retried_later():
    assert selfheal.TRANSIENT.search("download failed: DownloadError: ERROR: [youtube] x: Sign in to confirm "
                                     "you’re not a bot. Use --cookies-from-browser")


def _clip_with_ig_post(session, created=NOW - timedelta(hours=3), platforms=None, niche="irl_streamer"):
    from autoclip.models import Campaign
    ig = session.scalars(select(Account).where(Account.platform == "instagram")).first()
    if ig is None:
        ig = Account(platform="instagram", handle="@ig", niche="irl_streamer", style_preset="p", active=True,
                     daily_cap=8)
        yt = Account(platform="youtube", handle="@yt", niche="irl_streamer,podcast", style_preset="p",
                     active=True, daily_cap=3)
        session.add_all([ig, yt])
    camp = Campaign(marketplace="cr", external_id=f"c{created.timestamp()}{platforms}", name="Jason Pool",
                    status="joined", url="u", brief={"platforms": platforms} if platforms else None)
    v = SourceVideo(platform="youtube", video_id=f"bf{created.timestamp()}{platforms}{niche}", channel_id="c",
                    channel_title="c", title="t", duration_s=600, license="", views=1, status="done",
                    profile=niche)
    session.add_all([camp, v])
    session.flush()
    h = Highlight(source_video_id=v.id, start_s=0, end_s=20, title="t", reason="r", score=80, llm="m",
                  status="rendered")
    session.add(h)
    session.flush()
    c = Clip(highlight_id=h.id, campaign_id=camp.id, path="p", layout="g", style_preset="story_pop",
             duration_s=20, status="scheduled", created_at=created)
    session.add(c)
    session.flush()
    session.add(Post(clip_id=c.id, account_id=ig.id, platform="instagram", status="scheduled",
                     scheduled_at=NOW + timedelta(hours=5), arms={"style_preset": "story_pop"}))
    session.flush()
    return c


def test_a_clip_without_a_youtube_post_gets_one_once_a_slot_opens(session):
    c = _clip_with_ig_post(session)
    added = selfheal.backfill_platforms(session, NOW)
    yt = session.scalars(select(Post).where(Post.clip_id == c.id, Post.platform == "youtube")).all()
    assert len(added) == 1 and len(yt) == 1 and yt[0].status == "scheduled" and selfheal._aware(yt[0].scheduled_at) > NOW
    assert selfheal.backfill_platforms(session, NOW) == []  # never a second one
    yt[0].status = "removed"
    assert selfheal.backfill_platforms(session, NOW) == []  # a removed post is not revived


def test_backfill_skips_stale_clips_disallowed_platforms_and_other_niches(session):
    _clip_with_ig_post(session, created=NOW - timedelta(days=3))
    _clip_with_ig_post(session, platforms=["instagram"])
    _clip_with_ig_post(session, niche="gaming")
    assert selfheal.backfill_platforms(session, NOW) == []
