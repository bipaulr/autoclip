"""End-to-end and orchestration tests. Everything external is mocked, so these run offline."""
import json
import random
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from autoclip.celery_app import celery
from autoclip.config import Settings
from autoclip.integrations.downloader import ChatMessage, Media, MockDownloader
from autoclip.integrations.feeds import MockTwitch
from autoclip.integrations.instagram import MockInstagramAuth, TokenInfo
from autoclip.integrations.llm import MockLLM
from autoclip.integrations.telegram import MockNotifier
from autoclip.integrations.transcriber import MockTranscriber, Segment, TranscriptResult, Word
from autoclip.models import (Account, Campaign, Clip, DecisionLog, Highlight, Post, SourceVideo, Submission,
                             Transcript)
from autoclip.pipeline.decide import Bandit
from autoclip.pipeline.render import RenderResult
from autoclip.publishers import MockPublisher
from autoclip.rights import Video
from autoclip.services.pipeline import Deps, next_video, process_video
from autoclip.services.settings_store import set_setting
from autoclip.sources import parse_sources
from autoclip.tasks import (in_heavy_hours, run_discovery, run_ig_refresh, run_process_next, run_publish,
                            run_stream_watch)
from autoclip.token_store import MemoryTokenStore

NOW = datetime(2026, 11, 19, 6, tzinfo=timezone.utc)


def test_every_task_is_scheduled():
    names = {e["task"] for e in celery.conf.beat_schedule.values()}
    assert names == {"autoclip.discover_sources", "autoclip.stream_watch", "autoclip.process_next_video",
                     "autoclip.publish_due", "autoclip.collect_metrics", "autoclip.tuner", "autoclip.daily_summary", "autoclip.morning_schedule",
                     "autoclip.refresh_instagram_token", "autoclip.backup", "autoclip.self_heal",
                     "autoclip.campaign_scan", "autoclip.telegram_commands", "autoclip.auto_submit",
                     "autoclip.process_live", "autoclip.cleanup", "autoclip.net_check"}
    assert set(celery.tasks) >= names
    assert celery.conf.task_acks_late is True


def test_heavy_hours():
    s = Settings(_env_file=None, heavy_hours="0-8")
    assert in_heavy_hours(s, datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc))  # 01:30 IST
    assert not in_heavy_hours(s, datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc))  # 13:30 IST
    assert in_heavy_hours(Settings(_env_file=None, heavy_hours=""), NOW)
    assert in_heavy_hours(Settings(_env_file=None, heavy_hours="22-6"), datetime(2026, 11, 19, 18, tzinfo=timezone.utc))


def test_ig_refresh_task_commits(session_factory):
    store = MemoryTokenStore(IG_ACCESS_TOKEN="OLD")
    auth = MockInstagramAuth(TokenInfo(True, NOW + timedelta(days=5)), refreshed=("NEW", NOW + timedelta(days=60)))
    out = run_ig_refresh(Settings(_env_file=None), auth=auth, store=store, notifier=MockNotifier(),
                         session_factory=session_factory, now=NOW)
    assert out.startswith("refreshed:") and store.get("IG_ACCESS_TOKEN") == "NEW"


# ------------------------------------------------------------ end to end
def transcript():
    segs = []
    for i in range(80):
        words = [Word(f"w{i}_{k}", i * 7.5 + k * 1.2, i * 7.5 + k * 1.2 + 1.0) for k in range(6)]
        segs.append(Segment(i * 7.5, i * 7.5 + 7.4, f"Sentence {i} about the stream.", words))
    return TranscriptResult("en", segs)


def llm_answer(prompt):
    if "content safety reviewer" in prompt:
        n = len(re.findall(r"^\[(\d+)\]", prompt, re.M))
        return json.dumps({"results": [{"id": i, "flags": []} for i in range(n)]})
    if "Write post text" in prompt:
        return json.dumps({p: {"title": "Kai lost it", "description": "GTA 6 day one", "hashtags": ["#gta6"]}
                           for p in ("youtube", "instagram")})
    ids = [int(x) for x in re.findall(r"^\[(\d+)\]", prompt, re.M)]
    return json.dumps({"scores": [{"id": i, "hook_strength": 8, "payoff": 7, "standalone_clarity": 8, "emotion": 9,
                                   "niche_fit": 9, "virality": 60 + (i * 7) % 35, "suggested_title": f"Moment {i}",
                                   "hook_headline": "He did NOT see that coming", "hook_type": "reaction",
                                   "context_line": "First hour of GTA 6", "why": "huge reaction"} for i in ids]})


