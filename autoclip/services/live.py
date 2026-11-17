"""Live clipping: turns closed recording chunks of an allowlisted stream into short SourceVideos that
the normal pipeline (transcribe -> signals -> highlights -> gate -> render -> schedule) processes within
minutes, instead of waiting for the full VOD.

Rights: only channels in sources.yaml or joined campaigns (inside their dates) are ever recorded.
The chunk inherits that channel's rights basis and campaign, so the gate runs exactly as for a VOD."""
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.integrations.downloader import probe_duration, run_cmd
from autoclip.models import SourceVideo
from autoclip.pipeline.chat_spikes import detect_spikes
from autoclip.services.discovery import campaign_sources, pick_sources
from autoclip.services.focus import TITLE_PREFIX
from autoclip.services.settings_store import get_setting, set_setting
from autoclip.sources import ChannelSource, Sources

MODULE = "live"
LIVE_PLATFORMS = ("twitch", "kick", "youtube")
MIN_CHUNK_S = 45  # a shorter tail (stream ended mid-chunk) is not worth a pipeline run
LIVE_RANK = 50.0  # live chunks jump the discovery queue: first clips out earn the most
CHUNK_RE = re.compile(r"chunk_(\d+)\.ts$")


@dataclass
class LiveJob:
    channel: ChannelSource
    folder: Path  # where the recorder writes chunk_NNNNN.ts
    started: float  # unix time the recording began
    chunk_s: int
    done: set[int] = field(default_factory=set)
    chat: list[tuple[float, str]] = field(default_factory=list)
    # Chat reacts after the moment: viewers react and type (~5-15 s) and the recorder starts a few seconds
    # behind the clock. Messages are moved back by this much so a spike lines up with what caused it.
    chat_lag_s: float = 15.0
    # Each chunk also starts with the end of the previous one, so a moment across a chunk border is whole
    # in one of them. Clips lying entirely inside this lead-in are dropped (the previous chunk had them).
    overlap_s: float = 30.0
    gta: bool = False  # the stream is GTA 6 right now (Twitch game or title): its chunks are tagged and go first

    @property
    def login(self) -> str:
        return self.channel.channel_id


def allowed_channels(sources: Sources, session: Session, today: date) -> list[ChannelSource]:
    """The rights allowlist for live recording: permissioned channels plus campaigns active today."""
    pool = sources.channels + [c for c in sources.campaigns if c.active_on(today)] + [
        c for c in campaign_sources(session) if c.active_on(today)]
    return [c for c in pick_sources(pool).values() if c.platform in LIVE_PLATFORMS]


