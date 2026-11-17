"""Campaign discovery: new marketplace listings -> filtered, ranked -> one Telegram message to the owner.

Joining stays with the owner (it means accepting the campaign's terms on the marketplace). The owner
replies "/joined <code>" to the bot and the campaign goes live here: discovery starts pulling footage
from its creator's channels on the next run. "/skip <code>" hides it. Every step is in decision_log.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.marketplace import Listing, Marketplace, MarketplaceError
from autoclip.integrations.telegram import Notifier
from autoclip.models import Campaign
from autoclip.services.portfolio import GAMBLING
from autoclip.services.settings_store import get_setting, set_setting

MODULE = "campaign_feed"
OUR_PLATFORMS = {"instagram", "youtube"}
MIN_BUDGET_LEFT = 300.0
# Campaigns the pipeline cannot do without a person: filming yourself, or posting with a given song.
NEEDS_A_PERSON = re.compile(r"\bugc\b|film yourself|face ?cam|your own face|use (the|this) (song|sound|audio)", re.I)
NOT_FOR_US_CATEGORIES = {"music"}
# On top of portfolio.GAMBLING: poker rooms, parlays and "<n>bet"-style bookmaker brands
BETTING = re.compile(r"\b(poker|parlay\w*|casino\w*|\w*bet|bookie|wager\w*)\b", re.I)
NICHE_BY_CATEGORY = {"streamer": "irl_streamer", "gaming": "irl_streamer", "entertainment": "irl_streamer",
                     "youtuber": "irl_streamer", "sports": "irl_streamer", "comedy podcast": "podcast_business",
                     "podcast": "podcast_business", "business": "podcast_business",
                     "personal brand": "podcast_business"}


def short_code(listing_id: str) -> str:
    return listing_id.replace("-", "")[:6]


def _norm(title: str) -> str:
    t = re.sub(r"\((reach|official)\)", "", (title or "").lower())
    return re.sub(r"[^a-z0-9]+", "", t)


@dataclass
class Verdict:
    ok: bool
    reason: str
    score: float = 0.0
    alert: bool = False  # worth a message: automatic footage, or a priority topic (GTA 6)


def _matches(text: str, keywords: list[str]) -> bool:
    low = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return any(re.sub(r"\s+", " ", k.lower()) in low for k in keywords if k)


def judge(item: Listing, priority_keywords: list[str]) -> Verdict:
    text = f"{item.title} {item.description}"
    ours = OUR_PLATFORMS & set(item.platforms)
    if not ours:
        return Verdict(False, f"pays only on {', '.join(item.platforms) or 'no listed platform'}")
    if GAMBLING.search(f"{text} {item.category}") or BETTING.search(f"{text} {item.category}"):
        return Verdict(False, "gambling content")
    if item.category.lower() in NOT_FOR_US_CATEGORIES:
        return Verdict(False, f"{item.category} campaign (needs its song on the post)")
    if NEEDS_A_PERSON.search(text):
        return Verdict(False, "needs filmed or hand-made content (UGC)")
    if item.budget_left is not None and item.budget_left < MIN_BUDGET_LEFT:
        return Verdict(False, f"only ${item.budget_left:,.0f} budget left")
    score = (item.rate_per_1k or 0.5) * 10 + min(item.budget_left or 0, 50000) / 5000
    low = text.lower()
    gta = any(re.sub(r"\s+", " ", k.lower()) in re.sub(r"[^a-z0-9]+", " ", low) for k in priority_keywords if k)
    if gta:
        score += 100
    auto = any(c["platform"] in ("twitch", "kick") for c in item.channels)
    if auto:
        score += 30  # stream footage: the pipeline can run it end to end
    if item.category.lower() in ("streamer", "gaming"):
        score += 15
    if item.requires_application:
        score -= 5
    why = ("GTA 6; " if gta else "") + f"{item.category or 'uncategorised'}, {', '.join(sorted(ours))}"
    return Verdict(True, why, round(score, 2), alert=auto or gta)


def _known(session: Session, item: Listing) -> Campaign | None:
    for c in session.scalars(select(Campaign)):
        if item.id in (c.url or "") or c.external_id == item.id or _norm(c.name) == _norm(item.title):
            if "/c/campaigns/" not in (c.url or ""):
                # campaigns.yaml often only had the homepage: store the real campaign page, so submission
                # alerts link straight to where the post link is submitted
                log_decision(session, MODULE, "linked", f"{c.name}: campaign page is {item.url} (was {c.url})",
                             subject=item.url)
                c.url = item.url
            return c
    return None


def _footage(item: Listing) -> str:
    streams = [f"{c['platform']}/{c['channel_id']}" for c in item.channels if c["platform"] in ("twitch", "kick")]
    yt = [f"YouTube @{c['channel_id']}" for c in item.channels if c["platform"] == "youtube_handle"]
    if streams:
        return "streams: " + ", ".join(streams[:3]) + " (fully automatic)"
    if yt:
        return ", ".join(yt[:2]) + " (I need the channel id after you join)"
    return "no channel link in the listing: footage must be added by hand"


def _geo_warning(item: Listing) -> str:
    m = re.search(r"\[pays \$0 for views from: ([^\]]+)\]", item.description or "")
    return f"\nWarning: pays $0 for views from {m.group(1)}" if m else ""


def scan(session: Session, market: Marketplace, notifier: Notifier, now: datetime | None = None, *,
         priority_keywords: list[str] | None = None, max_alerts: int = 6) -> str:
    """New relevant listings become 'open' campaigns (not used for clipping until joined) and are sent
    to the owner in one message. Each listing is judged once."""
    now = now or datetime.now(timezone.utc)
    seen = set(get_setting(session, "campaign_feed.seen", []) or [])
    try:
        items = market.listings(seen)
    except MarketplaceError as e:
        log_decision(session, MODULE, "paused", f"could not read listings: {e}")
        return f"paused: {e}"
    seen |= set(getattr(market, "inactive", []) or [])  # ended or paused campaigns: never opened again
    fresh, quiet, gta_skipped = [], 0, []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        if _known(session, item):
            continue
        v = judge(item, priority_keywords or [])
        if not v.ok:
            log_decision(session, MODULE, "skipped", f"{item.title}: {v.reason}", subject=item.url)
            if _matches(f"{item.title} {item.description}", priority_keywords or []):
                gta_skipped.append((item, v.reason))
            continue
        c = Campaign(marketplace=market.name, external_id=item.id, name=item.title[:255], url=item.url,
                     creator=item.brand or None, niche=NICHE_BY_CATEGORY.get(item.category.lower()),
                     rate_per_1k=item.rate_per_1k, budget_total=item.budget_total, budget_remaining=item.budget_left,
                     allowed_platforms=item.platforms, rules_text=item.description or item.title,
                     source_channels=[ch for ch in item.channels if ch["platform"] in ("twitch", "kick")] or None,
                     status="open", ev_score=v.score, ev_reason=v.reason)
        session.add(c)
        log_decision(session, MODULE, "listed", f"{item.title}: {v.reason} (score {v.score})", subject=item.url)
        if v.alert:
            fresh.append((v.score, item))
        else:
            quiet += 1
    set_setting(session, "campaign_feed.seen", sorted(seen))
    fresh.sort(key=lambda x: -x[0])
    # one message per campaign, so each gets its own Joined / Skip buttons
    for _, it in fresh[:max_alerts]:
        rate = f"${it.rate_per_1k:.2f}/1k views" if it.rate_per_1k else it.kind or "rate unknown"
        left = f"${it.budget_left:,.0f} left" if it.budget_left is not None else "budget unknown"
        code = short_code(it.id)
        notifier.send(f"New campaign worth joining [{code}]\n{it.title}\n{rate}, {left}, {', '.join(it.platforms)}"
                      f"{' (application needed)' if it.requires_application else ''}\n"
                      f"Footage: {_footage(it)}{_geo_warning(it)}\n\nJoin on the site, then tap Joined.",
                      [[{"text": "Open campaign", "url": it.url}],
                       [{"text": "Joined", "callback_data": f"join:{code}"},
                        {"text": "Skip", "callback_data": f"skip:{code}"}]])
    for it, why in gta_skipped:  # a priority topic is never skipped silently: the owner sees why
        notifier.send(f"GTA campaign seen but skipped: {it.title}\nWhy: {why}\n{it.url}")
    if len(fresh) > max_alerts or quiet:
        extra = [f"{len(fresh) - max_alerts} more automatic campaign(s)"] if len(fresh) > max_alerts else []
        extra += [f"{quiet} campaign(s) that need footage added by hand"] if quiet else []
        notifier.send("Also new: " + " and ".join(extra) + ". Send /campaigns to see them.")
    session.flush()
    return f"{len(items)} listings, {len(fresh)} new worth joining, {quiet} need footage by hand"


# ------------------------------------------------------------------ owner replies on Telegram
def _by_code(session: Session, code: str) -> Campaign | None:
    code = code.strip().lower()
    if len(code) < 4:
        return None
    for c in session.scalars(select(Campaign).where(Campaign.status.in_(("open", "skipped")))):
        if c.external_id.replace("-", "").lower().startswith(code):
            return c
    return None


def handle_command(session: Session, text: str) -> str | None:
    """'/joined CODE', '/skip CODE', '/submitted N', '/campaigns'. Returns the reply, or None if not a command."""
    from autoclip.services import submissions
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd, arg = parts[0].lower().split("@")[0], (parts[1] if len(parts) > 1 else "")
    if cmd in ("/joined", "/skip"):
        c = _by_code(session, arg)
        if c is None:
            return f"No open campaign with code '{arg}'. Send /campaigns to list them."
        if cmd == "/skip":
            c.status = "skipped"
            log_decision(session, MODULE, "skipped", f"owner skipped {c.name} on Telegram", subject=c.url)
            return f"Skipped: {c.name}"
        c.status = "joined"
        log_decision(session, MODULE, "joined", f"owner joined {c.name} on the marketplace and confirmed on Telegram",
                     subject=c.url)
        src = ", ".join(f"{s['platform']}/{s['channel_id']}" for s in c.source_channels or [])
        return (f"Live: {c.name}. " + (f"Pulling footage from {src} on the next discovery run (within 20 min)."
                                       if src else "No stream channel in the listing: send me the footage source "
                                                   "or add it to config/campaigns.yaml."))
    if cmd == "/submitted":
        try:
            submissions.set_status(session, int(arg), "submitted")
            return f"Submission #{arg} marked submitted."
        except (ValueError, KeyError):
            return f"No submission #{arg}."
    if cmd == "/campaigns":
        rows = session.scalars(select(Campaign).where(Campaign.status == "open", Campaign.ev_score.is_not(None))
                               .order_by(Campaign.ev_score.desc()).limit(12)).all()
        return "\n".join(f"[{short_code(c.external_id)}] {c.name} ({c.ev_reason})" for c in rows) or "No open campaigns."
    return None


def _handle_tap(session: Session, bot, cb: dict, chat_id: str) -> int:
    """A button under one of our alerts: Submitted (sub:ID), Joined (join:CODE) or Skip (skip:CODE)."""
    msg = cb.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != str(chat_id):
        return 0  # only the owner's chat is obeyed
    kind, _, arg = (cb.get("data") or "").partition(":")
    if kind == "sub":
        from autoclip.models import Submission
        from autoclip.services import submissions
        sub = session.get(Submission, int(arg)) if arg.isdigit() else None
        if sub is None:
            bot.answer(cb["id"], "That submission no longer exists.")
            return 0
        if sub.status != "pending":
            bot.answer(cb["id"], f"Already marked {sub.status}.")
        else:
            submissions.set_status(session, sub.id, "submitted")
            bot.answer(cb["id"], "Marked submitted.")
        reply = f"Submission #{sub.id}: {sub.status}"
    elif kind == "ytl" and arg.isdigit():
        set_setting(session, AWAITING_LINK, int(arg))
        bot.answer(cb["id"], "Paste the Short's link")
        prompt = getattr(bot, "prompt", None) or bot.send
        prompt(f"Paste the YouTube Shorts link for post #{arg} as a reply to this message.")
        return 1  # the video keeps its button until the link arrives
    elif kind in ("join", "skip"):
        reply = handle_command(session, f"/{'joined' if kind == 'join' else 'skip'} {arg}") or "Unknown campaign."
        bot.answer(cb["id"], reply[:190])
    else:
        bot.answer(cb["id"], "Unknown button.")
        return 0
    if msg.get("message_id") is not None:  # replace the buttons with the outcome, so the alert reads as done
        bot.edit(str(chat_id), msg["message_id"], f"{msg.get('text', '')}\n\nDone: {reply}")
    return 1


AWAITING_LINK = "telegram.awaiting_youtube_link"
POST_REF = re.compile(r"post #(\d+)")


def _link_target(session: Session, msg: dict) -> int | None:
    """Which manual YouTube post a pasted link belongs to: the post named in the message it replies to,
    else the post whose button was tapped last, else the only (or oldest) one still waiting."""
    from autoclip.models import Post
    quoted = POST_REF.search(((msg.get("reply_to_message") or {}).get("text")
                              or (msg.get("reply_to_message") or {}).get("caption") or ""))
    if quoted:
        return int(quoted.group(1))
    tapped = get_setting(session, AWAITING_LINK)
    if tapped:
        return int(tapped)
    waiting = session.scalars(select(Post.id).where(Post.platform == "youtube", Post.status == "manual")
                              .order_by(Post.scheduled_at)).first()
    return waiting


def handle_link(session: Session, msg: dict, notifier, auto_submit_keywords: tuple[str, ...] = ()) -> str | None:
    """A YouTube link pasted in the chat records the matching manual upload. None if no link."""
    from autoclip.services.publishing import YOUTUBE_LINK, record_manual_post
    text = msg.get("text") or ""
    if not YOUTUBE_LINK.search(text):
        return None
    target = _link_target(session, msg)
    if target is None:
        return "Got a YouTube link, but no post is waiting for a manual upload."
    reply = record_manual_post(session, target, text, notifier, auto_submit_keywords)
    if get_setting(session, AWAITING_LINK) == target:
        set_setting(session, AWAITING_LINK, None)
    return reply


def poll_commands(session: Session, bot, chat_id: str, auto_submit_keywords: tuple[str, ...] = ()) -> int:
    """Reads new bot messages; only the owner's chat is obeyed."""
    offset = int(get_setting(session, "telegram.offset", 0) or 0)
    handled = 0
    for upd in bot.updates(offset):
        offset = max(offset, upd["update_id"] + 1)
        if upd.get("callback_query"):
            handled += _handle_tap(session, bot, upd["callback_query"], chat_id)
            continue
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            continue
        reply = handle_command(session, msg.get("text", "")) or handle_link(session, msg, bot, auto_submit_keywords)
        if reply:
            bot.send(reply)
            handled += 1
    set_setting(session, "telegram.offset", offset)
    return handled
