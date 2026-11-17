import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from autoclip.models import Account, Clip, DecisionLog, Highlight, Post, SourceVideo
from autoclip.services import cleanup
from autoclip.services.cleanup import CLEANED, purge_clips, purge_recordings, run_cleanup

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def make_clip(session, media, name, status="posted", posts=(), created=NOW - timedelta(days=2)):
    v = SourceVideo(platform="twitch", video_id=name, channel_id="c", channel_title="C", title="t", duration_s=1,
                    license="", views=0, status="done")
    session.add(v)
    session.flush()
    h = Highlight(source_video_id=v.id, start_s=0, end_s=20, title="t", reason="r", score=50, llm="x",
                  status="rendered")
    session.add(h)
    session.flush()
    folder = Path(media) / "clips" / f"h{h.id}"
    folder.mkdir(parents=True)
    (folder / "clip.mp4").write_bytes(b"x" * 1000)
    (folder / "thumb.jpg").write_bytes(b"y" * 10)
    clip = Clip(highlight_id=h.id, path=str(folder / "clip.mp4"), layout="g", style_preset="p", duration_s=20,
                status=status, created_at=created)
    session.add(clip)
    session.flush()
    acct = session.scalars(select(Account)).first()
    if acct is None:
        acct = Account(platform="youtube", handle="a", niche="gaming", style_preset="p")
        session.add(acct)
        session.flush()
    for i, (st, posted_at) in enumerate(posts):
        session.add(Post(clip_id=clip.id, account_id=acct.id, platform=("youtube", "instagram")[i],
                         scheduled_at=NOW, posted_at=posted_at, status=st))
    session.flush()
    return clip, folder


def test_posted_clip_is_deleted_after_the_grace_period(session, tmp_path):
    clip, folder = make_clip(session, tmp_path, "a", posts=[("posted", NOW - timedelta(hours=2))])
    freed = purge_clips(session, tmp_path, NOW)
    assert freed == 1010 and not folder.exists()
    assert clip.status_reason == CLEANED and clip.status == "posted"
    assert any(d.action == "cleaned" for d in session.scalars(select(DecisionLog)))


def test_recently_posted_clip_is_kept_for_the_grace_period(session, tmp_path):
    _, folder = make_clip(session, tmp_path, "a", posts=[("posted", NOW - timedelta(minutes=5))])
    assert purge_clips(session, tmp_path, NOW) == 0 and folder.exists()


def test_clip_with_an_unposted_post_is_kept(session, tmp_path):
    _, folder = make_clip(session, tmp_path, "a", posts=[("posted", NOW - timedelta(hours=2)),
                                                         ("scheduled", None)])
    assert purge_clips(session, tmp_path, NOW) == 0 and folder.exists()
    _, f2 = make_clip(session, tmp_path, "b", posts=[("held", None)])
    _, f3 = make_clip(session, tmp_path, "c", status="rendered", posts=[])
    assert purge_clips(session, tmp_path, NOW) == 0 and f2.exists() and f3.exists()


def test_failed_post_keeps_the_file_for_a_retry(session, tmp_path):
    _, folder = make_clip(session, tmp_path, "a", posts=[("failed", None)])
    assert purge_clips(session, tmp_path, NOW) == 0 and folder.exists()


def test_blocked_duplicate_goes_after_a_day(session, tmp_path):
    _, folder = make_clip(session, tmp_path, "a", status="blocked", created=NOW - timedelta(days=2))
    _, fresh = make_clip(session, tmp_path, "b", status="blocked", created=NOW - timedelta(hours=1))
    purge_clips(session, tmp_path, NOW)
    assert not folder.exists() and fresh.exists()


def test_clip_outside_media_clips_is_never_deleted(session, tmp_path):
    media, other = tmp_path / "media", tmp_path / "precious"
    other.mkdir()
    (other / "keep.mp4").write_bytes(b"z")
    clip, _ = make_clip(session, media, "a", posts=[("posted", NOW - timedelta(hours=2))])
    clip.path = str(other / "keep.mp4")
    assert purge_clips(session, media, NOW) == 0 and (other / "keep.mp4").exists()


def test_already_cleaned_clip_is_skipped(session, tmp_path):
    clip, _ = make_clip(session, tmp_path, "a", posts=[("posted", NOW - timedelta(hours=2))])
    purge_clips(session, tmp_path, NOW)
    assert purge_clips(session, tmp_path, NOW) == 0


def test_old_recordings_are_removed_and_fresh_ones_kept(tmp_path):
    old, new = tmp_path / "live" / "old", tmp_path / "live" / "new"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "chunk_00000.ts").write_bytes(b"x" * 500)
    t = time.time() - 10 * 3600
    os.utime(old / "chunk_00000.ts", (t, t))
    assert purge_recordings(tmp_path) == 500 and not old.exists() and new.exists()


def test_low_space_cleans_without_waiting_for_the_grace_period(session, tmp_path, monkeypatch):
    _, folder = make_clip(session, tmp_path, "a", posts=[("posted", NOW - timedelta(minutes=5))])
    monkeypatch.setattr(cleanup, "free_bytes", lambda p: 1)
    assert run_cleanup(session, tmp_path, min_free=10**12, now=NOW) == 1010 and not folder.exists()
    assert any(d.action == "low_space" for d in session.scalars(select(DecisionLog)))


def test_enough_space_keeps_recently_posted_files(session, tmp_path, monkeypatch):
    _, folder = make_clip(session, tmp_path, "a", posts=[("posted", NOW - timedelta(minutes=5))])
    monkeypatch.setattr(cleanup, "free_bytes", lambda p: 10**13)
    assert run_cleanup(session, tmp_path, min_free=10**9, now=NOW) == 0 and folder.exists()
