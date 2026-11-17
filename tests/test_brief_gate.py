import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from autoclip.integrations.llm import LLMError, MockLLM
from autoclip.pipeline.brief import Brief, BriefError, parse_brief
from autoclip.pipeline.gate import check_brief, check_source, check_text_rules, moderate

# Five briefs written from the real campaign pages (Clyper, Oct-Nov 2026).
BRIEFS = {
    "kai": ("Clips must come from Kai Cenat's Twitch and YouTube, from October 21 to 31, 2026. Kai must be the "
            "main focus. TikTok, Instagram Reels and YouTube Shorts only. Caption must use #KaiCenat. No hate "
            "farming, no logos. $10,000 pool, 350,000 approved views minimum. Submit within 30 minutes.",
            {"allowed_sources": ["twitch.tv/kaicenat", "youtube.com/@KaiCenatLive"], "date_from": "2026-10-21",
             "date_to": "2026-10-31", "platforms": ["tiktok", "Reels", "Shorts"], "required_hashtags": ["KaiCenat"],
             "no_logos": True, "no_negative_portrayal": True, "submit_within_minutes": 30,
             "min_views_to_qualify": 350000}),
    "speed": ("Expedia x IShowSpeed. $1 per 1,000 views, $10 to $500 a clip. Only footage from the November 6 "
              "stream from 2:56:29. TikTok, Reels, Shorts. #IShowSpeed required. US/UK/CA/AU/NZ audience 50%+.",
              {"platforms": ["tiktok", "instagram", "youtube"], "rate_per_1k": 1.0, "min_payout": 10,
               "max_payout": 500, "required_hashtags": ["#IShowSpeed"], "audience_geo": ["US", "GB", "CA", "AU", "NZ"],
               "audience_geo_min_share": 0.5, "date_from": "2026-11-06", "date_to": "2026-11-06"}),
    "jason": ("Any video featuring JasonTheWeen, him as main focus, his name in caption, #JasonTheWeen. TikTok, "
              "Reels, Shorts and X. $9,000 pool, 700,000 views minimum.",
              {"platforms": ["tiktok", "instagram", "youtube", "x"], "name_in_caption": "JasonTheWeen",
               "required_hashtags": ["#JasonTheWeen"], "min_views_to_qualify": 700000}),
    "lacy": ("$25,000 pool for TikTok, Reels and Shorts. Lacy must be the main focus, name him, use #Lacy. "
             "1,000,000 approved views to get a share. No botting, no AI-generated content.",
             {"platforms": ["tiktok", "instagram", "youtube"], "required_hashtags": ["#Lacy"],
              "name_in_caption": "Lacy", "min_views_to_qualify": 1000000, "ai_generated_allowed": False}),
    "vyro": ("MrBeast Roblox round. $2.50 per 1K views, up to $1,000 per clip. YouTube Shorts and Reels. "
             "Clips 15-60 seconds. Disclosure: #ad must be in the caption.",
             {"platforms": ["youtube", "instagram"], "rate_per_1k": 2.5, "max_payout": 1000, "min_seconds": 15,
              "max_seconds": 60, "disclosure_text": "#ad"}),
}


@pytest.mark.parametrize("name", list(BRIEFS))
def test_parse_brief_from_llm_json(name):
    text, extracted = BRIEFS[name]
    llm = MockLLM([f"```json\n{json.dumps(extracted)}\n```"])
    b = parse_brief(llm, text)
    assert text in llm.prompts[0]
    assert all(p in ("instagram", "youtube", "tiktok", "x", "facebook") for p in b.platforms)
    assert all(t.startswith("#") for t in b.required_hashtags)
    if name == "kai":
        assert b.platforms == ["tiktok", "instagram", "youtube"] and b.date_to == date(2026, 10, 31)
    if name == "lacy":
        assert b.ai_generated_allowed is False


