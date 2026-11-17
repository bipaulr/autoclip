import pytest

from autoclip.integrations.live import (LiveError, MockChatReader, MockRecorder, StreamlinkRecorder,
                                        TwitchAnonChat, parse_privmsg)


def test_parse_privmsg_extracts_text_and_ignores_other_lines():
    line = "@badge-info=;color=#fff :bob!bob@bob.tmi.twitch.tv PRIVMSG #speed :NO WAY LMAO"
    assert parse_privmsg(line + "\r\n") == "NO WAY LMAO"
    assert parse_privmsg(":bob!bob@bob.tmi.twitch.tv PRIVMSG #speed :hi: there") == "hi: there"
    assert parse_privmsg("PING :tmi.twitch.tv") is None
    assert parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome") is None


class FakeSocket:
    def __init__(self, chunks):
        self.chunks, self.sent, self.closed = list(chunks), [], False

    def settimeout(self, _):
        pass

    def sendall(self, b):
        self.sent.append(b.decode())

    def recv(self, _):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


def test_anonymous_chat_logs_in_without_a_token_and_collects_messages():
    sock = FakeSocket([b"PING :tmi.twitch.tv\r\n:a!a@a PRIVMSG #speed :one\r\n:b!b@b PRIVMSG #sp",
                       b"eed :two\r\n"])
    chat = TwitchAnonChat(connect=lambda: sock, clock=lambda: 100.0)
    chat._session("speed")
    assert chat.drain() == [(100.0, "one"), (100.0, "two")]
    assert chat.drain() == []
    assert "PASS SCHMOOPIIE\r\n" in sock.sent and "JOIN #speed\r\n" in sock.sent
    assert any(s.startswith("NICK justinfan") for s in sock.sent)  # anonymous: no account
    assert "PONG :tmi.twitch.tv\r\n" in sock.sent and sock.closed


def test_mock_chat_reader_drains_once():
    c = MockChatReader([(1.0, "x")])
    c.start("speed")
    assert c.drain() == [(1.0, "x")] and c.drain() == [] and c.started == ["speed"]


class FakeProc:
    def __init__(self, alive=True):
        self.stdout, self.alive, self.terminated = None, alive, False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated, self.alive = True, False


def test_streamlink_recorder_pipes_into_ffmpeg_segments(tmp_path):
    calls = []

    def popen(args, **kw):
        calls.append((args, kw))
        return FakeProc()

    rec = StreamlinkRecorder(popen=popen)
    rec.start("twitch", "speed", tmp_path / "live", 300)
    sl, ff = calls[0][0], calls[1][0]
    assert sl[0] == "streamlink" and "https://www.twitch.tv/speed" in sl
    assert ff[0] == "ffmpeg" and "segment" in ff and "300" in ff and ff[-1].endswith("chunk_%05d.ts")
    assert rec.is_running()
    rec.stop()
    assert not rec.is_running()


def test_recorder_refuses_unknown_platform_and_missing_binary(tmp_path):
    with pytest.raises(LiveError):
        StreamlinkRecorder(popen=lambda *a, **k: FakeProc()).start("tiktok", "x", tmp_path, 60)

    def boom(*a, **k):
        raise FileNotFoundError("streamlink")

    with pytest.raises(LiveError):
        StreamlinkRecorder(popen=boom).start("twitch", "x", tmp_path, 60)


def test_mock_recorder_tracks_state(tmp_path):
    r = MockRecorder()
    r.start("twitch", "x", tmp_path, 60)
    assert r.is_running() and r.calls[0][1] == "x"
    r.stop()
    assert not r.is_running()


def test_recorder_downloads_segments_in_parallel_and_drops_the_dead_ads_flag(tmp_path):
    # Docker on this PC caps each connection at ~80 KB/s (16 Nov 2026); 720p60 needs ~450 KB/s, so one
    # segment at a time fell behind and Twitch dropped whole runs of segments ("Sequence gap").
    calls = []
    rec = StreamlinkRecorder(popen=lambda args, **kw: calls.append(args) or FakeProc(), segment_threads=6)
    rec.start("twitch", "speed", tmp_path / "live", 300)
    sl = calls[0]
    assert sl[sl.index("--stream-segment-threads") + 1] == "6"
    assert "--twitch-disable-ads" not in sl  # disabled in streamlink 8: it only logs a warning
