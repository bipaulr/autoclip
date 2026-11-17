import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from autoclip.models import (Account, BanditArm, Campaign, Clip, DecisionLog, Highlight, Metric, Post, PromptVersion,
                             SourceVideo)
from autoclip.pipeline.decide import (ET_OFFSET, MIN_GAP, Bandit, explore_rate, length_bucket, next_slot, parse_away,
                                     plan_clip)
from autoclip.services.analytics import (InstagramInsights, MockMetrics, collect, earnings, estimated_earnings,
                                         reward)
from autoclip.services.settings_store import get_setting
from autoclip.services.tuner import (adjust_threshold, choose_prompt_version, judge_ab, pause_weak_sources,
                                     rewrite_prompt, rollback, update_bandit, welch_p)

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def acct(session, platform="instagram", niche="irl_streamer", cap=3, handle=None):
    a = Account(platform=platform, handle=handle or f"@{platform}-{niche}", niche=niche, style_preset="bold_pop",
                daily_cap=cap, followers=1000, active=True)
    session.add(a)
    session.flush()
    return a


# ------------------------------------------------------------ decision engine
def test_explore_rate_decays():
    assert explore_rate(0) == 0.3 and explore_rate(49) == 0.3
    assert 0.1 < explore_rate(70) < 0.3 and explore_rate(500) == 0.1
    assert [length_bucket(s) for s in (20, 30, 55)] == ["15-25", "25-40", "40-60"]


def test_thompson_sampling_converges_to_the_better_arm_within_200_rounds(session):
    a = acct(session)
    bandit = Bandit(session, random.Random(7))
    truth = {p: 0.3 for p in ("bold_pop", "neon_box", "impact_yellow", "clean_white", "minimal_lower", "karaoke_blue",
                              "story_pop")}
    truth["neon_box"] = 0.7
    sim = random.Random(11)
    picks = []
    for rnd in range(200):
        choice, _ = bandit.choose(a.id, "style_preset", posts_so_far=rnd)
        picks.append(choice)
        bandit.update(a.id, {"style_preset": choice}, sim.random() < truth[choice])
    last = picks[-50:]
    assert last.count("neon_box") / len(last) >= 0.6
    means = {p: bandit.mean(a.id, "style_preset", p) for p in truth}
    assert max(means, key=means.get) == "neon_box"


def test_plan_clip_one_account_per_platform_matching_niche_and_brief(session):
    ig1, ig2 = acct(session, handle="@a"), acct(session, handle="@b")
    yt = acct(session, platform="youtube")
    acct(session, niche="podcast_business", handle="@pod")
    plan = plan_clip(session, niche="irl_streamer", allowed_platforms=None, length_s=33, hook_type="reaction",
                     bandit=Bandit(session, random.Random(1)))
    assert sorted(a.platform for a in plan.accounts) == ["instagram", "youtube"]
    assert {a.id for a in plan.accounts} == {ig1.id, yt.id}
    assert plan.arms["length_bucket"] == "25-40" and plan.arms["hook_type"] == "reaction"
    only_ig = plan_clip(session, niche="irl_streamer", allowed_platforms=["instagram"], length_s=20, hook_type=None,
                        bandit=Bandit(session))
    assert [a.platform for a in only_ig.accounts] == ["instagram"]
    assert plan_clip(session, niche="youtuber_challenge", allowed_platforms=None, length_s=20, hook_type=None,
                     bandit=Bandit(session)) is None


def test_slots_respect_gap_cap_and_jitter(session):
    a = acct(session, cap=2)
    bandit, rng = Bandit(session, random.Random(3)), random.Random(5)
    v = SourceVideo(platform="youtube", video_id="v", channel_id="c", channel_title="c", title="t", duration_s=600,
                    license="", views=1, status="highlighted", rights_basis="x")
    session.add(v)
    session.flush()
    h = Highlight(source_video_id=v.id, start_s=0, end_s=30, title="t", reason="r", score=1, llm="m", status="x")
    session.add(h)
    session.flush()
    slots = []
    for i in range(6):
        c = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="rendered")
        session.add(c)
        session.flush()
        t, bucket = next_slot(session, a, bandit, NOW, rng)
        session.add(Post(clip_id=c.id, account_id=a.id, platform="instagram", scheduled_at=t, status="scheduled"))
        session.flush()
        slots.append(t)
    slots.sort()
    assert all(b - x >= MIN_GAP for x, b in zip(slots, slots[1:]))
    per_day = {}
    for t in slots:
        per_day[(t + ET_OFFSET).date()] = per_day.get((t + ET_OFFSET).date(), 0) + 1
    assert max(per_day.values()) <= 2 and all(t > NOW for t in slots)


