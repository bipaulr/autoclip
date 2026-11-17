"""Orchestrator (plan prompt 16): takes one video through every stage.

discovered -> ingested -> transcribed -> signals -> highlighted -> gated -> clips (render, style,
package) -> scheduled. Each stage saves its result before the next starts, so a crash, sleep or
reboot resumes from the last finished stage. Every decision is logged with a reason.
"""
import json
import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.progress import noop
from autoclip.integrations.downloader import DownloadError, Downloader
from autoclip.integrations.llm import LLM, LLMError
from autoclip.integrations.transcriber import Segment, TranscribeError, Transcriber
from autoclip.models import Campaign, Clip, Cost, Highlight, Post, SourceVideo, Transcript
from autoclip.pipeline import gate
from autoclip.pipeline.brief import Brief
from autoclip.pipeline.decide import Bandit, next_slot, plan_clip
from autoclip.pipeline.highlights import pick_highlights
from autoclip.pipeline.package import is_duplicate, package_text, pick_thumbnail, video_phash
from autoclip.pipeline.profiles import get_profile
from autoclip.pipeline.render import RenderError, RenderJob, render
from autoclip.pipeline.signals import Timeline, build_timeline
from autoclip.services import focus
from autoclip.services.launch import LaunchMode, fast_slot
from autoclip.services.settings_store import get_setting, set_setting
from autoclip.services.tuner import choose_prompt_version
from autoclip.sources import Sources

MODULE = "pipeline"
FINISHED = ("scheduled", "done", "rejected", "no_highlights", "blocked", "failed")


def disk_free(path: Path) -> int | None:
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(path).free
    except OSError:
        return None


def delete_source(media_dir: Path, video) -> int:
    """Deletes a finished video's downloaded files (video, audio, chat). Rendered clips live
    elsewhere (media/clips) and are kept. Returns the bytes freed."""
    folder = Path(video.media_path) if video.media_path else Path(media_dir) / f"{video.platform}_{video.video_id}"
    try:
        if Path(media_dir).resolve() not in folder.resolve().parents or not folder.is_dir():
            return 0  # only ever delete inside the media folder
    except OSError:
        return 0
    size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    shutil.rmtree(folder, ignore_errors=True)
    return size
ACTIVE = ("discovered", "ingested", "transcribed", "signals", "highlighted", "gated")
DEDUPE_WINDOW = timedelta(days=60)



def scaled_top_n(base: int, duration_s: float | None) -> int:
    """A long stream has many more good moments than a short video: base for the first hour,
    3 more for each further hour, at most 20."""
    hours = (duration_s or 0) / 3600
    return min(20, max(base, base + round(3 * (hours - 1))))


def scaled_candidates(duration_s: float | None) -> int:
    """Windows sent to the LLM: 60 for an hour or less, about 40 per hour beyond, at most 180
    (a wide net, so more moments clear the 75 render threshold)."""
    return min(180, max(60, round(40 * (duration_s or 0) / 3600)))


@dataclass
class Deps:
    downloader: Downloader
    transcriber: Transcriber
    llm: LLM
    sources: Sources
    media_dir: Path
    bandit: Bandit
    renderer: Callable = render
    detector: object = None
    allow_ai_assisted: bool = False
    owner_away: tuple[int, int] | None = None  # IST minutes; campaign posts avoid it
    quiet: object = None  # decide.Windows (or an IST no-post window)
    # "gameplay" = whole landscape frame centred over a blurred fill (nothing cropped out); None = auto
    layout: str | None = "gameplay"
    min_free_bytes: int = 15 * 10**9  # never start a download with less than this free
    min_s: int = 15
    max_s: int = 60
    top_n: int = 5
    live_top_n: int = 2  # clips from one live chunk (ids start with "live_")
    sfx_level: str = "full"  # story-style sound effects: off, light or full
    force_style: str = ""  # every new clip uses this preset (only story styles are honoured)
    live_dry_run: bool = False  # live chunks go through everything, but their posts are held, never published
    launch: LaunchMode = field(default_factory=LaunchMode)  # GTA 6 launch week: live clips post at once
    focus_on: bool = False  # GTA focus mode (services/focus.py): GTA clips use the fast lane, others yield
    priority_keywords: tuple[str, ...] = ()
    rng: random.Random = field(default_factory=random.Random)
    progress: Callable = noop  # progress(stage, detail="", pct=None) for the dashboard's Live tab


