"""Sound effects for the story style.

Every sound is synthesised with ffmpeg from a formula, so nothing is downloaded and nothing can be
copyright-matched by Instagram or YouTube. The files are made once into a cache folder.

Two levels (SFX_LEVEL): "light" = boom on the big moment, whoosh on scene cuts, ka-ching on money;
"full" adds pops on key words, a riser into the big moment, a record scratch on a second loud moment,
theme sounds keyed by words (an alarm for "wake up"), and a ding on the last word.
"""
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from autoclip.integrations.transcriber import Word

# name: (duration seconds, aevalsrc formula of t, extra ffmpeg filters). 48 kHz mono; stereo when mixed.
RECIPES: dict[str, tuple[float, str, str]] = {
    # deep hit: a sine falling from 120 Hz to 40 Hz with a fast decay
    "boom": (0.9, "0.95*sin(2*PI*(40+80*exp(-6*t))*t)*exp(-4*t)", ""),
    # short bubble pop
    "pop": (0.12, "0.7*sin(2*PI*(500+1100*exp(-40*t))*t)*exp(-32*t)", ""),
    # air rushing past: noise that swells and fades
    "whoosh": (0.6, "0.6*(random(0)*2-1)*sin(PI*t/0.6)", "highpass=f=500,lowpass=f=4500"),
    # rising tone with hiss, building into the moment
    "riser": (1.5, "(0.22*sin(2*PI*(150*t+380*t*t))+0.07*(random(0)*2-1))*(t/1.5)^2", ""),
    # bright two-note bell
    "ding": (1.0, "0.4*(sin(2*PI*1568*t)+0.6*sin(2*PI*2349*t))*exp(-3.5*t)", ""),
    # digital alarm clock: beep-beep-beep
    "alarm": (1.0, "0.3*sgn(sin(2*PI*1000*t))*lt(mod(t,0.25),0.12)", "lowpass=f=4000"),
    # record scratch: noise wobbling up and down in pitch
    "scratch": (0.35, "0.6*(random(0)*2-1)*abs(sin(2*PI*(4+14*t)*t))", "bandpass=f=1800:width_type=h:w=1400,volume=2.5"),
    # cash register: a click of noise, then two bells
    "kaching": (0.9, "0.35*(random(0)*2-1)*exp(-45*t)+0.35*(sin(2*PI*2637*t)+gt(t,0.09)*sin(2*PI*3136*t))"
                     "*exp(-4.5*t)", ""),
}
GAIN = {"boom": 0.8, "pop": 0.35, "whoosh": 0.45, "riser": 0.35, "ding": 0.35, "alarm": 0.2,
        "scratch": 0.4, "kaching": 0.45}
LEVELS = ("off", "light", "full")
MAX_CUES = {"light": 4, "full": 8}
# Words that call for a theme sound (full level), matched on the cleaned word
THEMES = {"wake": "alarm", "alarm": "alarm", "money": "kaching", "paid": "kaching", "cash": "kaching",
          "rich": "kaching"}


@dataclass(frozen=True)
class Cue:
    t: float  # seconds into the clip (before the cold open is added)
    name: str

    @property
    def gain(self) -> float:
        return GAIN[self.name]


def cache_dir() -> Path:
    return Path(os.environ.get("SFX_DIR") or Path(tempfile.gettempdir()) / "autoclip_sfx")


def sound_file(name: str, run: Callable, folder: Path | None = None) -> Path:
    """The cached .wav for a sound, made with ffmpeg the first time it is needed."""
    folder = folder or cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.wav"
    if not path.exists():
        dur, formula, filters = RECIPES[name]
        src = f"aevalsrc='{formula}':d={dur}:s=48000" + (f",{filters}" if filters else "")
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", src, "-ac", "1", "-ar", "48000", str(path)])
    return path


def _clean(w: str) -> str:
    return re.sub(r"[^\w$¥€£]", "", w).lower()


def plan(level: str, words: list[Word], *, emphasis: set[str], cuts: list[float], peaks: list[float],
         big: float | None, duration: float) -> list[Cue]:
    """Which sound goes where. words are in clip time. Cues never crowd each other (0.5 s apart),
    and the most important ones win when there are too many."""
    if level not in MAX_CUES:
        return []
    ranked: list[tuple[int, Cue]] = []  # (priority, cue): lower number = more important
    if big is not None:
        ranked.append((0, Cue(big, "boom")))
        if level == "full" and big >= 2.0:
            ranked.append((4, Cue(big - 1.5, "riser")))
    for c in cuts[:3]:
        ranked.append((2, Cue(max(0.0, c - 0.3), "whoosh")))
    money = [w for w in words if re.search(r"[$¥€£]\d|\d[\d,]{3,}", w.w)]
    for w in money[:2]:
        ranked.append((1, Cue(w.start, "kaching")))
    if level == "full":
        themed = {}
        for w in words:
            name = THEMES.get(_clean(w.w))
            if name and name not in themed:
                themed[name] = w.start
        for name, t in themed.items():
            ranked.append((3, Cue(t, name)))
        for w in [w for w in words if _clean(w.w) in emphasis][:3]:
            ranked.append((5, Cue(w.start, "pop")))
        second = [p for p in peaks if big is None or abs(p - big) > 3]
        if second:
            ranked.append((4, Cue(second[0], "scratch")))
        if words:
            ranked.append((6, Cue(min(words[-1].end, duration - 0.3), "ding")))
    chosen: list[Cue] = []
    for _, cue in sorted(ranked, key=lambda pc: (pc[0], pc[1].t)):
        if len(chosen) >= MAX_CUES[level]:
            break
        if 0 <= cue.t < duration and all(abs(cue.t - o.t) >= 0.5 for o in chosen):
            chosen.append(cue)
    return sorted(chosen, key=lambda c: c.t)
