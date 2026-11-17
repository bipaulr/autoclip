import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from autoclip.integrations.telegram import MockNotifier
from autoclip.models import Account, Campaign, Clip, DecisionLog, Highlight, Post, SourceVideo, Submission
from autoclip.publishers import MockPublisher, PublishError, PublishRequest, PublishResult, QuotaFull
from autoclip.publishers.instagram import InstagramPublisher
from autoclip.publishers.youtube import YouTubePublisher
from autoclip.services.publishing import PublishSettings, publish_due
from autoclip.services.submissions import escalate, set_status

NOW = datetime(2026, 11, 19, 20, tzinfo=timezone.utc)


@pytest.fixture
def clipfile(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"0" * 1000)
    return f


def req(path, privacy="public"):
    return PublishRequest(path, "Title", "Desc", ["#a", "#b"], privacy)


# ------------------------------------------------------------ YouTube
def yt_handler(returned_privacy="public", fail_put=0):
    state = {"puts": 0}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "AT"})
        assert request.headers["Authorization"] == "Bearer AT"
        if request.method == "POST":
            p = request.url.params
            assert (p["uploadType"], p["notifySubscribers"]) == ("resumable", "false")
            meta = json.loads(request.content)
            assert meta["status"]["containsSyntheticMedia"] is False and meta["snippet"]["tags"] == ["a", "b"]
            assert request.headers["X-Upload-Content-Length"] == "1000"
            return httpx.Response(200, headers={"location": "https://upload.example/session"})
        state["puts"] += 1
        if state["puts"] <= fail_put:
            return httpx.Response(503, json={"error": {"errors": [{"reason": "backendError"}]}})
        return httpx.Response(200, json={"id": "vid123", "status": {"privacyStatus": returned_privacy}})

    return handler, state


def test_youtube_resumable_upload(clipfile):
    h, _ = yt_handler()
    r = YouTubePublisher("c", "s", "r", httpx.Client(transport=httpx.MockTransport(h))).publish(req(clipfile))
    assert (r.external_id, r.status, r.url) == ("vid123", "posted", "https://www.youtube.com/shorts/vid123")


def test_youtube_detects_locked_private(clipfile):
    h, _ = yt_handler(returned_privacy="private")
    r = YouTubePublisher("c", "s", "r", httpx.Client(transport=httpx.MockTransport(h))).publish(req(clipfile))
    assert r.status == "locked_private"
    h2, _ = yt_handler(returned_privacy="private")
    asked_private = YouTubePublisher("c", "s", "r", httpx.Client(transport=httpx.MockTransport(h2))).publish(
        req(clipfile, privacy="private"))
    assert asked_private.status == "posted"


def test_youtube_retries_server_errors_with_backoff(clipfile):
    h, state = yt_handler(fail_put=2)
    sleeps = []
    r = YouTubePublisher("c", "s", "r", httpx.Client(transport=httpx.MockTransport(h)), sleep=sleeps.append
                         ).publish(req(clipfile))
    assert r.status == "posted" and state["puts"] == 3 and sleeps == [10, 20]


def test_youtube_token_errors_do_not_leak(clipfile):
    h = lambda r: httpx.Response(400, json={"error": "invalid_grant"})  # noqa: E731
    with pytest.raises(PublishError) as e:
        YouTubePublisher("c", "SECRET", "REFRESH", httpx.Client(transport=httpx.MockTransport(h))).publish(req(clipfile))
    assert "invalid_grant" in str(e.value) and "SECRET" not in str(e.value) and "REFRESH" not in str(e.value)


# ------------------------------------------------------------ Instagram
def ig_handler(quota_used=0, statuses=("IN_PROGRESS", "FINISHED")):
    seen = []
    st = iter(statuses)

    def handler(request):
        seen.append((request.method, request.url.host, request.url.path))
        if request.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json={"data": [{"quota_usage": quota_used, "config": {"quota_total": 50}}]})
        if request.url.host == "rupload.facebook.com":
            assert request.headers["Authorization"] == "OAuth TOKEN"
            assert (request.headers["offset"], request.headers["file_size"]) == ("0", "1000")
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and request.url.path.endswith("/media"):
            body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert (body["media_type"], body["upload_type"]) == ("REELS", "resumable")
            return httpx.Response(200, json={"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/C1"})
        if request.url.path.endswith("/C1"):
            return httpx.Response(200, json={"status_code": next(st)})
        if request.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "M1"})
        return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc/"})

    return handler, seen


def test_instagram_resumable_reel_publish(clipfile):
    h, seen = ig_handler()
    r = InstagramPublisher("IGU", "TOKEN", client=httpx.Client(transport=httpx.MockTransport(h)),
                           sleep=lambda s: None).publish(req(clipfile))
    assert (r.external_id, r.url, r.status) == ("M1", "https://www.instagram.com/reel/abc/", "posted")
    hosts = [x[1] for x in seen]
    assert "rupload.facebook.com" in hosts and seen[0][2].endswith("content_publishing_limit")


