"""Discovery (plan prompt 10): finds new long videos from allowlisted sources and joined campaigns,
passes every one through the rights gate, and ranks what passes.

YouTube channels are polled through RSS (no quota) with videos.list for stats (1 unit per 50);
Twitch through Helix; Kick through its website endpoint (best effort). Creative Commons search
(100 units per query) runs only if enabled in sources.yaml.
"""
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.feeds import FeedError
from autoclip.integrations.youtube import YouTubeAPIError, YouTubeQuotaError
from autoclip.models import Campaign, ChannelState, Clip, Highlight, Post, SourceVideo
from autoclip.rights import RightsGate, Video
from autoclip.sources import ChannelSource, Sources

MODULE = "discovery"


def language_problem(video, allowed: list[str]) -> str | None:
    """None if the video's language is acceptable, otherwise the reason it is not."""
    if not allowed:
        return None
    declared = (video.language or "").split("-")[0].lower()
    if declared:
        return None if declared in allowed else f"language '{video.language}' is not one of {allowed}"
    # No declared language: only a rough check is possible until transcription detects it.
    letters = [c for c in video.title if c.isalpha()]
    if letters and sum(c.isascii() for c in letters) / len(letters) >= 0.9:
        return None
    return "language not declared and the title is not in Latin script"


def campaign_sources(session: Session) -> list[ChannelSource]:
    """Joined campaigns in the database become allowlist entries for their source channels."""
    out = []
    for c in session.scalars(select(Campaign).where(Campaign.status == "joined")):
        for ch in c.source_channels or []:
            out.append(ChannelSource(platform=str(ch["platform"]).lower(), channel_id=str(ch["channel_id"]),
                                     name=c.name, basis=f"{c.name} on {c.marketplace} ({c.url})",
                                     starts=c.starts, ends=c.ends, profile=c.niche, campaign_id=c.id))
    return out


def pick_sources(pool: list[ChannelSource]) -> dict[tuple[str, str], ChannelSource]:
    """One allowlist entry per channel. sources.yaml permission is never replaced; when several campaigns
    cover a channel, the campaign named after the streamer wins (e.g. the Lacy pool over a brand campaign
    that only linked his channel): its clips are paid there and its rules are about them."""
    best: dict[tuple[str, str], ChannelSource] = {}
    for c in pool:
        key = (c.platform, c.channel_id.lower())
        cur = best.get(key)
        if cur is None or (_named_after(c) and not _named_after(cur) and cur.campaign_id is not None):
            best[key] = c
    return best


def _named_after(c: ChannelSource) -> bool:
    return c.channel_id.lower() in (c.name or "").lower()


def add_campaign_videos(session: Session, now: datetime) -> int:
    """Campaigns that only allow specific approved footage list it as source_videos."""
    n = 0
    for c in session.scalars(select(Campaign).where(Campaign.status == "joined")):
        for sv in c.source_videos or []:
            platform, vid = str(sv["platform"]).lower(), str(sv["video_id"])
            if session.scalars(select(SourceVideo.id).where(SourceVideo.platform == platform,
                                                            SourceVideo.video_id == vid)).first():
                continue
            session.add(SourceVideo(platform=platform, video_id=vid, channel_id=f"campaign:{c.external_id}",
                                    channel_title=c.creator or c.name, title=sv.get("title", vid),
                                    duration_s=int(float(sv.get("minutes", 0)) * 60), license="", views=0,
                                    published_at=now, status="discovered", campaign_id=c.id, profile=c.niche,
                                    rights_basis=f"approved footage of {c.name} on {c.marketplace} ({c.url})",
                                    rank_score=10.0))
            log_decision(session, MODULE, "discovered", f"approved campaign footage for {c.name}",
                         subject=f"{platform}:{vid}", data={"title": sv.get("title")})
            n += 1
    session.flush()
    return n


def source_rewards(session: Session) -> dict[tuple[str, str], float]:
    """Mean post reward per source channel, relative to the overall mean (1.0 = average)."""
    rows = session.execute(
        select(SourceVideo.platform, SourceVideo.channel_id, func.avg(Post.reward))
        .join(Highlight, Highlight.source_video_id == SourceVideo.id)
        .join(Clip, Clip.highlight_id == Highlight.id).join(Post, Post.clip_id == Clip.id)
        .where(Post.reward.is_not(None)).group_by(SourceVideo.platform, SourceVideo.channel_id)).all()
    if not rows:
        return {}
    overall = sum(r[2] for r in rows) / len(rows) or 1.0
    return {(p, c): max(0.2, float(r) / overall) for p, c, r in rows}


