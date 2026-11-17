"""PRIVATE live-clipping test on any Twitch channel. Nothing is ever posted.

Runs the real live path (streamlink + anonymous chat -> chunks -> transcribe -> signals -> highlights ->
render) against a throwaway SQLite database and its own folder, so its clips never reach the real queue,
the publisher or the cleanup task. The only account is a placeholder that exists in the test database.
Writes index.html (a gallery of every clip with its score and reason) next to the clips.

    docker compose cp scripts/live_test.py worker:/app/live_test.py
    docker compose exec -d worker python /app/live_test.py <twitch_login> --minutes 30
"""
import argparse
import html
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import autoclip.models  # noqa: F401
from autoclip.config import get_settings
from autoclip.db import Base, make_engine
from autoclip.integrations.downloader import YtDlpDownloader
from autoclip.integrations.live import StreamlinkRecorder, TwitchAnonChat
from autoclip.integrations.llm import CachedLLM
from autoclip.integrations.transcriber import FasterWhisperTranscriber
from autoclip.models import Account, Clip, DecisionLog, Highlight, SourceVideo
from autoclip.pipeline.decide import Bandit
from autoclip.services.live import LiveJob, finalize_chunks
from autoclip.services.pipeline import Deps, process_video
from autoclip.services.settings_store import set_setting
from autoclip.sources import ChannelSource, Sources
from autoclip.tasks import build_llm

BASIS = "PRIVATE TECHNICAL TEST: never posted"


def log(out: Path, msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    with open(out / "progress.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def gallery(session, out: Path, channel: str, threshold: int) -> int:
    rows = []
    hls = session.scalars(select(Highlight).order_by(Highlight.source_video_id, Highlight.start_s)).all()
    for h in hls:
        v = session.get(SourceVideo, h.source_video_id)
        clip = session.scalars(select(Clip).where(Clip.highlight_id == h.id)).first()
        rel = Path(clip.path).relative_to(out).as_posix() if clip else None
        badge = "passes 75" if h.score >= threshold else f"below {threshold}"
        rows.append(f"""<div class=c><h3>{html.escape(h.title)}</h3>
<p class=m>{html.escape(v.title)} &middot; {h.start_s:.0f}-{h.end_s:.0f}s &middot; score <b>{h.score}</b>
 ({badge}) &middot; {html.escape(h.status)}</p>
{f'<video src="{rel}" controls preload=metadata></video>' if rel else f'<p class=x>not rendered: {html.escape(h.gate_reason or "")}</p>'}
<p class=hook>Hook: {html.escape(h.hook_headline or "")}</p><p>{html.escape(h.reason)}</p></div>""")
    chunks = []
    for d in session.scalars(select(DecisionLog).where(DecisionLog.module == "live").order_by(DecisionLog.id)):
        chunks.append(f"<li>{html.escape(d.subject or '')}: {html.escape(d.reason)}</li>")
    (out / "index.html").write_text(f"""<!doctype html><meta charset=utf-8><title>Live test: {channel}</title>
<style>body{{font-family:system-ui;background:#111;color:#eee;margin:16px}}.g{{display:flex;flex-wrap:wrap;gap:16px}}
.c{{background:#1c1c1c;border-radius:8px;padding:12px;width:300px}}video{{width:100%;border-radius:6px;background:#000}}
h3{{margin:0 0 4px;font-size:15px}}.m{{color:#aaa;font-size:12px}}.hook{{color:#ffd54a}}.x{{color:#f77}}p{{font-size:13px}}</style>
<h1>Live test: {channel}</h1><p>Private test. Nothing here was posted. {len(hls)} highlight(s), updated {datetime.now():%H:%M:%S}.</p>
<div class=g>{''.join(rows)}</div><h2>Chunks</h2><ul>{''.join(chunks)}</ul>""", encoding="utf-8")
    return len(hls)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("channel")
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--chunk", type=int, default=300)
    a = ap.parse_args()
    s = get_settings()
    out = Path(s.media_dir) / "livetest" / f"{a.channel}_{datetime.now():%m%d_%H%M}"
    media = out / "media"
    media.mkdir(parents=True, exist_ok=True)
    engine = make_engine(f"sqlite:///{(out / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    session.add(Account(platform="youtube", handle="TEST-NEVER-POSTED", niche="irl_streamer",
                        style_preset="bold_pop", daily_cap=999, active=True))
    set_setting(session, "render_threshold", 0)  # render everything, so every candidate can be seen
    session.commit()

    deps = Deps(downloader=YtDlpDownloader(media), transcriber=FasterWhisperTranscriber(
        s.whisper_model, s.whisper_device, s.whisper_compute_type), llm=CachedLLM(build_llm(s), session),
        sources=Sources(), media_dir=media, bandit=Bandit(session), allow_ai_assisted=True,
        layout=None if s.clip_layout == "auto" else s.clip_layout, min_s=s.clip_min_seconds,
        max_s=s.clip_max_seconds, top_n=s.highlights_per_video, live_top_n=s.live_top_n, live_dry_run=True)

    ch = ChannelSource("twitch", a.channel, a.channel, BASIS, profile="irl_streamer")
    rec, chat = StreamlinkRecorder(s.live_quality, segment_threads=s.live_segment_threads), TwitchAnonChat()
    rec.start("twitch", a.channel, out / "rec", a.chunk)
    chat.start(a.channel)
    job = LiveJob(ch, out / "rec", time.time(), a.chunk, chat_lag_s=s.live_chat_lag_s)
    deadline = time.time() + a.minutes * 60
    log(out, f"recording {a.channel} for {a.minutes:.0f} min in {a.chunk}s chunks -> {out}")
    gallery(session, out, a.channel, 75)

    def handle(recording: bool) -> None:
        job.chat += chat.drain()
        for vid in finalize_chunks(session, job, media, datetime.now(timezone.utc), recording=recording):
            session.commit()
            v = session.scalars(select(SourceVideo).where(SourceVideo.video_id == vid)).one()
            log(out, f"processing {vid} ({v.duration_s}s)")
            t0 = time.time()
            try:
                res = process_video(v, session, deps)
            except Exception as e:  # noqa: BLE001 - keep recording; report and move on
                session.rollback()
                res = f"crashed: {type(e).__name__}: {e}"
            session.commit()
            n = gallery(session, out, a.channel, 75)
            log(out, f"{vid}: {res} in {time.time() - t0:.0f}s; {n} highlight(s) so far")
        session.commit()

    try:
        while time.time() < deadline and rec.is_running():
            handle(recording=True)
            time.sleep(15)
    finally:
        rec.stop()
        chat.stop()
        time.sleep(5)  # let ffmpeg close the last chunk
        handle(recording=False)
        n = gallery(session, out, a.channel, 75)
        log(out, f"DONE: {n} highlight(s); gallery at {out / 'index.html'}")
        (out / "done.json").write_text(json.dumps({"highlights": n}), encoding="utf-8")


if __name__ == "__main__":
    main()
