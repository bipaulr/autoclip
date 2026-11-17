import math
import struct
import wave

from autoclip.integrations.downloader import ChatMessage
from autoclip.pipeline.signals import (Timeline, build_timeline, chat_activity, find_peaks, loudness_per_second,
                                       shot_cuts, zscores)


def write_wav(path, seconds=30, loud_at=12, rate=16000):
    """Quiet tone everywhere, one loud burst during second `loud_at`."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(seconds * rate):
            amp = 20000 if loud_at * rate <= i < (loud_at + 1) * rate else 600
            frames += struct.pack("<h", int(amp * math.sin(2 * math.pi * 220 * i / rate)))
        w.writeframes(bytes(frames))


def test_loud_burst_produces_a_peak_at_the_right_second(tmp_path):
    f = tmp_path / "a.wav"
    write_wav(f)
    db = loudness_per_second(f)
    assert len(db) == 30
    assert find_peaks(db) == [12]


def test_chat_burst_spikes_with_keywords():
    chat = [ChatMessage(t, "hello") for t in range(0, 300, 10)]
    chat += [ChatMessage(120 + i * 0.1, "NO WAY he did that LMAO") for i in range(60)]
    counts, spikes, keywords = chat_activity(chat, 300)
    assert len(counts) == 30 and counts[12] == 61
    assert spikes == [120]
    assert "WAY" in keywords[120] and "LMAO" in keywords[120] and "that" not in keywords[120]


def test_quiet_chat_has_no_spikes():
    counts, spikes, _ = chat_activity([ChatMessage(t, "hi") for t in range(0, 100, 5)], 100)
    assert spikes == []


def test_zscores_handles_flat_and_empty():
    assert zscores([]) == [] and zscores([3, 3, 3]) == [0.0, 0.0, 0.0]


def test_shot_cuts_parses_ffmpeg_showinfo():
    stderr = ("[Parsed_showinfo_2] n:0 pts:40 pts_time:10.004 duration:1\n"
              "[Parsed_showinfo_2] n:1 pts:97 pts_time:24.25 duration:1\n")
    seen = {}

    def run(args):
        seen["args"] = args
        return stderr

    assert shot_cuts("v.mp4", run=run) == [10.0, 24.25]
    assert any("select='gt(scene,0.35)'" in a for a in seen["args"]) and "nokey" in seen["args"]


def test_timeline_build_summary_and_json(tmp_path):
    f = tmp_path / "a.wav"
    write_wav(f, seconds=40, loud_at=20)
    chat = [ChatMessage(25 + i * 0.1, "CLIP IT") for i in range(50)] + [ChatMessage(t, "x") for t in range(0, 200, 4)]

    def run(args):
        raise RuntimeError("no ffmpeg in tests")

    tl = build_timeline(f, tmp_path / "missing.mp4", chat, 200.0, run=run)
    assert tl.loud_peaks == [20] and tl.chat_spikes == [20] and tl.shot_cuts == []
    summary = tl.summary(15, 35)
    assert "1 loudness peak" in summary and "1 chat spike" in summary and "CLIP" in summary
    assert Timeline.from_json(tl.to_json()) == tl


def test_timeline_without_audio_or_chat():
    tl = build_timeline(None, None, [], 100.0)
    assert tl.duration_s == 100 and tl.loud_peaks == [] and tl.chat_spikes == []
