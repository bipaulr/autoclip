"""Decision engine (plan prompt 13).

For each passed clip: which accounts (one per platform), which style preset and caption density
(chosen before rendering), and which time slot. Thompson sampling with Beta posteriors over whether
a post beats its account's median reward. Explore 30% of the time for an account's first 50 posts,
decaying to 10%. Constraints: daily cap per account, at least 2 hours between posts, +-25 minutes of
jitter, one account per platform per clip, and the campaign's allowed platforms.
"""
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoclip.models import Account, BanditArm, Post
from autoclip.pipeline.style import PRESETS

DIMENSIONS = {
    "style_preset": list(PRESETS),
    "length_bucket": ["15-25", "25-40", "40-60"],
    "hook_type": ["question", "bold_claim", "reaction", "story", "number", "conflict", "reveal"],
    # Hours in US Eastern time: campaigns want a US/UK audience, so post for US evenings.
    "post_hour_bucket": ["07-10", "11-14", "15-18", "19-22", "23-02"],
    "caption_density": ["2", "3"],
}
# A head start for a new arm we have outside evidence for: the story style copies how the top-viewed
# campaign clips are edited, so it is tried more often until our own results confirm or reject it.
PRIORS = {("style_preset", "story_pop"): (3.0, 1.0)}
BUCKET_START_ET = {"07-10": 7, "11-14": 11, "15-18": 15, "19-22": 19, "23-02": 23}
ET_OFFSET = timedelta(hours=-5)  # EST; DST is ignored, a one-hour shift is inside the bucket width
MIN_GAP = timedelta(hours=2)
JITTER_MIN = 25
IST_OFFSET = timedelta(hours=5, minutes=30)
SUBMIT_MARGIN = timedelta(minutes=45)  # campaign links must be submitted within ~30 min of posting


def parse_away(spec: str) -> tuple[int, int] | None:
    """'02:00-10:00' (IST, when the owner is asleep) -> (start, end) minutes of the IST day; '' -> None."""
    if not spec or not spec.strip():
        return None
    a, b = (p.strip() for p in spec.split("-"))
    to_min = lambda s: int(s.split(":")[0]) * 60 + int(s.split(":")[1] if ":" in s else 0)  # noqa: E731
    return to_min(a), to_min(b)


def owner_away(t: datetime, away: tuple[int, int] | None) -> bool:
    if away is None:
        return False
    ist = t + IST_OFFSET
    m = ist.hour * 60 + ist.minute
    start, end = away
    return start <= m < end if start <= end else (m >= start or m < end)


@dataclass(frozen=True)
class Windows:
    """Where in the IST day posts may go. `default` is the no-post window for most clips; clips from
    campaigns whose name contains a `morning_keywords` entry post only inside `morning` instead."""
    default: tuple[int, int] | None = None
    morning: tuple[int, int] | None = None
    morning_keywords: tuple[str, ...] = ()
    # Campaigns whose links are submitted automatically may also post inside `auto` (US prime time,
    # while the owner sleeps): no one needs to be awake for the 30-minute submission window.
    auto: tuple[int, int] | None = None
    auto_keywords: tuple[str, ...] = ()
    # (keyword, posts per day per account): campaigns that only get a few slots a day
    caps: tuple[tuple[str, int], ...] = ()

    def cap_for(self, campaign_name: str | None) -> tuple[str, int] | None:
        name = (campaign_name or "").lower()
        return next(((k, n) for k, n in self.caps if name and k in name), None)

    def auto_for(self, campaign_name: str | None) -> tuple[int, int] | None:
        name = (campaign_name or "").lower()
        return self.auto if self.auto and name and any(k == "*" or k in name for k in self.auto_keywords) else None

    def quiet_for(self, campaign_name: str | None) -> tuple[int, int] | None:
        name = (campaign_name or "").lower()
        if self.morning and name and any(k in name for k in self.morning_keywords):
            return self.morning[1], self.morning[0]  # everything outside the morning window
        return self.default


def _words(s: str) -> tuple[str, ...]:
    return tuple(k.strip().lower() for k in (s or "").split(",") if k.strip())


def parse_caps(spec: str) -> tuple[tuple[str, int], ...]:
    """'stream house:1, bobby:2' -> (('stream house', 1), ('bobby', 2))"""
    out = []
    for part in (spec or "").split(","):
        name, _, n = part.rpartition(":")
        if name.strip() and n.strip().isdigit():
            out.append((name.strip().lower(), int(n)))
    return tuple(out)


def make_windows(no_post: str, morning: str = "", morning_campaigns: str = "", auto: str = "",
                 auto_campaigns: str = "", campaign_caps: str = "") -> Windows:
    return Windows(parse_away(no_post), parse_away(morning), _words(morning_campaigns),
                   parse_away(auto), _words(auto_campaigns), parse_caps(campaign_caps))


