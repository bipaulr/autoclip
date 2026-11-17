"""Live chat spike detection: count messages per 10 s window and flag windows far above the
recent baseline. Unlike signals.chat_activity (one z-score over a finished video) the baseline here is
the trailing few minutes, so it works on a stream that is still running and adapts to chat speed."""
import math
import re
from dataclasses import dataclass, field

WINDOW_S = 10
WORD = re.compile(r"[A-Za-z][A-Za-z0-9']{1,20}")
STOP = {"the", "and", "you", "that", "this", "for", "are", "was", "what", "with", "his", "him", "she", "her",
        "they", "have", "just", "but", "not", "its", "it's", "can", "dont", "don't", "all", "get", "hi"}


@dataclass
class Spike:
    start: float  # seconds from the start of the chat log
    end: float
    count: int  # messages in the busiest window of the spike
    keywords: list[str] = field(default_factory=list)

    @property
    def peak_t(self) -> float:
        return self.start


def window_counts(messages: list[tuple[float, str]], duration_s: float) -> list[int]:
    counts = [0] * max(1, math.ceil(duration_s / WINDOW_S))
    for t, _ in messages:
        i = int(t // WINDOW_S)
        if 0 <= i < len(counts):
            counts[i] += 1
    return counts


def _keywords(texts: list[str]) -> list[str]:
    freq: dict[str, int] = {}
    for text in texts:
        for w in WORD.findall(text):
            w = w if w.isupper() else w.lower()
            if w.lower() not in STOP:
                freq[w] = freq.get(w, 0) + 1
    return [k for k, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:5]]


def detect_spikes(messages: list[tuple[float, str]], duration_s: float, *, z: float = 3.0,
                  min_count: int = 15, baseline_windows: int = 36, ratio: float = 2.5) -> list[Spike]:
    """A window spikes when it has at least `min_count` messages, `ratio` times the trailing mean, and
    is `z` standard deviations above it. Consecutive spiking windows merge into one Spike."""
    counts = window_counts(messages, duration_s)
    texts: dict[int, list[str]] = {}
    for t, text in messages:
        texts.setdefault(int(t // WINDOW_S), []).append(text)
    flagged = []
    for i, c in enumerate(counts):
        past = counts[max(0, i - baseline_windows):i]
        if len(past) < 3:  # warm-up: no baseline yet, so nothing can be called a spike
            continue
        mean = sum(past) / len(past)
        sd = math.sqrt(sum((x - mean) ** 2 for x in past) / len(past))
        if c >= min_count and c >= ratio * max(mean, 1.0) and c >= mean + z * max(sd, 1.0):
            flagged.append(i)
    spikes: list[Spike] = []
    run: list[int] = []
    for i in flagged + [None]:
        if run and (i is None or i != run[-1] + 1):
            ws = run
            spikes.append(Spike(ws[0] * WINDOW_S, (ws[-1] + 1) * WINDOW_S, max(counts[w] for w in ws),
                                _keywords([t for w in ws for t in texts.get(w, [])])))
            run = []
        if i is not None:
            run.append(i)
    return spikes