def rank_score(v: Video, now: datetime, reward: float = 1.0, boost: float = 1.0) -> float:
    """Views per hour (log-scaled) x recency decay (half-life 3 days) x source reward."""
    published = v.published_at
    if published is not None and published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)  # Kick sends times without a timezone
    age_h = max(1.0, (now - published).total_seconds() / 3600) if published else 72.0
    vph = math.log10(1 + v.views / age_h)
    recency = 0.5 ** (age_h / 72)
    return round((1 + vph) * (0.3 + recency) * reward * boost, 4)


def topic_boost(title: str, keywords: list[str] | tuple[str, ...], factor: float) -> float:
    """Priority topics (GTA 6 around its launch) jump the queue: whole-word match, case-insensitive."""
    t = " " + re.sub(r"[^a-z0-9]+", " ", (title or "").lower()) + " "
    for k in keywords:
        k = " " + re.sub(r"[^a-z0-9]+", " ", k.lower()).strip() + " "
        if k.strip() and k in t:
            return factor
    return 1.0


@dataclass
class DiscoverySummary:
    discovered: int = 0
    rejected: int = 0
    already_known: int = 0
    paused: list[str] = field(default_factory=list)
    quota_units: int = 0


def discover(sources: Sources, session: Session, *, youtube=None, rss=None, twitch=None, kick=None,
             now: datetime | None = None, only: list[ChannelSource] | None = None,
             boost: float = 1.0, priority_keywords: list[str] | tuple[str, ...] = (),
             priority_boost: float = 1.0) -> DiscoverySummary:
    """`only` limits the run to some channels (used by stream-ended triggers)."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    summary = DiscoverySummary()
    db_campaigns = campaign_sources(session)
    effective = Sources(sources.min_duration_minutes, sources.creative_commons, sources.channels,
                        sources.campaigns + db_campaigns)
    gate = RightsGate(effective)
    targets = only if only is not None else (
        effective.channels + [c for c in effective.campaigns if c.active_on(today)])

    def pause(reason: str):
        summary.paused.append(reason)
        log_decision(session, MODULE, "paused", reason)

    if only is None:
        summary.discovered += add_campaign_videos(session, now)
    found: dict[tuple[str, str], tuple[str, ChannelSource | None]] = {}
    ready: dict[tuple[str, str], Video] = {}
    for ch in targets:
        try:
            if ch.platform == "youtube" and rss is not None:
                for vid in rss.latest(ch.channel_id):
                    found.setdefault(("youtube", vid), (f"RSS of '{ch.name}'", ch))
            elif ch.platform == "twitch" and twitch is not None:
                for v in twitch.vods(ch.channel_id):
                    found.setdefault(("twitch", v.video_id), (f"Twitch VODs of '{ch.name}'", ch))
                    ready[("twitch", v.video_id)] = v
            elif ch.platform == "kick" and kick is not None:
                for v in kick.vods(ch.channel_id):
                    found.setdefault(("kick", v.video_id), (f"Kick VODs of '{ch.name}'", ch))
                    ready[("kick", v.video_id)] = v
        except FeedError as e:
            pause(f"{ch.platform} feed for '{ch.name}' failed: {e}")

    cc = effective.creative_commons
    if only is None and cc.enabled and cc.queries and youtube is not None:
        try:
            after = now - timedelta(days=cc.max_age_days)
            start = today.toordinal() % len(cc.queries)
            for i in range(min(cc.max_searches_per_run, len(cc.queries))):
                query = cc.queries[(start + i) % len(cc.queries)]
                for vid in youtube.search_cc(query, after, language=cc.languages[0] if cc.languages else None):
                    found.setdefault(("youtube", vid), (f"Creative Commons search '{query}'", None))
        except YouTubeQuotaError as e:
            pause(f"YouTube quota reached ({e}); will retry on the next run")
        except YouTubeAPIError as e:
            pause(f"YouTube API error: {e}")

    known = set()
    for platform in {p for p, _ in found}:
        ids = [v for p, v in found if p == platform]
        known |= {(platform, v) for v in session.scalars(select(SourceVideo.video_id).where(
            SourceVideo.platform == platform, SourceVideo.video_id.in_(ids)))}
    summary.already_known = len(known)

    yt_ids = [v for (p, v) in found if p == "youtube" and (p, v) not in known]
    if yt_ids and youtube is not None:
        try:
            for v in youtube.videos(yt_ids):
                ready[("youtube", v.video_id)] = v
        except YouTubeQuotaError as e:
            pause(f"YouTube quota reached ({e}); will retry on the next run")
        except YouTubeAPIError as e:
            pause(f"YouTube API error: {e}")

    rewards = source_rewards(session)
    picked = pick_sources(effective.channels + [c for c in effective.campaigns if c.active_on(today)])
    for key, v in ready.items():
        if key in known:
            continue
        origin, ch = found.get(key, ("unknown", None))
        decision = gate.check(v, today)
        allowed, reason = decision.allowed, decision.reason
        via_cc = allowed and decision.attribution is not None
        lang_issue = language_problem(v, cc.languages) if via_cc else None
        if lang_issue:
            allowed, reason = False, lang_issue
        elif allowed and v.duration_s < effective.min_duration_minutes * 60:
            allowed, reason = False, f"too short ({v.duration_s // 60} min, minimum {effective.min_duration_minutes})"
        elif via_cc and v.views < cc.min_views:
            allowed, reason = False, f"too few views ({v.views}, minimum {cc.min_views})"

        match = picked.get((v.platform, v.channel_id.lower()), ch)
        score = rank_score(v, now, rewards.get((v.platform, v.channel_id), 1.0),
                           boost * topic_boost(v.title, priority_keywords, priority_boost)) if allowed else None
        session.add(SourceVideo(
            platform=v.platform, video_id=v.video_id, channel_id=v.channel_id,
            channel_title=v.channel_title, title=v.title, duration_s=v.duration_s, license=v.license,
            views=v.views, published_at=v.published_at, language=v.language or None,
            status="discovered" if allowed else "rejected",
            rights_basis=decision.basis if allowed else None,
            attribution=decision.attribution if allowed else None,
            campaign_id=match.campaign_id if (allowed and match) else None,
            profile=match.profile if match else None, rank_score=score))
        log_decision(session, MODULE, "discovered" if allowed else "rejected",
                     f"{reason}; found via {origin}" + (f"; rank {score}" if score is not None else ""),
                     subject=f"{v.platform}:{v.video_id}", data={"title": v.title, "channel": v.channel_title})
        summary.discovered += allowed
        summary.rejected += not allowed

    summary.quota_units = getattr(youtube, "units_used", 0) if youtube is not None else 0
    log_decision(session, MODULE, "run_finished",
                 f"{summary.discovered} discovered, {summary.rejected} rejected, "
                 f"{summary.already_known} already known, {summary.quota_units} YouTube quota units")
    return summary


def stream_ended(sources: Sources, session: Session, *, twitch=None, kick=None) -> list[ChannelSource]:
    """Compares current live state with the last check. Channels that just went offline are returned
    so their VOD can be queued within minutes (the first clips out take most of a campaign's budget)."""
    channels = sources.channels + sources.campaigns + campaign_sources(session)
    ended = []
    for platform, client in (("twitch", twitch), ("kick", kick)):
        chans = [c for c in channels if c.platform == platform]
        if client is None or not chans:
            continue
        try:
            live = client.live_logins([c.channel_id for c in chans])
        except FeedError as e:
            log_decision(session, MODULE, "paused", f"{platform} live check failed: {e}")
            continue
        for c in chans:
            state = session.scalars(select(ChannelState).where(
                ChannelState.platform == platform, ChannelState.channel_id == c.channel_id)).first()
            now_live = c.channel_id.lower() in {x.lower() for x in live}
            if state is None:
                session.add(ChannelState(platform=platform, channel_id=c.channel_id, live=now_live))
                continue
            if state.live and not now_live:
                ended.append(c)
                log_decision(session, MODULE, "stream_ended", f"'{c.name}' went offline; queueing the VOD now",
                             subject=f"{platform}:{c.channel_id}")
            state.live, state.updated_at = now_live, datetime.now(timezone.utc)
    session.flush()
    return ended
