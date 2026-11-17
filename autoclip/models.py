from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from autoclip.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DecisionLog(Base):
    """Every decision the system makes (chosen, skipped, posted, paused, refreshed...) with a reason."""

    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    module: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Campaign(Base):
    """A paid clipping campaign (Content Rewards, Vyro, Clipster, Reach...)."""

    __tablename__ = "campaign"
    __table_args__ = (UniqueConstraint("marketplace", "external_id", name="uq_campaign_marketplace_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    creator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    niche: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rate_per_1k: Mapped[float | None] = mapped_column(Float, nullable=True)
    budget_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    budget_remaining: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_payout: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_payout: Mapped[float | None] = mapped_column(Float, nullable=True)
    allowed_platforms: Mapped[list | None] = mapped_column(JSON, nullable=True)
    rules_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # none_as_null: clearing the brief must be SQL NULL, which is what the brief engine looks for
    brief: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)  # parsed by the brief engine
    source_channels: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{platform, channel_id}]
    source_videos: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{platform, video_id, title, minutes}]
    starts: Mapped[date | None] = mapped_column(Date, nullable=True)
    ends: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | joined | paused | ended
    approved_in_principle: Mapped[bool] = mapped_column(Boolean, default=False)
    ev_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ev_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SourceVideo(Base):
    """A long video found by discovery, with the rights verdict it received."""

    __tablename__ = "source_video"
    __table_args__ = (UniqueConstraint("platform", "video_id", name="uq_source_video_platform_video"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    video_id: Mapped[str] = mapped_column(String(64))
    channel_id: Mapped[str] = mapped_column(String(64))
    channel_title: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    duration_s: Mapped[int] = mapped_column(Integer)
    license: Mapped[str] = mapped_column(String(32))
    views: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    rights_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaign.id"), nullable=True)
    profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    media_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rank_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)


class Transcript(Base):
    __tablename__ = "transcript"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_video_id: Mapped[int] = mapped_column(ForeignKey("source_video.id"), unique=True)
    language: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    segments: Mapped[list] = mapped_column(JSON)  # [{s, e, t, w: [[word, start, end], ...]}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Highlight(Base):
    """A candidate moment. status: candidate -> blocked | rendered."""

    __tablename__ = "highlight"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_video_id: Mapped[int] = mapped_column(ForeignKey("source_video.id"), index=True)
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    title: Mapped[str] = mapped_column(String(255))
    hook_headline: Mapped[str] = mapped_column(String(120), default="")
    context_line: Mapped[str | None] = mapped_column(String(200), nullable=True)
    hook_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    scores: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    score: Mapped[int] = mapped_column(Integer)  # final blended score 0-100
    llm: Mapped[str] = mapped_column(String(64))
    prompt_version_id: Mapped[int | None] = mapped_column(ForeignKey("prompt_version.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    gate_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Clip(Base):
    """A rendered 9:16 clip of one highlight."""

    __tablename__ = "clip"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    highlight_id: Mapped[int] = mapped_column(ForeignKey("highlight.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaign.id"), nullable=True)
    path: Mapped[str] = mapped_column(Text)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    layout: Mapped[str] = mapped_column(String(32))
    style_preset: Mapped[str] = mapped_column(String(32))
    duration_s: Mapped[float] = mapped_column(Float)
    phash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    package: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # per-platform title/description/tags
    status: Mapped[str] = mapped_column(String(16), index=True)  # rendered | blocked | scheduled | posted
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Account(Base):
    __tablename__ = "account"
    __table_args__ = (UniqueConstraint("platform", "handle", name="uq_account_platform_handle"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))  # youtube | instagram
    handle: Mapped[str] = mapped_column(String(128))
    niche: Mapped[str] = mapped_column(String(128))  # comma-separated if the account serves several
    style_preset: Mapped[str] = mapped_column(String(32))
    daily_cap: Mapped[int] = mapped_column(Integer, default=3)
    followers: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_approve_score: Mapped[int] = mapped_column(Integer, default=60)


class Post(Base):
    __tablename__ = "post"
    __table_args__ = (UniqueConstraint("clip_id", "platform", name="uq_post_clip_platform"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clip.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), index=True)
    platform: Mapped[str] = mapped_column(String(16))
    arms: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # bandit choices for this post
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    privacy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)  # scheduled | held | posted | failed
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    responses: Mapped[list | None] = mapped_column(JSON, nullable=True)


class Metric(Base):
    __tablename__ = "metric"
    __table_args__ = (UniqueConstraint("post_id", "age_bucket", name="uq_metric_post_bucket"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), index=True)
    age_bucket: Mapped[str] = mapped_column(String(8))  # 1h | 6h | 24h | 72h | 7d
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    saves: Mapped[int] = mapped_column(Integer, default=0)
    avg_view_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    approved_views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Submission(Base):
    """Campaign submission queue: a posted campaign clip waiting for the user to submit its link."""

    __tablename__ = "submission"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), unique=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaign.id"), index=True)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), index=True)  # pending | submitted | approved | rejected | paid
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BanditArm(Base):
    __tablename__ = "bandit_arm"
    __table_args__ = (UniqueConstraint("account_id", "dimension", "value", name="uq_bandit_arm"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), index=True)
    dimension: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(String(64))
    alpha: Mapped[float] = mapped_column(Float, default=1.0)
    beta: Mapped[float] = mapped_column(Float, default=1.0)
    pulls: Mapped[int] = mapped_column(Integer, default=0)


class PromptVersion(Base):
    __tablename__ = "prompt_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), index=True)  # e.g. highlight:irl_streamer
    version: Mapped[int] = mapped_column(Integer)
    examples: Mapped[list] = mapped_column(JSON)  # few-shot examples
    status: Mapped[str] = mapped_column(String(16))  # active | testing | retired | rolled_back
    ab_share: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Cost(Base):
    __tablename__ = "cost"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # llm | transcription | compute
    units: Mapped[float] = mapped_column(Float)
    usd: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class LLMCache(Base):
    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the prompt
    llm: Mapped[str] = mapped_column(String(64))
    response: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChannelState(Base):
    """Last known live state of an allowlisted channel, for stream-ended triggers."""

    __tablename__ = "channel_state"
    __table_args__ = (UniqueConstraint("platform", "channel_id", name="uq_channel_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    channel_id: Mapped[str] = mapped_column(String(64))
    live: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Setting(Base):
    """Small key-value store for values the tuner adjusts (thresholds, paused sources)."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
