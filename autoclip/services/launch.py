"""GTA 6 launch mode (LAUNCH_MODE_UNTIL, e.g. 2026-11-26): speed over the normal posting rhythm.

While it is on:
- live clips post as soon as they are rendered: the earliest time that keeps LAUNCH_GAP_MINUTES from the
  account's other posts, at any hour (no quiet window, no bandit hour bucket);
- every account may post up to max(its own cap, LAUNCH_DAILY_CAP) in any 24 hours;
- a live clip that could not go out within LAUNCH_STALE_HOURS is not scheduled (or expires at publish
  time): by then every other clipper has posted the moment;
- YouTube posts that the API would force private (audit pending) are sent to Telegram for a manual upload.
It switches itself off after the date."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.models import Post

TAKEN = ("scheduled", "posted", "held", "manual")


@dataclass
class LaunchMode:
    until: date | None = None
    gap: timedelta = timedelta(minutes=45)
    daily_cap: int = 12
    stale: timedelta = timedelta(hours=3)

    def active(self, now: datetime) -> bool:
        return self.until is not None and now.date() <= self.until

    @classmethod
    def from_settings(cls, s) -> "LaunchMode":
        until = None
        if getattr(s, "launch_mode_until", ""):
            until = date.fromisoformat(str(s.launch_mode_until).strip())
        return cls(until, timedelta(minutes=s.launch_gap_minutes), s.launch_daily_cap,
                   timedelta(hours=s.launch_stale_hours))


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def fast_slot(session: Session, account, now: datetime, mode: LaunchMode,
              ignore: set[int] | frozenset = frozenset()) -> datetime | None:
    """Earliest time >= now + 2 min that is at least `gap` from every other post of this account and keeps
    the rolling 24-hour count under the cap. None when that time would already be stale."""
    # `ignore`: posts that yield to this one (GTA focus: non-GTA posts not yet published)
    taken = sorted(_utc(t) for pid, t in session.execute(select(Post.id, Post.scheduled_at).where(
        Post.account_id == account.id, Post.status.in_(TAKEN), Post.scheduled_at >= now - timedelta(days=1)))
        if pid not in ignore)
    cap = max(account.daily_cap or 0, mode.daily_cap)
    t = now + timedelta(minutes=2)
    while t - now <= mode.stale:
        clash = next((x for x in taken if abs(t - x) < mode.gap), None)
        if clash is not None:
            t = clash + mode.gap
            continue
        if sum(1 for x in taken if t - timedelta(days=1) < x <= t) >= cap:
            t = min(x for x in taken if x > t - timedelta(days=1)) + timedelta(days=1)
            continue
        return t
    return None