def needs_owner_ok(t: datetime, away: tuple[int, int] | None) -> bool:
    """A campaign post needs the owner awake from posting until the submission window closes."""
    return not owner_away(t, away) and not owner_away(t + SUBMIT_MARGIN, away)


def length_bucket(seconds: float) -> str:
    return "15-25" if seconds < 25 else "25-40" if seconds < 40 else "40-60"


def explore_rate(posts_so_far: int) -> float:
    if posts_so_far < 50:
        return 0.30
    return max(0.10, 0.30 - (posts_so_far - 50) * 0.004)  # reaches 10% after 100 posts


class Bandit:
    def __init__(self, session: Session, rng: random.Random | None = None):
        self.session, self.rng = session, rng or random.Random()

    def arm(self, account_id: int, dimension: str, value: str) -> BanditArm:
        a = self.session.scalars(select(BanditArm).where(BanditArm.account_id == account_id,
                                                         BanditArm.dimension == dimension,
                                                         BanditArm.value == value)).first()
        if a is None:
            alpha, beta = PRIORS.get((dimension, value), (1.0, 1.0))
            a = BanditArm(account_id=account_id, dimension=dimension, value=value, alpha=alpha, beta=beta, pulls=0)
            self.session.add(a)
            self.session.flush()
        return a

    def choose(self, account_id: int, dimension: str, options: list[str] | None = None,
               posts_so_far: int = 0) -> tuple[str, bool]:
        """Returns (value, explored)."""
        options = options or DIMENSIONS[dimension]
        if self.rng.random() < explore_rate(posts_so_far):
            return self.rng.choice(options), True
        draws = {o: self.rng.betavariate(*self._ab(account_id, dimension, o)) for o in options}
        return max(draws, key=draws.get), False

    def _ab(self, account_id, dimension, value):
        a = self.arm(account_id, dimension, value)
        return a.alpha, a.beta

    def mean(self, account_id: int, dimension: str, value: str) -> float:
        a, b = self._ab(account_id, dimension, value)
        return a / (a + b)

    def update(self, account_id: int, arms: dict, success: bool) -> None:
        for dim, val in arms.items():
            if dim in DIMENSIONS:
                a = self.arm(account_id, dim, str(val))
                a.alpha += 1.0 if success else 0.0
                a.beta += 0.0 if success else 1.0
                a.pulls += 1


@dataclass
class ClipPlan:
    accounts: list[Account]
    preset: str
    caption_density: int
    arms: dict


def plan_clip(session: Session, *, niche: str | None, allowed_platforms: list[str] | None, length_s: float,
              hook_type: str | None, bandit: Bandit) -> ClipPlan | None:
    """Picks one active account per platform in the clip's niche, and the render choices."""
    q = select(Account).where(Account.active.is_(True))
    chosen: dict[str, Account] = {}
    for acc in session.scalars(q.order_by(Account.id)):
        # An account may serve several niches, written "irl_streamer,podcast_business".
        if niche and niche not in [n.strip() for n in acc.niche.split(",")]:
            continue
        if allowed_platforms and acc.platform not in allowed_platforms:
            continue
        chosen.setdefault(acc.platform, acc)  # never more than one account per platform
    if not chosen:
        return None
    lead = next(iter(chosen.values()))
    n = session.scalar(select(func.count(Post.id)).where(Post.account_id == lead.id)) or 0
    preset, _ = bandit.choose(lead.id, "style_preset", posts_so_far=n)
    density, _ = bandit.choose(lead.id, "caption_density", posts_so_far=n)
    arms = {"style_preset": preset, "caption_density": density, "length_bucket": length_bucket(length_s)}
    if hook_type:
        arms["hook_type"] = hook_type
    return ClipPlan(list(chosen.values()), preset, int(density), arms)


def _bucket_reachable(bucket: str, away: tuple[int, int] | None) -> bool:
    base = datetime(2026, 1, 5, BUCKET_START_ET[bucket], tzinfo=timezone.utc) - ET_OFFSET
    return any(needs_owner_ok(base + timedelta(minutes=m), away) for m in range(0, 4 * 60, 20))


def _bucket_touches(bucket: str, window: tuple[int, int] | None) -> bool:
    if window is None:
        return False
    base = datetime(2026, 1, 5, BUCKET_START_ET[bucket], tzinfo=timezone.utc) - ET_OFFSET
    return any(owner_away(base + timedelta(minutes=m), window) for m in range(0, 4 * 60, 20))