def chunk_files(folder: Path) -> list[tuple[int, Path]]:
    found = []
    for p in Path(folder).glob("chunk_*.ts"):
        m = CHUNK_RE.search(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return sorted(found)


def mark_recorded(session: Session, channel: ChannelSource, now: datetime) -> None:
    """Remember that this channel was recorded live, so the post-stream VOD can be skipped."""
    rec = dict(get_setting(session, "live_recorded", {}))
    rec[f"{channel.platform}:{channel.channel_id.lower()}"] = now.isoformat()
    set_setting(session, "live_recorded", rec)


def recorded_recently(session: Session, channel: ChannelSource, now: datetime, hours: int = 24) -> bool:
    ts = get_setting(session, "live_recorded", {}).get(f"{channel.platform}:{channel.channel_id.lower()}")
    if not ts:
        return False
    return (now - datetime.fromisoformat(ts)).total_seconds() < hours * 3600


def finalize_chunks(session: Session, job: LiveJob, media_dir: Path, now: datetime, *, recording: bool,
                    run: Callable = run_cmd, probe: Callable = probe_duration) -> list[str]:
    """Turns every closed chunk into a SourceVideo (status 'discovered'). While the recorder still runs
    the newest chunk is still being written, so it is left alone. Returns the new video ids."""
    chunks = [(i, p) for i, p in chunk_files(job.folder) if i not in job.done]
    if recording and chunks:
        chunks = chunks[:-1]
    ch, created = job.channel, []
    for idx, ts in chunks:
        job.done.add(idx)
        stamp = datetime.fromtimestamp(job.started, timezone.utc).strftime("%m%d%H%M")
        video_id = f"live_{ch.channel_id}_{stamp}_{idx}"[:64]
        subject = f"{ch.platform}:{video_id}"
        folder = Path(media_dir) / f"{ch.platform}_{video_id}"
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / "source.mp4"
        prev = job.folder / f"chunk_{idx - 1:05d}.ts"
        lead = 0.0
        try:
            if job.overlap_s > 0 and idx > 0 and prev.exists():
                prev_s, cur_s = probe(prev), probe(ts)
                # The concat demuxer: each chunk's timestamps restart at 0 (-reset_timestamps), which the
                # 'concat:' protocol cannot join (on real Twitch chunks it kept only the lead-in). Stream copy
                # starts at the keyframe at or before the inpoint, so the clip never opens mid-GOP.
                listing = folder / "join.txt"
                listing.write_text("".join(
                    f"file '{str(p.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n" + extra
                    for p, extra in ((prev, f"inpoint {max(0.0, prev_s - job.overlap_s):.2f}\n"), (ts, ""))),
                    encoding="utf-8")
                run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                     "-c", "copy", "-movflags", "+faststart", str(source)])
                listing.unlink(missing_ok=True)
                duration = probe(source)
                lead = max(0.0, min(prev_s, duration - cur_s))
                if duration < cur_s - 1:  # the join lost part of the new chunk: never lose footage for a lead-in
                    log_decision(session, MODULE, "fallback", f"joining chunk {idx} with the lead-in gave "
                                 f"{duration:.0f}s instead of {cur_s:.0f}s+; using the chunk alone", subject=subject)
                    run(["ffmpeg", "-y", "-v", "error", "-i", str(ts), "-c", "copy", "-movflags", "+faststart",
                         str(source)])
                    duration, lead = probe(source), 0.0
            else:
                run(["ffmpeg", "-y", "-v", "error", "-i", str(ts), "-c", "copy", "-movflags", "+faststart",
                     str(source)])
                duration = probe(source)
        except Exception as e:  # noqa: BLE001 - a bad chunk must not stop the others
            log_decision(session, MODULE, "skipped", f"chunk {idx} of '{ch.name}' could not be read: {e}",
                         subject=subject)
            continue
        finally:
            prev.unlink(missing_ok=True)  # the lead-in is taken: the previous raw chunk is never needed again
        (folder / "live.json").write_text(json.dumps({"lead_s": round(lead, 2)}), encoding="utf-8")
        if duration - lead < MIN_CHUNK_S:
            log_decision(session, MODULE, "skipped", f"chunk {idx} is only {duration:.0f}s long", subject=subject)
            continue
        t0 = job.started + idx * job.chunk_s - lead
        lag = job.chat_lag_s
        chat = [(round(t - t0 - lag, 2), text) for t, text in job.chat if t0 + lag <= t < t0 + duration + lag]
        (folder / "chat.json").write_text(json.dumps([[t, x] for t, x in chat]), encoding="utf-8")
        spikes = detect_spikes(chat, duration)
        session.add(SourceVideo(
            platform=ch.platform, video_id=video_id, channel_id=ch.channel_id, channel_title=ch.name,
            title=f"{TITLE_PREFIX if job.gta else ''}{ch.name} LIVE {stamp} part {idx + 1}", duration_s=int(duration), license="", views=0,
            published_at=datetime.fromtimestamp(t0, timezone.utc), status="discovered",
            rights_basis=ch.basis, campaign_id=ch.campaign_id, profile=ch.profile, rank_score=LIVE_RANK,
            media_path=str(folder)))
        log_decision(session, MODULE, "chunk_queued",
                     f"{duration / 60:.1f} min of '{ch.name}' live ({lead:.0f}s lead-in), {len(chat)} chat messages, "
                     f"{len(spikes)} chat spike(s) at {[int(s.start) for s in spikes][:5]}", subject=subject,
                     data={"spikes": [{"t": s.start, "count": s.count, "words": s.keywords} for s in spikes]})
        created.append(video_id)
    if not recording:  # the stream is over: no next chunk will need a lead-in
        for i, p in chunk_files(job.folder):
            if i in job.done:
                p.unlink(missing_ok=True)
    # chat older than the finished chunks (and the next lead-in) is no longer needed
    horizon = (job.started + (max(job.done) + 1) * job.chunk_s - job.overlap_s) if job.done else job.started
    job.chat = [m for m in job.chat if m[0] >= horizon]
    session.flush()
    return created


def live_chunk_ids(session: Session) -> list[str]:
    """Queued live chunks, GTA chunks first, then oldest first."""
    rows = session.execute(select(SourceVideo.video_id, SourceVideo.title).where(
        SourceVideo.video_id.like("live\\_%", escape="\\"), SourceVideo.status == "discovered")).all()
    return [vid for vid, title in sorted(rows, key=lambda r: (not (r[1] or "").startswith(TITLE_PREFIX), r[0]))]


def now_utc() -> datetime:
    return datetime.fromtimestamp(time.time(), timezone.utc)
