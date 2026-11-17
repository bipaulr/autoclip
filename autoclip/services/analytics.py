"""Analytics (plan prompt 14): metrics at 1h, 6h, 24h, 72h and 7d after each post, reward and earnings.

Campaign 'approved views' are only visible inside the marketplace, so they are entered from the
dashboard (or left empty); estimated earnings use them when present.
"""
import math
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.models import Account, Campaign, Clip, Metric, Post
from autoclip.services.settings_store import get_setting, set_setting

MODULE = "analytics"
BUCKETS = {"1h": timedelta(hours=1), "6h": timedelta(hours=6), "24h": timedelta(hours=24),
           "72h": timedelta(hours=72), "7d": timedelta(days=7)}
FEE = 0.10


class MetricsError(Exception):
    pass


class MetricsSource(Protocol):
    def fetch(self, post: Post, clip_seconds: float) -> dict: ...


class MockMetrics:
    def __init__(self, values: dict[int, dict] | None = None, error: str | None = None):
        self.values, self.error, self.calls = values or {}, error, []

    def fetch(self, post: Post, clip_seconds: float) -> dict:
        self.calls.append(post.id)
        if self.error:
            raise MetricsError(self.error)
        return dict(self.values.get(post.id, {"views": 0}))


class InstagramInsights:
    METRICS = "views,likes,comments,shares,saved,ig_reels_avg_watch_time"

    def __init__(self, token: str, host: str = "graph.facebook.com", client: httpx.Client | None = None):
        self.token, self.base, self.client = token, f"https://{host}", client or httpx.Client(timeout=20)

    def fetch(self, post: Post, clip_seconds: float) -> dict:
        try:
            r = self.client.get(f"{self.base}/{post.external_id}/insights",
                                params={"metric": self.METRICS, "access_token": self.token})
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MetricsError(f"Instagram insights failed ({type(e).__name__})") from None
        if r.status_code != 200:
            raise MetricsError(f"Instagram insights HTTP {r.status_code}: {(body.get('error') or {}).get('message', '')}")
        v = {m["name"]: (m.get("values") or [{}])[0].get("value", 0) for m in body.get("data", [])}
        watch_s = float(v.get("ig_reels_avg_watch_time", 0)) / 1000
        return {"views": int(v.get("views", 0)), "likes": int(v.get("likes", 0)), "comments": int(v.get("comments", 0)),
                "shares": int(v.get("shares", 0)), "saves": int(v.get("saved", 0)),
                "avg_view_pct": min(100.0, 100 * watch_s / clip_seconds) if clip_seconds else None, "raw": v}


class YouTubeAnalytics:
    """Views, likes and comments come from the Data API's live counters (1 quota unit a call). The
    Analytics reports run 1-2 days behind, so they read 0 for every new Short; they are asked only for
    what the counters lack (shares, average view percentage) and never override the live counts."""
    DATA = "https://www.googleapis.com/youtube/v3/videos"
    REPORTS = "https://youtubeanalytics.googleapis.com/v2/reports"

    def __init__(self, token_provider, client: httpx.Client | None = None):
        self.token_provider, self.client = token_provider, client or httpx.Client(timeout=20)

    def _get(self, url: str, params: dict, what: str) -> dict:
        try:
            r = self.client.get(url, params=params, headers={"Authorization": f"Bearer {self.token_provider()}"})
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MetricsError(f"{what} failed ({type(e).__name__})") from None
        if r.status_code != 200:
            raise MetricsError(f"{what} HTTP {r.status_code}")
        return body

    def fetch(self, post: Post, clip_seconds: float) -> dict:
        body = self._get(self.DATA, {"part": "statistics", "id": post.external_id}, "YouTube Data API")
        items = body.get("items") or []
        if not items:
            raise MetricsError(f"YouTube video {post.external_id} not found (deleted or private?)")
        st = items[0].get("statistics") or {}
        out = {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
               "comments": int(st.get("commentCount", 0)), "shares": 0, "avg_view_pct": None,
               "raw": {"statistics": st}}
        start = (post.posted_at or datetime.now(timezone.utc)).date().isoformat()
        try:
            rep = self._get(self.REPORTS, {"ids": "channel==MINE", "metrics": "shares,averageViewPercentage",
                                           "filters": f"video=={post.external_id}", "startDate": start,
                                           "endDate": date.today().isoformat()}, "YouTube Analytics")
        except MetricsError:
            return out  # the live counts are what matter; the report fills in later readings
        rows = rep.get("rows") or []
        if rows:  # an empty report means the numbers are not in yet, not zero
            out["shares"] = int(rows[0][0] or 0)
            out["avg_view_pct"] = float(rows[0][1]) if rows[0][1] is not None else None
            out["raw"]["report"] = rows[0]
        return out


