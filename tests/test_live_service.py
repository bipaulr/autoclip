import json
from datetime import date, datetime, timezone

from sqlalchemy import select

from autoclip.models import Campaign, DecisionLog, SourceVideo
from autoclip.services.live import (LiveJob, allowed_channels, chunk_files, finalize_chunks, mark_recorded,
                                    recorded_recently)
from autoclip.sources import ChannelSource, Sources

NOW = datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc)
CH = ChannelSource("twitch", "speed", "IShowSpeed", "campaign terms accepted 2026-11-18", campaign_id=None,
                   profile="gaming")


def fake_run(args):
    # "ffmpeg ... -i chunk -c copy ... out.mp4": create the output
    open(args[-1], "wb").write(b"x")
    return ""


def make_job(tmp_path, n=3, started=1000.0, chat=None):
    folder = tmp_path / "rec"
    folder.mkdir()
    for i in range(n):
        (folder / f"chunk_{i:05d}.ts").write_bytes(b"ts")
    return LiveJob(CH, folder, started, 300, chat=chat or [])


def test_chunk_files_sorted_numerically(tmp_path):
    job = make_job(tmp_path, 3)
    assert [i for i, _ in chunk_files(job.folder)] == [0, 1, 2]


def test_newest_chunk_is_left_alone_while_recording(session, tmp_path):
    job = make_job(tmp_path, 3)
    ids = finalize_chunks(session, job, tmp_path / "media", NOW, recording=True, run=fake_run,
                          probe=lambda p: 300.0)
    assert len(ids) == 2 and job.done == {0, 1}
    assert (job.folder / "chunk_00002.ts").exists()  # still being written
    assert not (job.folder / "chunk_00000.ts").exists()  # remuxed chunks are deleted at once


def test_all_chunks_taken_once_the_stream_ended(session, tmp_path):
    job = make_job(tmp_path, 2)
    assert len(finalize_chunks(session, job, tmp_path / "m", NOW, recording=False, run=fake_run,
                               probe=lambda p: 300.0)) == 2
    assert finalize_chunks(session, job, tmp_path / "m", NOW, recording=False, run=fake_run,
                           probe=lambda p: 300.0) == []


def test_source_video_inherits_rights_and_gets_chat_and_priority(session, tmp_path):
    chat = [(1000 + 35 + i * 0.1, "NO WAY") for i in range(5)] + [(1000 + 400, "other chunk")]
    job = make_job(tmp_path, 2, chat=chat)
    ids = finalize_chunks(session, job, tmp_path / "m", NOW, recording=True, run=fake_run, probe=lambda p: 300.0)
    v = session.scalars(select(SourceVideo)).one()
    assert v.video_id == ids[0] and v.status == "discovered" and v.rank_score >= 50
    assert v.rights_basis == CH.basis and v.profile == "gaming" and v.duration_s == 300
    saved = json.loads((tmp_path / "m" / f"twitch_{v.video_id}" / "chat.json").read_text())
    assert len(saved) == 5 and saved[0][0] == 20.0  # relative to the chunk, other chunk's chat excluded
    assert any(d.action == "chunk_queued" for d in session.scalars(select(DecisionLog)))
    assert [m[1] for m in job.chat] == ["other chunk"]  # consumed chat is dropped


def test_short_tail_and_unreadable_chunks_are_skipped_with_a_reason(session, tmp_path):
    job = make_job(tmp_path, 2)
    durations = iter([10.0, 0])

    def probe(p):
        d = next(durations)
        if d == 0:
            raise RuntimeError("corrupt")
        return d

    assert finalize_chunks(session, job, tmp_path / "m", NOW, recording=False, run=fake_run, probe=probe) == []
    reasons = [d.reason for d in session.scalars(select(DecisionLog)) if d.action == "skipped"]
    assert any("only 10s" in r for r in reasons) and any("could not be read" in r for r in reasons)
    assert list(session.scalars(select(SourceVideo))) == []


def test_allowlist_is_rights_gated_by_dates_and_platform(session):
    src = Sources(channels=[ChannelSource("twitch", "a", "A", "permission")],
                  campaigns=[ChannelSource("twitch", "b", "B", "campaign", starts=date(2026, 11, 1),
                                           ends=date(2026, 11, 10)),
                             ChannelSource("kick", "c", "C", "campaign", starts=date(2026, 11, 1),
                                           ends=date(2026, 12, 1)),
                             ChannelSource("tiktok", "d", "D", "x")])
    session.add(Campaign(marketplace="m", external_id="1", name="GTA", url="u", status="joined",
                         source_channels=[{"platform": "twitch", "channel_id": "A"}], ends=date(2026, 12, 1)))
    session.flush()
    got = {(c.platform, c.channel_id) for c in allowed_channels(src, session, date(2026, 11, 19))}
    assert got == {("twitch", "a"), ("kick", "c")}  # B ended, D is not live-recordable, duplicate A once


def test_live_recorded_marker_expires(session):
    mark_recorded(session, CH, NOW)
    assert recorded_recently(session, CH, NOW)
    assert not recorded_recently(session, CH, datetime(2026, 11, 21, 21, tzinfo=timezone.utc))


