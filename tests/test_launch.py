from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from autoclip.config import Settings
from autoclip.integrations.telegram import MockNotifier
from autoclip.models import Account, Clip, DecisionLog, Highlight, Post, SourceVideo
from autoclip.services.cleanup import purge_clips
from autoclip.services.launch import LaunchMode, fast_slot
from autoclip.services.publishing import PublishSettings, publish_due
from tests.test_tasks import e2e  # noqa: F401 - shared end-to-end fixture

NOW = datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc)  # 09:30 IST: inside the normal no-post window
MODE = LaunchMode(date(2026, 11, 26))


def account(session, platform="instagram", cap=8):
    a = Account(platform=platform, handle=f"@{platform}", niche="irl_streamer", style_preset="p", daily_cap=cap,
                active=True)
    session.add(a)
    session.flush()
    return a


def post_at(session, acc, t, status="scheduled", clip_id=1):
    p = Post(clip_id=clip_id, account_id=acc.id, platform=acc.platform, scheduled_at=t, status=status)
    session.add(p)
    session.flush()
    return p


def test_mode_is_on_until_the_date_and_reads_settings():
    assert MODE.active(NOW) and not MODE.active(datetime(2026, 11, 27, 0, 1, tzinfo=timezone.utc))
    assert not LaunchMode().active(NOW)
    m = LaunchMode.from_settings(Settings(_env_file=None, launch_mode_until="2026-11-26", launch_gap_minutes=30,
                                          launch_daily_cap=16, launch_stale_hours=2))
    assert (m.until, m.gap, m.daily_cap, m.stale) == (date(2026, 11, 26), timedelta(minutes=30), 16,
                                                      timedelta(hours=2))


def test_free_account_posts_two_minutes_from_now_at_any_hour(session):
    assert fast_slot(session, account(session), NOW, MODE) == NOW + timedelta(minutes=2)


def test_slot_keeps_the_gap_from_other_posts(session):
    acc = account(session)
    post_at(session, acc, NOW - timedelta(minutes=10), "posted")
    post_at(session, acc, NOW + timedelta(minutes=35), clip_id=2)
    # 10 min ago blocks until +35, which is taken, so the next free time is +35 + 45
    assert fast_slot(session, acc, NOW, MODE) == NOW + timedelta(minutes=80)


def test_cap_counts_a_rolling_day_and_launch_cap_overrides_a_lower_account_cap(session):
    acc = account(session, cap=3)
    for k in range(11):
        post_at(session, acc, NOW - timedelta(hours=23) + timedelta(hours=2 * k), "posted", clip_id=k + 1)
    # 11 posts in the last day < 12: allowed now even though the account's own cap is 3
    assert fast_slot(session, acc, NOW + timedelta(hours=1), MODE) is not None


def test_no_slot_within_the_stale_window_means_none(session):
    acc = account(session)
    for k in range(12):
        post_at(session, acc, NOW - timedelta(hours=10) + timedelta(minutes=45 * k), "posted", clip_id=k + 1)
    assert fast_slot(session, acc, NOW, MODE) is None  # day full until hours from now: stale


def make_live_post(session, tmp_path, platform="instagram", created=NOW - timedelta(minutes=10), vid="live_x_1_0"):
    v = SourceVideo(platform="twitch", video_id=vid, channel_id="x", channel_title="X", title="t", duration_s=600,
                    license="", views=0, status="scheduled", rights_basis="permission")
    session.add(v)
    session.flush()
    h = Highlight(source_video_id=v.id, start_s=0, end_s=20, title="t", reason="r", score=80, llm="x",
                  status="rendered")
    session.add(h)
    session.flush()
    folder = tmp_path / "clips" / f"h{h.id}"
    folder.mkdir(parents=True)
    (folder / "clip.mp4").write_bytes(b"x" * 100)
    c = Clip(highlight_id=h.id, path=str(folder / "clip.mp4"), layout="g", style_preset="p", duration_s=20,
             status="scheduled", created_at=created,
             package={platform: {"title": "Kai loses it", "description": "GTA 6 day one", "hashtags": ["#gta6"]}})
    session.add(c)
    session.flush()
    acc = session.scalars(select(Account).where(Account.platform == platform)).first() or account(session, platform)
    return post_at(session, acc, NOW - timedelta(minutes=1), clip_id=c.id), folder


