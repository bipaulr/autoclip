from datetime import date, datetime, timezone

from sqlalchemy import select

from autoclip.config import Settings
from autoclip.integrations.feeds import FeedError
from autoclip.integrations.live import MockChatReader, MockRecorder
from autoclip.live_daemon import LiveManager
from autoclip.models import DecisionLog, SourceVideo
from autoclip.sources import ChannelSource, Sources

SRC = Sources(channels=[ChannelSource("twitch", "speed", "IShowSpeed", "campaign terms accepted 2026-11-18"),
                        ChannelSource("twitch", "kai", "Kai", "written permission")])


class Client:
    def __init__(self, live=(), error=None):
        self.live, self.error = set(live), error

    def live_logins(self, logins):
        if self.error:
            raise FeedError(self.error)
        return {x for x in logins if x in self.live}


def settings(tmp_path, **kw):
    kw.setdefault("live_min_free_gb", 0)
    return Settings(media_dir=str(tmp_path / "media"), live_chunk_seconds=300, **kw)


def fake_run(args):
    open(args[-1], "wb").write(b"x")
    return ""


def manager(tmp_path, session_factory, client, recs, chats, triggered, **kw):
    return LiveManager(settings(tmp_path, **kw), session_factory, twitch=client, kick=None, sources=SRC,
                       recorder_factory=lambda: recs.append(MockRecorder()) or recs[-1],
                       chat_factory=lambda: chats.append(MockChatReader()) or chats[-1],
                       trigger=triggered.extend, clock=lambda: 1_800_000_000.0, run=fake_run,
                       probe=lambda p: 300.0)


def test_goes_live_records_reads_chat_and_queues_closed_chunks(tmp_path, session_factory):
    recs, chats, trig = [], [], []
    m = manager(tmp_path, session_factory, Client({"speed"}), recs, chats, trig)
    m.tick()
    assert len(recs) == 1 and recs[0].calls[0][:2] == ("twitch", "speed") and chats[0].started == ["speed"]
    folder = recs[0].calls[0][2]
    for i in range(2):
        (folder / f"chunk_{i:05d}.ts").write_bytes(b"ts")
    chats[0].messages = [(1_800_000_020.0 + i * 0.1, "NO WAY") for i in range(30)]
    m.tick()  # chunk 0 closed (chunk 1 still being written)
    assert len(trig) == 1 and trig[0].startswith("live_speed_")
    with session_factory() as s:
        v = s.scalars(select(SourceVideo)).one()
        assert v.status == "discovered" and v.rights_basis.startswith("campaign terms")


def test_second_tick_does_not_start_a_second_recorder(tmp_path, session_factory):
    recs, chats, trig = [], [], []
    m = manager(tmp_path, session_factory, Client({"speed"}), recs, chats, trig)
    m.tick()
    m.tick()
    assert len(recs) == 1


def test_stream_end_closes_the_job_and_takes_the_last_chunk(tmp_path, session_factory):
    recs, chats, trig = [], [], []
    m = manager(tmp_path, session_factory, Client({"speed"}), recs, chats, trig)
    m.tick()
    (recs[0].calls[0][2] / "chunk_00000.ts").write_bytes(b"ts")
    recs[0].stop()  # streamlink exits when the stream ends
    m.tick()
    assert m.jobs == {} and chats[0].stopped and len(trig) == 1
    with session_factory() as s:
        assert "live_ended" in {d.action for d in s.scalars(select(DecisionLog))}


def test_channels_outside_the_allowlist_are_never_recorded(tmp_path, session_factory):
    recs, chats, trig = [], [], []
    m = manager(tmp_path, session_factory, Client({"randomstreamer"}), recs, chats, trig)
    m.tick()
    assert recs == []


def test_respects_the_max_simultaneous_streams(tmp_path, session_factory):
    recs, chats, trig = [], [], []
    m = manager(tmp_path, session_factory, Client({"speed", "kai"}), recs, chats, trig, live_max_streams=1)
    m.tick()
    assert len(recs) == 1
    with session_factory() as s:
        assert any("already being recorded" in d.reason for d in s.scalars(select(DecisionLog)))


def test_low_disk_space_pauses_instead_of_recording(tmp_path, session_factory, monkeypatch):
    recs, chats, trig = [], [], []
    m = manager(tmp_path, session_factory, Client({"speed"}), recs, chats, trig, live_min_free_gb=10**6)  # a petabyte
    m.tick()
    assert recs == []
    with session_factory() as s:
        assert any(d.action == "paused" and "free" in d.reason for d in s.scalars(select(DecisionLog)))