def test_chat_is_moved_back_by_the_reaction_lag(session, tmp_path):
    # the moment is at 290 s of chunk 0; chat reacts 15 s later, i.e. in the first seconds of chunk 1
    chat = [(1000 + 305 + i * 0.1, "OMG") for i in range(5)]
    job = make_job(tmp_path, 2, chat=chat)
    job.chat_lag_s = 15
    finalize_chunks(session, job, tmp_path / "m", NOW, recording=True, run=fake_run, probe=lambda p: 300.0)
    v = session.scalars(select(SourceVideo)).one()
    saved = json.loads((tmp_path / "m" / f"twitch_{v.video_id}" / "chat.json").read_text())
    assert len(saved) == 5 and saved[0][0] == 290.0  # attributed to the chunk where the moment happened


def overlap_probe(p):
    # raw chunks are 300 s; a joined source is 300 s + the 30 s lead-in
    return 300.0 if str(p).endswith(".ts") else (330.0 if "_1" in str(p) or "_2" in str(p) else 300.0)


def test_each_chunk_starts_with_the_end_of_the_previous_one(session, tmp_path):
    calls, lists = [], []

    def run(args):
        calls.append(args)
        if "concat" in args:
            lists.append(open(args[args.index("-i") + 1], encoding="utf-8").read())
        return fake_run(args)

    chat = [(1000 + 280 + 15, "lead-in moment"), (1000 + 400 + 15, "main moment")]
    job = make_job(tmp_path, 3, chat=chat)
    ids = finalize_chunks(session, job, tmp_path / "m", NOW, recording=True, run=run, probe=overlap_probe)
    assert len(ids) == 2
    joined = calls[1]
    assert joined[joined.index("-f") + 1] == "concat"  # the demuxer, not the 'concat:' protocol
    assert joined[-1].endswith("source.mp4")
    assert "chunk_00000.ts'\ninpoint 270.00\nfile '" in lists[0] and "chunk_00001.ts'" in lists[0]
    folder = tmp_path / "m" / f"twitch_{ids[1]}"
    assert json.loads((folder / "live.json").read_text())["lead_s"] == 30.0
    assert not (folder / "join.txt").exists()
    saved = json.loads((folder / "chat.json").read_text())
    # chunk 1 starts at 300 s - 30 s lead-in = 270 s: the 280 s moment is at 10 s, the 400 s one at 130 s
    assert [m[0] for m in saved] == [10.0, 130.0]
    assert not (job.folder / "chunk_00000.ts").exists()  # its lead-in was taken
    assert (job.folder / "chunk_00001.ts").exists()  # kept as the lead-in for chunk 2
    assert (job.folder / "chunk_00002.ts").exists()  # still being written


def test_stream_end_deletes_every_raw_chunk(session, tmp_path):
    job = make_job(tmp_path, 2)
    finalize_chunks(session, job, tmp_path / "m", NOW, recording=False, run=fake_run, probe=overlap_probe)
    assert list(job.folder.glob("*.ts")) == []


def test_first_chunk_has_no_lead_in(session, tmp_path):
    job = make_job(tmp_path, 1)
    ids = finalize_chunks(session, job, tmp_path / "m", NOW, recording=False, run=fake_run, probe=overlap_probe)
    assert json.loads((tmp_path / "m" / f"twitch_{ids[0]}" / "live.json").read_text())["lead_s"] == 0.0


def test_a_join_that_loses_the_new_chunk_falls_back_to_the_chunk_alone(session, tmp_path):
    # the bug seen on real Twitch chunks: the joined file held only the 30 s lead-in
    last = {"concat": False}

    def probe(p):  # a joined file holds only the 30 s lead-in; anything else is a full 300 s
        return 30.0 if not str(p).endswith(".ts") and last["concat"] else 300.0

    def run(args):
        last["concat"] = "concat" in args
        return fake_run(args)

    job = make_job(tmp_path, 2)
    ids = finalize_chunks(session, job, tmp_path / "m", NOW, recording=False, run=run, probe=probe)
    assert len(ids) == 2  # chunk 1 kept, not skipped as "only 30s long"
    assert json.loads((tmp_path / "m" / f"twitch_{ids[1]}" / "live.json").read_text())["lead_s"] == 0.0
    assert any(d.action == "fallback" for d in session.scalars(select(DecisionLog)))


def test_campaign_named_after_the_streamer_wins_over_a_brand_campaign(session):
    session.add(Campaign(marketplace="cr", external_id="brand", name="$5,000 TapCap Clipping Campaign", url="u1",
                         status="joined", source_channels=[{"platform": "twitch", "channel_id": "lacy"}]))
    session.add(Campaign(marketplace="cr", external_id="pool", name="$30,000 LACY 30 DAY CAMPAIGN (Reach)", url="u2",
                         status="joined", source_channels=[{"platform": "twitch", "channel_id": "lacy"}]))
    session.flush()
    got = allowed_channels(Sources(), session, date(2026, 11, 19))
    assert [c.name for c in got] == ["$30,000 LACY 30 DAY CAMPAIGN (Reach)"]


def test_sources_yaml_permission_is_never_replaced_by_a_campaign(session):
    session.add(Campaign(marketplace="cr", external_id="pool", name="LACY pool", url="u2", status="joined",
                         source_channels=[{"platform": "twitch", "channel_id": "lacy"}]))
    session.flush()
    src = Sources(channels=[ChannelSource("twitch", "lacy", "Lacy", "written permission")])
    got = allowed_channels(src, session, date(2026, 11, 19))
    assert len(got) == 1 and got[0].basis == "written permission"
