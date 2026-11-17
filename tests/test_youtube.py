from datetime import datetime, timezone

import httpx
import pytest

from autoclip.integrations.youtube import (YouTubeAPIError, YouTubeDataAPI, YouTubeQuotaError,
                                           parse_duration)

AFTER = datetime(2026, 10, 13, tzinfo=timezone.utc)


def api(handler):
    def wrapped(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "ACCESS"})
        assert request.headers["Authorization"] == "Bearer ACCESS"
        return handler(request)

    return YouTubeDataAPI("CID", "CSECRET", "REFRESH", client=httpx.Client(transport=httpx.MockTransport(wrapped)))


def test_parse_duration():
    assert parse_duration("PT1H2M3S") == 3723
    assert parse_duration("PT45M") == 2700
    assert parse_duration("P1DT1S") == 86401
    assert parse_duration("") == 0 and parse_duration("garbage") == 0


def test_search_asks_only_for_long_creative_commons_videos():
    def handler(request):
        p = request.url.params
        assert request.url.path.endswith("/search")
        assert (p["videoLicense"], p["videoDuration"], p["type"], p["q"]) == (
            "creativeCommon", "long", "video", "lecture")
        assert p["publishedAfter"] == "2026-10-13T00:00:00Z"
        return httpx.Response(200, json={"items": [{"id": {"videoId": "a"}}, {"id": {"videoId": "b"}}]})

    y = api(handler)
    assert y.search_cc("lecture", AFTER) == ["a", "b"]
    assert y.units_used == 100


def test_search_passes_language_hint_and_videos_report_language():
    seen = {}

    def handler(request):
        if request.url.path.endswith("/search"):
            seen["lang"] = request.url.params.get("relevanceLanguage")
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [
            {"id": "a", "snippet": {"defaultAudioLanguage": "en-US", "defaultLanguage": "fr"}},
            {"id": "b", "snippet": {"defaultLanguage": "hi"}}, {"id": "c", "snippet": {}}]})

    y = api(handler)
    y.search_cc("lecture", AFTER, language="en")
    assert seen["lang"] == "en"
    assert [v.language for v in y.videos(["a", "b", "c"])] == ["en-US", "hi", ""]


def test_channel_uploads_uses_uploads_playlist():
    def handler(request):
        assert request.url.params["playlistId"] == "UUabc"
        return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": "v1"}}]})

    y = api(handler)
    assert y.channel_uploads("UCabc") == ["v1"] and y.units_used == 1


def test_videos_parses_licence_duration_views_and_batches():
    calls = []

    def handler(request):
        ids = request.url.params["id"].split(",")
        calls.append(len(ids))
        return httpx.Response(200, json={"items": [{
            "id": i, "snippet": {"channelId": "UCx", "channelTitle": "Chan", "title": "T",
                                 "publishedAt": "2026-11-01T10:00:00Z"},
            "contentDetails": {"duration": "PT30M"}, "status": {"license": "creativeCommon"},
            "statistics": {"viewCount": "1234"}} for i in ids]})

    y = api(handler)
    vids = y.videos([f"v{i}" for i in range(60)])
    assert calls == [50, 10] and len(vids) == 60 and y.units_used == 2
    v = vids[0]
    assert (v.duration_s, v.license, v.views, v.channel_id) == (1800, "creativeCommon", 1234, "UCx")
    assert v.published_at == datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
    assert y.videos([]) == []


def test_quota_error_is_distinguished():
    def handler(request):
        return httpx.Response(403, json={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})

    with pytest.raises(YouTubeQuotaError):
        api(handler).search_cc("x", AFTER)


def test_other_errors_and_no_credential_leak():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    with pytest.raises(YouTubeAPIError, match="bad request"):
        api(handler).videos(["a"])

    def token_fail(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    y = YouTubeDataAPI("CID", "CSECRET", "REFRESH", client=httpx.Client(transport=httpx.MockTransport(token_fail)))
    with pytest.raises(YouTubeAPIError) as e:
        y.videos(["a"])
    assert "invalid_grant" in str(e.value) and "REFRESH" not in str(e.value) and "CSECRET" not in str(e.value)