# ------------------------------------------------------------ analytics
def test_reward_and_earnings():
    small = reward({"views": 1000, "avg_view_pct": 60, "shares": 10}, followers=100)
    big = reward({"views": 100000, "avg_view_pct": 60, "shares": 1000}, followers=100)
    assert big > small > 0
    assert reward({"views": 1000, "avg_view_pct": 60, "shares": 10}, followers=1_000_000) < small
    assert reward({}, 0) == 0.0
    assert earnings(100_000, 1.30) == 117.0 and earnings(None, 1.0) == 0.0


def test_instagram_insights_parsing():
    def handler(request):
        assert "ig_reels_avg_watch_time" in request.url.params["metric"]
        return httpx.Response(200, json={"data": [
            {"name": "views", "values": [{"value": 5000}]}, {"name": "likes", "values": [{"value": 300}]},
            {"name": "shares", "values": [{"value": 40}]}, {"name": "saved", "values": [{"value": 12}]},
            {"name": "comments", "values": [{"value": 9}]},
            {"name": "ig_reels_avg_watch_time", "values": [{"value": 15000}]}]})

    post = Post(external_id="M1")
    m = InstagramInsights("T", client=httpx.Client(transport=httpx.MockTransport(handler))).fetch(post, 30.0)
    assert (m["views"], m["saves"], m["avg_view_pct"]) == (5000, 12, 50.0)


@pytest.fixture
def posted(session):
    camp = Campaign(marketplace="cr", external_id="c", name="C", url="u", status="joined", rate_per_1k=1.0)
    session.add(camp)
    session.flush()
    v = SourceVideo(platform="twitch", video_id="v", channel_id="kai", channel_title="Kai", title="t", duration_s=3600,
                    license="", views=1, status="highlighted", rights_basis="x")
    session.add(v)
    session.flush()
    a = acct(session)
    posts = []
    for i in range(20):
        h = Highlight(source_video_id=v.id, start_s=i * 40, end_s=i * 40 + 30, title=f"h{i}", reason="r",
                      score=50 + i, llm="m", status="rendered")
        session.add(h)
        session.flush()
        c = Clip(highlight_id=h.id, campaign_id=camp.id, path="p", layout="g", style_preset="bold_pop",
                 duration_s=30, status="posted")
        session.add(c)
        session.flush()
        p = Post(clip_id=c.id, account_id=a.id, platform="instagram", scheduled_at=NOW - timedelta(days=4),
                 posted_at=NOW - timedelta(days=4, hours=-i), status="posted", external_id=f"M{i}",
                 arms={"style_preset": "neon_box" if i % 2 else "bold_pop", "post_hour_bucket": "19-22"})
        session.add(p)
        posts.append(p)
    session.flush()
    return {"posts": posts, "account": a, "video": v}


def test_collect_takes_newest_due_bucket_and_sets_reward(session, posted):
    m = MockMetrics({p.id: {"views": 1000 * (i + 1), "avg_view_pct": 50, "shares": i} for i, p in enumerate(posted["posts"])})
    assert collect(session, {"instagram": m}, now=NOW) == 20
    rows = session.scalars(select(Metric)).all()
    assert {r.age_bucket for r in rows} == {"72h"} and all(p.reward for p in posted["posts"])
    assert collect(session, {"instagram": m}, now=NOW) == 0  # nothing new due
    assert collect(session, {"instagram": m}, now=NOW + timedelta(days=4)) == 20
    rows[0].approved_views = 50_000
    assert estimated_earnings(session) == 45.0