def fake_renderer(job, detector=None):
    job.out_dir.mkdir(parents=True, exist_ok=True)
    p = job.out_dir / "clip.mp4"
    p.write_bytes(b"x" * 100)
    return RenderResult(p, "talking_head", job.preset, job.end - job.start, [])


@pytest.fixture
def e2e(session_factory, tmp_path, monkeypatch):
    monkeypatch.setattr("autoclip.services.pipeline.video_phash", lambda p: "%0128x" % random.getrandbits(512))
    monkeypatch.setattr("autoclip.services.pipeline.pick_thumbnail", lambda p, out: None)
    with session_factory() as s:
        s.add(Campaign(marketplace="content_rewards", external_id="jason", name="Jason Pool", url="https://cr/jason",
                       status="joined", niche="irl_streamer",
                       brief={"platforms": ["instagram", "youtube"], "required_hashtags": ["#JasonTheWeen"],
                              "name_in_caption": "JasonTheWeen", "submit_within_minutes": 30},
                       source_channels=[{"platform": "twitch", "channel_id": "jasontheween"}]))
        s.add_all([Account(platform="instagram", handle="@irl", niche="irl_streamer", style_preset="bold_pop",
                           daily_cap=3, active=True),
                   Account(platform="youtube", handle="IRL", niche="irl_streamer", style_preset="neon_box",
                           daily_cap=3, active=True)])
        s.commit()
    media = Media(tmp_path / "src" / "source.mp4", tmp_path / "src" / "audio.wav", 600.0,
                  [ChatMessage(300 + i * 0.1, "NO WAY") for i in range(80)])
    return media


def test_new_vod_becomes_scheduled_posts_then_published_with_submission_alert(session_factory, e2e, tmp_path):
    tw = MockTwitch({"jasontheween": [Video("twitch", "v77", "jasontheween", "JasonTheWeen", "GTA 6 DAY ONE",
                                            3 * 3600, "", 50000, NOW - timedelta(hours=2), "en")]},
                    live={"jasontheween"})
    s = Settings(_env_file=None, sources_file=str(tmp_path / "none.yaml"), campaigns_file=str(tmp_path / "none.yaml"),
                 accounts_file=str(tmp_path / "none.yaml"), media_dir=str(tmp_path / "media"))
    # 1. the stream is live, then ends: the VOD is discovered within one watch cycle
    assert run_stream_watch(s, session_factory=session_factory, twitch=tw) == "0 stream(s) ended"
    tw.live = set()
    assert run_stream_watch(s, session_factory=session_factory, twitch=tw, now=NOW) == "1 stream(s) ended"

    # 2. the pipeline runs every stage with no manual step
    with session_factory() as session:
        llm = MockLLM(llm_answer)
        deps = Deps(MockDownloader(e2e), MockTranscriber(transcript()), llm, parse_sources({}), tmp_path / "media",
                    Bandit(session, random.Random(2)), renderer=fake_renderer, rng=random.Random(2))
        session.commit()
    out = run_process_next(s, session_factory=session_factory, deps=deps, now=NOW)
    assert "scheduled" in out, out

    with session_factory() as session:
        v = session.scalars(select(SourceVideo)).one()
        assert v.status == "scheduled" and v.campaign_id is not None and v.signals["chat_spikes"]
        assert session.scalars(select(Transcript)).one().language == "en"
        assert session.scalars(select(Highlight).where(Highlight.status == "rendered")).all()
        posts = session.scalars(select(Post)).all()
        assert posts and {p.platform for p in posts} == {"instagram", "youtube"}
        for clip in session.scalars(select(Clip)):
            assert len({p.platform for p in posts if p.clip_id == clip.id}) == len(
                [p for p in posts if p.clip_id == clip.id])  # never two accounts on one platform
            assert "#JasonTheWeen" in clip.package["instagram"]["hashtags"]
        last = max(p.scheduled_at for p in posts).replace(tzinfo=timezone.utc)

    # 3. publishing at the scheduled time: Instagram goes live, YouTube waits for the audit
    note = MockNotifier()
    ig, yt = MockPublisher("instagram"), MockPublisher("youtube")
    later = last + timedelta(minutes=1)
    s.upload_rate_per_hour = 100
    run_publish(s, session_factory=session_factory, publishers={"instagram": ig, "youtube": yt}, notifier=note,
                now=later)
    with session_factory() as session:
        statuses = {(p.platform, p.status) for p in session.scalars(select(Post))}
        assert ("instagram", "posted") in statuses and yt.requests == []
        assert session.scalars(select(Submission)).all()
        log = [f"{d.module}:{d.action}" for d in session.scalars(select(DecisionLog).order_by(DecisionLog.id))]
    assert any("SUBMIT NOW" in m for m in note.sent)
    for step in ("discovery:stream_ended", "discovery:discovered", "pipeline:ingested", "pipeline:transcribed",
                 "pipeline:signals", "pipeline:clip_chosen", "gate:passed", "decide:scheduled", "publish:posted",
                 "publish:held", "submissions:queued"):
        assert step in log, step
    print("\n".join(log))


