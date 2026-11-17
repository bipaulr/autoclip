"""Self-tuner (plan prompt 15), daily at 03:00 IST. Every change gets a plain-English changelog row
in the decision log (module 'tuner')."""
import math
import random
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.models import Clip, Highlight, Post, PromptVersion, SourceVideo, Transcript
from autoclip.pipeline.decide import Bandit
from autoclip.services.settings_store import get_setting, set_setting

MODULE = "tuner"
AB_DAYS = 3
AB_SHARE = 0.2


def _aware(t: datetime) -> datetime:
    return t.replace(tzinfo=t.tzinfo or timezone.utc)


def _rewarded(session: Session):
    return session.execute(select(Post, Clip, Highlight, SourceVideo)
                           .join(Clip, Clip.id == Post.clip_id).join(Highlight, Highlight.id == Clip.highlight_id)
                           .join(SourceVideo, SourceVideo.id == Highlight.source_video_id)
                           .where(Post.reward.is_not(None))).all()


def update_bandit(session: Session, bandit: Bandit) -> int:
    """Each rewarded post updates its arms once: success = beat its account's median reward."""
    rows = _rewarded(session)
    by_account: dict[int, list[float]] = {}
    for post, *_ in rows:
        by_account.setdefault(post.account_id, []).append(post.reward)
    done = set(get_setting(session, "tuner.bandit_posts", []))
    n = 0
    for post, *_ in rows:
        if post.id in done or not post.arms:
            continue
        success = post.reward > statistics.median(by_account[post.account_id])
        bandit.update(post.account_id, post.arms, success)
        done.add(post.id)
        n += 1
    set_setting(session, "tuner.bandit_posts", sorted(done))
    if n:
        log_decision(session, MODULE, "bandit_updated", f"updated style/length/hook/hour arms from {n} new posts")
    return n


def welch_p(a: list[float], b: list[float]) -> float:
    """One-sided p-value that mean(a) > mean(b), Welch's t with a normal approximation."""
    if len(a) < 2 or len(b) < 2:
        return 1.0
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b)) or 1e-9
    t = (statistics.mean(a) - statistics.mean(b)) / se
    return 0.5 * math.erfc(t / math.sqrt(2))


def _examples(session: Session, rows, top: bool) -> list[dict]:
    rows = sorted(rows, key=lambda r: r[0].reward, reverse=top)[:8]
    out = []
    for post, clip, hl, video in rows:
        tr = session.scalars(select(Transcript).where(Transcript.source_video_id == video.id)).first()
        text = " ".join(s["t"] for s in (tr.segments if tr else []) if hl.start_s <= s["s"] < hl.end_s)
        out.append({"label": "TOP clip" if top else "WEAK clip", "excerpt": text[:300],
                    "outcome": f"reward {post.reward:.2f}, scores {hl.scores or {}}"})
    return out


def rewrite_prompt(session: Session, name: str, now: datetime) -> PromptVersion | None:
    rows = _rewarded(session)
    if len(rows) < 16 or session.scalars(select(PromptVersion).where(PromptVersion.name == name,
                                                                     PromptVersion.status == "testing")).first():
        return None
    latest = session.scalars(select(PromptVersion).where(PromptVersion.name == name)
                             .order_by(PromptVersion.version.desc())).first()
    pv = PromptVersion(name=name, version=(latest.version + 1) if latest else 1, status="testing",
                       ab_share=AB_SHARE, created_at=now,
                       examples=_examples(session, rows, True) + _examples(session, rows, False),
                       notes="few-shot rewrite from top 8 and bottom 8 clips by reward")
    session.add(pv)
    session.flush()
    log_decision(session, MODULE, "prompt_rewritten",
                 f"new highlight prompt v{pv.version} with fresh examples; testing on {int(AB_SHARE * 100)}% "
                 f"of candidates for {AB_DAYS} days")
    return pv


