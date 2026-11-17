from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from autoclip.models import Campaign, DecisionLog
from autoclip.services import focus

NOW = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)


def test_gta_detected_from_twitch_game_or_title():
    assert focus.is_gta_stream({"game": "Grand Theft Auto VI", "title": "day one"})
    assert not focus.is_gta_stream({"game": "Just Chatting", "title": "GTA 6 IN 2 DAYS"})  # game decides
    assert not focus.is_gta_stream({"game": "Grand Theft Auto V", "title": "rp server"})
    assert focus.is_gta_stream({"game": "", "title": "PLAYING GTA 6 ALL NIGHT"})  # Kick: no game, title counts
    assert not focus.is_gta_text("Agta 60 fps")  # whole words only


def test_focus_turns_on_with_a_gta_stream_and_off_12_hours_later(session):
    sent = []
    assert not focus.refresh(session, NOW, sent.append)
    focus.note_gta_stream(session, "twitch:lacy", NOW)
    assert focus.refresh(session, NOW, sent.append) and "ON" in sent[0] and "twitch:lacy" in sent[0]
    assert focus.refresh(session, NOW + timedelta(hours=11), sent.append) and len(sent) == 1  # no repeat
    assert not focus.refresh(session, NOW + timedelta(hours=12, minutes=1), sent.append)
    assert "OFF" in sent[1]
    assert [d.action for d in session.scalars(select(DecisionLog))] == ["on", "off"]


def test_joined_gta_campaign_keeps_focus_on(session):
    session.add(Campaign(marketplace="cr", external_id="g", name="GTA 6 Launch Clipping", url="u", status="joined",
                         ends=date(2026, 11, 30)))
    session.flush()
    assert focus.active(session, NOW)
    session.add(Campaign(marketplace="cr", external_id="o", name="Other", url="u2", status="joined"))
    session.scalars(select(Campaign).where(Campaign.external_id == "g")).one().ends = date(2026, 11, 1)
    session.flush()
    assert not focus.active(session, NOW)  # ended


# ------------------------------------------------------------ posting and processing while focus is on
from autoclip.config import Settings  # noqa: E402
from autoclip.integrations.telegram import MockNotifier  # noqa: E402
from autoclip.models import Post, SourceVideo  # noqa: E402
from autoclip.services.launch import LaunchMode  # noqa: E402
from autoclip.services.publishing import PublishSettings, publish_due  # noqa: E402
from tests.test_launch import NOW as LAUNCH_NOW  # noqa: E402
from tests.test_launch import make_live_post  # noqa: E402
from tests.test_tasks import e2e  # noqa: E402,F401


class Pub:
    def __init__(self):
        self.calls = 0

    def publish(self, req):
        from autoclip.publishers import PublishResult
        self.calls += 1
        return PublishResult("x1", "https://ig/x1", "posted", "public", [])


def test_focus_holds_non_gta_posts_and_publishes_gta_ones(session, tmp_path):
    other, _ = make_live_post(session, tmp_path, vid="live_x_1_0")
    gta, _ = make_live_post(session, tmp_path, vid="live_y_1_0")
    session.get(SourceVideo, 2).title = "GTA 6 · Lacy LIVE part 1"
    session.flush()
    pub = Pub()
    publish_due(session, {"instagram": pub}, MockNotifier(),
                PublishSettings(focus_on=True, launch=LaunchMode(date(2026, 11, 26))), LAUNCH_NOW)
    assert other.status == "held" and other.status_reason == focus.FOCUS_HOLD_REASON
    assert gta.status == "posted" and pub.calls == 1


def test_focus_over_releases_held_posts_back_to_the_schedule(session_factory, tmp_path):
    from autoclip.tasks import release_focus_holds
    with session_factory() as s:
        post, _ = make_live_post(s, tmp_path)
        post.status, post.status_reason = "held", focus.FOCUS_HOLD_REASON
        other_held, _ = make_live_post(s, tmp_path, vid="live_z_1_0")
        other_held.status, other_held.status_reason = "held", "score 40 below auto-approve"
        s.commit()
        assert release_focus_holds(s, Settings(_env_file=None), NOW) == 1
        assert post.status in ("scheduled", "held") and post.status_reason != focus.FOCUS_HOLD_REASON
        assert other_held.status == "held"  # holds for other reasons stay


def test_new_non_gta_vod_waits_while_focus_is_on(session_factory, tmp_path):
    from autoclip.tasks import run_process_next
    with session_factory() as s:
        s.add(SourceVideo(platform="youtube", video_id="v1", channel_id="c", channel_title="C", title="Cooking stream",
                          duration_s=600, license="", views=0, status="discovered", rights_basis="ok"))
        focus.note_gta_stream(s, "twitch:lacy", datetime.now(timezone.utc))
        s.commit()
    out = run_process_next(Settings(_env_file=None, heavy_hours=""), session_factory=session_factory, deps=object())
    assert "GTA focus mode" in out and "waits" in out


def test_gta_vod_in_focus_gets_the_fast_lane_and_gta_tags(session_factory, e2e, tmp_path):
    import random

    from autoclip.integrations.downloader import MockDownloader
    from autoclip.integrations.llm import MockLLM
    from autoclip.integrations.transcriber import MockTranscriber
    from autoclip.models import Campaign, Clip
    from autoclip.pipeline.decide import Bandit
    from autoclip.services.pipeline import Deps
    from autoclip.sources import parse_sources
    from autoclip.tasks import run_process_next
    from tests.test_tasks import fake_renderer, llm_answer, transcript
    now = datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc)  # 09:30 IST: normally a no-post hour
    s = Settings(_env_file=None, sources_file=str(tmp_path / "n.yaml"), campaigns_file=str(tmp_path / "n.yaml"),
                 accounts_file=str(tmp_path / "n.yaml"), media_dir=str(tmp_path / "media"))
    with session_factory() as session:
        camp = session.scalars(select(Campaign)).one()
        session.add(SourceVideo(platform="twitch", video_id="v9", channel_id="jasontheween", channel_title="J",
                                title="JASON PLAYS GTA 6 FOR THE FIRST TIME", duration_s=600, license="", views=0,
                                status="discovered", rights_basis="campaign", campaign_id=camp.id,
                                profile="irl_streamer", rank_score=9.0, published_at=now))
        focus.note_gta_stream(session, "twitch:jasontheween", now)
        deps = Deps(MockDownloader(e2e), MockTranscriber(transcript()), MockLLM(llm_answer), parse_sources({}),
                    tmp_path / "media", Bandit(session, random.Random(2)), renderer=fake_renderer,
                    rng=random.Random(2), launch=LaunchMode(date(2026, 11, 26)))
        session.commit()
    run_process_next(s, session_factory=session_factory, deps=deps, video_id="v9", now=now)
    with session_factory() as session:
        posts = session.scalars(select(Post)).all()
        assert posts and min(p.scheduled_at.replace(tzinfo=timezone.utc) for p in posts) - now <= timedelta(minutes=3)
        tags = session.scalars(select(Clip)).first().package["instagram"]["hashtags"]
        assert "#GTA6" in tags and "#GTAVI" in tags