class Pub:
    def __init__(self):
        self.calls = 0

    def publish(self, req):
        self.calls += 1
        raise AssertionError("must not upload")


def test_stale_live_post_expires_instead_of_posting(session, tmp_path):
    post, _ = make_live_post(session, tmp_path, created=NOW - timedelta(hours=4))
    pub = Pub()
    publish_due(session, {"instagram": pub}, MockNotifier(), PublishSettings(launch=MODE), NOW)
    assert post.status == "expired" and pub.calls == 0


def test_youtube_post_goes_to_telegram_for_a_manual_upload(session, tmp_path):
    post, folder = make_live_post(session, tmp_path, platform="youtube")
    note = MockNotifier()
    publish_due(session, {"youtube": Pub()}, note, PublishSettings(launch=MODE), NOW)
    assert post.status == "manual"
    path, caption = note.videos[0]
    assert Path(path) == folder / "clip.mp4" and "POST TO YOUTUBE SHORTS" in caption and "#gta6" in caption


def test_failed_telegram_send_is_retried_not_lost(session, tmp_path):
    post, _ = make_live_post(session, tmp_path, platform="youtube")
    note = MockNotifier()
    note.video_ok = False
    publish_due(session, {"youtube": Pub()}, note, PublishSettings(launch=MODE), NOW)
    assert post.status == "scheduled" and post.scheduled_at > NOW


def test_outside_launch_mode_youtube_is_still_held(session, tmp_path):
    post, _ = make_live_post(session, tmp_path, platform="youtube")
    publish_due(session, {"youtube": Pub()}, MockNotifier(), PublishSettings(), NOW)
    assert post.status == "held"


def test_manual_and_expired_clips_are_cleaned_up(session, tmp_path):
    post, folder = make_live_post(session, tmp_path, platform="youtube")
    post.status = "manual"
    session.flush()
    purge_clips(session, tmp_path, NOW + timedelta(hours=1))
    assert not folder.exists()


def test_live_chunk_in_launch_mode_is_scheduled_within_minutes(session_factory, e2e, tmp_path):
    import random

    from autoclip.integrations.downloader import MockDownloader
    from autoclip.integrations.llm import MockLLM
    from autoclip.integrations.transcriber import MockTranscriber
    from autoclip.models import Campaign
    from autoclip.pipeline.decide import Bandit
    from autoclip.services.pipeline import Deps
    from autoclip.sources import parse_sources
    from autoclip.tasks import run_process_next
    from tests.test_tasks import fake_renderer, llm_answer, transcript
    s = Settings(_env_file=None, sources_file=str(tmp_path / "n.yaml"), campaigns_file=str(tmp_path / "n.yaml"),
                 accounts_file=str(tmp_path / "n.yaml"), media_dir=str(tmp_path / "media"))
    with session_factory() as session:
        camp = session.scalars(select(Campaign)).one()
        session.add(SourceVideo(platform="twitch", video_id="live_jasontheween_1_0", channel_id="jasontheween",
                                channel_title="J", title="J LIVE", duration_s=600, license="", views=0,
                                status="discovered", rights_basis="campaign", campaign_id=camp.id,
                                profile="irl_streamer", rank_score=50.0, published_at=NOW))
        deps = Deps(MockDownloader(e2e), MockTranscriber(transcript()), MockLLM(llm_answer), parse_sources({}),
                    tmp_path / "media", Bandit(session, random.Random(2)), renderer=fake_renderer,
                    rng=random.Random(2), launch=MODE)
        session.commit()
    run_process_next(s, session_factory=session_factory, deps=deps, video_id="live_jasontheween_1_0", now=NOW)
    with session_factory() as session:
        posts = session.scalars(select(Post)).all()
        assert posts
        first = min(p.scheduled_at.replace(tzinfo=timezone.utc) for p in posts)
        assert first - NOW <= timedelta(minutes=3)  # 09:30 IST is normally a no-post hour
        for acc_id in {p.account_id for p in posts}:
            ts = sorted(p.scheduled_at for p in posts if p.account_id == acc_id)
            assert all(b - a >= timedelta(minutes=45) for a, b in zip(ts, ts[1:]))