def test_an_excluded_post_leaves_every_view_and_reward(session, posted):
    from autoclip.services.analytics import exclude_post
    m = MockMetrics({p.id: {"views": 1000, "avg_view_pct": 50, "shares": 1} for p in posted["posts"]})
    collect(session, {"instagram": m}, now=NOW)
    gone = posted["posts"][0]
    assert exclude_post(session, gone.id, "never submitted to Content Rewards") == 1
    assert gone.reward is None and not session.scalars(select(Metric).where(Metric.post_id == gone.id)).all()
    assert collect(session, {"instagram": m}, now=NOW + timedelta(days=4)) == 19  # never collected again
    assert any(d.action == "excluded" for d in session.scalars(select(DecisionLog)))


def test_collect_logs_metric_failures(session, posted):
    collect(session, {"instagram": MockMetrics(error="token expired")}, now=NOW)
    assert any("token expired" in d.reason for d in session.scalars(select(DecisionLog)))


# ------------------------------------------------------------ tuner
def test_welch_p():
    assert welch_p([5, 6, 7, 6, 5, 7], [1, 2, 1, 2, 1, 2]) < 0.01
    assert welch_p([1, 2, 1], [5, 6, 7]) > 0.9 and welch_p([1], [2]) == 1.0


def test_bandit_update_runs_once_per_post(session, posted):
    for i, p in enumerate(posted["posts"]):
        p.reward = 2.0 if p.arms["style_preset"] == "neon_box" else 0.5
    bandit = Bandit(session)
    assert update_bandit(session, bandit) == 20 and update_bandit(session, bandit) == 0
    a = posted["account"].id
    assert bandit.mean(a, "style_preset", "neon_box") > bandit.mean(a, "style_preset", "bold_pop")


def test_prompt_rewrite_ab_promote_and_rollback(session, posted):
    for i, p in enumerate(posted["posts"]):
        p.reward = float(i)
    base = PromptVersion(name="highlight:irl_streamer", version=1, examples=[], status="active", ab_share=1.0,
                         created_at=NOW - timedelta(days=10))
    session.add(base)
    session.flush()
    pv = rewrite_prompt(session, "highlight:irl_streamer", NOW)
    assert pv.version == 2 and pv.status == "testing" and len(pv.examples) == 16
    assert rewrite_prompt(session, "highlight:irl_streamer", NOW) is None  # one test at a time
    assert judge_ab(session, "highlight:irl_streamer", NOW + timedelta(days=1)) is None  # too early

    hls = session.scalars(select(Highlight)).all()
    for h in hls:
        h.prompt_version_id = pv.id if h.score >= 60 else base.id  # newer prompt got the better clips
    assert judge_ab(session, "highlight:irl_streamer", NOW + timedelta(days=4)) == "promoted"
    assert pv.status == "active" and base.status == "retired"
    assert rollback(session, "highlight:irl_streamer", 1).status == "active" and pv.status == "retired"


def test_ab_rolls_back_when_not_better(session, posted):
    for p in posted["posts"]:
        p.reward = 1.0
    session.add(PromptVersion(name="highlight:x", version=1, examples=[], status="active", ab_share=1.0,
                              created_at=NOW))
    session.flush()
    pv = rewrite_prompt(session, "highlight:x", NOW)
    assert judge_ab(session, "highlight:x", NOW + timedelta(days=4)) == "rolled_back" and pv.status == "rolled_back"


def test_choose_prompt_version_splits_traffic(session):
    session.add_all([PromptVersion(name="h", version=1, examples=[], status="active", ab_share=1.0),
                     PromptVersion(name="h", version=2, examples=[], status="testing", ab_share=0.2)])
    session.flush()
    rng = random.Random(4)
    picks = [choose_prompt_version(session, "h", rng).version for _ in range(500)]
    assert 0.12 < picks.count(2) / 500 < 0.28


def test_pinned_threshold_is_not_auto_adjusted(session, posted):
    from autoclip.services.settings_store import set_setting
    set_setting(session, "render_threshold", 50)
    set_setting(session, "render_threshold_pinned", True)
    assert adjust_threshold(session) is None
    assert get_setting(session, "render_threshold") == 50


