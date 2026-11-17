from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from autoclip.integrations.telegram import MockNotifier
from autoclip.models import Account, Campaign, Clip, DecisionLog, Highlight, Metric, Post, SourceVideo
from autoclip.services.health import (check_publish_warnings, check_view_drop, daily_summary, sync_accounts)
from autoclip.services.portfolio import expected_value, rank

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def test_sync_accounts_from_yaml(session, tmp_path):
    f = tmp_path / "accounts.yaml"
    f.write_text("accounts:\n  - {platform: instagram, handle: '@a', niche: irl_streamer, daily_cap: 4}\n",
                 encoding="utf-8")
    assert sync_accounts(f, session) == 1 and sync_accounts(f, session) == 1
    a = session.scalars(select(Account)).one()
    assert (a.handle, a.daily_cap, a.active) == ("@a", 4, True)
    assert sync_accounts(tmp_path / "missing.yaml", session) == 0


def _post(session, account, views_by_day):
    v = SourceVideo(platform="youtube", video_id=f"v{account.id}", channel_id="c", channel_title="c", title="t",
                    duration_s=600, license="", views=1, status="x", rights_basis="x")
    session.add(v)
    session.flush()
    for i, views in enumerate(views_by_day):
        h = Highlight(source_video_id=v.id, start_s=i, end_s=i + 30, title="t", reason="r", score=1, llm="m", status="x")
        session.add(h)
        session.flush()
        c = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="posted")
        session.add(c)
        session.flush()
        p = Post(clip_id=c.id, account_id=account.id, platform=account.platform, scheduled_at=NOW,
                 posted_at=NOW - timedelta(days=13 - i), status="posted")
        session.add(p)
        session.flush()
        session.add(Metric(post_id=p.id, age_bucket="24h", views=views))
    session.flush()


def test_view_drop_pauses_account_and_alerts(session):
    a = Account(platform="instagram", handle="@drop", niche="n", style_preset="s", active=True)
    b = Account(platform="youtube", handle="@fine", niche="n", style_preset="s", active=True)
    session.add_all([a, b])
    session.flush()
    _post(session, a, [5000] * 7 + [300] * 7)  # fell 94%
    _post(session, b, [5000] * 7 + [4000] * 7)
    note = MockNotifier()
    assert check_view_drop(session, note, NOW) == ["@drop"]
    assert not a.active and "views fell" in a.paused_reason and b.active
    assert any("PAUSED @drop" in m for m in note.sent)


def test_platform_warning_pauses_account(session):
    a = Account(platform="instagram", handle="@w", niche="n", style_preset="s", active=True)
    session.add(a)
    session.flush()
    _post(session, a, [10])
    p = session.scalars(select(Post)).one()
    p.status, p.posted_at, p.status_reason = "failed", None, "Instagram HTTP 400: Action blocked for spam"
    note = MockNotifier()
    assert check_publish_warnings(session, note, NOW) == 1 and not a.active
    assert any(d.action == "paused" for d in session.scalars(select(DecisionLog)))


def test_daily_summary_text(session):
    text = daily_summary(session, NOW)
    for part in ("Clips made: 0", "Posted: 0", "Spend: $0.00", "Paused accounts: none"):
        assert part in text


def test_expected_value_and_gambling_exclusion():
    rate = Campaign(name="Vyro Roblox", rules_text="", rate_per_1k=2.5, min_payout=10, max_payout=1000,
                    budget_total=10000, budget_remaining=9000)
    low = Campaign(name="Low", rules_text="", rate_per_1k=0.35, min_payout=10, budget_total=10000, budget_remaining=1000)
    casino = Campaign(name="Stake casino slots", rules_text="", rate_per_1k=5.0)
    history = [2000, 8000, 15000, 40000]
    assert expected_value(rate, history)[0] > expected_value(low, history)[0] > 0
    assert expected_value(casino, history) == (-1.0, "gambling campaign: never joined")
    pool = Campaign(name="Pool", rules_text="", brief={"min_views_to_qualify": 1_000_000})
    ev, why = expected_value(pool, history)
    assert ev > 0 and "P(cross minimum)" in why


def test_rank_pauses_low_budget_and_suggests_joins(session):
    session.add_all([
        Campaign(marketplace="m", external_id="a", name="A", url="ua", status="joined", rate_per_1k=1.0,
                 budget_total=1000, budget_remaining=100),
        Campaign(marketplace="m", external_id="b", name="B", url="ub", status="open", rate_per_1k=2.0),
        Campaign(marketplace="m", external_id="c", name="C slots casino", url="uc", status="open", rate_per_1k=9.0)])
    session.flush()
    ranked = rank(session)
    by = {c.external_id: c for c in ranked}
    assert by["a"].status == "paused" and by["c"].ev_score == -1.0 and by["b"].ev_score > 0
    actions = {(d.action, d.subject) for d in session.scalars(select(DecisionLog))}
    assert ("suggest_join", "ub") in actions and ("suggest_join", "uc") not in actions


