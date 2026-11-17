"""The rights gate. Nothing is processed unless check() allows it."""
from dataclasses import dataclass
from datetime import date, datetime

from autoclip.sources import Sources

CC_LICENSE = "creativeCommon"  # value the YouTube API reports in status.license


@dataclass
class Video:
    platform: str
    video_id: str
    channel_id: str
    channel_title: str
    title: str
    duration_s: int
    license: str
    views: int
    published_at: datetime | None = None
    language: str = ""  # as declared on YouTube; often empty

    @property
    def url(self) -> str:
        return {"twitch": f"https://www.twitch.tv/videos/{self.video_id}",
                "kick": f"https://kick.com/video/{self.video_id}"}.get(
            self.platform, f"https://www.youtube.com/watch?v={self.video_id}")


@dataclass
class RightsDecision:
    allowed: bool
    reason: str
    basis: str | None = None       # set only when allowed
    attribution: str | None = None  # credit text that must accompany any clip


class RightsGate:
    def __init__(self, sources: Sources):
        self.sources = sources

    def check(self, video: Video, today: date) -> RightsDecision:
        s = self.sources
        if video.channel_id in s.creative_commons.blocked_channels:
            return RightsDecision(False, "channel is on the blocked list")

        for ch in s.channels:
            if (ch.platform, ch.channel_id) == (video.platform, video.channel_id):
                return RightsDecision(True, f"permissioned creator '{ch.name}'", basis=f"permission: {ch.basis}")

        expired = None
        # a campaign named after the streamer comes before a brand campaign that only lists the channel
        for c in sorted(s.campaigns, key=lambda c: c.channel_id.lower() not in (c.name or "").lower()):
            if (c.platform, c.channel_id) == (video.platform, video.channel_id):
                if c.active_on(today):
                    return RightsDecision(True, f"active campaign '{c.name}'", basis=f"campaign: {c.basis}")
                expired = c.name

        if s.creative_commons.enabled and video.platform == "youtube" and video.license == CC_LICENSE:
            credit = (f'Source: "{video.title}" by {video.channel_title} ({video.url}), '
                      "used under a Creative Commons Attribution (CC BY) licence. Edited into a short clip.")
            return RightsDecision(True, "Creative Commons licence verified via YouTube API",
                                  basis="Creative Commons Attribution (CC BY), per-video licence check",
                                  attribution=credit)

        if expired:
            return RightsDecision(False, f"campaign '{expired}' is not active today")
        return RightsDecision(False, "not in sources.yaml, no active campaign, and not Creative Commons licensed")