def next_slot(session: Session, account: Account, bandit: Bandit, now: datetime,
              rng: random.Random | None = None, *, campaign: bool = False,
              away: tuple[int, int] | None = None,
              quiet: "tuple[int, int] | Windows | None" = None,
              campaign_name: str | None = None) -> tuple[datetime, str] | None:
    """Earliest slot in a bandit-chosen hour bucket that respects the cap and the 2-hour gap.

    Campaign posts (campaign=True) also avoid the owner's away hours, because their links must be
    submitted by hand within about 30 minutes of posting."""
    rng = rng or random.Random()
    auto, cap = None, None
    if isinstance(quiet, Windows):
        auto = quiet.auto_for(campaign_name) if campaign else None
        cap = quiet.cap_for(campaign_name)
        quiet = quiet.quiet_for(campaign_name)
    n = session.scalar(select(func.count(Post.id)).where(Post.account_id == account.id)) or 0
    check_owner = campaign and away is not None
    options = [b for b in DIMENSIONS["post_hour_bucket"]
               if not check_owner or _bucket_reachable(b, away) or _bucket_touches(b, auto)]
    if not options:
        return None
    bucket, _ = bandit.choose(account.id, "post_hour_bucket", options, posts_so_far=n)
    taken = sorted(p.replace(tzinfo=p.tzinfo or timezone.utc) for p in session.scalars(
        select(Post.scheduled_at).where(Post.account_id == account.id,
                                        # held posts count too: approving them later must not overbook a day
                                        Post.status.in_(("scheduled", "posted", "held")),
                                        Post.scheduled_at >= now - timedelta(days=1))))
    for day in range(0, 7):
        et_day = (now + ET_OFFSET).date() + timedelta(days=day)
        same_day = [t for t in taken if (t + ET_OFFSET).date() == et_day]
        if len(same_day) >= account.daily_cap:
            continue
        if cap and _campaign_posts_on(session, account, cap[0], et_day, now) >= cap[1]:
            continue  # this campaign has had its slots on this account that day
        # The bandit's bucket first; when it is full that day, any other allowed bucket the same day,
        # so a high daily cap can actually be used.
        for b in [bucket] + [o for o in options if o != bucket]:
            base = datetime(et_day.year, et_day.month, et_day.day, BUCKET_START_ET[b], tzinfo=timezone.utc) - ET_OFFSET
            for offset_min in range(0, 4 * 60, 20):
                t = base + timedelta(minutes=offset_min + rng.randint(-JITTER_MIN, JITTER_MIN))
                if t < now + timedelta(minutes=2):
                    continue
                in_auto = owner_away(t, auto)  # inside the auto-submit window (owner_away = "t is inside")
                if check_owner and not needs_owner_ok(t, away) and not in_auto:
                    continue
                if owner_away(t, quiet) and not in_auto:  # IST hours with no posting at all
                    continue
                if all(abs(t - x) >= MIN_GAP for x in taken):
                    return t, b
    return None


def _campaign_posts_on(session: Session, account: Account, keyword: str, et_day, now: datetime) -> int:
    from autoclip.models import Campaign, Clip
    rows = session.execute(select(Post.scheduled_at, Campaign.name).join(Clip, Clip.id == Post.clip_id)
                           .join(Campaign, Campaign.id == Clip.campaign_id)
                           .where(Post.account_id == account.id, Post.status.in_(("scheduled", "posted", "held")),
                                  Post.scheduled_at >= now - timedelta(days=1))).all()
    return sum(1 for t, name in rows if keyword in (name or "").lower()
               and (t.replace(tzinfo=t.tzinfo or timezone.utc) + ET_OFFSET).date() == et_day)


def reslot(session: Session, account: Account, bandit: Bandit, now: datetime,
           rng: random.Random | None = None, away: tuple[int, int] | None = None,
           quiet: "tuple[int, int] | Windows | None" = None) -> int:
    """Re-spaces an account's upcoming scheduled posts so the daily cap and 2-hour gap hold,
    keeping campaign posts out of the owner's away hours."""
    from autoclip.models import Campaign, Clip
    upcoming = list(session.scalars(select(Post).where(Post.account_id == account.id, Post.status == "scheduled",
                                                       Post.scheduled_at >= now).order_by(Post.scheduled_at)))
    for p in upcoming:
        p.status = "reslotting"  # excluded while the others are placed
    session.flush()
    moved = 0
    for p in upcoming:
        clip = session.get(Clip, p.clip_id)
        slot = next_slot(session, account, bandit, now, rng,
                         campaign=bool(clip and clip.campaign_id), away=away, quiet=quiet,
                         campaign_name=camp.name if (camp := clip and clip.campaign_id and
                                                     session.get(Campaign, clip.campaign_id)) else None)
        p.status = "scheduled" if slot else "held"
        if slot:
            p.scheduled_at = slot[0]
        else:
            p.status_reason = "no free slot this week"
        moved += 1
        session.flush()
    return moved
