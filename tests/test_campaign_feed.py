import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from autoclip.integrations.marketplace import (ContentRewardsPublic, Listing, MarketplaceError, MockMarketplace,
                                               channels_in, parse_discover)
from autoclip.integrations.telegram import MockNotifier
from autoclip.models import Campaign, DecisionLog
from autoclip.services import campaign_feed
from autoclip.services.campaign_feed import handle_command, judge, poll_commands, scan, short_code

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
GTA = ["gta 6", "gta vi"]


def raw(id_, title, platforms=("instagram", "youtube"), category="Streamer", desc="", rate=1.0, left=5000.0, **kw):
    return {"id": id_, "title": title, "brand": "Reach", "category": category, "description": desc,
            "platforms": list(platforms), "payoutSortRaw": rate, "type": "cpm", "budgetTotalRaw": 10000,
            "availableBudgetRaw": left, "requiresApplication": False, **kw}


def page(campaigns) -> str:
    payload = json.dumps({"queries": [{"state": {"data": {"pages": [{"campaigns": campaigns}]}}}]}, separators=(",", ":"))
    half = len(payload) // 2  # the real page splits its data across several script tags
    return "".join(f'<script>self.__next_f.push([1,{json.dumps(part)}])</script>'
                   for part in (payload[:half], payload[half:]))


def listing(id_="11111111-2222-3333-4444-555555555555", title="Kai GTA 6 Clipping", **kw) -> Listing:
    r = raw(id_, title, **kw)
    return parse_discover(page([r]))[0]


def test_parse_discover_reads_campaigns_and_channels():
    items = parse_discover(page([
        raw("aaaaaaaa-0000-0000-0000-000000000001", "$$9,000 Jason Pool Campaign",
            desc="Clip from https://www.twitch.tv/jasontheween and https://kick.com/jason"),
        raw("aaaaaaaa-0000-0000-0000-000000000002", "Brand", platforms=("tiktok",)),
    ]))
    a = {i.title: i for i in items}["$9,000 Jason Pool Campaign"]
    assert a.rate_per_1k == 1.0 and a.budget_left == 5000.0 and a.url.endswith("000000000001")
    assert a.channels == [{"platform": "twitch", "channel_id": "jasontheween"}, {"platform": "kick", "channel_id": "jason"}]
    with pytest.raises(MarketplaceError):
        parse_discover('<script>self.__next_f.push([1,"bad \\x"])</script>')


def test_channels_ignore_non_channel_paths():
    assert channels_in("https://www.twitch.tv/videos/123 https://youtube.com/@TJRTrades") == [
        {"platform": "youtube_handle", "channel_id": "TJRTrades"}]


def test_judge_filters_and_ranks():
    assert not judge(listing(platforms=("tiktok", "x")), GTA).ok
    assert not judge(listing(title="Stake casino slots stream"), GTA).ok
    assert not judge(listing(title="Song promo", category="Music"), GTA).ok
    assert not judge(listing(title="UGC for a brand"), GTA).ok
    assert not judge(listing(title="Tiny", left=100), GTA).ok
    gta = judge(listing(desc="https://twitch.tv/kai"), GTA)
    plain = judge(listing(title="Podcast clips", category="Podcast"), GTA)
    assert gta.ok and plain.ok and gta.score > plain.score + 100 and "GTA 6" in gta.reason


def test_scan_lists_new_relevant_campaigns_once_and_skips_known_ones(session):
    session.add(Campaign(marketplace="content_rewards", external_id="reach-jason", name="$9,000 Jason Pool Campaign (Reach)",
                         url="https://contentrewards.com", status="joined"))
    session.flush()
    market = MockMarketplace([
        listing("aaaaaaaa-0000-0000-0000-000000000001", "GTA 6 Kai Clipping", desc="https://twitch.tv/kaicenat"),
        listing("aaaaaaaa-0000-0000-0000-000000000002", "$9,000 Jason Pool Campaign"),  # already in campaigns.yaml
        listing("aaaaaaaa-0000-0000-0000-000000000003", "TikTok only", platforms=("tiktok",)),
    ])
    note = MockNotifier()
    assert scan(session, market, note, NOW, priority_keywords=GTA) == "3 listings, 1 new worth joining, 0 need footage by hand"
    c = session.scalars(select(Campaign).where(Campaign.external_id == "aaaaaaaa-0000-0000-0000-000000000001")).one()
    assert c.status == "open" and c.source_channels == [{"platform": "twitch", "channel_id": "kaicenat"}]
    assert c.niche == "irl_streamer" and "[aaaaaa]" in note.sent[0] and "twitch/kaicenat" in note.sent[0]
    assert scan(session, market, note, NOW, priority_keywords=GTA) == "3 listings, 0 new worth joining, 0 need footage by hand"
    assert len(note.sent) == 1
    actions = [d.action for d in session.scalars(select(DecisionLog))]
    assert actions.count("listed") == 1 and actions.count("skipped") == 1


