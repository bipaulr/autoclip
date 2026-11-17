"""Keeps the Instagram access token alive. Runs daily; acts only when expiry is near."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.instagram import InstagramAuthAPI, InstagramAuthError
from autoclip.integrations.telegram import Notifier
from autoclip.token_store import TokenStore

MODULE = "ig_token"
TOKEN_KEY = "IG_ACCESS_TOKEN"
FIX_HINT = "To fix it, run: py -3.12 setup_wizard.py --only 4"


@dataclass
class RefreshResult:
    status: str  # refreshed | skipped | failed
    reason: str


def refresh_instagram_token(auth: InstagramAuthAPI, store: TokenStore, notifier: Notifier,
                            session: Session, *, now: datetime | None = None,
                            threshold_days: int = 15) -> RefreshResult:
    now = now or datetime.now(timezone.utc)

    def done(status: str, reason: str, alert: bool = False) -> RefreshResult:
        log_decision(session, MODULE, status, reason, subject=TOKEN_KEY)
        if alert:
            notifier.send(f"autoclip: Instagram token needs attention - {reason}. {FIX_HINT}")
        return RefreshResult(status, reason)

    token = store.get(TOKEN_KEY)
    if not token:
        return done("skipped", "no Instagram token configured")
    try:
        info = auth.inspect(token)
    except InstagramAuthError as e:
        return done("failed", f"could not inspect token: {e}", alert=True)
    if not info.is_valid:
        return done("failed", "token is invalid or expired", alert=True)
    if info.never_expires:
        return done("skipped", "token never expires")

    left = "unknown"
    if info.expires_at:
        days = (info.expires_at - now).days
        left = f"{days} days"
        if days > threshold_days:
            return done("skipped", f"{left} left, above the {threshold_days}-day threshold")

    try:
        new_token, new_exp = auth.refresh(token)
        never = False
        if new_exp is None:
            new_info = auth.inspect(new_token)
            new_exp, never = new_info.expires_at, new_info.never_expires
    except InstagramAuthError as e:
        return done("failed", f"refresh failed with {left} left: {e}", alert=True)

    extended = never or (new_exp is not None and (
        info.expires_at is None or new_exp > info.expires_at + timedelta(days=1)))
    if not extended:
        return done("failed", f"refresh did not extend the expiry ({left} left)", alert=True)

    store.set(**{TOKEN_KEY: new_token})
    return done("refreshed", "new token never expires" if never else f"new expiry {new_exp:%Y-%m-%d}")
