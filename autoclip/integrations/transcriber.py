"""Speech-to-text behind an interface: faster-whisper locally, or a mock."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol


class TranscribeError(Exception):
    pass


@dataclass
class Word:
    w: str
    start: float
    end: float


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"s": self.start, "e": self.end, "t": self.text,
                "w": [[x.w, x.start, x.end] for x in self.words]}

    @classmethod
    def from_json(cls, d: dict) -> "Segment":
        return cls(d["s"], d["e"], d["t"], [Word(*w) for w in d.get("w", [])])


@dataclass
class TranscriptResult:
    language: str
    segments: list[Segment] = field(default_factory=list)

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]


class Transcriber(Protocol):
    name: str

    def transcribe(self, media_path: Path, on_progress=None) -> TranscriptResult: ...


class MockTranscriber:
    name = "mock"

    def __init__(self, result: TranscriptResult | None = None, error: str | None = None):
        self.result, self.error = result or TranscriptResult("en"), error
        self.calls: list[Path] = []
        self.released = 0

    def release(self) -> None:
        self.released += 1

    def transcribe(self, media_path: Path, on_progress=None) -> TranscriptResult:
        self.calls.append(Path(media_path))
        if self.error:
            raise TranscribeError(self.error)
        return self.result


class FasterWhisperTranscriber:
    """Word-level timestamps on CPU (int8) or GPU. The model loads on first use;
    model_factory is injectable so tests need no model files."""

    def __init__(self, model_name: str, device: str = "cpu", compute_type: str = "int8",
                 model_factory: Callable | None = None):
        self.model_name, self.device, self.compute_type = model_name, device, compute_type
        self.model_factory = model_factory
        self.name = f"faster-whisper:{model_name}" + (f":{device}" if device != "cpu" else "")
        self._model = None
        self.fallback_reason: str | None = None

    def _load(self):
        if self._model is None:
            factory = self.model_factory
            if factory is None:
                from faster_whisper import WhisperModel  # heavy import, only when really needed
                factory = WhisperModel
            try:
                self._model = factory(self.model_name, device=self.device, compute_type=self.compute_type)
            except Exception as e:  # noqa: BLE001 - no GPU, driver or CUDA libraries: keep working on CPU
                if self.device == "cpu":
                    raise
                self.fallback_reason = f"{self.device} unavailable ({type(e).__name__}); using CPU"
                self.device, self.compute_type = "cpu", "int8"
                self.name = f"faster-whisper:{self.model_name}:cpu"
                self._model = factory(self.model_name, device="cpu", compute_type="int8")
        return self._model

    CHUNK_S = 20 * 60  # a 4-hour stream in one piece needs several GB and got the worker killed

    @staticmethod
    def _wav_chunks(path: Path, chunk_s: int):
        """(offset seconds, float32 samples) pieces of a 16 kHz mono 16-bit WAV, or None for other files."""
        import wave

        import numpy as np
        path = Path(path)
        if path.suffix.lower() != ".wav":
            return None
        w = wave.open(str(path), "rb")
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            w.close()
            return None

        def gen():
            with w:
                offset, frames = 0.0, chunk_s * 16000
                while True:
                    data = w.readframes(frames)
                    if not data:
                        return
                    yield offset, np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    offset += len(data) / 2 / 16000
        return gen(), w.getnframes() / 16000

    def transcribe(self, media_path: Path, on_progress=None) -> TranscriptResult:
        out, language = [], None
        try:
            chunked = self._wav_chunks(media_path, self.CHUNK_S)
            pieces, total = chunked if chunked else ([(0.0, str(media_path))], 0.0)
            for offset, audio in pieces:
                segments, info = self._load().transcribe(audio, vad_filter=True, word_timestamps=True)
                language = language or info.language
                total = total or float(getattr(info, "duration", 0) or 0)
                for s in segments:
                    end = offset + float(s.end)
                    if on_progress and total:
                        on_progress(100 * end / total, f"{end / 60:.0f} of {total / 60:.0f} min")
                    text = s.text.strip()
                    if not text:
                        continue
                    words = [Word(w.word.strip(), round(offset + float(w.start), 2), round(offset + float(w.end), 2))
                             for w in (getattr(s, "words", None) or []) if w.word.strip()]
                    out.append(Segment(round(offset + float(s.start), 2), round(end, 2), text, words))
                del audio
        except Exception as e:  # the library raises many types; none carry secrets
            raise TranscribeError(f"{type(e).__name__}: {str(e)[:200]}") from None
        return TranscriptResult(language=language or "en", segments=out)

    def release(self) -> None:
        """Frees the model's GPU memory. On a 4 GB card the local LLM fallback (Ollama) needs that
        memory right after transcription; the model is loaded again for the next video."""
        if self._model is not None:
            self._model = None
            import gc
            gc.collect()
