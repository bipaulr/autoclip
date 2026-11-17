"""FastAPI backend for the dashboard, plus /health and a plain /status page.

Reads are open on localhost (the port is bound to 127.0.0.1 only). Every change requires the
admin password from ADMIN_PASSWORD in .env, sent as the X-Admin-Password header.
"""
import hmac
import html
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from autoclip.config import get_settings
from autoclip import progress
from autoclip.db import get_session
from autoclip.decisions import log_decision
from autoclip.models import (Account, BanditArm, Campaign, Clip, Cost, DecisionLog, Highlight, Metric, Post,
                             PromptVersion, SourceVideo, Submission)
from autoclip.pipeline.decide import Bandit, make_windows, parse_away, reslot
from autoclip.services import submissions as subs
from autoclip.services.analytics import earnings, estimated_earnings
from autoclip.services.settings_store import get_setting, set_setting
from autoclip.services.tuner import rollback
from autoclip.sources import load_sources

app = FastAPI(title="autoclip")
_media = Path(get_settings().media_dir)
_media.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=str(_media)), name="files")


def require_admin(x_admin_password: str = Header(default="")) -> None:
    expected = get_settings().admin_password
    if not expected:
        raise HTTPException(403, "Set ADMIN_PASSWORD in .env to enable changes from the dashboard")
    if not hmac.compare_digest(x_admin_password.encode(), expected.encode()):
        raise HTTPException(401, "Wrong admin password")