def judge_ab(session: Session, name: str, now: datetime) -> str | None:
    test = session.scalars(select(PromptVersion).where(PromptVersion.name == name,
                                                       PromptVersion.status == "testing")).first()
    if test is None or now - _aware(test.created_at) < timedelta(days=AB_DAYS):
        return None
    active = session.scalars(select(PromptVersion).where(PromptVersion.name == name,
                                                         PromptVersion.status == "active")).first()

    def rewards(pv_id):
        return [p.reward for p, c, h, v in _rewarded(session) if h.prompt_version_id == pv_id]

    a, b = rewards(test.id), rewards(active.id if active else None)
    p = welch_p(a, b)
    if a and b and statistics.mean(a) > statistics.mean(b) and p < 0.1:
        if active:
            active.status = "retired"
        test.status, test.ab_share = "active", 1.0
        log_decision(session, MODULE, "prompt_promoted",
                     f"prompt v{test.version} beat the current one (mean reward {statistics.mean(a):.3f} vs "
                     f"{statistics.mean(b):.3f}, p={p:.3f}); it is now active")
        return "promoted"
    test.status, test.ab_share = "rolled_back", 0.0
    log_decision(session, MODULE, "prompt_rolled_back",
                 f"prompt v{test.version} did not clearly win (p={p:.3f}, {len(a)} vs {len(b)} clips); kept the old one")
    return "rolled_back"


def adjust_threshold(session: Session, keep_share: float = 0.3) -> int | None:
    """Sets the score above which candidates get rendered, so about the top 30% pass."""
    if get_setting(session, "render_threshold_pinned", False):
        return None  # the owner set the threshold by hand and raises it gradually
    scores = sorted(session.scalars(select(Highlight.score)).all(), reverse=True)
    if len(scores) < 10:
        return None
    new = int(scores[max(0, int(len(scores) * keep_share) - 1)])
    old = get_setting(session, "render_threshold", 0)
    if new != old:
        set_setting(session, "render_threshold", new)
        log_decision(session, MODULE, "threshold_changed",
                     f"render threshold {old} -> {new} so roughly the top {int(keep_share * 100)}% of candidates render")
    return new


def pause_weak_sources(session: Session, streak: int = 10) -> list[str]:
    """Pauses a source whose last 10 clips all scored under their account's 25th percentile."""
    rows = sorted(_rewarded(session), key=lambda r: _aware(r[0].posted_at or datetime.min.replace(tzinfo=timezone.utc)))
    p25: dict[int, float] = {}
    for acc in {r[0].account_id for r in rows}:
        vals = sorted(r[0].reward for r in rows if r[0].account_id == acc)
        p25[acc] = vals[max(0, len(vals) // 4 - 1)] if len(vals) >= 4 else -1
    by_source: dict[str, list[bool]] = {}
    for post, clip, hl, video in rows:
        key = f"{video.platform}:{video.channel_id}"
        by_source.setdefault(key, []).append(post.reward < p25[post.account_id])
    paused = set(get_setting(session, "paused_sources", []))
    newly = []
    for key, weak in by_source.items():
        if key not in paused and len(weak) >= streak and all(weak[-streak:]):
            paused.add(key)
            newly.append(key)
            log_decision(session, MODULE, "source_paused",
                         f"paused {key}: its last {streak} clips all scored below the account's 25th percentile")
    set_setting(session, "paused_sources", sorted(paused))
    return newly


def choose_prompt_version(session: Session, name: str, rng: random.Random | None = None) -> PromptVersion | None:
    """20% of candidates go to the version under test, the rest to the active one."""
    rng = rng or random.Random()
    test = session.scalars(select(PromptVersion).where(PromptVersion.name == name,
                                                       PromptVersion.status == "testing")).first()
    active = session.scalars(select(PromptVersion).where(PromptVersion.name == name,
                                                         PromptVersion.status == "active")).first()
    if test and rng.random() < test.ab_share:
        return test
    return active


def rollback(session: Session, name: str, version: int) -> PromptVersion:
    """Dashboard one-click rollback to an earlier version."""
    target = session.scalars(select(PromptVersion).where(PromptVersion.name == name,
                                                         PromptVersion.version == version)).one()
    for pv in session.scalars(select(PromptVersion).where(PromptVersion.name == name,
                                                          PromptVersion.status.in_(("active", "testing")))):
        pv.status = "retired"
    target.status, target.ab_share = "active", 1.0
    log_decision(session, MODULE, "prompt_rolled_back", f"manually rolled {name} back to v{version}")
    return target


def run_daily(session: Session, bandit: Bandit, profiles: list[str], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    out = {"bandit_updates": update_bandit(session, bandit), "threshold": adjust_threshold(session),
           "paused": pause_weak_sources(session), "prompts": {}}
    for name in [f"highlight:{p}" for p in profiles]:
        out["prompts"][name] = judge_ab(session, name, now) or (
            "testing new version" if rewrite_prompt(session, name, now) else "unchanged")
    return out