def test_pipeline_resumes_from_last_finished_stage(session_factory, e2e, tmp_path):
    with session_factory() as session:
        camp = session.scalars(select(Campaign)).one()
        v = SourceVideo(platform="twitch", video_id="v1", channel_id="jasontheween", channel_title="J", title="t",
                        duration_s=600, license="", views=1, status="discovered", rights_basis="campaign",
                        campaign_id=camp.id, profile="irl_streamer")
        session.add(v)
        session.commit()
        trans = MockTranscriber(transcript())
        failing = MockLLM(error=__import__("autoclip.integrations.llm", fromlist=["LLMError"]).LLMError("quota"))
        deps = Deps(MockDownloader(e2e), trans, failing, parse_sources({}), tmp_path / "m", Bandit(session),
                    renderer=fake_renderer)
        out = process_video(v, session, deps, NOW)
        session.commit()
        assert out.startswith("paused") and v.status == "signals" and len(trans.calls) == 1
        assert trans.released == 1  # GPU memory handed back before the LLM step
        deps.llm = MockLLM(llm_answer)
        assert next_video(session).id == v.id
        out = process_video(v, session, deps, NOW)
        assert v.status == "scheduled" and len(trans.calls) == 1  # transcription was not repeated


def test_ended_campaign_blocks_processing(session_factory, e2e, tmp_path):
    with session_factory() as session:
        camp = session.scalars(select(Campaign)).one()
        camp.status = "ended"
        v = SourceVideo(platform="twitch", video_id="v2", channel_id="jasontheween", channel_title="J", title="t",
                        duration_s=600, license="", views=1, status="discovered", rights_basis="campaign",
                        campaign_id=camp.id)
        session.add(v)
        session.flush()
        deps = Deps(MockDownloader(e2e), MockTranscriber(transcript()), MockLLM(llm_answer), parse_sources({}),
                    tmp_path / "m", Bandit(session), renderer=fake_renderer)
        assert process_video(v, session, deps, NOW).startswith("blocked") and deps.downloader.calls == []


def test_paused_source_is_skipped_and_kill_switch_stops_publishing(session_factory, e2e, tmp_path):
    with session_factory() as session:
        session.add(SourceVideo(platform="twitch", video_id="p", channel_id="bad", channel_title="b", title="t",
                                duration_s=600, license="", views=1, status="discovered", rights_basis="x"))
        set_setting(session, "paused_sources", ["twitch:bad"])
        set_setting(session, "kill_switch", True)
        session.commit()
        assert next_video(session) is None
    ig = MockPublisher("instagram")
    out = run_publish(Settings(_env_file=None), session_factory=session_factory, publishers={"instagram": ig},
                      notifier=MockNotifier(), now=NOW)
    assert out == "kill switch on" and ig.requests == []


