"""Ingest: fetch a source video, its audio track and (for streams) its chat replay."""
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol


class DownloadError(Exception):
    pass


@dataclass
class ChatMessage:
    t: float  # seconds from the start of the video
    text: str


@dataclass
class Media:
    video: Path
    audio: Path | None = None
    duration_s: float = 0.0
    chat: list[ChatMessage] = field(default_factory=list)


class Downloader(Protocol):
    def fetch(self, platform: str, video_id: str, on_progress=None) -> Media: ...


class MockDownloader:
    def __init__(self, media: Media | None = None, error: str | None = None):
        self.media, self.error = media or Media(Path("mock.mp4"), Path("mock.wav"), 600.0), error
        self.calls: list[tuple[str, str]] = []

    def fetch(self, platform: str, video_id: str, on_progress=None) -> Media:
        self.calls.append((platform, video_id))
        if self.error:
            raise DownloadError(self.error)
        return self.media


URLS = {
    "youtube": "https://www.youtube.com/watch?v={id}",
    "twitch": "https://www.twitch.tv/videos/{id}",
    "kick": "https://kick.com/video/{id}",
    "gdrive": "https://drive.google.com/file/d/{id}/view",
}


def run_cmd(args: list[str], timeout: int = 3600) -> str:
    """Runs a command without a shell; raises DownloadError with the tail of stderr."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DownloadError(f"{args[0]} failed to run ({type(e).__name__})") from None
    if p.returncode != 0:
        raise DownloadError(f"{args[0]} exited {p.returncode}: {p.stderr.strip()[-300:]}")
    return p.stdout


def probe_duration(path: Path, run: Callable = run_cmd) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    try:
        return float(json.loads(out)["format"]["duration"])
    except (ValueError, KeyError):
        raise DownloadError("could not read the video duration") from None


class YtDlpDownloader:
    """Files live under media_dir/{platform}_{id}/. Anything already on disk is reused."""

    def __init__(self, media_dir: str | Path, max_height: int = 1080, ydl_factory: Callable | None = None,
                 run: Callable = run_cmd, chat_fetcher: Callable | None = None, stream_max_height: int = 720,
                 fragments: int = 8):
        self.root = Path(media_dir)
        self.max_height, self.ydl_factory, self.run, self.chat_fetcher = max_height, ydl_factory, run, chat_fetcher
        # Streams run for hours: an 8-hour 1080p VOD is ~25 GB, the same stream at 720p is about half.
        self.stream_max_height = stream_max_height
        self.fragments = fragments

    def fetch(self, platform: str, video_id: str, on_progress=None) -> Media:
        if platform == "file":
            return self._local(video_id, on_progress)
        if platform not in URLS:
            raise DownloadError(f"unsupported platform '{platform}'")
        url = URLS[platform].format(id=video_id)
        folder = self.root / f"{platform}_{video_id}"
        video, audio, chat_file = folder / "source.mp4", folder / "audio.wav", folder / "chat.json"
        folder.mkdir(parents=True, exist_ok=True)

        height = self.stream_max_height if platform in ("twitch", "kick") else self.max_height
        if not (video.exists() and video.stat().st_size > 0):
            factory = self.ydl_factory
            if factory is None:
                from yt_dlp import YoutubeDL
                factory = YoutubeDL
            opts = {
                # H.264 first: OpenCV (face tracking, hashing) cannot decode AV1 in this container.
                "format": (f"bv*[vcodec^=avc1][height<={height}]+ba/"
                           f"bv*[height<={height}]+ba/b[height<={height}]/b"),
                "merge_output_format": "mp4", "outtmpl": str(folder / "source.%(ext)s"),
                "noplaylist": True, "quiet": True, "no_warnings": True, "retries": 3, "writeinfojson": True,
                # Twitch/Kick VODs are thousands of small HLS pieces; fetched one at a time the per-request
                # wait dominates (6 GB took ~4 h). Fetching several at once fills the connection.
                "concurrent_fragment_downloads": self.fragments, "fragment_retries": 10,
            }
            if on_progress:
                def hook(d):
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    if d.get("status") == "downloading" and total:
                        on_progress(100 * d.get("downloaded_bytes", 0) / total,
                                    f"{d.get('downloaded_bytes', 0) / 1e6:.0f} of {total / 1e6:.0f} MB")
                opts["progress_hooks"] = [hook]
            try:
                with factory(opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                raise DownloadError(f"{type(e).__name__}: {str(e)[:300]}") from None
            if not video.exists():
                raise DownloadError("download finished but the file is missing")

        if not (audio.exists() and audio.stat().st_size > 0):
            if on_progress:
                on_progress(None, "extracting audio")
            self.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)])

        chat: list[ChatMessage] = []
        if chat_file.exists():
            chat = [ChatMessage(*m) for m in json.loads(chat_file.read_text(encoding="utf-8"))]
        elif self.chat_fetcher and platform in ("twitch", "kick"):
            try:
                chat = self.chat_fetcher(url)
            except Exception:  # chat is a bonus signal; never fail the ingest over it
                chat = []
            chat_file.write_text(json.dumps([[m.t, m.text] for m in chat]), encoding="utf-8")
        return Media(video, audio, probe_duration(video, self.run), chat)


def _safe_name(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", path)[-120:]


def _local_fetch(self, rel_path: str, on_progress=None) -> Media:
    """Campaign footage already on disk under media/library (e.g. an unzipped Dropbox folder).
    The file is hard-linked into its own working folder, so the per-video cleanup removes only the
    link and the library copy is never touched."""
    library = (self.root / "library").resolve()
    src = (library / rel_path).resolve()
    if library not in src.parents or not src.is_file():
        raise DownloadError(f"library file not found: {rel_path}")
    folder = self.root / f"file_{_safe_name(rel_path)}"
    folder.mkdir(parents=True, exist_ok=True)
    video, audio = folder / "source.mp4", folder / "audio.wav"
    if not video.exists():
        try:
            os.link(src, video)
        except OSError:
            shutil.copy2(src, video)  # different drive: fall back to a copy
    if not (audio.exists() and audio.stat().st_size > 0):
        if on_progress:
            on_progress(None, "extracting audio")
        self.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)])
    return Media(video, audio, probe_duration(video, self.run), [])


YtDlpDownloader._local = _local_fetch


def fetch_chat_replay(url: str, max_messages: int = 200000) -> list[ChatMessage]:
    """Twitch/Kick chat replay with timestamps via the chat-downloader library."""
    from chat_downloader import ChatDownloader
    out = []
    for m in ChatDownloader().get_chat(url):
        if "time_in_seconds" in m and m.get("message"):
            out.append(ChatMessage(float(m["time_in_seconds"]), str(m["message"])))
            if len(out) >= max_messages:
                break
    return out
