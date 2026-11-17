import json
from datetime import datetime, timezone

from sqlalchemy import select

from autoclip.integrations.llm import MockLLM
from autoclip.models import Account, Campaign, Clip, DecisionLog, Highlight, Post, SourceVideo, Transcript
from autoclip.pipeline.brief import Brief
from autoclip.pipeline.gate import check_brief
from autoclip.services.repackage import repackage_campaign
from autoclip.tasks import parse_new_briefs

AT = datetime(2026, 11, 17, 14, tzinfo=timezone.utc)
OLD = {"instagram": {"title": "t", "description": "d", "hashtags": ["#funny"], "disclosure": {}},
       "youtube": {"title": "t", "description": "d", "hashtags": ["#shorts"], "disclosure": {}}}


def clip_with_posts(session, statuses, reason=None):
    camp = Campaign(marketplace="cr", external_id="core", name="$25,000 CORE Pool Campaign (Reach)", url="u",
                    status="joined", rules_text="Use #CORE and a hashtag for each CORE member in the video.")
    v = SourceVideo(platform="youtube", video_id="core1", channel_id="UC", channel_title="CORE",
                    title="CORE JEOPARDY ft. Jason", duration_s=600, license="", views=1, status="done",
                    rights_basis="x")
    session.add_all([camp, v])
    session.flush()
    session.add(Transcript(source_video_id=v.id, language="en", model="m", segments=[
        {"s": 10, "e": 14, "t": "Jason, that is not the answer", "w": []},
        {"s": 300, "e": 304, "t": "much later, another clip", "w": []}]))
    h = Highlight(source_video_id=v.id, start_s=8, end_s=40, title="Wrong answer", reason="r", score=60, llm="m",
                  status="rendered")
    session.add(h)
    session.flush()
    c = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="scheduled",
             campaign_id=camp.id, package=OLD)
    session.add(c)
    session.flush()
    for i, st in enumerate(statuses):
        acc = Account(platform=("instagram", "youtube")[i % 2], handle=f"@a{i}", niche="x", style_preset="s",
                      active=True)
        session.add(acc)
        session.flush()
        session.add(Post(clip_id=c.id, account_id=acc.id, platform=acc.platform, scheduled_at=AT, status=st,
                         status_reason=reason))
    session.flush()
    return camp, c


def writer(seen):
    def answer(prompt):
        seen.append(prompt)
        return json.dumps({p: {"title": "Jason blanks", "description": "d", "hashtags": ["#JasonTheWeen"]}
                           for p in ("youtube", "instagram")})
    return answer


def test_a_new_brief_rewrites_unposted_clips_with_the_required_tags_and_who_is_in_it(session):
    camp, c = clip_with_posts(session, ["scheduled", "held"])
    seen = []
    write = writer(seen)
    llm = MockLLM(lambda p: json.dumps({"required_hashtags": ["CORE"], "caption_rules": "a hashtag per CORE member"})
                  if p.startswith("Extract the rules") else write(p))
    assert parse_new_briefs(session, llm) == 1
    tags = c.package["instagram"]["hashtags"]
    assert tags[:2] == ["#CORE", "#JasonTheWeen"]
    assert "Jason, that is not the answer" in seen[0] and "another clip" not in seen[0]  # only this clip's words
    caption = " ".join([c.package["youtube"]["title"], c.package["youtube"]["description"],
                        " ".join(c.package["youtube"]["hashtags"])])
    assert check_brief(Brief.model_validate(camp.brief), video=None, length_s=30, platform="youtube",
                       caption=caption).ok
    assert any(d.action == "repackaged" for d in session.scalars(select(DecisionLog)))


def test_a_post_blocked_for_a_missing_tag_is_scheduled_again(session):
    camp, c = clip_with_posts(session, ["blocked"], reason="caption is missing #CORE")
    camp.brief = Brief(required_hashtags=["CORE"]).model_dump(mode="json")
    assert repackage_campaign(session, MockLLM(writer([])), camp) == 1
    p = session.scalars(select(Post)).one()
    assert p.status == "scheduled" and "rewritten" in p.status_reason


def test_posted_clips_and_other_blocks_are_left_alone(session):
    camp, c = clip_with_posts(session, ["posted", "blocked"], reason="rights: campaign ended")
    camp.brief = Brief(required_hashtags=["CORE"]).model_dump(mode="json")
    assert repackage_campaign(session, MockLLM(writer([])), camp) == 0
    assert c.package == OLD


def test_without_the_llm_the_required_tags_are_still_added(session):
    camp, c = clip_with_posts(session, ["scheduled"])
    camp.brief = Brief(required_hashtags=["CORE"], name_in_caption="Lacy").model_dump(mode="json")
    repackage_campaign(session, None, camp)
    assert c.package["instagram"]["hashtags"][0] == "#CORE" and "Lacy" in c.package["instagram"]["description"]


def test_a_brief_naming_the_channel_by_handle_still_allows_the_campaigns_channel_id(session):
    camp, c = clip_with_posts(session, ["scheduled"])
    camp.source_channels = [{"platform": "youtube", "channel_id": "UC"}]
    llm = MockLLM(lambda p: json.dumps({"allowed_sources": ["youtube.com/@createownruneverything"]})
                  if p.startswith("Extract the rules") else "{}")
    parse_new_briefs(session, llm)
    video = session.scalars(select(SourceVideo)).one()
    assert camp.brief["allowed_sources"] == ["youtube.com/@createownruneverything", "UC"]
    assert check_brief(Brief.model_validate(camp.brief), video=video, length_s=30, platform="youtube").ok
    video.channel_id = "UCimpostor"
    assert not check_brief(Brief.model_validate(camp.brief), video=video, length_s=30, platform="youtube").ok