def test_campaigns_without_footage_do_not_count_toward_the_active_limit(session):
    src = [{"platform": "twitch", "channel_id": "x"}]
    for i in range(6):
        session.add(Campaign(marketplace="m", external_id=f"s{i}", name=f"S{i}", url=f"u{i}", status="joined",
                             rate_per_1k=0.5, source_channels=src))
    for i in range(3):
        session.add(Campaign(marketplace="m", external_id=f"n{i}", name=f"N{i}", url=f"n{i}", status="joined",
                             rate_per_1k=5.0))
    session.flush()
    rank(session)
    assert all(c.status == "joined" for c in session.scalars(select(Campaign)))


def test_active_limit_never_pauses_a_campaign_with_clips_waiting_to_post(session):
    from datetime import datetime, timezone
    from autoclip.models import Account
    src = [{"platform": "twitch", "channel_id": "x"}]
    camps = []
    for i in range(7):
        c = Campaign(marketplace="m", external_id=f"w{i}", name=f"W{i}", url=f"w{i}", status="joined",
                     rate_per_1k=5.0 - i * 0.5, source_channels=src)
        session.add(c)
        camps.append(c)
    session.flush()
    low = camps[-1]  # lowest EV: would be paused by the limit
    acc = Account(platform="instagram", handle="h", niche="irl", style_preset="default")
    session.add(acc)
    session.flush()
    v = SourceVideo(platform="twitch", video_id="wv", channel_id="c", channel_title="c", title="t", duration_s=600,
                    license="", views=1, status="done", rights_basis="x", campaign_id=low.id)
    session.add(v)
    session.flush()
    h = Highlight(source_video_id=v.id, start_s=0, end_s=30, title="t", reason="r", score=60, llm="m",
                  status="rendered")
    session.add(h)
    session.flush()
    clip = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="rendered")
    session.add(clip)
    session.flush()
    session.add(Post(clip_id=clip.id, account_id=acc.id, platform="instagram", status="scheduled",
                     scheduled_at=datetime.now(timezone.utc)))
    session.flush()
    rank(session)
    assert low.status == "joined"


def test_morning_schedule_lists_the_days_posts_in_ist(session):
    from autoclip.services.health import day_schedule
    now = datetime(2026, 11, 16, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
    assert "nothing scheduled" in day_schedule(session, now)
    acc = Account(platform="instagram", handle="@a", niche="x", style_preset="s", active=True)
    camp = Campaign(marketplace="m", external_id="j", name="Jason", url="u", status="joined")
    v = SourceVideo(platform="twitch", video_id="ms", channel_id="c", channel_title="c", title="t", duration_s=600,
                    license="", views=1, status="done", rights_basis="x")
    session.add_all([acc, camp, v])
    session.flush()
    for i, (hours, campaign) in enumerate([(1, None), (8, camp.id), (30, None)]):
        h = Highlight(source_video_id=v.id, start_s=i, end_s=i + 30, title=f"Moment {i}", reason="r", score=60,
                      llm="m", status="rendered")
        session.add(h)
        session.flush()
        c = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="rendered",
                 campaign_id=campaign)
        session.add(c)
        session.flush()
        session.add(Post(clip_id=c.id, account_id=acc.id, platform="instagram", status="scheduled",
                         scheduled_at=now + timedelta(hours=hours)))
    session.flush()
    text = day_schedule(session, now)
    assert "2 posts" in text and "11:00 instagram: Moment 0" in text
    assert "18:00 instagram: Moment 1 [campaign: Jason" in text and "Moment 2" not in text


def test_active_limit_never_pauses_a_priority_campaign(session):
    src = [{"platform": "twitch", "channel_id": "x"}]
    for i in range(6):
        session.add(Campaign(marketplace="m", external_id=f"p{i}", name=f"P{i}", url=f"p{i}", status="joined",
                             rate_per_1k=5.0, source_channels=src))
    jason = Campaign(marketplace="m", external_id="j", name="$9,000 Jason Pool Campaign", url="j",
                     status="joined", rate_per_1k=0.1, source_channels=src)  # lowest EV
    session.add(jason)
    session.flush()
    rank(session, ("jason",))
    assert jason.status == "joined"
    assert sum(c.status == "paused" for c in session.scalars(select(Campaign))) == 1  # another one made room
