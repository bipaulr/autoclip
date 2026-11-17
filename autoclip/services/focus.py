"""GTA focus mode: while GTA 6 is being streamed (or a GTA campaign is joined), everything serves GTA.

It turns on by itself when an allowlisted channel is live with Twitch's game set to Grand Theft Auto VI
(or a GTA title), or when a joined campaign is about GTA. It turns off 12 hours after the last GTA stream
was seen, if no GTA campaign is joined. While on:
- the live daemon records GTA streams first and stops a non-GTA recording to make room;
- GTA live chunks are processed first and new non-GTA VODs wait;
- GTA clips post through the fast lane and non-GTA posts are held until focus ends (then re-slotted);
- GTA captions carry #GTA6 #GTAVI.
Every switch is logged and sent to Telegram.
"""
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.models import Campaign, Clip, Highlight, Post, SourceVideo
from autoclip.services.settings_store import get_setting, set_setting

MODULE = "focus"
KEY = "focus.gta"
HOLD = timedelta(hours=12)
GTA_GAMES = ("grand theft auto vi", "grand theft auto 6", "gta vi", "gta 6")
DEFAULT_KEYWORDS = ("gta 6", "gta vi", "gta6", "gtavi", "grand theft auto 6", "grand theft auto vi")
FOCUS_HOLD_REASON = "GTA focus: non-GTA post held until GTA focus ends"
GTA_TAGS = ("#GTA6", "#GTAVI")
TITLE_PREFIX = "GTA 6 · "


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "


def is_gta_text(text: str, keywords=DEFAULT_KEYWORDS) -> bool:
    t = _norm(text)
    return any(_norm(k) in t for k in (keywords or DEFAULT_KEYWORDS) if k.strip())


def is_gta_stream(info: dict | None, keywords=DEFAULT_KEYWORDS) -> bool:
    """Twitch reports the game: trust only that (before launch many titles say "GTA 6 in 2 days" while the
    stream is something else). Without a game (Kick) the title decides."""
    info = info or {}
    game = (info.get("game") or "").lower()
    if game:
        return any(g in game for g in GTA_GAMES)
    return is_gta_text(info.get("title", ""), keywords)


def gta_campaign_joined(session: Session, now: datetime, keywords=DEFAULT_KEYWORDS) -> str | None:
    for c in session.scalars(select(Campaign).where(Campaign.status == "joined")):
        if (c.ends is None or now.date() <= c.ends) and is_gta_text(f"{c.name} {c.rules_text or ''}", keywords):
            return c.name
    return None


def _state(session: Session) -> dict:
    return dict(get_setting(session, KEY, {}) or {})


def note_gta_stream(session: Session, channel_key: str, now: datetime) -> None:
    s = _state(session)
    s.setdefault("channels", {})[channel_key] = now.isoformat()
    set_setting(session, KEY, s)


def active(session: Session, now: datetime, keywords=DEFAULT_KEYWORDS) -> bool:
    s = _state(session)
    if any(now - datetime.fromisoformat(t) < HOLD for t in (s.get("channels") or {}).values()):
        return True
    return gta_campaign_joined(session, now, keywords) is not None


def refresh(session: Session, now: datetime, notify=lambda text: None, keywords=DEFAULT_KEYWORDS) -> bool:
    """Current state; on a change it is logged and sent to Telegram once."""
    on = active(session, now, keywords)
    s = _state(session)
    if on != bool(s.get("on")):
        s["on"] = on
        set_setting(session, KEY, s)
        if on:
            why = ", ".join(k for k, t in (s.get("channels") or {}).items()
                            if now - datetime.fromisoformat(t) < HOLD) or gta_campaign_joined(session, now, keywords)
            text = (f"GTA focus mode ON ({why}). GTA streams are recorded first, GTA clips post first, "
                    "non-GTA posts and new VODs wait.")
        else:
            text = "GTA focus mode OFF: no GTA stream for 12 hours. Held non-GTA posts are being re-scheduled."
        log_decision(session, MODULE, "on" if on else "off", text)
        notify(text)
    return on


def is_gta_video(video, keywords=DEFAULT_KEYWORDS) -> bool:
    return bool(video) and is_gta_text(video.title or "", keywords)


def non_gta_post_ids(session: Session, account_id: int, keywords=DEFAULT_KEYWORDS) -> set[int]:
    """Scheduled or focus-held posts of this account whose source is not GTA (they yield to GTA clips)."""
    rows = session.execute(
        select(Post.id, SourceVideo.title).join(Clip, Clip.id == Post.clip_id)
        .join(Highlight, Highlight.id == Clip.highlight_id).join(SourceVideo, SourceVideo.id == Highlight.source_video_id)
        .where(Post.account_id == account_id, Post.status.in_(("scheduled", "held")))).all()
    return {pid for pid, title in rows if not is_gta_text(title or "", keywords)}