def test_threshold_and_source_pausing(session, posted):
    assert adjust_threshold(session) == 64  # scores 50..69, top 30% = 6 clips -> threshold is the 6th best
    assert get_setting(session, "render_threshold") == 64
    for i, p in enumerate(posted["posts"]):
        p.reward = 5.0 if i < 10 else 0.01 * i  # kai: last 10 clips are weak
    other = SourceVideo(platform="youtube", video_id="o", channel_id="good", channel_title="g", title="t",
                        duration_s=600, license="", views=1, status="x", rights_basis="x")
    session.add(other)
    session.flush()
    for i in range(40):  # a healthy second source on the same account
        h = Highlight(source_video_id=other.id, start_s=i, end_s=i + 30, title="g", reason="r", score=10, llm="m",
                      status="rendered")
        session.add(h)
        session.flush()
        c = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="posted")
        session.add(c)
        session.flush()
        session.add(Post(clip_id=c.id, account_id=posted["account"].id, platform="instagram", scheduled_at=NOW,
                         posted_at=NOW - timedelta(days=5, hours=i), status="posted", reward=1.0 + i * 0.01))
    session.flush()
    paused = pause_weak_sources(session)
    assert paused == ["twitch:kai"] and get_setting(session, "paused_sources") == ["twitch:kai"]
    assert any(d.action == "source_paused" for d in session.scalars(select(DecisionLog)))


def test_account_serving_two_niches(session):
    a = acct(session, niche="irl_streamer,podcast_business", handle="@both")
    for niche in ("irl_streamer", "podcast_business"):
        plan = plan_clip(session, niche=niche, allowed_platforms=None, length_s=30, hook_type=None,
                         bandit=Bandit(session))
        assert [x.id for x in plan.accounts] == [a.id]
    assert plan_clip(session, niche="youtuber_challenge", allowed_platforms=None, length_s=30, hook_type=None,
                     bandit=Bandit(session)) is None


def test_owner_away_hours_parse_and_wrap():
    from autoclip.pipeline.decide import owner_away
    away = parse_away("02:00-10:00")
    assert away == (120, 600) and parse_away("") is None
    assert owner_away(datetime(2026, 11, 14, 21, 0, tzinfo=timezone.utc), away)  # 02:30 IST
    assert not owner_away(datetime(2026, 11, 14, 5, 0, tzinfo=timezone.utc), away)  # 10:30 IST
    assert owner_away(datetime(2026, 11, 14, 19, 0, tzinfo=timezone.utc), (1380, 120))  # 00:30 IST, wraps


def test_campaign_posts_avoid_owner_away_hours_but_others_do_not(session):
    from autoclip.pipeline.decide import SUBMIT_MARGIN, owner_away
    a = acct(session, cap=3)
    away = parse_away("02:00-10:00")
    for seed in range(40):  # every bucket the bandit might pick
        slot = next_slot(session, a, Bandit(session, random.Random(seed)), NOW, random.Random(seed),
                         campaign=True, away=away)
        assert slot is not None
        assert not owner_away(slot[0], away) and not owner_away(slot[0] + SUBMIT_MARGIN, away)
    plain = [next_slot(session, a, Bandit(session, random.Random(s)), NOW, random.Random(s), away=away)[0]
             for s in range(40)]
    assert any(owner_away(t, away) for t in plain)  # non-campaign posts may still go out overnight


def test_reslot_spreads_a_pile_of_approved_posts(session):
    from autoclip.pipeline.decide import reslot
    a = acct(session, cap=3)
    v = SourceVideo(platform="youtube", video_id="r", channel_id="c", channel_title="c", title="t", duration_s=600,
                    license="", views=1, status="x", rights_basis="x")
    session.add(v)
    session.flush()
    for i in range(8):
        h = Highlight(source_video_id=v.id, start_s=i, end_s=i + 30, title="t", reason="r", score=1, llm="m", status="x")
        session.add(h)
        session.flush()
        c = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="scheduled")
        session.add(c)
        session.flush()
        session.add(Post(clip_id=c.id, account_id=a.id, platform="instagram", status="scheduled",
                         scheduled_at=NOW + timedelta(minutes=5 + i)))  # all piled up within minutes
    session.flush()
    assert reslot(session, a, Bandit(session, random.Random(1)), NOW, random.Random(1)) == 8
    times = sorted(p.scheduled_at for p in session.scalars(select(Post).where(Post.status == "scheduled")))
    assert len(times) == 8 and all(b - x >= MIN_GAP for x, b in zip(times, times[1:]))
    per_day = {}
    for t in times:
        d = (t + ET_OFFSET).date()
        per_day[d] = per_day.get(d, 0) + 1
    assert max(per_day.values()) <= 3


