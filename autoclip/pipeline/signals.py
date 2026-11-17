"""Per-second signal timeline: loudness peaks, chat spikes, scene changes.

Heavy model-based signals from the plan (YAMNet laughter, motion) are not included yet;
loudness, chat and shot cuts cover most of what streamer highlights need.
"""
import array
import math
import re
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from autoclip.integrations.downloader import ChatMessage


@dataclass
class Timeline:
    duration_s: int
    loudness_db: list[float] = field(default_factory=list)  # one value per second
    loud_peaks: list[int] = field(default_factory=list)  # seconds
    chat_per_10s: list[int] = field(default_factory=list)
    chat_spikes: list[int] = field(default_factory=list)  # window start seconds
    chat_keywords: dict[int, list[str]] = field(default_factory=dict)
    shot_cuts: list[float] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"duration_s": self.duration_s, "loudness_db": self.loudness_db, "loud_peaks": self.loud_peaks,
                "chat_per_10s": self.chat_per_10s, "chat_spikes": self.chat_spikes,
                "chat_keywords": {str(k): v for k, v in self.chat_keywords.items()},
                "shot_cuts": self.shot_cuts}

    @classmethod
    def from_json(cls, d: dict) -> "Timeline":
        return cls(d["duration_s"], d.get("loudness_db", []), d.get("loud_peaks", []), d.get("chat_per_10s", []),
                   d.get("chat_spikes", []), {int(k): v for k, v in d.get("chat_keywords", {}).items()},
                   d.get("shot_cuts", []))

    def summary(self, start: float, end: float) -> str:
        """Short text describing the signals inside a window, for the LLM prompt."""
        s, e = int(start), int(math.ceil(end))
        peaks = [p for p in self.loud_peaks if s <= p < e]
        spikes = [w for w in self.chat_spikes if s - 10 < w < e]
        words = sorted({k for w in spikes for k in self.chat_keywords.get(w, [])})[:6]
        cuts = sum(1 for c in self.shot_cuts if s <= c < e)
        parts = [f"{len(peaks)} loudness peak(s)" + (f" at {', '.join(str(p - s) + 's' for p in peaks[:3])} in"
                                                     if peaks else "")]
        parts.append(f"{len(spikes)} chat spike(s)" + (f" (chat said: {', '.join(words)})" if words else ""))
        parts.append(f"{cuts} shot cut(s)")
        return "; ".join(parts)


def zscores(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = sum(values) / len(values)
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    return [0.0 if sd == 0 else (v - mean) / sd for v in values]


def loudness_per_second(wav_path: Path) -> list[float]:
    """RMS level in dBFS for each second of a 16-bit mono WAV."""
    with wave.open(str(wav_path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("expected 16-bit mono WAV")
        rate, out = w.getframerate(), []
        while True:
            frames = w.readframes(rate)
            if not frames:
                break
            samples = array.array("h", frames)
            rms = math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0
            out.append(round(20 * math.log10(rms / 32768) if rms > 0 else -96.0, 1))
    return out


def find_peaks(db: list[float], z_threshold: float = 2.0, min_gap_s: int = 5) -> list[int]:
    """Seconds where loudness jumps well above the video's own norm."""
    z, peaks = zscores(db), []
    for i, v in enumerate(z):
        if v >= z_threshold and (not peaks or i - peaks[-1] >= min_gap_s):
            peaks.append(i)
    return peaks


WORD = re.compile(r"[A-Za-z][A-Za-z0-9']{1,20}")
STOP = {"the", "and", "you", "that", "this", "for", "are", "was", "what", "with", "his", "him", "she", "her",
        "they", "have", "just", "but", "not", "its", "it's", "lol", "can", "dont", "don't", "all", "get"}


def chat_activity(chat: list[ChatMessage], duration_s: int, z_threshold: float = 2.0):
    """Messages per 10s window, z-score spikes, and the top words in each spiking window."""
    n = max(1, math.ceil(duration_s / 10))
    counts = [0] * n
    texts: dict[int, list[str]] = {}
    for m in chat:
        i = int(m.t // 10)
        if 0 <= i < n:
            counts[i] += 1
            texts.setdefault(i, []).append(m.text)
    z = zscores([float(c) for c in counts])
    spikes = [i * 10 for i, v in enumerate(z) if v >= z_threshold and counts[i] >= 5]
    keywords = {}
    for w in spikes:
        freq: dict[str, int] = {}
        for t in texts.get(w // 10, []):
            for word in WORD.findall(t):
                word = word.lower() if not word.isupper() else word
                if word.lower() not in STOP:
                    freq[word] = freq.get(word, 0) + 1
        keywords[w] = [k for k, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:5]]
    return counts, spikes, keywords


SCENE = re.compile(r"pts_time:([0-9.]+)")


def run_stderr(args: list[str], timeout: int = 7200) -> str:
    """ffmpeg's showinfo filter reports on stderr, so return that instead of stdout."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"{args[0]} failed to run ({type(e).__name__})") from None
    if p.returncode != 0:
        raise RuntimeError(f"{args[0]} exited {p.returncode}")
    return p.stderr


def shot_cuts(video_path: Path, threshold: float = 0.35, run: Callable = run_stderr) -> list[float]:
    """Shot boundaries via ffmpeg's scene score (no extra dependency)."""
    # Keyframes only, at low resolution: ~10x faster than decoding every frame, and cut times
    # only need to be accurate to a couple of seconds (they nudge clip starts and crop resets).
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-skip_frame", "nokey", "-i", str(video_path), "-an",
               "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-fps_mode", "vfr",
               "-f", "null", "-"])
    return [round(float(t), 2) for t in SCENE.findall(out)]


def build_timeline(audio: Path | None, video: Path | None, chat: list[ChatMessage], duration_s: float,
                   run: Callable = run_stderr) -> Timeline:
    d = int(math.ceil(duration_s))
    db = loudness_per_second(audio) if audio and Path(audio).exists() else []
    counts, spikes, keywords = chat_activity(chat, d) if chat else ([], [], {})
    cuts = []
    if video and Path(video).exists():
        try:
            cuts = shot_cuts(Path(video), run=run)
        except RuntimeError:
            cuts = []  # cuts only refine cut points; never fail the pipeline over them
    return Timeline(d, db, find_peaks(db), counts, spikes, keywords, cuts)
