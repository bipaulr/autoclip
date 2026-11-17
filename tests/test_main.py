import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from autoclip.db import get_session
from autoclip.decisions import log_decision
from autoclip.main import app


@pytest.fixture
def client(session_factory):
    def override():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_health_db(client):
    assert client.get("/health/db").json() == {"status": "ok"}


def test_decisions_lists_newest_first(client, session_factory):
    with session_factory() as s:
        log_decision(s, "a", "skipped", "first")
        log_decision(s, "b", "posted", "second")
        s.commit()
    rows = client.get("/decisions?limit=1").json()
    assert len(rows) == 1 and rows[0]["reason"] == "second" and rows[0]["module"] == "b"


def test_source_videos_lists_only_requested_status(client, session_factory):
    from autoclip.models import SourceVideo

    def row(vid, status, views):
        return SourceVideo(platform="youtube", video_id=vid, channel_id="UCx", channel_title="Chan",
                           title=vid, duration_s=3600, license="creativeCommon", views=views,
                           status=status, rights_basis="CC" if status == "discovered" else None)

    with session_factory() as s:
        s.add_all([row("a", "discovered", 10), row("b", "discovered", 99), row("c", "rejected", 500)])
        s.commit()
    got = client.get("/sources/videos").json()
    assert [g["video_id"] for g in got] == ["b", "a"] and got[0]["minutes"] == 60
    assert [g["video_id"] for g in client.get("/sources/videos?status=rejected").json()] == ["c"]


# ------------------------------------------------------------ dashboard API
from datetime import datetime, timedelta, timezone  # noqa: E402

from autoclip.config import get_settings  # noqa: E402
from autoclip.models import Account, Campaign, Clip, Highlight, Post, SourceVideo, Submission  # noqa: E402


@pytest.fixture
def seeded(session_factory):
    with session_factory() as s:
        camp = Campaign(marketplace="cr", external_id="c", name="Camp", url="https://cr/c", status="open")
        s.add(camp)
        s.flush()
        v = SourceVideo(platform="twitch", video_id="v", channel_id="k", channel_title="K", title="T", duration_s=600,
                        license="", views=1, status="scheduled", rights_basis="campaign", campaign_id=camp.id)
        s.add(v)
        s.flush()
        h = Highlight(source_video_id=v.id, start_s=0, end_s=30, title="Moment", reason="r", score=70, llm="m",
                      status="rendered", hook_headline="Wow", scores={"virality": 70})
        s.add(h)
        s.flush()
        c = Clip(highlight_id=h.id, campaign_id=camp.id, path="/nowhere/clip.mp4", layout="gameplay",
                 style_preset="bold_pop", duration_s=30, status="scheduled")
        a = Account(platform="instagram", handle="@x", niche="irl_streamer", style_preset="bold_pop", active=True,
                    daily_cap=3, auto_approve_score=60)
        s.add_all([c, a])
        s.flush()
        p = Post(clip_id=c.id, account_id=a.id, platform="instagram", status="held", status_reason="below score",
                 scheduled_at=datetime.now(timezone.utc) - timedelta(hours=1), url="https://ig/p")
        s.add(p)
        s.flush()
        s.add(Submission(post_id=p.id, campaign_id=camp.id, deadline=datetime.now(timezone.utc), status="pending"))
        s.commit()
        return {"campaign": camp.id, "account": a.id, "post": p.id}


def test_read_endpoints(client, seeded):
    o = client.get("/api/overview").json()
    assert o["held"] == 1 and o["pending_submissions"] == 1 and o["kill_switch"] is False
    for path in ("/api/campaigns", "/api/videos", "/api/clips", "/api/accounts", "/api/posts", "/api/submissions",
                 "/api/analytics", "/api/bandit", "/api/prompts", "/api/money", "/api/decisions", "/api/sources"):
        assert client.get(path).status_code == 200, path



def test_clips_page_shows_only_scheduled_clips_soonest_first(client, seeded, session_factory):
    assert client.get("/api/clips").json() == []  # the seeded clip's only post is held: not shown
    with session_factory() as s:
        h = s.scalars(select(Highlight)).first()
        acc = s.scalars(select(Account)).first()
        soon = datetime.now(timezone.utc) + timedelta(hours=2)
        clips = []
        for title, when in (("Later", soon + timedelta(days=1)), ("Sooner", soon)):
            c = Clip(highlight_id=h.id, path=f"/nowhere/{title}.mp4", layout="g", style_preset="story_pop",
                     duration_s=24, status="scheduled")
            s.add(c)
            s.flush()
            s.add(Post(clip_id=c.id, account_id=acc.id, platform="instagram", status="scheduled", scheduled_at=when))
            clips.append(c.id)
        s.add(Post(clip_id=clips[1], account_id=acc.id, platform="youtube", status="scheduled",
                   scheduled_at=soon + timedelta(hours=1)))
        removed = Clip(highlight_id=h.id, path="/nowhere/x.mp4", layout="g", style_preset="s", duration_s=20,
                       status="replaced")
        s.add(removed)
        s.flush()
        s.add(Post(clip_id=removed.id, account_id=acc.id, platform="instagram", status="removed", scheduled_at=soon))
        s.commit()
    data = client.get("/api/clips").json()
    assert [c["clip_id"] for c in data] == [clips[1], clips[0]]  # soonest first; removed and held never shown
    assert [p["platform"] for p in data[0]["posts"]] == ["instagram", "youtube"]
    assert all(c["status"] == "scheduled" for c in data)
    assert "autoclip status" in client.get("/status").text


def test_changes_need_the_admin_password(client, seeded, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_password", "")
    assert client.post("/api/kill-switch", json={"on": True}).status_code == 403
    monkeypatch.setattr(get_settings(), "admin_password", "s3cret")
    assert client.post("/api/kill-switch", json={"on": True}).status_code == 401
    assert client.post("/api/kill-switch", json={"on": True}, headers={"X-Admin-Password": "nope"}).status_code == 401
    hdr = {"X-Admin-Password": "s3cret"}
    assert client.post("/api/kill-switch", json={"on": True}, headers=hdr).json() == {"kill_switch": True}
    assert client.get("/api/overview").json()["kill_switch"] is True

    assert client.post(f"/api/posts/{seeded['post']}/approve", headers=hdr).status_code == 200
    assert client.post(f"/api/posts/{seeded['post']}/approve", headers=hdr).status_code == 409
    assert client.patch(f"/api/accounts/{seeded['account']}", json={"active": False, "auto_approve_score": 150},
                        headers=hdr).status_code == 200
    acc = client.get("/api/accounts").json()[0]
    assert acc["active"] is False and acc["auto_approve_score"] == 100
    assert client.patch(f"/api/campaigns/{seeded['campaign']}", json={"status": "bogus"}, headers=hdr).status_code == 422
    assert client.patch(f"/api/campaigns/{seeded['campaign']}", json={"status": "joined"}, headers=hdr).status_code == 200
    assert client.post("/api/submissions/1", json={"status": "submitted"}, headers=hdr).status_code == 200
    assert client.get("/api/submissions").json()[0]["status"] == "submitted"
    assert client.post("/api/submissions/999", json={"status": "submitted"}, headers=hdr).status_code == 404


def test_live_endpoint_without_redis(client, seeded, monkeypatch):
    import autoclip.main as m

    class Down:
        def exists(self, k):
            raise ConnectionError("no redis")

    monkeypatch.setattr(m, "_redis_client", lambda: Down())
    d = client.get("/api/live").json()
    assert d["processing"] is False and d["current"] is None
    assert "queue" in d and "recent_clips" in d and "upcoming_posts" in d and "errors" in d