def test_parse_brief_retries_then_fails():
    assert parse_brief(MockLLM(["junk", '{"platforms": ["reels"]}']), "x").platforms == ["instagram"]
    with pytest.raises(BriefError):
        parse_brief(MockLLM(["junk", '{"date_from": "not a date"}']), "x")


def video(published=datetime(2026, 10, 25, tzinfo=timezone.utc), channel="kaicenat", basis="campaign: Kai"):
    return SimpleNamespace(rights_basis=basis, published_at=published, channel_id=channel, channel_title="Kai Cenat")


def kai_brief():
    return Brief.model_validate(BRIEFS["kai"][1])


def test_null_lists_and_flags_mean_the_brief_is_silent():
    # what the local model returns for a short brief: null everywhere, as the prompt asks
    out = {"allowed_sources": None, "platforms": None, "no_logos": None, "no_negative_portrayal": None,
           "required_hashtags": None, "audience_geo": ["US"], "ai_generated_allowed": None, "max_seconds": None}
    b = parse_brief(MockLLM([json.dumps(out)]), "x")
    assert b.allowed_sources == [] and b.no_logos is False and b.audience_geo == ["US"]
    assert b.ai_generated_allowed is None


def test_an_unreadable_payout_field_is_dropped_but_rules_are_kept():
    b = parse_brief(MockLLM([json.dumps({"rate_per_1k": "$1.5 per 1k", "required_hashtags": ["ad"]})]), "x")
    assert b.rate_per_1k is None and b.required_hashtags == ["#ad"]


def test_source_check():
    camp = SimpleNamespace(name="Kai", status="joined", ends=date(2026, 11, 30))
    assert check_source(video(), camp, date(2026, 11, 13)).ok
    assert not check_source(video(basis=None), None, date(2026, 11, 13)).ok
    assert "not joined" in check_source(video(), SimpleNamespace(name="K", status="open", ends=None),
                                        date(2026, 11, 13)).reason
    assert "ended" in check_source(video(), camp, date(2026, 12, 1)).reason


def test_brief_platform_length_dates_and_source():
    b = kai_brief()
    assert check_brief(b, video=video(), length_s=40, platform="instagram").ok
    assert "does not pay on x" in check_brief(b, video=video(), length_s=40, platform="x").reason
    assert "before the brief's range" in check_brief(
        b, video=video(published=datetime(2026, 10, 1, tzinfo=timezone.utc)), length_s=40).reason
    impostor = video(channel="someoneelse")  # same display name "Kai Cenat", different channel
    assert "not from a source" in check_brief(b, video=impostor, length_s=40).reason
    yt = SimpleNamespace(**{**vars(video(channel="UCabc")), "channel_handle": "@KaiCenatLive"})
    assert check_brief(b, video=yt, length_s=40).ok
    v = Brief.model_validate(BRIEFS["vyro"][1])
    assert "maximum is 60s" in check_brief(v, video=video(), length_s=75).reason
    assert "minimum is 15s" in check_brief(v, video=video(), length_s=9).reason


def test_brief_caption_requirements():
    b = kai_brief()
    assert "#KaiCenat" in check_brief(b, video=video(), length_s=40, caption="crazy moment").reason
    assert check_brief(b, video=video(), length_s=40, caption="crazy moment #kaicenat").ok
    j = Brief.model_validate(BRIEFS["jason"][1])
    missing = check_brief(j, video=video(channel="x"), length_s=30, caption="wild moment").reason
    assert "#JasonTheWeen" in missing and missing.count("JasonTheWeen") == 2
    v = Brief.model_validate(BRIEFS["vyro"][1])
    assert "disclosure" in check_brief(v, video=video(), length_s=30, caption="wow").reason


def test_brief_forbidding_ai_holds_for_user_decision():
    r = check_brief(Brief.model_validate(BRIEFS["lacy"][1]), video=video(channel="x"), length_s=30)
    assert not r.ok and r.hold and "your decision" in r.reason


def test_no_brief_passes():
    assert check_brief(None, video=video(), length_s=30).ok


