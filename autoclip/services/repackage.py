"""When a campaign's caption rules change (a new brief, a "Before you submit" notice), clips already made for it
but not posted yet get their post text written again, so they are not blocked at upload for a missing tag."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.llm import LLM
from autoclip.integrations.transcriber import Segment
from autoclip.models import Campaign, Clip, Highlight, Post, SourceVideo, Transcript
from autoclip.pipeline.brief import Brief
from autoclip.pipeline.package import package_text
from autoclip.services import focus

MODULE = "repackage"
UNPOSTED = ("scheduled", "held")
CAPTION_BLOCK = "caption is missing"  # the publish gate's reason when a required tag or name is absent


def repackage_campaign(session: Session, llm: LLM | None, campaign: Campaign,
                       keywords: tuple[str, ...] = ()) -> int:
    """Rewrites the post text of the campaign's unposted clips under its current brief. Posts the publish gate
    blocked only for a missing caption tag are scheduled again. Returns the number of clips rewritten."""
    if not campaign.brief:
        return 0
    brief = Brief.model_validate(campaign.brief)
    posts = list(session.scalars(select(Post).join(Clip, Clip.id == Post.clip_id).where(
        Clip.campaign_id == campaign.id, Post.status.in_(UNPOSTED + ("blocked",)))))
    posts = [p for p in posts if p.status != "blocked" or CAPTION_BLOCK in (p.status_reason or "")]
    n = 0
    for clip_id in dict.fromkeys(p.clip_id for p in posts):
        clip = session.get(Clip, clip_id)
        hl = session.get(Highlight, clip.highlight_id)
        video = session.get(SourceVideo, hl.source_video_id)
        tr = session.scalars(select(Transcript).where(Transcript.source_video_id == video.id)).first()
        said = " ".join(s.text for s in (Segment.from_json(x) for x in (tr.segments if tr else []))
                        if s.end > hl.start_s and s.start < hl.end_s)
        tag_brief = brief
        if focus.is_gta_video(video, keywords or focus.DEFAULT_KEYWORDS):
            tag_brief = brief.model_copy(update={"required_hashtags": list(dict.fromkeys(
                list(brief.required_hashtags) + list(focus.GTA_TAGS)))})
        pkg = package_text(llm, title=hl.title, hook=hl.hook_headline, excerpt=hl.reason,
                           creator=video.channel_title, attribution=video.attribution, brief=tag_brief,
                           source_title=video.title or "", said=said)
        clip.package = {k: {"title": v.title, "description": v.description, "hashtags": v.hashtags,
                            "disclosure": v.disclosure} for k, v in pkg.items()}
        for p in posts:
            if p.clip_id == clip_id and p.status == "blocked":
                p.status, p.status_reason = "scheduled", "caption rewritten under the campaign's new rules"
        log_decision(session, MODULE, "repackaged", f"{campaign.name} rules changed: post text written again "
                                                    f"({', '.join(pkg['instagram'].hashtags)})",
                     subject=f"clip:{clip.id}")
        n += 1
    session.flush()
    return n