def test_spend_cap_stops_new_jobs(session_factory):
    from autoclip.models import Cost
    with session_factory() as session:
        session.add(Cost(kind="llm", units=1, usd=0.5, created_at=NOW))
        session.commit()
    assert run_process_next(Settings(_env_file=None, daily_spend_cap_usd=0.0), session_factory=session_factory,
                            now=NOW) == "spend cap reached"


def test_campaign_briefs_are_parsed_once(session_factory):
    from autoclip.tasks import parse_new_briefs
    with session_factory() as session:
        session.add(Campaign(marketplace="cr", external_id="x", name="X", url="u", status="open",
                             rules_text="Use #X. 700,000 views minimum.", allowed_platforms=["instagram"]))
        session.flush()
        llm = MockLLM(['{"required_hashtags": ["X"], "min_views_to_qualify": 700000}'])
        assert parse_new_briefs(session, llm) == 1 and parse_new_briefs(session, llm) == 0
        b = session.scalars(select(Campaign)).one().brief
        assert b["min_views_to_qualify"] == 700000 and b["platforms"] == ["instagram"]
        assert b["required_hashtags"] == ["#X"] and len(llm.prompts) == 1


def test_source_files_are_deleted_when_a_video_finishes_and_clips_kept(session_factory, e2e, tmp_path):
    from autoclip.services.pipeline import delete_source
    media = tmp_path / "media"
    src = media / "twitch_v9"
    src.mkdir(parents=True)
    (src / "source.mp4").write_bytes(b"x" * 1000)
    (media / "clips" / "h1").mkdir(parents=True)
    (media / "clips" / "h1" / "clip.mp4").write_bytes(b"c")
    v = SourceVideo(platform="twitch", video_id="v9", channel_id="c", channel_title="c", title="t", duration_s=600,
                    license="", views=1, status="done", rights_basis="x", media_path=str(src))
    assert delete_source(media, v) == 1000 and not src.exists() and (media / "clips" / "h1" / "clip.mp4").exists()
    outside = SourceVideo(platform="twitch", video_id="x", channel_id="c", channel_title="c", title="t",
                          duration_s=1, license="", views=1, status="done", media_path=str(tmp_path))
    assert delete_source(media, outside) == 0 and tmp_path.exists()  # never deletes outside the media folder


def test_low_disk_pauses_before_downloading(session_factory, e2e, tmp_path):
    with session_factory() as session:
        camp = session.scalars(select(Campaign)).one()
        v = SourceVideo(platform="twitch", video_id="big", channel_id="jasontheween", channel_title="J", title="t",
                        duration_s=600, license="", views=1, status="discovered", rights_basis="campaign",
                        campaign_id=camp.id, profile="irl_streamer")
        session.add(v)
        session.flush()
        deps = Deps(MockDownloader(e2e), MockTranscriber(transcript()), MockLLM(llm_answer), parse_sources({}),
                    tmp_path / "m", Bandit(session), renderer=fake_renderer, min_free_bytes=10**18)
        out = process_video(v, session, deps, NOW)
        assert out.startswith("paused") and "GB free" in out and deps.downloader.calls == []


def test_publish_task_skips_while_another_run_is_uploading(monkeypatch):
    import redis

    from autoclip import tasks

    class Busy:
        def acquire(self, blocking=False):
            return False

    class R:
        def lock(self, *a, **k):
            return Busy()

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **k: R()))
    called = []
    monkeypatch.setattr(tasks, "run_publish", lambda *a, **k: called.append(1) or "ran")
    assert tasks.publish_due_task.run() == "another publish run is still uploading"
    assert called == []


