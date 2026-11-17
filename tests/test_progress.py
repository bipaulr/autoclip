import json
from pathlib import Path
from types import SimpleNamespace

from autoclip.integrations.transcriber import FasterWhisperTranscriber
from autoclip.progress import KEY, RedisProgress, read


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, k, v, ex=None):
        self.store[k] = v

    def get(self, k):
        return self.store.get(k)


def test_progress_tracks_stages_and_finish():
    r = FakeRedis()
    p = RedisProgress("redis://x", {"video_id": "v1", "title": "T"}, client=r)
    p("downloading", "10 of 100 MB", 10)
    p("downloading", "50 of 100 MB", 50)
    p("transcribing", "3 of 57 min", 5)
    state = read(r)
    assert state["stage"] == "transcribing" and state["pct"] == 5 and state["running"]
    assert [s["stage"] for s in state["stages"]] == ["downloading", "transcribing"]
    assert "ended" in state["stages"][0]
    p.finish("scheduled: 3 posts")
    done = json.loads(r.store[KEY])
    assert done["running"] is False and done["detail"] == "scheduled: 3 posts"


def test_progress_never_breaks_the_pipeline():
    class Broken:
        def set(self, *a, **k):
            raise ConnectionError("redis down")

        def get(self, k):
            raise ConnectionError("redis down")

    p = RedisProgress("redis://x", {}, client=Broken())
    p("downloading")
    p.finish("ok")
    assert read(Broken()) is None


def test_transcription_reports_percent():
    class Model:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, audio, vad_filter, word_timestamps):
            segs = [SimpleNamespace(start=0, end=30, text="a", words=[]), SimpleNamespace(start=30, end=60, text="b", words=[])]
            return iter(segs), SimpleNamespace(language="en", duration=120.0)

    seen = []
    FasterWhisperTranscriber("small", model_factory=Model).transcribe(Path("x.mp4"), on_progress=lambda p, d: seen.append(p))
    assert seen == [25.0, 50.0]
