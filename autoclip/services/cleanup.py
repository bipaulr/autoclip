"""Disk space: footage that has been posted is deleted so live recording never runs out of room.

What is deleted, and when:
- a rendered clip once EVERY post of it is 'posted' (after a short grace period), and blocked
  duplicates (never posted) after a day. A clip with a scheduled, held or failed post is kept: it
  still has to be uploaded.
- raw live recordings left behind by a crashed recorder (media/live), after a few hours.
- when free space is below the floor, the grace periods drop to zero.
Library footage (media/library), unprocessed sources and anything outside media_dir are never touched."""
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.models import Clip, Post

MODULE = "cleanup"
CLEANED = "file deleted after posting to free disk space"


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _inside(root: Path, p: Path) -> bool:
    try:
        return root.resolve() in p.resolve().parents
    except OSError:
        return False


def _folder_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _remove(media_dir: Path, folder: Path) -> int:
    """Deletes a folder under media_dir/clips only. Returns bytes freed."""
    clips_root = Path(media_dir) / "clips"
    if not folder.is_dir() or not _inside(clips_root, folder):
        return 0
    size = _folder_size(folder)
    shutil.rmtree(folder, ignore_errors=True)
    return size


def purge_clips(session: Session, media_dir: Path, now: datetime, *, grace: timedelta = timedelta(minutes=30),
                blocked_after: timedelta = timedelta(days=1)) -> int:
    freed = 0
    for clip in session.scalars(select(Clip).where(Clip.status.in_(("posted", "scheduled", "blocked")))):
        if clip.status_reason == CLEANED:
            continue
        posts = list(session.scalars(select(Post).where(Post.clip_id == clip.id)))
        if clip.status == "blocked":
            ok = not posts and _aware(clip.created_at) <= now - blocked_after
            why = "a blocked duplicate that was never posted"
        else:
            # posted, sent for a manual upload (Telegram has the file) or expired as stale: nothing left to upload
            ok = bool(posts) and all(
                (p.status == "posted" and p.posted_at and _aware(p.posted_at) <= now - grace)
                or (p.status in ("manual", "expired") and _aware(p.scheduled_at) <= now - grace) for p in posts)
            why = f"posted to {len(posts)} account(s)"
        if not ok:
            continue
        n = _remove(media_dir, Path(clip.path).parent)
        if n:
            freed += n
            clip.status_reason = CLEANED
            log_decision(session, MODULE, "cleaned", f"deleted the rendered clip ({n / 1e6:.0f} MB): {why}",
                         subject=f"clip:{clip.id}")
    return freed


def purge_recordings(media_dir: Path, now_ts: float | None = None, max_age_h: float = 6) -> int:
    """Raw recorder output (media/live) older than max_age_h. The live loop removes each chunk as it
    is handled; this only catches what a crash left behind."""
    root, freed = Path(media_dir) / "live", 0
    cutoff = (now_ts or time.time()) - max_age_h * 3600
    if not root.is_dir():
        return 0
    for d in root.iterdir():
        try:
            if d.is_dir() and max((f.stat().st_mtime for f in d.rglob("*") if f.is_file()),
                                  default=d.stat().st_mtime) < cutoff:
                freed += _folder_size(d)
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue
    return freed


def free_bytes(path: Path) -> int | None:
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(path).free
    except OSError:
        return None


def run_cleanup(session: Session, media_dir: Path, min_free: int, now: datetime | None = None) -> int:
    """Routine cleanup, and an emergency pass (no grace period) when free space is below `min_free`."""
    now = now or datetime.now(timezone.utc)
    freed = purge_clips(session, media_dir, now) + purge_recordings(media_dir)
    free = free_bytes(media_dir)
    if free is not None and free < min_free:
        freed += purge_clips(session, media_dir, now, grace=timedelta(0), blocked_after=timedelta(0))
        freed += purge_recordings(media_dir, max_age_h=0.5)
        log_decision(session, MODULE, "low_space",
                     f"{free / 1e9:.1f} GB free is below {min_free / 1e9:.0f} GB: cleaned immediately, "
                     f"{freed / 1e9:.2f} GB freed")
    if freed:
        log_decision(session, MODULE, "run_finished", f"{freed / 1e9:.2f} GB freed")
    session.flush()
    return freed