def test_request_urls_with_tokens_are_not_logged():
    import logging

    import autoclip.celery_app  # noqa: F401
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_live_chunks_are_processed_oldest_first_until_none_left(session_factory):
    from autoclip.models import SourceVideo
    from autoclip.tasks import run_live_chunks
    with session_factory() as s:
        for vid in ("live_a_1_1", "live_a_1_0", "plain_vod"):
            s.add(SourceVideo(platform="twitch", video_id=vid, channel_id="a", channel_title="A", title="t",
                              duration_s=1, license="", views=0, status="discovered"))
        s.commit()
    seen = []

    def process(vid):
        seen.append(vid)
        with session_factory() as s:
            s.query(SourceVideo).filter_by(video_id=vid).one().status = "done"
            s.commit()

    out = run_live_chunks(Settings(), session_factory=session_factory, process=process)
    assert seen == ["live_a_1_0", "live_a_1_1"] and out == "2 live chunk(s) processed"


def test_priority_campaign_videos_jump_the_queue(session):
    from autoclip.models import Campaign, SourceVideo
    from autoclip.services.pipeline import next_video
    core = Campaign(marketplace="m", external_id="core", name="$25,000 CORE Pool Campaign", url="u", status="joined")
    other = Campaign(marketplace="m", external_id="o", name="Bobby", url="u2", status="joined")
    session.add_all([core, other])
    session.flush()

    def vid(vid_id, camp, rank, status="discovered"):
        v = SourceVideo(platform="youtube", video_id=vid_id, channel_id=vid_id, channel_title="c", title="t",
                        duration_s=600, license="", views=1, status=status, rights_basis="x", campaign_id=camp.id,
                        rank_score=rank)
        session.add(v)
        session.flush()
        return v
    top, low_core = vid("a", other, 9.0), vid("b", core, 1.0)
    assert next_video(session).id == top.id
    assert next_video(session, ("core",)).id == low_core.id
    busy = vid("c", other, 0.5, status="transcribed")
    assert next_video(session, ("core",)).id == busy.id  # half-finished work still comes first


@pytest.mark.parametrize("dry", [True, False])
def test_live_chunk_runs_end_to_end_and_dry_run_holds_every_post(session_factory, e2e, tmp_path, dry):
    s = Settings(_env_file=None, sources_file=str(tmp_path / "none.yaml"), campaigns_file=str(tmp_path / "none.yaml"),
                 accounts_file=str(tmp_path / "none.yaml"), media_dir=str(tmp_path / "media"))
    with session_factory() as session:
        camp = session.scalars(select(Campaign)).one()
        session.add(SourceVideo(platform="twitch", video_id="live_jasontheween_11191900_0", channel_id="jasontheween",
                                channel_title="Jason Pool", title="Jason Pool LIVE part 1", duration_s=600,
                                license="", views=0, status="discovered", rights_basis="Jason Pool on content_rewards",
                                campaign_id=camp.id, profile="irl_streamer", rank_score=50.0, published_at=NOW))
        deps = Deps(MockDownloader(e2e), MockTranscriber(transcript()), MockLLM(llm_answer), parse_sources({}),
                    tmp_path / "media", Bandit(session, random.Random(2)), renderer=fake_renderer,
                    rng=random.Random(2), live_top_n=2, live_dry_run=dry)
        session.commit()
    out = run_process_next(s, session_factory=session_factory, deps=deps, video_id="live_jasontheween_11191900_0",
                           now=NOW)
    with session_factory() as session:
        posts = session.scalars(select(Post)).all()
        if dry:  # dry run records only: no transcription, no LLM call, nothing scheduled
            assert "LIVE_DRY_RUN" in out and "not scored" in out
            assert posts == []
            assert session.scalars(select(SourceVideo)).one().status == "rejected"
        else:
            assert "scheduled" in out, out
            assert len(session.scalars(select(Clip)).all()) <= 2  # live chunks yield at most live_top_n clips
            assert posts and all(p.status == "scheduled" for p in posts)