def test_no_post_window_is_never_used(session):
    from autoclip.pipeline.decide import owner_away
    a = acct(session, cap=3)
    quiet = parse_away("11:00-16:00")
    for seed in range(40):
        slot = next_slot(session, a, Bandit(session, random.Random(seed)), NOW, random.Random(seed),
                         campaign=bool(seed % 2), away=parse_away("02:00-10:00"), quiet=quiet)
        assert slot is not None and not owner_away(slot[0], quiet)


def test_a_daily_cap_of_8_fills_one_day_across_buckets(session):
    from autoclip.pipeline.decide import ET_OFFSET
    a = acct(session, cap=8)
    v = SourceVideo(platform="youtube", video_id="cap8", channel_id="c", channel_title="c", title="t",
                    duration_s=600, license="", views=1, status="x", rights_basis="x")
    session.add(v)
    session.flush()
    rng = random.Random(1)
    days = []
    for i in range(8):
        h = Highlight(source_video_id=v.id, start_s=i, end_s=i + 30, title="t", reason="r", score=1, llm="m",
                      status="x")
        session.add(h)
        session.flush()
        c = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="rendered")
        session.add(c)
        session.flush()
        t, _ = next_slot(session, a, Bandit(session, rng), NOW, rng, quiet=parse_away("11:00-16:00"))
        session.add(Post(clip_id=c.id, account_id=a.id, platform="instagram", scheduled_at=t, status="scheduled"))
        session.flush()
        days.append((t + ET_OFFSET).date())
    assert len(set(days)) <= 2  # 8 posts land within a day or two, not 3 a day over a week


def test_morning_campaigns_post_in_the_morning_and_the_rest_in_the_evening(session):
    from autoclip.pipeline.decide import IST_OFFSET, make_windows
    w = make_windows("01:00-17:00", "10:00-13:00", "jason")
    a = acct(session, cap=8)
    for seed in range(30):
        rng = random.Random(seed)
        m = next_slot(session, a, Bandit(session, rng), NOW, rng, campaign=True, away=parse_away("02:00-10:00"),
                      quiet=w, campaign_name="$9,000 Jason Pool Campaign")
        e = next_slot(session, a, Bandit(session, rng), NOW, rng, campaign=True, away=parse_away("02:00-10:00"),
                      quiet=w, campaign_name="Stream House LA Clipping")
        assert 10 <= (m[0] + IST_OFFSET).hour < 13
        h = (e[0] + IST_OFFSET).hour
        assert h >= 17 or h < 1


def test_a_capped_campaign_gets_one_slot_a_day_and_others_fill_the_rest(session):
    from autoclip.pipeline.decide import make_windows, parse_caps
    assert parse_caps("stream house:1, bobby:2, junk, :3") == (("stream house", 1), ("bobby", 2))
    w = make_windows("11:00-16:00", campaign_caps="stream house:1")
    a = acct(session, cap=8)
    camp = Campaign(marketplace="cr", external_id="sh", name="Stream House LA Clipping", status="joined", url="u")
    v = SourceVideo(platform="youtube", video_id="capsh", channel_id="c", channel_title="c", title="t",
                    duration_s=600, license="", views=1, status="x", rights_basis="x")
    session.add_all([camp, v])
    session.flush()
    rng, days = random.Random(3), []
    for i in range(3):
        h = Highlight(source_video_id=v.id, start_s=i, end_s=i + 30, title="t", reason="r", score=1, llm="m",
                      status="x")
        session.add(h)
        session.flush()
        c = Clip(highlight_id=h.id, campaign_id=camp.id, path="p", layout="g", style_preset="s", duration_s=30,
                 status="rendered")
        session.add(c)
        session.flush()
        t, _ = next_slot(session, a, Bandit(session, rng), NOW, rng, quiet=w, campaign_name=camp.name)
        session.add(Post(clip_id=c.id, account_id=a.id, platform="instagram", scheduled_at=t, status="scheduled"))
        session.flush()
        days.append((t + ET_OFFSET).date())
    assert len(set(days)) == 3  # one Stream House post per day
    other, _ = next_slot(session, a, Bandit(session, rng), NOW, rng, quiet=w, campaign_name="Jason Pool")
    assert (other + ET_OFFSET).date() == days[0]  # other campaigns still use that first day