def test_scan_pauses_quietly_when_the_page_fails(session):
    class Broken:
        name = "content_rewards"

        def listings(self, seen=None):
            raise MarketplaceError("HTTP 503")
    note = MockNotifier()
    assert scan(session, Broken(), note, NOW).startswith("paused") and note.sent == []


def test_owner_commands_join_skip_and_list(session):
    market = MockMarketplace([listing("abcdef12-0000-0000-0000-000000000001", "GTA 6 Kai", desc="https://twitch.tv/kai"),
                              listing("bcdef123-0000-0000-0000-000000000002", "Podcast", category="Podcast")])
    scan(session, market, MockNotifier(), NOW, priority_keywords=GTA)
    assert "[abcdef] GTA 6 Kai" in handle_command(session, "/campaigns")
    assert handle_command(session, "/joined abcdef").startswith("Live: GTA 6 Kai. Pulling footage from twitch/kai")
    assert session.scalars(select(Campaign).where(Campaign.name == "GTA 6 Kai")).one().status == "joined"
    assert handle_command(session, "/skip bcdef1") == "Skipped: Podcast"
    assert "No open campaign" in handle_command(session, "/joined zzzzzz")
    assert handle_command(session, "hello") is None and handle_command(session, "/unknown") is None


def test_poll_only_obeys_the_owner_chat(session):
    scan(session, MockMarketplace([listing("abcdef12-0000-0000-0000-000000000001", "GTA 6 Kai")]), MockNotifier(), NOW,
         priority_keywords=GTA)
    bot = MockNotifier()
    bot.inbox = [{"update_id": 5, "message": {"chat": {"id": 999}, "text": "/joined abcdef"}},
                 {"update_id": 6, "message": {"chat": {"id": 42}, "text": "/joined abcdef"}}]
    assert poll_commands(session, bot, "42") == 1 and bot.sent[0].startswith("Live")
    assert poll_commands(session, bot, "42") == 0  # offset moved past both


def test_public_client_reads_both_pages_and_reports_errors():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert "/api/" not in str(request.url)
        return httpx.Response(200, text=page([raw(f"aaaaaaaa-0000-0000-0000-00000000000{len(calls)}", f"C{len(calls)}")]))
    pub = ContentRewardsPublic(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(pub.listings()) == 3 and len(calls) == 4  # two Discover pages, GTA Clipper, the sitemap
    assert any(c.endswith("/gtaclipper") for c in calls)
    down = ContentRewardsPublic(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with pytest.raises(MarketplaceError, match="503"):
        down.listings()


def test_short_code():
    assert short_code("abcdef12-0000") == "abcdef" and campaign_feed.MODULE == "campaign_feed"


def test_hand_footage_campaigns_are_counted_not_listed(session):
    note = MockNotifier()
    out = scan(session, MockMarketplace([listing("cccccccc-0000-0000-0000-000000000001", "Trailer clips", category="Gaming")]),
               note, NOW, priority_keywords=GTA)
    assert out.endswith("0 new worth joining, 1 need footage by hand")
    assert note.sent == ["Also new: 1 campaign(s) that need footage added by hand. Send /campaigns to see them."]
    assert judge(listing(desc="https://twitch.tv/kai"), GTA).alert
    assert not judge(listing(title="Podcast clips", category="Podcast"), GTA).alert


def test_campaign_alert_has_join_and_skip_buttons(session):
    note = MockNotifier()
    scan(session, MockMarketplace([listing("abcdef12-0000-0000-0000-000000000001", "GTA 6 Kai", desc="https://twitch.tv/kai")]),
         note, NOW, priority_keywords=GTA)
    rows = note.buttons[0]
    assert rows[0][0]["url"].endswith("abcdef12-0000-0000-0000-000000000001")
    assert [b["callback_data"] for b in rows[1]] == ["join:abcdef", "skip:abcdef"]


def _tap(update_id, data, chat=42, message_id=7):
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "data": data,
                                                       "message": {"message_id": message_id, "chat": {"id": chat},
                                                                   "text": "SUBMIT NOW"}}}