def test_instagram_quota_and_processing_errors(clipfile):
    h, _ = ig_handler(quota_used=50)
    with pytest.raises(QuotaFull):
        InstagramPublisher("IGU", "TOKEN", client=httpx.Client(transport=httpx.MockTransport(h))).publish(req(clipfile))
    h, _ = ig_handler(statuses=("ERROR",))
    with pytest.raises(PublishError, match="ERROR"):
        InstagramPublisher("IGU", "TOKEN", client=httpx.Client(transport=httpx.MockTransport(h)),
                           sleep=lambda s: None).publish(req(clipfile))


# ------------------------------------------------------------ publishing service
@pytest.fixture
def world(session, clipfile):
    camp = Campaign(marketplace="content_rewards", external_id="k", name="Kai Pool", url="https://cr/kai",
                    status="joined", brief={"platforms": ["instagram", "youtube"], "required_hashtags": ["#KaiCenat"],
                                            "submit_within_minutes": 30})
    session.add(camp)
    session.flush()
    v = SourceVideo(platform="twitch", video_id="v1", channel_id="kaicenat", channel_title="Kai", title="t",
                    duration_s=3600, license="", views=1, status="highlighted", rights_basis="campaign: Kai",
                    campaign_id=camp.id)
    session.add(v)
    session.flush()
    h = Highlight(source_video_id=v.id, start_s=0, end_s=30, title="t", reason="r", score=80, llm="m",
                  status="rendered")
    session.add(h)
    session.flush()
    pkg = {p: {"title": "Wild", "description": "Kai", "hashtags": ["#KaiCenat"]} for p in ("instagram", "youtube")}
    c = Clip(highlight_id=h.id, campaign_id=camp.id, path=str(clipfile), layout="gameplay", style_preset="bold_pop",
             duration_s=30, package=pkg, status="scheduled")
    ig = Account(platform="instagram", handle="@irl", niche="irl_streamer", style_preset="bold_pop")
    yt = Account(platform="youtube", handle="IRL clips", niche="irl_streamer", style_preset="bold_pop")
    session.add_all([c, ig, yt])
    session.flush()
    posts = [Post(clip_id=c.id, account_id=a.id, platform=a.platform, scheduled_at=NOW - timedelta(minutes=1),
                  status="scheduled") for a in (ig, yt)]
    session.add_all(posts)
    session.flush()
    return {"campaign": camp, "clip": c, "ig": ig, "yt": yt, "posts": posts}


def test_publish_due_posts_instagram_holds_youtube_and_queues_submission(session, world):
    note = MockNotifier()
    pubs = {"instagram": MockPublisher("instagram"), "youtube": MockPublisher("youtube")}
    out = publish_due(session, pubs, note, PublishSettings(), now=NOW)
    ig_post, yt_post = world["posts"]
    assert ig_post.status == "posted" and ig_post.url and yt_post.status == "held"
    assert "audit" in yt_post.status_reason and pubs["youtube"].requests == []
    sub = session.scalars(select(Submission)).one()
    assert sub.status == "pending" and sub.deadline.replace(tzinfo=timezone.utc) == NOW + timedelta(minutes=30)
    assert any("SUBMIT NOW" in m and "https://cr/kai" in m for m in note.sent)
    assert len(out) == 2


def test_kill_switch_stops_everything(session, world):
    pubs = {"instagram": MockPublisher("instagram")}
    assert publish_due(session, pubs, MockNotifier(), PublishSettings(kill_switch=True), now=NOW) == ["kill switch on"]
    assert pubs["instagram"].requests == []


def test_gate_runs_again_before_publish(session, world):
    world["clip"].package["instagram"]["hashtags"] = []
    world["clip"].package = dict(world["clip"].package)
    world["clip"].package["instagram"] = {"title": "Wild", "description": "", "hashtags": []}
    pubs = {"instagram": MockPublisher("instagram")}
    publish_due(session, pubs, MockNotifier(), PublishSettings(youtube_audit_passed=True), now=NOW)
    assert world["posts"][0].status == "blocked" and "#KaiCenat" in world["posts"][0].status_reason
    assert pubs["instagram"].requests == []


def test_paused_account_and_ended_campaign_are_held_or_blocked(session, world):
    world["ig"].active, world["ig"].paused_reason = False, "platform warning"
    world["campaign"].status = "ended"
    publish_due(session, {"instagram": MockPublisher("instagram")}, MockNotifier(), PublishSettings(), now=NOW)
    assert world["posts"][0].status == "held" and "platform warning" in world["posts"][0].status_reason
    assert world["posts"][1].status == "blocked"


def test_locked_private_youtube_is_held_and_alerted(session, world):
    note = MockNotifier()
    yt = MockPublisher("youtube", PublishResult("v", "u", "locked_private", "private"))
    world["posts"][0].status = "posted"
    publish_due(session, {"youtube": yt}, note, PublishSettings(youtube_audit_passed=True), now=NOW)
    assert world["posts"][1].status == "held" and any("audit" in m for m in note.sent)