def test_stale_live_chunk_is_not_scored(session_factory, e2e, tmp_path):
    from datetime import timedelta
    s = Settings(_env_file=None, sources_file=str(tmp_path / "none.yaml"), campaigns_file=str(tmp_path / "none.yaml"),
                 accounts_file=str(tmp_path / "none.yaml"), media_dir=str(tmp_path / "media"))
    with session_factory() as session:
        camp = session.scalars(select(Campaign)).one()
        session.add(SourceVideo(platform="twitch", video_id="live_jasontheween_1_0", channel_id="jasontheween",
                                channel_title="J", title="J LIVE", duration_s=600, license="", views=0,
                                status="signals", rights_basis="campaign", campaign_id=camp.id,
                                profile="irl_streamer", rank_score=50.0, published_at=NOW - timedelta(hours=4)))
        deps = Deps(MockDownloader(e2e), MockTranscriber(transcript()), MockLLM(llm_answer), parse_sources({}),
                    tmp_path / "media", Bandit(session, random.Random(2)), renderer=fake_renderer)
        session.commit()
    out = run_process_next(s, session_factory=session_factory, deps=deps, video_id="live_jasontheween_1_0", now=NOW)
    assert "4.0 h old" in out and "not scored" in out
    with session_factory() as session:
        assert session.scalars(select(SourceVideo)).one().status == "rejected" and not session.scalars(select(Post)).all()


def test_live_clip_entirely_inside_the_lead_in_is_dropped(session_factory, e2e, tmp_path):
    from autoclip.services.pipeline import _live_lead
    folder = tmp_path / "chunkdir"
    folder.mkdir()
    (folder / "live.json").write_text('{"lead_s": 30.0}')
    v = SourceVideo(platform="twitch", video_id="live_x_1_1", channel_id="x", channel_title="X", title="t",
                    duration_s=330, license="", views=0, status="discovered", media_path=str(folder))
    assert _live_lead(v) == 30.0
    v.video_id = "vod123"
    assert _live_lead(v) == 0.0  # VODs never have a lead-in

    s = Settings(_env_file=None, sources_file=str(tmp_path / "none.yaml"), campaigns_file=str(tmp_path / "none.yaml"),
                 accounts_file=str(tmp_path / "none.yaml"), media_dir=str(tmp_path / "media"))
    with session_factory() as session:
        camp = session.scalars(select(Campaign)).one()
        session.add(SourceVideo(platform="twitch", video_id="live_jasontheween_1_1", channel_id="jasontheween",
                                channel_title="J", title="J LIVE", duration_s=600, license="", views=0,
                                status="discovered", rights_basis="campaign", campaign_id=camp.id,
                                profile="irl_streamer", rank_score=50.0, published_at=NOW, media_path=str(folder)))
        # the LLM loves the first windows most: without the lead-in rule they would be chosen
        llm = MockLLM(lambda p: llm_answer(p) if "content safety" in p or "Write post text" in p else json.dumps(
            {"scores": [{"id": int(i), "hook_strength": 8, "payoff": 7, "standalone_clarity": 8, "emotion": 9,
                         "niche_fit": 9, "virality": 99 - int(i), "suggested_title": f"M{i}", "hook_headline": "x",
                         "hook_type": "reaction", "context_line": "", "why": "w"}
                        for i in re.findall(r"^\[(\d+)\]", p, re.M)]}))
        deps = Deps(MockDownloader(e2e), MockTranscriber(transcript()), llm, parse_sources({}), tmp_path / "media",
                    Bandit(session, random.Random(2)), renderer=fake_renderer, rng=random.Random(2), live_top_n=2)
        session.commit()
    run_process_next(s, session_factory=session_factory, deps=deps, video_id="live_jasontheween_1_1", now=NOW)
    with session_factory() as session:
        hl = session.scalars(select(Highlight)).all()
        assert hl and all(h.end_s > 31 for h in hl)
        assert any("lead-in" in d.reason for d in session.scalars(select(DecisionLog)))


def test_worker_start_clears_every_worker_lock(monkeypatch):
    import redis

    import autoclip.celery_app as app
    deleted = []

    class R:
        def delete(self, *keys):
            deleted.extend(keys)

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url: R()))
    app._clear_stale_pipeline_lock()
    assert set(deleted) == {"autoclip:pipeline", "autoclip:pipeline-live", "autoclip:publish"}
    assert "autoclip:publish" in open(app.__file__.replace("celery_app.py", "tasks.py"), encoding="utf-8").read()