def test_tapping_submitted_marks_it_once_and_edits_the_alert(session):
    from autoclip.models import Account, Clip, Highlight, Post, SourceVideo
    from autoclip.services.submissions import queue_submission
    camp = Campaign(marketplace="m", external_id="e", name="C", url="https://contentrewards.com/c/campaigns/x",
                    status="joined")
    acc = Account(platform="instagram", handle="@a", niche="x", style_preset="s", active=True)
    v = SourceVideo(platform="twitch", video_id="v", channel_id="c", channel_title="c", title="t", duration_s=600,
                    license="", views=1, status="done", rights_basis="x")
    session.add_all([camp, acc, v])
    session.flush()
    h = Highlight(source_video_id=v.id, start_s=0, end_s=30, title="t", reason="r", score=60, llm="m", status="rendered")
    session.add(h)
    session.flush()
    c = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="posted")
    session.add(c)
    session.flush()
    post = Post(clip_id=c.id, account_id=acc.id, platform="instagram", scheduled_at=NOW, status="posted",
                url="https://www.instagram.com/reel/x/")
    session.add(post)
    session.flush()
    alert = MockNotifier()
    sub = queue_submission(session, post, camp, alert, NOW)
    assert alert.buttons[0] == [[{"text": "Submitted", "callback_data": f"sub:{sub.id}"},
                                 {"text": "Open campaign", "url": camp.url}]]

    bot = MockNotifier()
    bot.inbox = [_tap(1, f"sub:{sub.id}", chat=999), _tap(2, f"sub:{sub.id}"), _tap(3, f"sub:{sub.id}")]
    assert poll_commands(session, bot, "42") == 2  # the stranger's tap is ignored
    assert sub.status == "submitted"
    assert bot.answers == [("cb2", "Marked submitted."), ("cb3", "Already marked submitted.")]
    assert bot.edits[0] == ("42", 7, f"SUBMIT NOW\n\nDone: Submission #{sub.id}: submitted")


def test_tapping_joined_makes_the_campaign_live(session):
    scan(session, MockMarketplace([listing("abcdef12-0000-0000-0000-000000000001", "GTA 6 Kai", desc="https://twitch.tv/kai")]),
         MockNotifier(), NOW, priority_keywords=GTA)
    bot = MockNotifier()
    bot.inbox = [_tap(1, "join:abcdef"), _tap(2, "bogus:1")]
    assert poll_commands(session, bot, "42") == 1
    assert session.scalars(select(Campaign).where(Campaign.name == "GTA 6 Kai")).one().status == "joined"
    assert bot.answers[0][1].startswith("Live: GTA 6 Kai") and bot.answers[1][1] == "Unknown button."


def detail_page(cid, status="active", rates=None, tiers=None, desc="Clip the launch video."):
    rates = rates or {"instagram": 150, "youtube": 200, "tiktok": 200}
    data = {"id": cid, "name": "Ring - Drone Cam Launch Clipping", "status": status, "description": desc,
            "organization": {"name": "Clipping Culture"}, "type": "cpm", "requiresApplication": False,
            "metrics": {"budgetSpentCents": 491712},
            "configuration": {"budget": 2625000, "content": {"contentCategories": [{"id": "e", "name": "Entertainment"}]},
                              "payoutModel": [{"platform": p, "rate": r} for p, r in rates.items()],
                              "geoRates": {"tiers": tiers or []}, "requirement": {"preApproval": False}}}
    payload = json.dumps([{"state": {"data": data}, "queryKey": ["campaigns", "detail", cid]}], separators=(",", ":"))
    return f'<script>self.__next_f.push([1,{json.dumps(payload)}])</script>'


def test_parse_campaign_page_reads_rates_budget_and_zero_rate_countries():
    from autoclip.integrations.marketplace import parse_campaign_page
    cid = "42086467-e927-4418-92e1-b6a5700ccc57"
    item = parse_campaign_page(detail_page(cid, tiers=[{"label": "Pak, Ind, Ban", "platform": "youtube", "rate": 0}]), cid)
    assert item.title == "Ring - Drone Cam Launch Clipping" and item.brand == "Clipping Culture"
    assert item.platforms == ["instagram", "tiktok", "youtube"] and item.rate_per_1k == 2.0
    assert item.budget_total == 26250 and item.budget_left == pytest.approx(26250 - 4917.12)
    assert "pays $0 for views from: Pak, Ind, Ban on youtube" in item.description
    assert "Warning: pays $0 for views from Pak, Ind, Ban on youtube" in campaign_feed._geo_warning(item)
    assert parse_campaign_page(detail_page(cid, status="ended"), cid) is None
    with pytest.raises(MarketplaceError):
        parse_campaign_page(page([]), cid)