def next_video(session: Session, priority_campaigns: tuple[str, ...] = ()) -> SourceVideo | None:
    """Half-finished work first, then new videos of PRIORITY_CAMPAIGNS (words matched in campaign names),
    then the highest-ranked new video from a source that is not paused."""
    paused = set(get_setting(session, "paused_sources", []))
    first = {c.id for c in session.scalars(select(Campaign))
             if any(k in (c.name or "").lower() for k in priority_campaigns)} if priority_campaigns else set()
    rows = [v for v in session.scalars(select(SourceVideo).where(SourceVideo.status.in_(ACTIVE))
                                       .order_by((SourceVideo.status == "discovered"),
                                                 SourceVideo.rank_score.desc().nulls_last(), SourceVideo.views.desc()))
            if f"{v.platform}:{v.channel_id}" not in paused]
    rows.sort(key=lambda v: (v.status == "discovered", v.campaign_id not in first))  # stable: keeps rank order
    return rows[0] if rows else None


REGATE_KEY = "pipeline.regate_at"
REGATE_EVERY = timedelta(hours=1)


def requeue_moderation_holds(session: Session, now: datetime) -> int:
    """Highlights held only because moderation could not run go back to the gate (at most hourly).
    Their video returns to 'highlighted'; if any then pass, the source is re-downloaded to render."""
    last = get_setting(session, REGATE_KEY)
    if last and now - datetime.fromisoformat(last) < REGATE_EVERY:
        return 0
    set_setting(session, REGATE_KEY, now.isoformat())
    held = session.scalars(select(Highlight).where(Highlight.status == "held",
                                                   or_(Highlight.gate_reason.startswith(gate.MOD_UNAVAILABLE),
                                                       Highlight.gate_reason.startswith(gate.MOD_LOCAL_FLAG)))).all()
    videos = set()
    for h in held:
        h.status = "candidate"
        videos.add(h.source_video_id)
    for vid in videos:
        v = session.get(SourceVideo, vid)
        if v.status not in ACTIVE:
            v.status = "highlighted"
        log_decision(session, MODULE, "requeued", f"{sum(h.source_video_id == vid for h in held)} highlight(s) "
                     "held by moderation (it was down, or only the local model checked) go back through the gate",
                     subject=f"{v.platform}:{v.video_id}")
    session.commit()
    return len(held)


def story_style(preset: str) -> bool:
    from autoclip.pipeline.style import PRESETS
    p = PRESETS.get(preset)
    return bool(p and p.story)


def _segments(t: Transcript) -> list[Segment]:
    return [Segment.from_json(s) for s in t.segments]


def _words_between(segments: list[Segment], start: float, end: float):
    return [w for s in segments for w in s.words if w.end > start and w.start < end]


def _live_lead(video) -> float:
    """Seconds at the start of a live chunk that repeat the end of the previous chunk (0 otherwise)."""
    if not (video.video_id or "").startswith("live_") or not video.media_path:
        return 0.0
    try:
        return float(json.loads((Path(video.media_path) / "live.json").read_text(encoding="utf-8"))["lead_s"])
    except (OSError, ValueError, KeyError):
        return 0.0