def test_text_rules():
    assert check_text_rules("Speed in Tokyo", "we went to the market", "hello everyone").ok
    assert "leak" in check_text_rules("GTA 6 LEAKED gameplay", "", "").reason
    assert "gambling" in check_text_rules("Stream", "use my code on stake.com for slots", "").reason
    assert "violence" in check_text_rules("Stream", "", "there was blood everywhere").reason


def test_moderation_flags_passes_and_holds():
    llm = MockLLM([json.dumps({"results": [{"id": 0, "flags": []}, {"id": 1, "flags": ["hate"], "note": "slur"}]})])
    a, b = moderate(llm, ["fine clip", "bad clip"])
    assert a.ok and not b.ok and "hate" in b.reason and "slur" in b.reason and not b.hold
    held = moderate(MockLLM(error=LLMError("down")), ["x"])
    assert not held[0].ok and held[0].hold
    missing = moderate(MockLLM(['{"results": []}']), ["x"])
    assert missing[0].hold
    assert moderate(MockLLM(["{}"]), []) == []


def test_ai_rule_passes_when_you_allow_ai_assisted_clips():
    lacy = Brief.model_validate(BRIEFS["lacy"][1])
    assert check_brief(lacy, video=video(channel="x"), length_s=30, allow_ai_assisted=True).ok


def test_avoid_ranges_banned_words_and_hashtag_limit():
    from autoclip.pipeline.gate import check_ranges
    b = Brief(avoid_ranges=[[47, 53]], banned_caption_words=["lab-grown"], max_hashtags=3)
    assert not check_ranges(b, 40, 70).ok and check_ranges(b, 60, 90).ok and check_ranges(None, 0, 10).ok
    v = video()
    assert "banned words" in check_brief(b, video=v, length_s=30, caption="better than lab-grown").reason
    assert "at most 3" in check_brief(b, video=v, length_s=30, caption="#a #b #c #d").reason
    assert check_brief(b, video=v, length_s=30, caption="natural #a #b").ok


def test_moderation_asks_again_when_the_json_is_broken():
    llm = MockLLM(['{"results": [{"id": 0, "flags": [] ', '{"results": [{"id": 0, "flags": []}]}'])
    assert moderate(llm, ["fine clip"])[0].ok and len(llm.prompts) == 2
    stubborn = MockLLM(["not json"])
    assert moderate(stubborn, ["x"])[0].hold and len(stubborn.prompts) == 3
    assert len(set(stubborn.prompts)) == 3  # each retry differs, so a cached broken reply is not reused


def test_moderation_accepts_a_bare_list_and_code_fences():
    bare = '[\n  {"id": 0, "flags": []},\n  {"id": 1, "flags": ["hate"]}\n]'
    a, b = moderate(MockLLM([bare]), ["ok", "bad"])
    assert a.ok and not b.ok and not b.hold
    fenced = '```json\n{"results": [{"id": 0, "flags": []}]}\n```'
    assert moderate(MockLLM([fenced]), ["ok"])[0].ok


def test_a_local_model_flag_is_a_hold_for_gemini_not_a_block():
    flagged = json.dumps({"results": [{"id": 0, "flags": ["gambling"]}]})
    local = moderate(MockLLM([flagged], name="ollama:llama3.2:3b"), ["I lost twelve grand on that bill"])[0]
    assert not local.ok and local.hold and local.reason.startswith("local model flagged")
    gemini = moderate(MockLLM([flagged], name="gemini:gemini-flash-latest"), ["x"])[0]
    assert not gemini.ok and not gemini.hold  # Gemini's own flags still block


def test_local_model_answers_are_not_cached(session):
    from autoclip.integrations.llm import CachedLLM
    from autoclip.models import LLMCache
    CachedLLM(MockLLM(["a"], name="ollama:llama3.2:3b"), session).complete("p1")
    CachedLLM(MockLLM(["b"], name="gemini:x"), session).complete("p2")
    assert [r.llm for r in session.query(LLMCache)] == ["gemini:x"]