def test_sitemap_campaigns_are_opened_slowly_and_only_once(session):
    new, old, ended = ("aaaaaaaa-0000-0000-0000-00000000000a", "bbbbbbbb-0000-0000-0000-00000000000b",
                       "cccccccc-0000-0000-0000-00000000000c")
    opened, sleeps = [], []

    def handler(request):
        url = str(request.url)
        assert "/api/" not in url
        if url.endswith("sitemap.xml"):
            return httpx.Response(200, text="".join(f"<url><loc>https://contentrewards.com/c/campaigns/{i}</loc></url>"
                                                    for i in (new, old, ended)))
        if "/c/campaigns/" in url:
            cid = url.rsplit("/", 1)[-1]
            opened.append(cid)
            return httpx.Response(200, text=detail_page(cid, status="ended" if cid == ended else "active",
                                                        desc="https://twitch.tv/kai GTA 6"))
        return httpx.Response(200, text=page([raw("dddddddd-0000-0000-0000-00000000000d", "Featured")]))

    pub = ContentRewardsPublic(client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append)
    session.add(Campaign(marketplace="content_rewards", external_id=old, name="Old", url="u", status="joined"))
    from autoclip.services.settings_store import set_setting
    set_setting(session, "campaign_feed.seen", [old])
    out = scan(session, pub, MockNotifier(), NOW, priority_keywords=GTA)
    assert opened == [new, ended] and sleeps == [2.0, 2.0]
    assert out.startswith("2 listings, 1 new worth joining")
    opened.clear()
    scan(session, pub, MockNotifier(), NOW, priority_keywords=GTA)
    assert opened == []  # the new one is seen, the ended one is remembered as inactive


def test_gambling_categories_and_betting_brands_are_skipped():
    for title, cat in (("ParlayCapital Clipping", "Gambling"), ("Coinpoker Campaign", "Poker"),
                       ("Jacks Club Clipping", "Casino"), ("7bet Kliping", "Entertainment")):
        assert not judge(listing(title=title, category=cat), GTA).ok, title


def test_known_campaign_gets_its_exact_page_and_imports_keep_it(session, tmp_path):
    from autoclip.services.campaigns import import_campaigns
    from autoclip.services.submissions import where_to_submit
    yml = tmp_path / "c.yaml"
    yml.write_text("campaigns:\n  - marketplace: content_rewards\n    id: reach-jason\n"
                   "    name: $9,000 Jason Pool Campaign (Reach)\n    url: https://contentrewards.com\n    joined: true\n")
    import_campaigns(yml, session)
    c = session.scalars(select(Campaign)).one()
    assert "not known yet" in where_to_submit(c)
    scan(session, MockMarketplace([listing("0b178e3c-fbd3-4294-8ded-394e3da9c62f", "$9,000 Jason Pool Campaign")]),
         MockNotifier(), NOW)
    assert c.url == "https://contentrewards.com/c/campaigns/0b178e3c-fbd3-4294-8ded-394e3da9c62f"
    import_campaigns(yml, session)  # the yaml still says the homepage: the exact page is kept
    assert c.url.endswith("0b178e3c-fbd3-4294-8ded-394e3da9c62f") and where_to_submit(c) == c.url
    assert any(d.action == "linked" for d in session.scalars(select(DecisionLog)))


def test_gta_clipper_page_failing_alone_does_not_stop_the_scan():
    def handler(request):
        if request.url.path == "/gtaclipper":
            return httpx.Response(500)
        return httpx.Response(200, text=page([raw("aaaaaaaa-0000-0000-0000-000000000009", "C")]))
    pub = ContentRewardsPublic(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(pub.listings()) == 1


def test_skipped_gta_campaign_is_still_reported_with_the_reason(session):
    from autoclip.integrations.telegram import MockNotifier
    from autoclip.integrations.marketplace import MockMarketplace
    from autoclip.services.campaign_feed import scan
    music = listing("bbbbbbbb-0000-0000-0000-000000000001", "GTA 6 Clipping Campaign - Tokyo Girl", category="Music")
    other = listing("bbbbbbbb-0000-0000-0000-000000000002", "Some song campaign", category="Music")
    note = MockNotifier()
    scan(session, MockMarketplace([music, other]), note, priority_keywords=["gta 6"])
    sent = [m for m in note.sent if "seen but skipped" in m]
    assert len(sent) == 1 and "Tokyo Girl" in sent[0] and "Why:" in sent[0]