def process_video(video: SourceVideo, session: Session, deps: Deps, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    subject = f"{video.platform}:{video.video_id}"
    campaign = session.get(Campaign, video.campaign_id) if video.campaign_id else None
    brief = Brief.model_validate(campaign.brief) if campaign and campaign.brief else None
    profile = get_profile(video.profile)
    lead = _live_lead(video)  # read before ingest, which may repoint media_path

    def done(action: str, reason: str, status: str | None = None, data: dict | None = None) -> str:
        if status:
            video.status = status
        log_decision(session, MODULE, action, reason, subject=subject, data=data)
        session.commit()  # each finished stage is saved, so a crash or reboot resumes from here
        if status in FINISHED:
            freed = delete_source(deps.media_dir, video)
            if freed:
                log_decision(session, MODULE, "cleaned", f"deleted the downloaded source ({freed / 1e9:.1f} GB); "
                                                         "the clips are kept", subject=subject)
                session.commit()
        return f"{action}: {reason}"

    # ---- gate, first pass: the source itself
    if video.status not in ACTIVE:
        return done("skipped", f"status '{video.status}' is not processable")
    src = gate.check_source(video, campaign, now.date())
    if not src.ok:
        return done("blocked", src.reason, status="blocked")
    if video.video_id.startswith("live_") and video.status in ("discovered", "ingested", "transcribed", "signals"):
        # Scoring costs LLM quota (16-17 Nov 2026: 416 test chunks used up the free Gemini quota overnight and
        # stalled every real clip). A dry-run chunk is never posted, and a stale one would expire unposted.
        if deps.live_dry_run:
            return done("rejected", "LIVE_DRY_RUN: live chunk recorded but not scored (test clips are never "
                                    "posted; scoring would spend the LLM quota)", status="rejected")
        started = video.published_at.replace(tzinfo=video.published_at.tzinfo or timezone.utc) \
            if video.published_at else None
        if started and now - started > deps.launch.stale:
            return done("rejected", f"live chunk is {(now - started).total_seconds() / 3600:.1f} h old, past the "
                                    f"{deps.launch.stale.total_seconds() / 3600:.0f} h freshness window: not scored",
                        status="rejected")

    # ---- ingest
    folder = deps.media_dir / f"{video.platform}_{video.video_id}"
    media = None
    has_transcript = session.scalar(select(func.count(Transcript.id)).where(Transcript.source_video_id == video.id))
    if not has_transcript or not video.signals:  # files on disk are reused, so resuming is cheap
        try:
            free = disk_free(deps.media_dir)
            if free is not None and free < deps.min_free_bytes:
                return done("paused", f"only {free / 1e9:.1f} GB free where media is stored; waiting for space "
                                      f"(needs {deps.min_free_bytes / 1e9:.0f} GB)")
            deps.progress("downloading", "starting")
            media = deps.downloader.fetch(video.platform, video.video_id,
                                          on_progress=lambda pct, d: deps.progress("downloading", d, pct))
        except DownloadError as e:
            return done("failed", f"download failed: {e}", status="failed")
        video.media_path = str(media.video.parent)
        if video.status == "discovered":
            done("ingested", f"downloaded {media.duration_s / 60:.0f} min with "
                             f"{len(media.chat)} chat messages", status="ingested")

    # ---- transcribe
    tr = session.scalars(select(Transcript).where(Transcript.source_video_id == video.id)).first()
    if tr is None:
        try:
            deps.progress("transcribing", "loading the speech model")
            result = deps.transcriber.transcribe(media.audio or media.video,
                                                 on_progress=lambda pct, d: deps.progress("transcribing", d, pct))
        except TranscribeError as e:
            return done("failed", f"transcription failed: {e}", status="failed")
        finally:
            release = getattr(deps.transcriber, "release", None)
            if release:
                release()  # give the GPU back to the local LLM before highlight scoring
        tr = Transcript(source_video_id=video.id, language=result.language, model=deps.transcriber.name,
                        segments=[s.to_json() for s in result.segments])
        session.add(tr)
        session.add(Cost(kind="transcription", units=round(media.duration_s / 60, 1), usd=0.0,
                         detail=f"{deps.transcriber.name} on {subject} (local, free)"))
        done("transcribed", f"{len(result.segments)} segments, spoken language '{result.language}'",
             status="transcribed")
    allowed_lang = deps.sources.creative_commons.languages
    if video.attribution and allowed_lang and tr.language not in allowed_lang:
        return done("rejected", f"spoken language '{tr.language}' is not one of {allowed_lang}", status="rejected")
    segments = _segments(tr)
    if not segments:
        return done("rejected", "no speech found", status="rejected")

    # ---- signals
    if not video.signals:
        deps.progress("finding signals", "loudness, chat spikes and shot cuts")
        tl = build_timeline(media.audio if media else None, media.video if media else None,
                            media.chat if media else [], media.duration_s if media else video.duration_s)
        video.signals = tl.to_json()
        done("signals", f"{len(tl.loud_peaks)} loudness peaks, {len(tl.chat_spikes)} chat spikes, "
                        f"{len(tl.shot_cuts)} shot cuts", status="signals")
    tl = Timeline.from_json(video.signals)
    if brief and brief.priority_ranges:  # campaign's key moments count as strong signals
        tl.loud_peaks = sorted(set(tl.loud_peaks) | {int(a) + 2 for a, _ in brief.priority_ranges})

    # ---- highlights
    if video.status == "signals":
        deps.progress("picking highlights", f"scoring moments with {getattr(deps.llm, 'name', 'the LLM')}")
        pv = choose_prompt_version(session, f"highlight:{profile.name}", deps.rng)
        try:
            chosen, stats = pick_highlights(deps.llm, segments, tl, profile, min_s=deps.min_s,
                                            max_s=min(deps.max_s, (brief.max_seconds if brief and brief.max_seconds
                                                                   else deps.max_s)),
                                            top_n=(deps.live_top_n if video.video_id.startswith("live_")
                                                   else scaled_top_n(deps.top_n, video.duration_s)),
                                            max_candidates=scaled_candidates(video.duration_s),
                                            examples=pv.examples if pv else [],
                                            creator=(campaign.creator if campaign and campaign.creator
                                                     else video.channel_title))
        except LLMError as e:
            for ev in list(getattr(deps.llm, "events", None) or getattr(getattr(deps.llm, "inner", None), "events", [])):
                log_decision(session, MODULE, "llm_fallback", ev, subject=subject)
            return done("paused", f"highlight scoring will retry later: {e}")
        for ev in list(getattr(deps.llm, "events", None) or getattr(getattr(deps.llm, "inner", None), "events", [])):
            log_decision(session, MODULE, "llm_fallback", ev, subject=subject)
        session.add(Cost(kind="llm", units=stats.get("requests", 0), usd=0.0, detail=f"{stats.get('llm')} highlights"))
        for c in [c for c in chosen if lead and c.end <= lead + 1]:
            log_decision(session, MODULE, "skipped", f"{c.start:.0f}-{c.end:.0f}s lies inside the {lead:.0f}s "
                                                     "lead-in, which the previous live chunk already covered",
                         subject=subject)
        chosen = [c for c in chosen if not (lead and c.end <= lead + 1)]
        for c in chosen:
            session.add(Highlight(source_video_id=video.id, start_s=c.start, end_s=c.end, title=c.title,
                                  hook_headline=c.hook_headline, context_line=c.context_line or None,
                                  hook_type=c.hook_type, reason=c.reason, scores=c.scores, score=c.score,
                                  llm=stats["llm"], prompt_version_id=pv.id if pv else None, status="candidate"))
            log_decision(session, MODULE, "clip_chosen", f"{c.reason} (score {c.score}, {c.start:.0f}-{c.end:.0f}s)",
                         subject=subject, data={"title": c.title, "scores": c.scores})
        if not chosen:
            return done("no_highlights", f"no candidate scored well ({stats})", status="no_highlights")
        done("highlighted", f"{len(chosen)} highlights from {stats['candidates']} candidates in "
                            f"{stats['requests']} request(s) to {stats['llm']}", status="highlighted")

    hls = session.scalars(select(Highlight).where(Highlight.source_video_id == video.id)).all()

    # ---- gate, before render
    if video.status == "highlighted":
        threshold = get_setting(session, "render_threshold", 0)
        todo = [h for h in hls if h.status == "candidate"]
        texts = [" ".join(s.text for s in segments if s.end > h.start_s and s.start < h.end_s) for h in todo]
        deps.progress("safety gate", f"checking {len(todo)} highlights against the rules")
        mods = gate.moderate(deps.llm, texts)
        for h, text, mod in zip(todo, texts, mods):
            first7 = " ".join(w.w for w in _words_between(segments, h.start_s, h.start_s + 7))
            checks = [gate.GateResult(h.score >= threshold, f"score {h.score} below render threshold {threshold}"),
                      gate.check_text_rules(video.title, text, first7),
                      gate.check_ranges(brief, h.start_s, h.end_s),
                      gate.check_brief(brief, video=video, length_s=h.end_s - h.start_s,
                                       allow_ai_assisted=deps.allow_ai_assisted), mod]
            bad = next((c for c in checks if not c.ok), None)
            h.status = "passed" if bad is None else ("held" if bad.hold else "blocked")
            h.gate_reason = "passed every check" if bad is None else bad.reason
            log_decision(session, "gate", "passed" if bad is None else ("held" if bad.hold else "skipped"),
                         h.gate_reason, subject=f"highlight:{h.id}")
        done("gated", f"{sum(h.status == 'passed' for h in hls)} of {len(hls)} highlights passed the gate",
             status="gated")

    # ---- render, style, package, schedule
    recent = [c.phash for c in session.scalars(select(Clip).where(
        Clip.status.in_(("scheduled", "posted")), Clip.created_at >= now - DEDUPE_WINDOW, Clip.phash.is_not(None)))]
    source_file = Path(video.media_path or folder) / "source.mp4"
    if any(h.status == "passed" for h in hls) and not source_file.exists():
        # Sources are deleted once a video finishes; re-rendering it needs a fresh download.
        try:
            deps.progress("downloading", "re-downloading the source to render")
            media = deps.downloader.fetch(video.platform, video.video_id,
                                          on_progress=lambda pct, d: deps.progress("downloading", d, pct))
            video.media_path = str(media.video.parent)
            source_file = media.video
        except DownloadError as e:
            return done("failed", f"download failed: {e}", status="failed")
    scheduled = 0
    # A moment that is already live (from an earlier run of this video) is never posted twice.
    live = [(oh.start_s, oh.end_s) for oh, in session.execute(
        select(Highlight).join(Clip, Clip.highlight_id == Highlight.id).join(Post, Post.clip_id == Clip.id)
        .where(Highlight.source_video_id == video.id, Post.status == "posted"))]
    passed = []
    for h in [h for h in hls if h.status == "passed"]:
        if any(h.start_s < b and h.end_s > a for a, b in live):
            h.status, h.gate_reason = "blocked", "this moment is already posted"
            log_decision(session, "package", "skipped", h.gate_reason, subject=f"highlight:{h.id}")
        else:
            passed.append(h)
    for i, h in enumerate(passed):
        deps.progress("rendering", f"clip {i + 1} of {len(passed)}: {h.title[:60]}", 100 * i / max(1, len(passed)))
        # A video without a niche profile (e.g. Creative Commons test footage) only matches accounts
        # explicitly set to the 'generic' niche, so it can never land on a niche account.
        plan = plan_clip(session, niche=profile.name,
                         allowed_platforms=brief.platforms if brief else None, length_s=h.end_s - h.start_s,
                         hook_type=h.hook_type, bandit=deps.bandit)
        if plan is not None and story_style(deps.force_style or ""):
            plan.preset = plan.arms["style_preset"] = deps.force_style  # the owner fixed the style
        if plan is None:
            h.status, h.gate_reason = "held", f"no active account for niche '{profile.name}'"
            log_decision(session, "decide", "held", h.gate_reason, subject=f"highlight:{h.id}")
            continue
        out_dir = deps.media_dir / "clips" / f"h{h.id}"
        try:
            r = deps.renderer(RenderJob(source_file, h.start_s, h.end_s, out_dir,
                                        _words_between(segments, h.start_s, h.end_s), h.hook_headline,
                                        h.context_line or "", plan.preset,
                                        # the story style follows faces full-screen; the others keep the setting
                                        None if story_style(plan.preset) else deps.layout,
                                        tl.loud_peaks, tl.shot_cuts,
                                        plan.caption_density, list((h.scores or {}).get("emphasis") or []),
                                        str((h.scores or {}).get("reaction_tag") or ""), deps.sfx_level),
                                detector=deps.detector)
        except (RenderError, OSError, ValueError, RuntimeError) as e:
            h.status, h.gate_reason = "failed", f"render failed: {e}"
            log_decision(session, "render", "failed", h.gate_reason, subject=f"highlight:{h.id}")
            continue
        phash = video_phash(r.path)
        dup, sim = is_duplicate(phash, recent)
        tag_brief = brief
        if focus.is_gta_video(video, deps.priority_keywords or focus.DEFAULT_KEYWORDS):  # GTA clips: #GTA6 #GTAVI
            base = brief or Brief()
            tag_brief = base.model_copy(update={"required_hashtags": list(dict.fromkeys(
                list(base.required_hashtags) + list(focus.GTA_TAGS)))})
        pkg = package_text(deps.llm, title=h.title, hook=h.hook_headline, excerpt=h.reason,
                           creator=video.channel_title, attribution=video.attribution, brief=tag_brief,
                           # who is in the clip, for rules like "a hashtag for each CORE member in the video"
                           source_title=video.title or "",
                           said=" ".join(s.text for s in segments if s.end > h.start_s and s.start < h.end_s))
        clip = Clip(highlight_id=h.id, campaign_id=video.campaign_id, path=str(r.path), layout=r.layout,
                    style_preset=r.preset, duration_s=r.duration, phash=phash,
                    thumbnail_path=str(pick_thumbnail(r.path, out_dir / "thumb.jpg") or "") or None,
                    package={k: {"title": v.title, "description": v.description, "hashtags": v.hashtags,
                                 "disclosure": v.disclosure} for k, v in pkg.items()},
                    status="blocked" if dup else "rendered",
                    status_reason=f"{sim:.0%} similar to a clip posted in the last 60 days" if dup else None)
        session.add(clip)
        session.flush()
        h.status = "rendered"
        if dup:
            log_decision(session, "package", "skipped", clip.status_reason, subject=f"clip:{clip.id}")
            continue
        recent.append(phash)
        gta = focus.is_gta_video(video, deps.priority_keywords or focus.DEFAULT_KEYWORDS)
        focus_gta = deps.focus_on and gta
        fast = deps.launch.active(now) and (video.video_id.startswith("live_") or focus_gta)
        for acc in plan.accounts:
            if fast:
                t = fast_slot(session, acc, now, deps.launch,
                              focus.non_gta_post_ids(session, acc.id, deps.priority_keywords or focus.DEFAULT_KEYWORDS)
                              if focus_gta else frozenset())
                if t is None:
                    log_decision(session, "decide", "skipped",
                                 f"launch mode: {acc.handle} has no slot within "
                                 f"{deps.launch.stale.total_seconds() / 3600:.0f} h; the moment would be stale",
                                 subject=f"clip:{clip.id}")
                    continue
                slot = (t, "launch")
            else:
                slot = next_slot(session, acc, deps.bandit, now, deps.rng,
                                 campaign=bool(video.campaign_id), away=deps.owner_away, quiet=deps.quiet,
                                 campaign_name=campaign.name if campaign else None)
            if slot is None:
                log_decision(session, "decide", "held", f"no free slot this week for {acc.handle}",
                             subject=f"clip:{clip.id}")
                continue
            t, bucket = slot
            dry = deps.live_dry_run and video.video_id.startswith("live_")
            auto = not dry and h.score >= (acc.auto_approve_score or 0)
            session.add(Post(clip_id=clip.id, account_id=acc.id, platform=acc.platform, scheduled_at=t,
                             status="scheduled" if auto else "held",
                             status_reason=None if auto else (
                                 "LIVE_DRY_RUN: live test clip, not published; approve it in the dashboard" if dry
                                 else f"score {h.score} below this account's auto-approve "
                                      f"score {acc.auto_approve_score}; approve it in the dashboard"),
                             arms={**plan.arms, "post_hour_bucket": bucket}))
            log_decision(session, "decide", "scheduled" if auto else "held",
                         f"{acc.handle} on {acc.platform} at {t:%Y-%m-%d %H:%M} UTC (style {plan.preset}, "
                         f"bucket {bucket} ET)" + ("" if auto else "; waiting for your approval"),
                         subject=f"clip:{clip.id}")
            scheduled += 1
        clip.status = "scheduled" if scheduled else clip.status
        session.flush()
    return done("scheduled" if scheduled else "done", f"{scheduled} post(s) scheduled",
                status="scheduled" if scheduled else "done")


def spend_today(session: Session, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return float(session.scalar(select(func.coalesce(func.sum(Cost.usd), 0.0)).where(Cost.created_at >= start)) or 0)