def test_retryable_errors_reschedule_then_fail(session, world):
    note = MockNotifier()
    pub = MockPublisher("instagram", error=PublishError("HTTP 500", retryable=True))
    world["posts"][1].status = "posted"
    for i in range(3):
        publish_due(session, {"instagram": pub}, note, PublishSettings(), now=NOW + timedelta(hours=i))
    assert world["posts"][0].status == "failed" and len(pub.requests) == 3
    assert any("publishing failed" in m for m in note.sent)


def test_quota_full_defers_an_hour(session, world):
    world["posts"][1].status = "posted"
    publish_due(session, {"instagram": MockPublisher("instagram", error=QuotaFull("limit"))}, MockNotifier(),
                PublishSettings(), now=NOW)
    assert world["posts"][0].status == "scheduled" and world["posts"][0].scheduled_at == NOW + timedelta(hours=1)


def test_global_rate_limit(session, world):
    world["posts"][1].status = "posted"
    world["posts"][1].posted_at = NOW - timedelta(minutes=5)
    publish_due(session, {"instagram": MockPublisher("instagram")}, MockNotifier(),
                PublishSettings(upload_rate_per_hour=1), now=NOW)
    assert world["posts"][0].status == "scheduled"


def test_submission_escalation_and_status(session, world):
    note = MockNotifier()
    world["posts"][1].status = "posted"
    publish_due(session, {"instagram": MockPublisher("instagram")}, note, PublishSettings(), now=NOW)
    assert escalate(session, note, now=NOW + timedelta(minutes=10)) == 0
    assert escalate(session, note, now=NOW + timedelta(minutes=21)) == 1
    assert escalate(session, note, now=NOW + timedelta(minutes=40)) == 0
    assert any("REMINDER" in m for m in note.sent)
    sub = session.scalars(select(Submission)).one()
    assert set_status(session, sub.id, "submitted").status == "submitted"
    with pytest.raises(ValueError):
        set_status(session, sub.id, "nonsense")
    actions = [d.action for d in session.scalars(select(DecisionLog))]
    assert "posted" in actions and "queued" in actions and "escalated" in actions


def _ig_handler(uploads, fail_first):
    def handler(request):
        if request.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json={"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]})
        if request.url.host == "rupload.facebook.com":
            uploads.append(int(request.headers["file_size"]))
            if len(uploads) <= fail_first:
                return httpx.Response(400, json={"debug_info": {"retriable": False, "type": "ProcessingFailedError"}})
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": f"C{len(uploads)}", "uri": "https://rupload.facebook.com/x"})
        if request.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "M1"})
        if "/C" in request.url.path:
            return httpx.Response(200, json={"status_code": "FINISHED"})
        return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/ok/"})
    return handler


def _reencode(src, dst):
    dst.write_bytes(b"1" * 777)
    return dst


def test_instagram_retries_flaky_processing_failures_on_the_same_file(clipfile):
    uploads, sleeps = [], []
    r = InstagramPublisher("IGU", "TOKEN", client=httpx.Client(transport=httpx.MockTransport(_ig_handler(uploads, 2))),
                           sleep=sleeps.append, reencode=_reencode).publish(req(clipfile))
    assert r.status == "posted" and uploads == [1000, 1000, 1000] and sleeps[:2] == [10, 20]


def test_instagram_trims_clips_over_its_length_limit(clipfile):
    from dataclasses import replace
    uploads, trims = [], []

    def trim(src, dst, seconds):
        trims.append(seconds)
        dst.write_bytes(b"2" * 500)
        return dst

    pub = InstagramPublisher("IGU", "TOKEN", client=httpx.Client(transport=httpx.MockTransport(_ig_handler(uploads, 0))),
                             sleep=lambda s: None, max_seconds=30, trim=trim)
    assert pub.publish(replace(req(clipfile), duration_s=58.1)).status == "posted"
    assert trims == [30] and uploads == [500]
    uploads.clear()
    assert pub.publish(replace(req(clipfile), duration_s=28.5)).status == "posted"
    assert trims == [30] and uploads == [1000]  # short clips go up untouched


def test_trim_clip_keeps_the_first_seconds():
    from pathlib import Path
    from autoclip.pipeline.render import trim_clip
    calls = []
    trim_clip(Path("a.mp4"), Path("b.mp4"), 30, run=calls.append)
    cmd = calls[0]
    assert cmd[cmd.index("-t") + 1] == "30.000" and cmd[-1] == "b.mp4" and "+faststart" in cmd


def test_instagram_reencodes_after_retries_run_out(clipfile):
    uploads = []
    r = InstagramPublisher("IGU", "TOKEN", client=httpx.Client(transport=httpx.MockTransport(_ig_handler(uploads, 3))),
                           sleep=lambda s: None, reencode=_reencode, upload_attempts=3).publish(req(clipfile))
    assert r.status == "posted" and uploads == [1000, 1000, 1000, 777]
    uploads.clear()
    no_reencode = InstagramPublisher("IGU", "TOKEN", client=httpx.Client(transport=httpx.MockTransport(_ig_handler(uploads, 99))),
                                     sleep=lambda s: None, upload_attempts=2)
    with pytest.raises(PublishError, match="ProcessingFailedError"):
        no_reencode.publish(req(clipfile))
    assert uploads == [1000, 1000]