def reward(m: dict, followers: int) -> float:
    """0.5*log(views) + 0.3*avg_view_pct + 0.2*share_rate, normalised by account size."""
    views = max(0, int(m.get("views") or 0))
    pct = float(m.get("avg_view_pct") or 0) / 100
    share_rate = (int(m.get("shares") or 0) / views) if views else 0.0
    raw = 0.5 * math.log10(1 + views) + 0.3 * pct + 0.2 * min(1.0, share_rate * 20)
    return round(raw / math.log10(10 + max(0, followers)), 4)


def earnings(approved_views: int | None, rate_per_1k: float | None) -> float:
    if not approved_views or not rate_per_1k:
        return 0.0
    return round(approved_views / 1000 * rate_per_1k * (1 - FEE), 2)


EXCLUDED_KEY = "analytics.excluded_posts"


def excluded_posts(session: Session) -> set[int]:
    return set(get_setting(session, EXCLUDED_KEY, []))


def exclude_post(session: Session, post_id: int, reason: str) -> int:
    """Takes a post out of every view count, earnings figure and learning signal: its metric rows are
    deleted, its reward cleared, and no new readings are collected."""
    set_setting(session, EXCLUDED_KEY, sorted(excluded_posts(session) | {post_id}))
    removed = session.query(Metric).filter(Metric.post_id == post_id).delete()
    post = session.get(Post, post_id)
    if post is not None:
        post.reward = None
    log_decision(session, "analytics", "excluded", f"post #{post_id} left out of analytics: {reason}",
                 subject=post.url if post else None, data={"metrics_removed": removed})
    return removed


def collect(session: Session, sources: dict[str, MetricsSource], now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    taken = 0
    posts = session.scalars(select(Post).where(Post.status == "posted", Post.posted_at.is_not(None)))
    skip = excluded_posts(session)
    for post in posts:
        if post.id in skip:
            continue
        posted = post.posted_at.replace(tzinfo=post.posted_at.tzinfo or timezone.utc)
        order = list(BUCKETS)
        have = set(session.scalars(select(Metric.age_bucket).where(Metric.post_id == post.id)))
        newest = max((order.index(b) for b in have), default=-1)
        # Only buckets newer than the latest reading: older ones are never filled with today's numbers.
        due = [b for i, (b, age) in enumerate(BUCKETS.items()) if now - posted >= age and i > newest]
        if not due or post.platform not in sources:
            continue
        clip = session.get(Clip, post.clip_id)
        try:
            m = sources[post.platform].fetch(post, clip.duration_s)
        except MetricsError as e:
            log_decision(session, MODULE, "paused", f"metrics unavailable: {e}", subject=f"post:{post.id}")
            continue
        bucket = due[-1]  # the newest due bucket; skipped older ones are not back-filled with today's numbers
        session.add(Metric(post_id=post.id, age_bucket=bucket, taken_at=now, views=m.get("views", 0),
                           likes=m.get("likes", 0), comments=m.get("comments", 0), shares=m.get("shares", 0),
                           saves=m.get("saves", 0), avg_view_pct=m.get("avg_view_pct"), raw=m.get("raw")))
        if bucket in ("72h", "7d"):
            account = session.get(Account, post.account_id)
            post.reward = reward(m, account.followers)
        taken += 1
    session.flush()
    return taken


def estimated_earnings(session: Session) -> float:
    total = 0.0
    rows = session.execute(select(Metric.approved_views, Campaign.rate_per_1k)
                           .join(Post, Post.id == Metric.post_id).join(Clip, Clip.id == Post.clip_id)
                           .join(Campaign, Campaign.id == Clip.campaign_id)
                           .where(Metric.approved_views.is_not(None))).all()
    for approved, rate in rows:
        total += earnings(approved, rate)
    return round(total, 2)
