"""Loads and validates sources.yaml, the rights allowlist."""
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml


@dataclass
class ChannelSource:
    platform: str
    channel_id: str
    name: str
    basis: str
    starts: date | None = None  # campaigns only
    ends: date | None = None
    profile: str | None = None  # niche profile in config/profiles
    campaign_id: int | None = None  # set for campaigns that come from the database

    def active_on(self, day: date) -> bool:
        return (self.starts is None or self.starts <= day) and (self.ends is None or day <= self.ends)


@dataclass
class CreativeCommonsRule:
    enabled: bool = False
    queries: list[str] = field(default_factory=list)
    max_searches_per_run: int = 3
    max_age_days: int = 30
    min_views: int = 0
    languages: list[str] = field(default_factory=list)  # empty = any language
    blocked_channels: list[str] = field(default_factory=list)


@dataclass
class Sources:
    min_duration_minutes: int = 8
    creative_commons: CreativeCommonsRule = field(default_factory=CreativeCommonsRule)
    channels: list[ChannelSource] = field(default_factory=list)
    campaigns: list[ChannelSource] = field(default_factory=list)


def _channel(entry: dict, kind: str) -> ChannelSource:
    for key in ("platform", "channel_id", "basis"):
        if not str(entry.get(key) or "").strip():
            raise ValueError(f"sources.yaml: every {kind} needs '{key}' (rights must have a stated basis)")
    starts, ends = entry.get("starts"), entry.get("ends")
    if kind == "campaign" and not (isinstance(starts, date) and isinstance(ends, date)):
        raise ValueError("sources.yaml: every campaign needs 'starts' and 'ends' dates (YYYY-MM-DD)")
    return ChannelSource(platform=str(entry["platform"]).lower(), channel_id=str(entry["channel_id"]),
                         name=str(entry.get("name") or entry["channel_id"]), basis=str(entry["basis"]),
                         starts=starts, ends=ends, profile=entry.get("profile"))


def parse_sources(raw: dict | None) -> Sources:
    raw = raw or {}
    cc = raw.get("creative_commons") or {}
    return Sources(
        min_duration_minutes=int(raw.get("min_duration_minutes", 8)),
        creative_commons=CreativeCommonsRule(
            enabled=bool(cc.get("enabled", False)),
            queries=[str(q) for q in cc.get("queries") or []],
            max_searches_per_run=int(cc.get("max_searches_per_run", 3)),
            max_age_days=int(cc.get("max_age_days", 30)),
            min_views=int(cc.get("min_views", 0)),
            languages=[str(x).lower() for x in cc.get("languages") or []],
            blocked_channels=[str(c) for c in cc.get("blocked_channels") or []],
        ),
        channels=[_channel(e, "channel") for e in raw.get("channels") or []],
        campaigns=[_channel(e, "campaign") for e in raw.get("campaigns") or []],
    )


def load_sources(path: str | Path) -> Sources:
    """A missing file means an empty allowlist: nothing is permitted."""
    p = Path(path)
    if not p.exists():
        return Sources()
    return parse_sources(yaml.safe_load(p.read_text(encoding="utf-8")))
