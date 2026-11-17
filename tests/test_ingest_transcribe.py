import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoclip.integrations.downloader import (ChatMessage, DownloadError, Media, MockDownloader, YtDlpDownloader,
                                              probe_duration)
from autoclip.integrations.transcriber import (FasterWhisperTranscriber, MockTranscriber, Segment, TranscribeError,
                                               TranscriptResult, Word)


# ------------------------------------------------------------ transcription
def W(word, start, end):
    return SimpleNamespace(word=word, start=start, end=end)


class FakeWhisper:
    loads = 0

    def __init__(self, name, device, compute_type):
        FakeWhisper.loads += 1

    def transcribe(self, path, vad_filter, word_timestamps):
        assert vad_filter is True and word_timestamps is True
        segs = [SimpleNamespace(start=0.004, end=2.5, text=" Hello there. ",
                                words=[W(" Hello", 0.0, 0.8), W(" there.", 0.9, 2.5)]),
                SimpleNamespace(start=2.5, end=3.0, text="   ", words=[]),
                SimpleNamespace(start=3.0, end=6.126, text="Second line.",
                                words=[W(" Second", 3.0, 4.0), W(" line.", 4.1, 6.126)])]
        return iter(segs), SimpleNamespace(language="en")


def test_word_timestamps_are_kept_monotonic_and_inside_segments():
    FakeWhisper.loads = 0
    t = FasterWhisperTranscriber("small", model_factory=FakeWhisper)
    r = t.transcribe(Path("a.mp4"))
    t.transcribe(Path("b.mp4"))
    assert FakeWhisper.loads == 1 and r.language == "en" and len(r.segments) == 2
    assert [w.w for w in r.words] == ["Hello", "there.", "Second", "line."]
    starts = [w.start for w in r.words]
    assert starts == sorted(starts)
    for s in r.segments:
        assert all(s.start <= w.start <= w.end <= s.end + 0.01 for w in s.words)


def test_segment_json_round_trip():
    s = Segment(1.0, 3.0, "hi there", [Word("hi", 1.0, 1.4), Word("there", 1.5, 3.0)])
    assert Segment.from_json(json.loads(json.dumps(s.to_json()))) == s


def test_transcriber_errors_are_wrapped_and_mock_works():
    class Broken:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            raise RuntimeError("corrupt file")

    with pytest.raises(TranscribeError, match="corrupt file"):
        FasterWhisperTranscriber("small", model_factory=Broken).transcribe(Path("x.mp4"))
    m = MockTranscriber(TranscriptResult("fr", [Segment(0, 1, "bonjour")]))
    assert m.transcribe(Path("x")).language == "fr" and m.calls == [Path("x")]
    with pytest.raises(TranscribeError):
        MockTranscriber(error="nope").transcribe(Path("x"))


