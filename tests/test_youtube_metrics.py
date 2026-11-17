import httpx
import pytest

from autoclip.models import Post
from autoclip.services.analytics import MetricsError, YouTubeAnalytics


def client(data, report=None, report_status=200):
    def handle(req: httpx.Request):
        if "youtube/v3/videos" in str(req.url):
            assert req.url.params["id"] == "vid1" and req.headers["Authorization"] == "Bearer tok"
            return httpx.Response(200, json=data)
        return httpx.Response(report_status, json=report or {})
    return httpx.Client(transport=httpx.MockTransport(handle))


def post():
    return Post(clip_id=1, account_id=1, platform="youtube", external_id="vid1", status="posted")


def test_views_come_from_the_live_counters_while_the_report_is_still_empty():
    yt = YouTubeAnalytics(lambda: "tok", client(
        {"items": [{"statistics": {"viewCount": "141", "likeCount": "3", "commentCount": "1"}}]},
        {"rows": []}))  # the Analytics report lags a day or two: no rows yet
    m = yt.fetch(post(), 30)
    assert (m["views"], m["likes"], m["comments"], m["avg_view_pct"]) == (141, 3, 1, None)


def test_report_adds_shares_and_watch_percentage_but_not_views():
    yt = YouTubeAnalytics(lambda: "tok", client(
        {"items": [{"statistics": {"viewCount": "500"}}]}, {"rows": [[7, 64.5]]}))
    m = yt.fetch(post(), 30)
    assert (m["views"], m["shares"], m["avg_view_pct"]) == (500, 7, 64.5)


def test_a_failing_report_keeps_the_live_counts():
    yt = YouTubeAnalytics(lambda: "tok", client({"items": [{"statistics": {"viewCount": "9"}}]}, report_status=403))
    assert yt.fetch(post(), 30)["views"] == 9


def test_missing_video_is_an_error_not_zero_views():
    with pytest.raises(MetricsError):
        YouTubeAnalytics(lambda: "tok", client({"items": []})).fetch(post(), 30)
