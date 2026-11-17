"""Live capture: record a stream while it runs (streamlink -> ffmpeg segments) and read its chat
anonymously (Twitch IRC, read-only 'justinfan' login: no account, no token).

Both sit behind small interfaces with mocks, so the live loop is tested offline."""
import random
import re
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Protocol


class LiveError(Exception):
    pass


# ---------------------------------------------------------------- chat
class ChatReader(Protocol):
    def start(self, login: str) -> None: ...
    def drain(self) -> list[tuple[float, str]]: ...  # (unix time, text) since the last call
    def stop(self) -> None: ...


class MockChatReader:
    def __init__(self, messages: list[tuple[float, str]] | None = None):
        self.messages = list(messages or [])
        self.started: list[str] = []
        self.stopped = False

    def start(self, login: str) -> None:
        self.started.append(login)

    def drain(self) -> list[tuple[float, str]]:
        out, self.messages = self.messages, []
        return out

    def stop(self) -> None:
        self.stopped = True


PRIVMSG = re.compile(r"^(?:@\S+ )?:[^ ]+ PRIVMSG #\S+ :(.*)$")


def parse_privmsg(line: str) -> str | None:
    """The chat text of an IRC PRIVMSG line, or None for anything else (PING, JOIN, notices...)."""
    m = PRIVMSG.match(line.rstrip("\r\n"))
    return m.group(1) if m else None


class TwitchAnonChat:
    """Reads one channel's chat over IRC with an anonymous login. Reconnects until stopped."""

    HOST, PORT = "irc.chat.twitch.tv", 6697

    def __init__(self, connect: Callable | None = None, clock: Callable[[], float] = time.time):
        self._connect = connect or self._tls_connect
        self._clock = clock
        self._lock = threading.Lock()
        self._buf: list[tuple[float, str]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _tls_connect(self):
        raw = socket.create_connection((self.HOST, self.PORT), timeout=30)
        return ssl.create_default_context().wrap_socket(raw, server_hostname=self.HOST)

    def start(self, login: str) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(login.lower().lstrip("#"),), daemon=True)
        self._thread.start()

    def _run(self, login: str) -> None:
        while not self._stop.is_set():
            try:
                self._session(login)
            except (OSError, ssl.SSLError):
                pass
            self._stop.wait(5)  # back off, then reconnect

    def _session(self, login: str) -> None:
        sock = self._connect()
        try:
            sock.settimeout(60)
            for cmd in ("PASS SCHMOOPIIE", f"NICK justinfan{random.randint(10000, 99999)}", f"JOIN #{login}"):
                sock.sendall((cmd + "\r\n").encode())
            pending = b""
            while not self._stop.is_set():
                data = sock.recv(4096)
                if not data:
                    return
                pending += data
                *lines, pending = pending.split(b"\r\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                        continue
                    text = parse_privmsg(line)
                    if text:
                        with self._lock:
                            self._buf.append((self._clock(), text))
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def drain(self) -> list[tuple[float, str]]:
        with self._lock:
            out, self._buf = self._buf, []
        return out

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------- recording
class Recorder(Protocol):
    def start(self, platform: str, login: str, out_dir: Path, chunk_s: int) -> None: ...
    def is_running(self) -> bool: ...
    def stop(self) -> None: ...


class MockRecorder:
    def __init__(self):
        self.calls: list[tuple] = []
        self.running = False

    def start(self, platform, login, out_dir, chunk_s):
        self.calls.append((platform, login, Path(out_dir), chunk_s))
        Path(out_dir).mkdir(parents=True, exist_ok=True)  # like the real recorder
        self.running = True

    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.running = False


STREAM_URLS = {"twitch": "https://www.twitch.tv/{login}", "kick": "https://kick.com/{login}",
               "youtube": "https://www.youtube.com/channel/{login}/live"}


class StreamlinkRecorder:
    """streamlink writes the stream to stdout and ffmpeg cuts it into fixed-length MPEG-TS chunks
    (chunk_00000.ts, chunk_00001.ts, ...) without re-encoding. 720p keeps disk use at about 2-3 GB per hour."""

    def __init__(self, quality: str = "720p60,720p,best", popen: Callable = subprocess.Popen,
                 segment_threads: int = 6):
        # Several segments at once: Docker on this PC capped each connection at ~80 KB/s (16 Nov 2026) while
        # 720p60 needs ~450 KB/s; one at a time fell behind and Twitch dropped runs of segments.
        self.quality, self._popen, self.segment_threads = quality, popen, segment_threads
        self._procs: list = []

    def start(self, platform: str, login: str, out_dir: Path, chunk_s: int) -> None:
        if platform not in STREAM_URLS:
            raise LiveError(f"cannot record '{platform}' streams")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        url = STREAM_URLS[platform].format(login=login)
        try:
            # (no --twitch-disable-ads: streamlink 8 skips ad segments itself and only warns about the flag)
            sl = self._popen(["streamlink", "--stdout", "--stream-segment-threads", str(self.segment_threads),
                              "--retry-streams", "5", "--retry-max", "3", url, self.quality], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
            ff = self._popen(["ffmpeg", "-y", "-v", "error", "-i", "pipe:0", "-c", "copy", "-f", "segment",
                              "-segment_time", str(chunk_s), "-reset_timestamps", "1",
                              str(out_dir / "chunk_%05d.ts")], stdin=sl.stdout, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise LiveError(f"could not start streamlink/ffmpeg ({e})") from None
        if getattr(sl, "stdout", None) is not None and hasattr(sl.stdout, "close"):
            sl.stdout.close()  # ffmpeg owns the pipe now
        self._procs = [sl, ff]

    def is_running(self) -> bool:
        return bool(self._procs) and all(p.poll() is None for p in self._procs[1:])

    def stop(self) -> None:
        for p in self._procs:
            try:
                p.terminate()
            except OSError:
                pass
        self._procs = []