# ------------------------------------------------------------ ingest
class FakeYDL:
    downloads = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def download(self, urls):
        FakeYDL.downloads.append((urls[0], self.opts))
        Path(self.opts["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"video")


def fake_run(calls):
    def run(args, timeout=3600):
        calls.append(args[0])
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"wav")
            return ""
        return json.dumps({"format": {"duration": "61.5"}})
    return run


def test_ingest_downloads_extracts_audio_and_reuses_files(tmp_path):
    FakeYDL.downloads, calls = [], []
    d = YtDlpDownloader(tmp_path, max_height=1080, ydl_factory=FakeYDL, run=fake_run(calls))
    media = d.fetch("youtube", "abc123")
    folder = tmp_path / "youtube_abc123"
    assert media.video == folder / "source.mp4" and media.audio == folder / "audio.wav"
    assert media.duration_s == 61.5 and media.chat == []
    url, opts = FakeYDL.downloads[0]
    assert url == "https://www.youtube.com/watch?v=abc123"
    assert "height<=1080" in opts["format"] and opts["writeinfojson"] is True
    assert opts["format"].startswith("bv*[vcodec^=avc1]")
    assert opts["concurrent_fragment_downloads"] == 8 and opts["fragment_retries"] == 10
    assert calls == ["ffmpeg", "ffprobe"]

    d.fetch("youtube", "abc123")
    assert len(FakeYDL.downloads) == 1 and calls == ["ffmpeg", "ffprobe", "ffprobe"]


def test_twitch_chat_replay_is_fetched_once_and_cached(tmp_path):
    FakeYDL.downloads, fetched = [], []

    def chat(url):
        fetched.append(url)
        return [ChatMessage(1.5, "LOL"), ChatMessage(3.0, "W")]

    d = YtDlpDownloader(tmp_path, ydl_factory=FakeYDL, run=fake_run([]), chat_fetcher=chat)
    m = d.fetch("twitch", "999")
    assert FakeYDL.downloads[0][0] == "https://www.twitch.tv/videos/999"
    assert "height<=720" in FakeYDL.downloads[0][1]["format"]  # long streams download at 720p
    assert [c.text for c in m.chat] == ["LOL", "W"] and fetched == ["https://www.twitch.tv/videos/999"]
    assert [c.t for c in d.fetch("twitch", "999").chat] == [1.5, 3.0] and len(fetched) == 1


def test_chat_failure_never_fails_ingest(tmp_path):
    def chat(url):
        raise RuntimeError("chat unavailable")

    m = YtDlpDownloader(tmp_path, ydl_factory=FakeYDL, run=fake_run([]), chat_fetcher=chat).fetch("kick", "v1")
    assert m.chat == []


def test_ingest_errors(tmp_path):
    class Failing(FakeYDL):
        def download(self, urls):
            raise RuntimeError("Video unavailable")

    with pytest.raises(DownloadError, match="Video unavailable"):
        YtDlpDownloader(tmp_path, ydl_factory=Failing, run=fake_run([])).fetch("youtube", "gone")
    with pytest.raises(DownloadError, match="unsupported platform"):
        YtDlpDownloader(tmp_path, ydl_factory=FakeYDL).fetch("vimeo", "1")

    def bad_probe(args, timeout=0):
        return "not json"

    with pytest.raises(DownloadError, match="duration"):
        probe_duration(Path("x"), run=bad_probe)


def test_mock_downloader():
    m = MockDownloader(Media(Path("x.mp4")))
    assert m.fetch("youtube", "a").video == Path("x.mp4") and m.calls == [("youtube", "a")]
    with pytest.raises(DownloadError):
        MockDownloader(error="blocked").fetch("youtube", "a")


def test_wav_is_passed_as_samples_not_a_path(tmp_path):
    import struct
    import wave
    f = tmp_path / "a.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(struct.pack("<4h", 0, 16384, -16384, 32767))
    seen = {}

    class Capture(FakeWhisper):
        def transcribe(self, audio, vad_filter, word_timestamps):
            seen["audio"] = audio
            return iter([]), SimpleNamespace(language="en")

    FasterWhisperTranscriber("small", model_factory=Capture).transcribe(f)
    assert list(seen["audio"]) == [0.0, 0.5, -0.5, 32767 / 32768]
    FasterWhisperTranscriber("small", model_factory=Capture).transcribe(tmp_path / "v.mp4")
    assert seen["audio"] == str(tmp_path / "v.mp4")


def test_gpu_failure_falls_back_to_cpu():
    made = []

    class GpuMissing(FakeWhisper):
        def __init__(self, name, device, compute_type):
            made.append((device, compute_type))
            if device == "cuda":
                raise RuntimeError("CUDA driver not found")

    t = FasterWhisperTranscriber("small", "cuda", "float16", model_factory=GpuMissing)
    assert t.name == "faster-whisper:small:cuda"
    t.transcribe(Path("a.mp4"))
    assert made == [("cuda", "float16"), ("cpu", "int8")]
    assert t.name == "faster-whisper:small:cpu" and "using CPU" in t.fallback_reason


def test_library_file_is_linked_not_moved(tmp_path):
    lib = tmp_path / "library" / "streamhouse"
    lib.mkdir(parents=True)
    (lib / "Ep 1.mp4").write_bytes(b"video")
    d = YtDlpDownloader(tmp_path, run=fake_run([]))
    m = d.fetch("file", "streamhouse/Ep 1.mp4")
    assert m.video.read_bytes() == b"video" and m.video.parent.name.startswith("file_")
    import shutil as _sh
    _sh.rmtree(m.video.parent)  # what the per-video cleanup does
    assert (lib / "Ep 1.mp4").exists()  # the library copy survives
    with pytest.raises(DownloadError):
        d.fetch("file", "../../etc/passwd")


def test_release_frees_the_model_and_it_reloads_on_next_use():
    FakeWhisper.loads = 0
    t = FasterWhisperTranscriber("small", model_factory=FakeWhisper)
    t.transcribe(Path("a.mp4"))
    t.release()
    assert t._model is None
    t.transcribe(Path("b.mp4"))
    assert FakeWhisper.loads == 2


def test_long_wav_is_transcribed_in_chunks_with_correct_times(tmp_path):
    import wave
    from types import SimpleNamespace

    import numpy as np
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(np.zeros(16000 * 5, dtype=np.int16).tobytes())  # 5 s
    sizes = []

    class Chunky:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, audio, **kw):
            sizes.append(len(audio))
            seg = SimpleNamespace(start=0.5, end=1.0, text="hi",
                                  words=[SimpleNamespace(word="hi", start=0.5, end=1.0)])
            return iter([seg]), SimpleNamespace(language="en", duration=len(audio) / 16000)

    t = FasterWhisperTranscriber("small", model_factory=Chunky)
    t.CHUNK_S = 2
    r = t.transcribe(path)
    assert sizes == [32000, 32000, 16000]  # never the whole file at once
    assert [s.start for s in r.segments] == [0.5, 2.5, 4.5] and r.words[2].start == 4.5 and r.language == "en"
