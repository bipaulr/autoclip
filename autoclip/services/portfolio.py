"""Campaign portfolio (plan prompt 21): rank open campaigns by expected value.

EV = P(clip crosses the minimum payout) x expected approved views x rate x (1 - fee), adjusted for
competition and budget burn. Keeps 3 to 6 campaigns active, pauses joined ones under 15% budget,
and never considers gambling campaigns. It suggests; joining stays with the user.
"""
import math
import re
import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.models import Campaign, Clip, Highlight, Metric, Post, SourceVideo

FEE = 0.10
GAMBLING = re.compile(r"\b(casino|slots?|stake|roulette|gambl\w*|betting|sportsbook|crash game)\b", re.I)
MIN_ACTIVE, MAX_ACTIVE = 3, 6


def niche_views(session: Session, niche: str | None) -> list[int]:
    """Views at 72h of my past clips in this niche (empty until there is history)."""
    q = (select(Metric.views).join(Post, Post.id == Metric.post_id).join(Clip, Clip.id == Post.clip_id)
         .join(Highlight, Highlight.id == Clip.highlight_id).join(SourceVideo, SourceVideo.id == Highlight.source_video_id)
         .where(Metric.age_bucket == "72h"))
    if niche:
        q = q.where(SourceVideo.profile == niche)
    return list(session.scalars(q))


def expected_value(c: Campaign, history: list[int], competitors: int | None = None) -> tuple[float, str]:
    if GAMBLING.search(f"{c.name} {c.rules_text or ''}"):
        return -1.0, "gambling campaign: never joined"
    views = sorted(history) or [800, 1500, 3000, 6000, 20000]  # cautious prior for a new account
    rate = c.rate_per_1k or 1.0  # pool campaigns: assume about $1/1K effective
    min_views = (c.brief or {}).get("min_views_to_qualify") or (
        (c.min_payout or 0) / rate * 1000 if c.min_payout else 0)
    p_cross = sum(v >= min_views for v in views) / len(views) if min_views else 1.0
    if (c.brief or {}).get("min_views_to_qualify") and not c.rate_per_1k:  # pool minimum: cumulative over clips
        per_month = statistics.median(views) * 60
        p_cross = min(1.0, per_month / max(1, min_views))
    exp_views = statistics.mean(views)
    ev = p_cross * exp_views / 1000 * rate * (1 - FEE)
    if c.max_payout:
        ev = min(ev, c.max_payout * p_cross)
    comp = 1 / (1 + math.log10(1 + (competitors or 0) / 100))
    budget = 1.0
    if c.budget_total and c.budget_remaining is not None:
        budget = max(0.0, c.budget_remaining / c.budget_total)
    ev *= comp * (0.5 + 0.5 * budget)
    reason = (f"P(cross minimum) {p_cross:.0%}, expected {exp_views:,.0f} views/clip at ${rate:.2f}/1K, "
              f"budget left {budget:.0%}" + (f", ~{competitors} clippers" if competitors else ""))
    return round(ev, 2), reason


def rank(session: Session, priority: tuple[str, ...] = ()) -> list[Campaign]:
    """priority: words in campaign names (PRIORITY_CAMPAIGNS). Those campaigns take their places in the
    active set first and are never paused by the limit."""
    ranked = []
    for c in session.scalars(select(Campaign).where(Campaign.status.in_(("open", "joined")))):
        c.ev_score, c.ev_reason = expected_value(c, niche_views(session, c.niche))
        budget_left = (c.budget_remaining / c.budget_total) if (c.budget_total and c.budget_remaining is not None) else 1
        if c.status == "joined" and budget_left < 0.15:
            c.status = "paused"
            log_decision(session, "portfolio", "paused", f"{c.name}: under 15% budget left", subject=c.url)
        ranked.append(c)
    ranked.sort(key=lambda c: -(c.ev_score or 0))
    # Only campaigns with footage sources can produce clips, so only they count toward the limit;
    # a joined campaign waiting for its footage must not push out one that can clip.
    def is_priority(c: Campaign) -> bool:
        return any(k in (c.name or "").lower() for k in priority)

    joined = sorted((c for c in ranked if c.status == "joined" and c.source_channels),
                    key=lambda c: not is_priority(c))  # stable: EV order kept within each group
    if len(joined) < MIN_ACTIVE:
        for c in [c for c in ranked if c.status == "open" and (c.ev_score or 0) > 0][:MIN_ACTIVE - len(joined)]:
            log_decision(session, "portfolio", "suggest_join", f"{c.name}: {c.ev_reason} (EV ${c.ev_score}/clip)",
                         subject=c.url)
    # A campaign with clips already made and waiting to post stays joined: pausing it would block them.
    waiting = set(session.scalars(
        select(SourceVideo.campaign_id).join(Highlight, Highlight.source_video_id == SourceVideo.id)
        .join(Clip, Clip.highlight_id == Highlight.id).join(Post, Post.clip_id == Clip.id)
        .where(Post.status.in_(("scheduled", "held", "pending", "pending_approval")))))
    for c in joined[MAX_ACTIVE:]:
        if c.id in waiting or is_priority(c):
            continue
        c.status = "paused"
        log_decision(session, "portfolio", "paused", f"{c.name}: more than {MAX_ACTIVE} active, lowest EV",
                     subject=c.url)
    session.flush()
    return ranked