def test_a_failing_live_check_is_logged_not_raised(tmp_path, session_factory):
    recs, chats, trig = [], [], []
    m = manager(tmp_path, session_factory, Client(error="HTTP 500"), recs, chats, trig)
    m.tick()
    assert recs == []
    with session_factory() as s:
        assert any("live check failed" in d.reason for d in s.scalars(select(DecisionLog)))


def test_repeated_live_check_failures_alert_once_then_report_recovery(tmp_path, session_factory):
    recs, chats, trig, sent = [], [], [], []
    client = Client(error="HTTP 401")
    m = manager(tmp_path, session_factory, client, recs, chats, trig)
    m.notify = sent.append
    for _ in range(12):
        m.tick()
    assert len(sent) == 1 and "10 times" in sent[0] and "NOT being recorded" in sent[0]
    with session_factory() as s:
        assert len([d for d in s.scalars(select(DecisionLog)) if d.action == "paused"]) == 2  # 1st and 10th only
    client.error = None
    m.tick()
    assert len(sent) == 2 and "works again" in sent[1]


def test_leftover_recordings_are_picked_up_after_a_restart(tmp_path, session_factory):
    old = tmp_path / "media" / "live" / "speed_1799990000"
    old.mkdir(parents=True)
    for i in range(3):
        (old / f"chunk_{i:05d}.ts").write_bytes(b"ts")
    stranger = tmp_path / "media" / "live" / "randomguy_1799990000"
    stranger.mkdir()
    recs, chats, trig = [], [], []
    m = manager(tmp_path, session_factory, Client(), recs, chats, trig)
    m.tick()
    assert len(trig) == 3 and all(t.startswith("live_speed_") for t in trig)  # all chunks, incl. the last one
    assert not list(old.glob("*.ts"))
    with session_factory() as s:
        actions = [d.action for d in s.scalars(select(DecisionLog))]
        assert "recovered" in actions and "skipped" in actions  # the stranger is not allowlisted
    m.tick()
    assert len(trig) == 3  # adopted once only


def test_recorder_crash_while_still_live_starts_a_new_recording(tmp_path, session_factory):
    recs, chats, trig = [], [], []
    m = manager(tmp_path, session_factory, Client({"speed"}), recs, chats, trig)
    m.tick()
    recs[0].stop()  # streamlink died, but the channel is still live
    m.tick()  # job closed
    m.tick()  # still live: recording again
    assert len(recs) == 2 and recs[1].is_running()


class GameClient(Client):
    def __init__(self, games):
        super().__init__(set(games))
        self.games = games

    def live_streams(self, logins):
        return {x: {"game": self.games[x], "title": ""} for x in logins if x in self.games}


def test_gta_stream_turns_focus_on_and_its_chunks_are_tagged(tmp_path, session_factory):
    recs, chats, trig, sent = [], [], [], []
    m = manager(tmp_path, session_factory, GameClient({"speed": "Grand Theft Auto VI"}), recs, chats, trig)
    m.notify = sent.append
    m.tick()
    assert any("GTA focus mode ON" in t for t in sent)
    (recs[0].calls[0][2] / "chunk_00000.ts").write_bytes(b"ts")
    recs[0].stop()
    m.tick()
    with session_factory() as s:
        assert s.scalars(select(SourceVideo)).one().title.startswith("GTA 6 · IShowSpeed LIVE")


def test_gta_stream_takes_a_full_slot_from_a_non_gta_recording(tmp_path, session_factory):
    recs, chats, trig = [], [], []
    client = GameClient({"kai": "Just Chatting"})
    m = manager(tmp_path, session_factory, client, recs, chats, trig, live_max_streams=1)
    m.tick()
    assert [r.calls[0][1] for r in recs] == ["kai"]
    client.games["speed"] = "Grand Theft Auto VI"
    client.live.add("speed")
    m.tick()
    assert not recs[0].is_running() and recs[1].calls[0][1] == "speed" and recs[1].is_running()
    with session_factory() as s:
        assert any(d.action == "preempted" for d in s.scalars(select(DecisionLog)))


def test_gta_never_preempts_another_gta_stream(tmp_path, session_factory):
    recs, chats, trig = [], [], []
    client = GameClient({"kai": "Grand Theft Auto VI"})
    m = manager(tmp_path, session_factory, client, recs, chats, trig, live_max_streams=1)
    m.tick()
    client.games["speed"] = "Grand Theft Auto VI"
    client.live.add("speed")
    m.tick()
    assert len(recs) == 1 and recs[0].is_running()
