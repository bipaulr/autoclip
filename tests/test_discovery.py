from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from autoclip.integrations.feeds import (FeedError, MockTwitch, MockYouTubeFeed, TwitchHelix, YouTubeRSS,
                                         parse_twitch_duration)
from autoclip.integrations.youtube import MockYouTube
from autoclip.models import Campaign, ChannelState, DecisionLog, SourceVideo
from autoclip.rights import Video
from autoclip.services.campaigns import import_campaigns
from autoclip.services.discovery import discover, rank_score, stream_ended
from autoclip.sources import parse_sources

NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


def vid(video_id, license="creativeCommon", minutes=40, views=5000, channel="UCx", platform="youtube",
        hours_old=24, language="en"):
    return Video(platform, video_id, channel, "Chan", f"Title {video_id}", minutes * 60, license, views,
                 NOW - timedelta(hours=hours_old), language)


def rows(session):
    return {r.video_id: r for r in session.scalars(select(SourceVideo))}


def reasons(session):
    return {x.subject: x.reason for x in session.scalars(select(DecisionLog)) if x.subject}


# ------------------------------------------------------------ clients
def test_youtube_rss_parses_video_ids():
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
           '<entry><yt:videoId>aaa</yt:videoId></entry><entry><yt:videoId>bbb</yt:videoId></entry></feed>')

    def handler(request):
        assert request.url.params["channel_id"] == "UCabc"
        return httpx.Response(200, text=xml)

    assert YouTubeRSS(httpx.Client(transport=httpx.MockTransport(handler))).latest("UCabc") == ["aaa", "bbb"]
    bad = YouTubeRSS(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    with pytest.raises(FeedError):
        bad.latest("UCabc")


def test_twitch_helix_vods_and_live():
    def handler(request):
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        assert request.headers["Client-Id"] == "CID" and request.headers["Authorization"] == "Bearer T"
        if request.url.path.endswith("/users"):
            return httpx.Response(200, json={"data": [{"id": "42"}]})
        if request.url.path.endswith("/videos"):
            assert request.url.params["user_id"] == "42" and request.url.params["type"] == "archive"
            return httpx.Response(200, json={"data": [{"id": "v1", "title": "GTA 6 day one", "duration": "3h2m1s",
                                                       "view_count": 1000, "user_name": "Kai",
                                                       "created_at": "2026-11-12T20:00:00Z", "language": "en"}]})
        return httpx.Response(200, json={"data": [{"user_login": "kaicenat", "type": "live"}]})

    t = TwitchHelix("CID", "SECRET", httpx.Client(transport=httpx.MockTransport(handler)))
    v = t.vods("kaicenat")[0]
    assert (v.platform, v.video_id, v.duration_s, v.channel_id, v.url) == (
        "twitch", "v1", 10921, "kaicenat", "https://www.twitch.tv/videos/v1")
    assert t.live_logins(["kaicenat", "lacy"]) == {"kaicenat"}
    assert parse_twitch_duration("45m") == 2700 and parse_twitch_duration("") == 0


def test_twitch_revoked_token_is_replaced_and_the_request_retried_once():
    tokens = iter(["OLD", "NEW"])
    seen = []

    def handler(request):
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer OLD":
            return httpx.Response(401, json={"message": "Invalid OAuth token"})
        return httpx.Response(200, json={"data": [{"user_login": "lacy", "type": "live"}]})

    t = TwitchHelix("CID", "SECRET", httpx.Client(transport=httpx.MockTransport(handler)))
    assert t.live_logins(["lacy"]) == {"lacy"} and seen == ["Bearer OLD", "Bearer NEW"]


def test_twitch_401_twice_is_an_error_not_a_loop():
    def handler(request):
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(401, json={"message": "Invalid OAuth token"})

    with pytest.raises(FeedError, match="401"):
        TwitchHelix("CID", "S", httpx.Client(transport=httpx.MockTransport(handler))).live_logins(["x"])


def test_rank_score_prefers_fresh_fast_and_rewarded():
    fresh = rank_score(vid("a", views=100000, hours_old=5), NOW)
    old = rank_score(vid("b", views=100000, hours_old=24 * 9), NOW)
    assert fresh > old
    assert rank_score(vid("a", hours_old=5), NOW, reward=2.0) > rank_score(vid("a", hours_old=5), NOW)


# ------------------------------------------------------------ discovery runs
def test_allowlisted_youtube_channel_uses_rss_not_search(session):
    sources = parse_sources({"channels": [{"platform": "youtube", "channel_id": "UCok", "name": "OK",
                                           "basis": "email 1 Nov", "profile": "podcast_business"}]})
    rss = MockYouTubeFeed({"UCok": ["a", "b"]})
    yt = MockYouTube(videos=[vid("a", license="youtube", channel="UCok"),
                             vid("b", license="youtube", channel="UCok", minutes=5)])
    s = discover(sources, session, youtube=yt, rss=rss, now=NOW)
    assert (s.discovered, s.rejected) == (1, 1) and yt.searched == [] and s.quota_units == 1
    r = rows(session)
    assert r["a"].rights_basis == "permission: email 1 Nov" and r["a"].profile == "podcast_business"
    assert r["a"].rank_score > 0 and r["b"].rank_score is None
    assert "too short" in reasons(session)["youtube:b"]


def test_twitch_vods_from_a_joined_campaign_in_the_database(session):
    session.add(Campaign(marketplace="content_rewards", external_id="kai", name="Kai Cenat Pool",
                         url="https://contentrewards.com/c/kai", status="joined", niche="irl_streamer",
                         source_channels=[{"platform": "twitch", "channel_id": "kaicenat"}],
                         starts=date(2026, 11, 1), ends=date(2026, 11, 30)))
    session.flush()
    tw = MockTwitch({"kaicenat": [vid("v9", license="", channel="kaicenat", platform="twitch")]})
    s = discover(parse_sources({}), session, twitch=tw, now=NOW)
    r = rows(session)["v9"]
    assert s.discovered == 1 and r.campaign_id is not None and r.profile == "irl_streamer"
    assert "Kai Cenat Pool on content_rewards" in r.rights_basis


def test_vods_go_to_the_campaign_named_after_the_streamer_not_a_brand_campaign(session):
    # the brand campaign is added first: a first-match pick sent Lacy's VODs (and payouts) to it
    session.add(Campaign(marketplace="cr", external_id="brand", name="$5,000 TapCap Clipping Campaign", url="u1",
                         status="joined", source_channels=[{"platform": "twitch", "channel_id": "clix"},
                                                           {"platform": "twitch", "channel_id": "lacy"}]))
    session.add(Campaign(marketplace="cr", external_id="pool", name="$30,000 LACY 30 DAY CAMPAIGN (Reach)",
                         url="u2", status="joined", source_channels=[{"platform": "twitch", "channel_id": "lacy"}]))
    session.flush()
    pool = session.scalars(select(Campaign).where(Campaign.external_id == "pool")).one()
    brand = session.scalars(select(Campaign).where(Campaign.external_id == "brand")).one()
    tw = MockTwitch({"lacy": [vid("l1", license="", channel="lacy", platform="twitch")],
                     "clix": [vid("c1", license="", channel="clix", platform="twitch")]})
    discover(parse_sources({}), session, twitch=tw, now=NOW)
    r = rows(session)
    assert r["l1"].campaign_id == pool.id and r["c1"].campaign_id == brand.id
    assert "LACY" in r["l1"].rights_basis


def test_open_but_not_joined_campaign_is_not_a_source(session):
    session.add(Campaign(marketplace="content_rewards", external_id="x", name="X", url="u", status="open",
                         source_channels=[{"platform": "twitch", "channel_id": "lacy"}]))
    session.flush()
    tw = MockTwitch({"lacy": [vid("v1", license="", channel="lacy", platform="twitch")]})
    assert discover(parse_sources({}), session, twitch=tw, now=NOW).discovered == 0


def test_creative_commons_lane_and_language_filter(session):
    cc = {"enabled": True, "queries": ["lecture"], "max_searches_per_run": 1, "min_views": 1000, "languages": ["en"]}
    yt = MockYouTube(videos=[vid("good"), vid("fr", language="fr"), vid("quiet", views=10), vid("std", license="youtube")],
                     search={"lecture": ["good", "fr", "quiet", "std"]})
    s = discover(parse_sources({"creative_commons": cc}), session, youtube=yt, now=NOW)
    assert (s.discovered, s.rejected) == (1, 3) and yt.search_languages == ["en"]
    rs = reasons(session)
    assert "language 'fr'" in rs["youtube:fr"] and "too few views" in rs["youtube:quiet"]
    assert "not Creative Commons" in rs["youtube:std"]
    assert "Title good" in rows(session)["good"].attribution


def test_cc_disabled_means_no_search(session):
    yt = MockYouTube(videos=[vid("a")], search={"lecture": ["a"]})
    discover(parse_sources({"creative_commons": {"enabled": False, "queries": ["lecture"]}}), session, youtube=yt)
    assert yt.searched == []


def test_known_videos_are_not_fetched_again(session):
    sources = parse_sources({"channels": [{"platform": "youtube", "channel_id": "UCx", "basis": "email"}]})
    discover(sources, session, youtube=MockYouTube(videos=[vid("a", license="youtube")]),
             rss=MockYouTubeFeed({"UCx": ["a"]}), now=NOW)
    yt = MockYouTube(videos=[vid("a", license="youtube"), vid("b", license="youtube")])
    s = discover(sources, session, youtube=yt, rss=MockYouTubeFeed({"UCx": ["a", "b"]}), now=NOW)
    assert yt.fetched == ["b"] and s.already_known == 1 and s.discovered == 1


def test_feed_failure_pauses_that_source_only(session):
    class Broken:
        def vods(self, login, first=10):
            raise FeedError("blocked by Cloudflare")

    sources = parse_sources({"channels": [{"platform": "kick", "channel_id": "n3on", "basis": "discord program"},
                                          {"platform": "twitch", "channel_id": "lacy", "basis": "email"}]})
    tw = MockTwitch({"lacy": [vid("t1", license="", channel="lacy", platform="twitch")]})
    s = discover(sources, session, twitch=tw, kick=Broken(), now=NOW)
    assert s.discovered == 1 and "Cloudflare" in s.paused[0]


def test_stream_ended_trigger(session):
    sources = parse_sources({"channels": [{"platform": "twitch", "channel_id": "kaicenat", "name": "Kai",
                                           "basis": "campaign"}]})
    tw = MockTwitch(live={"kaicenat"})
    assert stream_ended(sources, session, twitch=tw) == []  # first sighting: just records state
    assert stream_ended(sources, session, twitch=tw) == []  # still live
    tw.live = set()
    ended = stream_ended(sources, session, twitch=tw)
    assert [c.channel_id for c in ended] == ["kaicenat"]
    assert session.scalars(select(ChannelState)).one().live is False
    assert stream_ended(sources, session, twitch=tw) == []  # already offline


def test_campaign_import(session, tmp_path):
    f = tmp_path / "campaigns.yaml"
    f.write_text("""
campaigns:
  - marketplace: content_rewards
    id: jason-pool
    name: $9,000 Jason Pool
    url: https://contentrewards.com/campaign/jason
    niche: irl_streamer
    budget_total: 9000
    allowed_platforms: [instagram, youtube]
    source_channels: [{platform: twitch, channel_id: jasontheween}]
    joined: true
  - marketplace: vyro
    id: old
    name: Old round
    url: https://vyro.com/old
    ends: 2026-01-01
""", encoding="utf-8")
    assert import_campaigns(f, session) == 2
    c = {x.external_id: x for x in session.scalars(select(Campaign))}
    assert c["jason-pool"].status == "joined" and c["jason-pool"].budget_total == 9000
    assert c["old"].status == "ended"
    with pytest.raises(ValueError):
        bad = tmp_path / "bad.yaml"
        bad.write_text("campaigns: [{name: x}]", encoding="utf-8")
        import_campaigns(bad, session)
    assert import_campaigns(tmp_path / "missing.yaml", session) == 0


def test_rank_score_accepts_times_without_timezone():
    v = vid("k", platform="kick")
    v.published_at = v.published_at.replace(tzinfo=None)
    assert rank_score(v, NOW) > 0


def test_campaign_specific_videos_are_registered_once(session):
    session.add(Campaign(marketplace="cr", external_id="marlon", name="Marlon", url="u", status="joined",
                         niche="brand_story", creator="Marlon",
                         source_videos=[{"platform": "gdrive", "video_id": "abc", "title": "Vlog", "minutes": 44}]))
    session.flush()
    s = discover(parse_sources({}), session, now=NOW)
    v = rows(session)["abc"]
    assert s.discovered == 1 and v.platform == "gdrive" and v.duration_s == 44 * 60 and v.profile == "brand_story"
    assert "approved footage of Marlon" in v.rights_basis
    assert discover(parse_sources({}), session, now=NOW).discovered == 0


def test_topic_boost_matches_whole_words():
    from autoclip.services.discovery import topic_boost
    kw = ["gta 6", "gta vi", "grand theft auto vi"]
    assert topic_boost("FIRST DAY IN GTA 6!!", kw, 3.0) == 3.0
    assert topic_boost("Grand Theft Auto VI launch stream", kw, 3.0) == 3.0
    assert topic_boost("gta-vi heists", kw, 3.0) == 3.0
    assert topic_boost("GTA 60 fps mod", kw, 3.0) == 1.0  # not "gta 6"
    assert topic_boost("just chatting", kw, 3.0) == 1.0 and topic_boost("GTA 6", [], 3.0) == 1.0


def test_gta6_vods_rank_above_others(session):
    session.add(Campaign(marketplace="content_rewards", external_id="g", name="Pool", url="u", status="joined",
                         source_channels=[{"platform": "twitch", "channel_id": "kaicenat"}],
                         starts=date(2026, 11, 1), ends=date(2026, 11, 30)))
    session.flush()
    a, b = vid("plain", license="", channel="kaicenat", platform="twitch"), vid("gta", license="", channel="kaicenat",
                                                                                platform="twitch")
    b.title = "GTA 6 ALL NIGHT"
    discover(parse_sources({}), session, twitch=MockTwitch({"kaicenat": [a, b]}), now=NOW,
             priority_keywords=["gta 6"], priority_boost=3.0)
    r = rows(session)
    assert r["gta"].rank_score == pytest.approx(r["plain"].rank_score * 3.0)


def test_youtube_feed_falls_back_to_the_data_api_when_rss_fails():
    from autoclip.integrations.feeds import FeedError, FeedWithFallback, MockYouTubeFeed

    class DeadRSS:
        def latest(self, channel_id):
            raise FeedError(f"YouTube RSS HTTP 404 for {channel_id}")

    class Api:
        def channel_uploads(self, cid, max_results=15):
            return {"UCcore": ["v1", "v2"]}[cid]
    api = Api()
    assert FeedWithFallback(DeadRSS(), api).latest("UCcore") == ["v1", "v2"]
    assert FeedWithFallback(MockYouTubeFeed({"UCcore": ["r1"]}), api).latest("UCcore") == ["r1"]

    class Broken:
        def channel_uploads(self, cid, max_results=15):
            raise RuntimeError("quota")
    import pytest
    with pytest.raises(FeedError, match="fallback failed"):
        FeedWithFallback(DeadRSS(), Broken()).latest("UCcore")