def file_url(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return "/files/" + Path(path).resolve().relative_to(_media.resolve()).as_posix()
    except ValueError:
        return None


def _iso(t):
    return t.isoformat() if t else None


def _404():
    raise HTTPException(404, "not found")


# ---------------------------------------------------------------- health and status
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/db")
def health_db(session: Session = Depends(get_session)) -> dict:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/overview")
def overview(session: Session = Depends(get_session)) -> dict:
    s = get_settings()
    day = datetime.now(timezone.utc) - timedelta(days=1)

    def count(q):
        return session.scalar(q) or 0

    return {
        "kill_switch": s.kill_switch or bool(get_setting(session, "kill_switch", False)),
        "youtube_audit_passed": s.youtube_audit_passed,
        "videos_in_progress": count(select(func.count(SourceVideo.id)).where(SourceVideo.status.in_(
            ("discovered", "ingested", "transcribed", "signals", "highlighted", "gated")))),
        "clips_24h": count(select(func.count(Clip.id)).where(Clip.created_at >= day)),
        "posted_24h": count(select(func.count(Post.id)).where(Post.posted_at >= day)),
        "scheduled": count(select(func.count(Post.id)).where(Post.status == "scheduled")),
        "held": count(select(func.count(Post.id)).where(Post.status == "held")),
        "pending_submissions": count(select(func.count(Submission.id)).where(Submission.status == "pending")),
        "failures_24h": count(select(func.count(DecisionLog.id)).where(DecisionLog.created_at >= day,
                                                                       DecisionLog.action == "failed")),
        "spend_24h": float(session.scalar(select(func.coalesce(func.sum(Cost.usd), 0.0))
                                          .where(Cost.created_at >= day)) or 0),
        "estimated_earnings": estimated_earnings(session),
        "render_threshold": get_setting(session, "render_threshold", 0),
    }


@app.get("/status", response_class=HTMLResponse)
def status_page(session: Session = Depends(get_session)) -> str:
    o = overview(session)
    rows = "".join(f"<tr><td>{html.escape(k.replace('_', ' '))}</td><td>{html.escape(str(v))}</td></tr>"
                   for k, v in o.items())
    log = "".join(f"<tr><td>{d['at'][:16]}</td><td>{html.escape(d['module'])}</td><td>{html.escape(d['action'])}</td>"
                  f"<td>{html.escape(d['reason'][:160])}</td></tr>" for d in decisions(30, session))
    return (f"<!doctype html><meta charset=utf-8><title>autoclip status</title>"
            f"<style>body{{font:14px system-ui;margin:24px;max-width:1000px}}td{{padding:4px 10px;"
            f"border-bottom:1px solid #ddd;vertical-align:top}}</style><h1>autoclip status</h1>"
            f"<table>{rows}</table><h2>Latest decisions</h2><table>{log}</table>")


# ---------------------------------------------------------------- live progress
_redis = None


def _redis_client():
    global _redis
    if _redis is None:
        import redis
        _redis = redis.Redis.from_url(get_settings().redis_url, socket_timeout=2)
    return _redis


@app.get("/api/live")
def live(session: Session = Depends(get_session)) -> dict:
    """What the pipeline is doing right now, what is queued next, and what just finished."""
    paused = set(get_setting(session, "paused_sources", []))
    active = ("ingested", "transcribed", "signals", "highlighted", "gated")
    queue = [v for v in session.scalars(select(SourceVideo).where(SourceVideo.status.in_(("discovered",) + active))
                                        .order_by((SourceVideo.status == "discovered"),
                                                  SourceVideo.rank_score.desc().nulls_last()).limit(60))
             if f"{v.platform}:{v.channel_id}" not in paused]
    try:
        lock = bool(_redis_client().exists("autoclip:pipeline"))
        current = progress.read(_redis_client())
    except Exception:  # noqa: BLE001
        lock, current = False, None
    day = datetime.now(timezone.utc) - timedelta(days=1)
    recent = session.execute(select(Clip, Highlight).join(Highlight, Highlight.id == Clip.highlight_id)
                             .order_by(Clip.id.desc()).limit(8)).all()
    upcoming = session.execute(select(Post, Account, Highlight).join(Account, Account.id == Post.account_id)
                               .join(Clip, Clip.id == Post.clip_id).join(Highlight, Highlight.id == Clip.highlight_id)
                               .where(Post.status == "scheduled").order_by(Post.scheduled_at).limit(6)).all()
    errors = session.scalars(select(DecisionLog).where(DecisionLog.action.in_(("failed", "paused")),
                                                       DecisionLog.created_at >= day)
                             .order_by(DecisionLog.id.desc()).limit(6)).all()
    return {
        "processing": lock, "current": current,
        "queue_total": len(queue),
        "queue": [{"platform": v.platform, "video_id": v.video_id, "title": v.title, "channel": v.channel_title,
                   "minutes": v.duration_s // 60, "status": v.status, "rank": v.rank_score} for v in queue[:8]],
        "recent_clips": [{"id": c.id, "title": h.title, "status": c.status, "created_at": _iso(c.created_at),
                          "preview": file_url(c.path)} for c, h in recent],
        "upcoming_posts": [{"id": p.id, "platform": p.platform, "account": a.handle, "title": h.title,
                            "scheduled_at": _iso(p.scheduled_at)} for p, a, h in upcoming],
        "errors": [{"at": _iso(d.created_at), "module": d.module, "action": d.action, "subject": d.subject,
                    "reason": d.reason} for d in errors],
    }


# ---------------------------------------------------------------- reads
@app.get("/decisions")
@app.get("/api/decisions")
def decisions(limit: int = 50, session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(DecisionLog).order_by(DecisionLog.id.desc()).limit(min(limit, 500))).all()
    return [{"id": r.id, "at": r.created_at.isoformat(), "module": r.module, "action": r.action,
             "subject": r.subject, "reason": r.reason} for r in rows]


@app.get("/sources/videos")
def source_videos(status: str = "discovered", limit: int = 50, session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(SourceVideo).where(SourceVideo.status == status)
                           .order_by(SourceVideo.views.desc()).limit(min(limit, 500))).all()
    return [{"video_id": r.video_id, "title": r.title, "channel": r.channel_title, "minutes": r.duration_s // 60,
             "views": r.views, "language": r.language, "rights_basis": r.rights_basis,
             "url": f"https://www.youtube.com/watch?v={r.video_id}"} for r in rows]


@app.get("/highlights")
def highlights(limit: int = 50, session: Session = Depends(get_session)) -> list[dict]:
    rows = session.execute(select(Highlight, SourceVideo).join(SourceVideo, SourceVideo.id == Highlight.source_video_id)
                           .order_by(Highlight.score.desc()).limit(min(limit, 500))).all()
    return [{"id": h.id, "title": h.title, "score": h.score, "start_s": h.start_s, "end_s": h.end_s,
             "seconds": round(h.end_s - h.start_s), "reason": h.reason, "status": h.status, "llm": h.llm,
             "video": v.title, "channel": v.channel_title} for h, v in rows]


@app.get("/api/sources")
def sources_view(session: Session = Depends(get_session)) -> dict:
    s = load_sources(get_settings().sources_file)
    return {"channels": [{"platform": c.platform, "channel_id": c.channel_id, "name": c.name, "basis": c.basis,
                          "profile": c.profile} for c in s.channels],
            "creative_commons": vars(s.creative_commons),
            "paused": get_setting(session, "paused_sources", [])}


@app.get("/api/campaigns")
def campaigns(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(Campaign).order_by(Campaign.ev_score.desc().nulls_last(), Campaign.id)).all()
    return [{"id": c.id, "name": c.name, "marketplace": c.marketplace, "url": c.url, "creator": c.creator,
             "niche": c.niche, "status": c.status, "rate_per_1k": c.rate_per_1k, "budget_total": c.budget_total,
             "budget_remaining": c.budget_remaining, "allowed_platforms": c.allowed_platforms,
             "ev_score": c.ev_score, "ev_reason": c.ev_reason, "approved_in_principle": c.approved_in_principle,
             "brief": c.brief, "ends": _iso(c.ends)} for c in rows]


@app.get("/api/videos")
def videos(limit: int = 100, session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(SourceVideo).where(SourceVideo.status != "rejected")
                           .order_by(SourceVideo.id.desc()).limit(min(limit, 500))).all()
    out = []
    for v in rows:
        n = session.scalar(select(func.count(Highlight.id)).where(Highlight.source_video_id == v.id)) or 0
        last = session.scalars(select(DecisionLog).where(DecisionLog.subject == f"{v.platform}:{v.video_id}")
                               .order_by(DecisionLog.id.desc())).first()
        out.append({"id": v.id, "platform": v.platform, "video_id": v.video_id, "title": v.title,
                    "channel": v.channel_title, "status": v.status, "rank": v.rank_score, "highlights": n,
                    "rights_basis": v.rights_basis, "last": last.reason if last else None,
                    "minutes": v.duration_s // 60})
    return out


@app.get("/api/clips")
def clips(limit: int = 60, session: Session = Depends(get_session)) -> list[dict]:
    """Only clips that are scheduled to go out, soonest first, with where and when each one posts."""
    rows = session.execute(select(Post, Account, Clip, Highlight, SourceVideo)
                           .join(Account, Account.id == Post.account_id)
                           .join(Clip, Clip.id == Post.clip_id).join(Highlight, Highlight.id == Clip.highlight_id)
                           .join(SourceVideo, SourceVideo.id == Highlight.source_video_id)
                           .where(Post.status == "scheduled").order_by(Post.scheduled_at)).all()
    out: dict[int, dict] = {}
    for p, a, c, h, v in rows:
        item = out.get(c.id)
        if item is None:
            if len(out) >= min(limit, 300):
                continue
            item = out[c.id] = {
                "highlight_id": h.id, "clip_id": c.id, "title": h.title, "hook": h.hook_headline,
                "context_line": h.context_line, "score": h.score, "scores": h.scores, "reason": h.reason,
                "status": "scheduled", "gate_reason": None, "video": v.title, "channel": v.channel_title,
                "start_s": h.start_s, "end_s": h.end_s, "duration_s": c.duration_s, "preview": file_url(c.path),
                "thumbnail": file_url(c.thumbnail_path), "style_preset": c.style_preset, "layout": c.layout,
                "package": c.package, "posts": []}
        item["posts"].append({"platform": p.platform, "account": a.handle,
                              "scheduled_at": p.scheduled_at.isoformat() if p.scheduled_at else None})
    return list(out.values())


@app.get("/api/accounts")
def accounts(session: Session = Depends(get_session)) -> list[dict]:
    return [{"id": a.id, "platform": a.platform, "handle": a.handle, "niche": a.niche, "style_preset": a.style_preset,
             "daily_cap": a.daily_cap, "active": a.active, "paused_reason": a.paused_reason,
             "auto_approve_score": a.auto_approve_score, "followers": a.followers}
            for a in session.scalars(select(Account).order_by(Account.id))]


@app.get("/api/posts")
def posts(days: int = 14, session: Session = Depends(get_session)) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(select(Post, Account, Clip, Highlight).join(Account, Account.id == Post.account_id)
                           .join(Clip, Clip.id == Post.clip_id).join(Highlight, Highlight.id == Clip.highlight_id)
                           .where(Post.scheduled_at >= since).order_by(Post.scheduled_at)).all()
    return [{"id": p.id, "platform": p.platform, "account": a.handle, "title": h.title, "status": p.status,
             "reason": p.status_reason, "scheduled_at": _iso(p.scheduled_at), "posted_at": _iso(p.posted_at),
             "url": p.url, "arms": p.arms, "reward": p.reward, "preview": file_url(c.path)} for p, a, c, h in rows]


@app.get("/api/submissions")
def submissions_list(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.execute(select(Submission, Post, Campaign).join(Post, Post.id == Submission.post_id)
                           .join(Campaign, Campaign.id == Submission.campaign_id)
                           .order_by(Submission.id.desc())).all()
    return [{"id": s.id, "status": s.status, "deadline": _iso(s.deadline), "post_url": p.url,
             "campaign": c.name, "submit_url": c.url} for s, p, c in rows]


@app.get("/api/analytics")
def analytics_view(session: Session = Depends(get_session)) -> dict:
    latest = select(Metric.post_id, func.max(Metric.id).label("mid")).group_by(Metric.post_id).subquery()
    rows = session.execute(select(Post, Metric, Account, Highlight, Campaign)
                           .join(latest, latest.c.post_id == Post.id).join(Metric, Metric.id == latest.c.mid)
                           .join(Account, Account.id == Post.account_id).join(Clip, Clip.id == Post.clip_id)
                           .join(Highlight, Highlight.id == Clip.highlight_id)
                           .outerjoin(Campaign, Campaign.id == Clip.campaign_id)).all()
    per_clip, per_account, per_campaign = [], {}, {}
    for p, m, a, h, camp in rows:
        e = earnings(m.approved_views, camp.rate_per_1k if camp else None)
        per_clip.append({"post_id": p.id, "title": h.title, "account": a.handle, "views": m.views, "likes": m.likes,
                         "shares": m.shares, "avg_view_pct": m.avg_view_pct, "reward": p.reward,
                         "approved_views": m.approved_views, "earnings": e, "bucket": m.age_bucket})
        acc = per_account.setdefault(a.handle, {"views": 0, "posts": 0})
        acc["views"] += m.views
        acc["posts"] += 1
        if camp:
            cp = per_campaign.setdefault(camp.name, {"views": 0, "approved_views": 0, "earnings": 0.0})
            cp["views"] += m.views
            cp["approved_views"] += m.approved_views or 0
            cp["earnings"] = round(cp["earnings"] + e, 2)
    return {"clips": per_clip, "accounts": per_account, "campaigns": per_campaign}


@app.get("/api/bandit")
def bandit(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.execute(select(BanditArm, Account).join(Account, Account.id == BanditArm.account_id)).all()
    out = [{"account": a.handle, "dimension": b.dimension, "value": b.value, "pulls": b.pulls,
            "mean": round(b.alpha / (b.alpha + b.beta), 3)} for b, a in rows]
    return sorted(out, key=lambda r: (r["account"], r["dimension"], -r["mean"]))


@app.get("/api/prompts")
def prompts(session: Session = Depends(get_session)) -> list[dict]:
    return [{"id": p.id, "name": p.name, "version": p.version, "status": p.status, "ab_share": p.ab_share,
             "notes": p.notes, "examples": len(p.examples or []), "created_at": _iso(p.created_at)}
            for p in session.scalars(select(PromptVersion).order_by(PromptVersion.name, PromptVersion.version.desc()))]


@app.get("/api/money")
def money(days: int = 30, session: Session = Depends(get_session)) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    spend: dict[str, float] = {}
    for c in session.scalars(select(Cost).where(Cost.created_at >= since)):
        d = c.created_at.date().isoformat()
        spend[d] = spend.get(d, 0.0) + c.usd
    earn: dict[str, float] = {}
    rows = session.execute(select(Metric, Campaign).join(Post, Post.id == Metric.post_id)
                           .join(Clip, Clip.id == Post.clip_id).join(Campaign, Campaign.id == Clip.campaign_id)
                           .where(Metric.approved_views.is_not(None), Metric.taken_at >= since)).all()
    for m, camp in rows:
        d = m.taken_at.date().isoformat()
        earn[d] = earn.get(d, 0.0) + earnings(m.approved_views, camp.rate_per_1k)
    out = []
    for i in range(days, -1, -1):
        d = (datetime.now(timezone.utc) - timedelta(days=i)).date().isoformat()
        out.append({"date": d, "spend": round(spend.get(d, 0.0), 2), "earnings": round(earn.get(d, 0.0), 2)})
    return out


# ---------------------------------------------------------------- changes (admin password required)
class Toggle(BaseModel):
    on: bool


class CampaignPatch(BaseModel):
    status: str | None = None
    approved_in_principle: bool | None = None


class AccountPatch(BaseModel):
    active: bool | None = None
    auto_approve_score: int | None = None
    daily_cap: int | None = None


class StatusBody(BaseModel):
    status: str


class Approved(BaseModel):
    approved_views: int


class RollbackBody(BaseModel):
    name: str
    version: int


@app.post("/api/kill-switch", dependencies=[Depends(require_admin)])
def kill_switch(body: Toggle, session: Session = Depends(get_session)) -> dict:
    set_setting(session, "kill_switch", body.on)
    log_decision(session, "dashboard", "kill_switch_on" if body.on else "kill_switch_off", "set from the dashboard")
    session.commit()
    return {"kill_switch": body.on}


@app.patch("/api/campaigns/{cid}", dependencies=[Depends(require_admin)])
def patch_campaign(cid: int, body: CampaignPatch, session: Session = Depends(get_session)) -> dict:
    c = session.get(Campaign, cid) or _404()
    if body.status:
        if body.status not in ("open", "joined", "paused", "ended"):
            raise HTTPException(422, "status must be open, joined, paused or ended")
        c.status = body.status
        log_decision(session, "dashboard", body.status, f"campaign marked {body.status} by you", subject=c.url)
    if body.approved_in_principle is not None:
        c.approved_in_principle = body.approved_in_principle
    session.commit()
    return {"ok": True}


@app.patch("/api/accounts/{aid}", dependencies=[Depends(require_admin)])
def patch_account(aid: int, body: AccountPatch, session: Session = Depends(get_session)) -> dict:
    a = session.get(Account, aid) or _404()
    if body.active is not None:
        a.active = body.active
        a.paused_reason = None if body.active else "paused from the dashboard"
        log_decision(session, "dashboard", "resumed" if body.active else "paused", "set from the dashboard",
                     subject=f"account:{a.handle}")
    if body.auto_approve_score is not None:
        a.auto_approve_score = max(0, min(100, body.auto_approve_score))
    if body.daily_cap is not None:
        a.daily_cap = max(0, min(10, body.daily_cap))
    session.commit()
    return {"ok": True}


@app.post("/api/posts/{pid}/approve", dependencies=[Depends(require_admin)])
def approve_post(pid: int, session: Session = Depends(get_session)) -> dict:
    p = session.get(Post, pid) or _404()
    if p.status != "held":
        raise HTTPException(409, f"post is {p.status}, not held")
    p.status, p.status_reason = "scheduled", None
    log_decision(session, "dashboard", "approved", "approved by you", subject=f"post:{pid}")
    session.flush()
    reslot(session, session.get(Account, p.account_id), Bandit(session), datetime.now(timezone.utc),
           away=parse_away(get_settings().owner_away_ist), quiet=make_windows(get_settings().no_post_ist, get_settings().morning_window_ist,
                                    get_settings().morning_campaigns, get_settings().auto_window_ist,
                                    get_settings().auto_submit_campaigns, get_settings().campaign_daily_caps))
    session.commit()
    return {"ok": True}


@app.post("/api/submissions/{sid}", dependencies=[Depends(require_admin)])
def submission_status(sid: int, body: StatusBody, session: Session = Depends(get_session)) -> dict:
    try:
        subs.set_status(session, sid, body.status)
    except KeyError:
        _404()
    except ValueError as e:
        raise HTTPException(422, str(e))
    session.commit()
    return {"ok": True}


@app.post("/api/posts/{pid}/approved-views", dependencies=[Depends(require_admin)])
def approved_views(pid: int, body: Approved, session: Session = Depends(get_session)) -> dict:
    m = session.scalars(select(Metric).where(Metric.post_id == pid).order_by(Metric.id.desc())).first() or _404()
    m.approved_views = max(0, body.approved_views)
    session.commit()
    return {"ok": True}


@app.post("/api/prompts/rollback", dependencies=[Depends(require_admin)])
def prompt_rollback(body: RollbackBody, session: Session = Depends(get_session)) -> dict:
    rollback(session, body.name, body.version)
    session.commit()
    return {"ok": True}
