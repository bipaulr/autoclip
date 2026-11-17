import shutil
import subprocess

import pytest

from autoclip.integrations.transcriber import Word
from autoclip.pipeline import sfx
from autoclip.pipeline.render import build_filter
from autoclip.pipeline.style import PRESETS

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def words(text: str, step: float = 0.5) -> list[Word]:
    return [Word(w, i * step, i * step + 0.4) for i, w in enumerate(text.split())]


CLIP = words("you know who was sleep wake up girl did you come here for the money he paid $12,000 "
             "for the bill and everyone lost it completely that day")


def test_light_is_sparse_boom_whoosh_and_money():
    cues = sfx.plan("light", CLIP, emphasis={"sleep"}, cuts=[6.0], peaks=[4.0, 12.0], big=12.0, duration=16)
    names = [c.name for c in cues]
    assert set(names) <= {"boom", "whoosh", "kaching"} and "boom" in names and "whoosh" in names
    assert any(c.name == "kaching" and c.t == 8.5 for c in cues)  # on "$12,000"
    assert len(cues) <= sfx.MAX_CUES["light"]


def test_full_adds_pops_riser_themes_and_keeps_the_cap_and_spacing():
    cues = sfx.plan("full", CLIP, emphasis={"sleep", "bill"}, cuts=[6.0], peaks=[4.0, 12.0], big=12.0,
                    duration=16)
    names = [c.name for c in cues]
    assert {"boom", "riser", "alarm", "pop"} <= set(names)
    assert len(cues) <= sfx.MAX_CUES["full"]
    ts = [c.t for c in cues]
    assert ts == sorted(ts) and all(b - a >= 0.5 for a, b in zip(ts, ts[1:]))
    assert next(c.t for c in cues if c.name == "riser") == pytest.approx(10.5)  # 1.5 s into the boom


def test_off_or_unknown_level_plans_nothing():
    assert sfx.plan("off", CLIP, emphasis=set(), cuts=[], peaks=[], big=3.0, duration=10) == []
    assert sfx.plan("loud", CLIP, emphasis=set(), cuts=[], peaks=[], big=3.0, duration=10) == []


def test_sound_files_are_mixed_in_as_extra_inputs():
    graph, _ = build_filter("gameplay", 1920, 1080, [], [], PRESETS["story_pop"], [3.0], True, 10,
                            [(3.0, 0.8), (6.5, 0.45)])
    assert "[1:a]" in graph and "[2:a]" in graph and "adelay=6500|6500" in graph and "volume=0.80" in graph
    assert "anoisesrc" not in graph  # the old swish is replaced, not stacked
    old, _ = build_filter("gameplay", 1920, 1080, [], [], PRESETS["bold_pop"], [3.0], True, 10)
    assert "anoisesrc" in old and "[1:a]" not in old


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_every_sound_synthesises_and_is_audible(tmp_path):
    run = lambda a: subprocess.run(a, check=True, capture_output=True)  # noqa: E731
    for name in sfx.RECIPES:
        path = sfx.sound_file(name, run, tmp_path)
        p = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True)
        peak = float(p.stderr.split("max_volume:")[1].split("dB")[0])
        assert -20 < peak <= 0, name
    calls = []
    sfx.sound_file("boom", lambda a: calls.append(a), tmp_path)
    assert calls == []  # cached: made once


def test_story_clips_skip_the_forced_blur_layout():
    from autoclip.services.pipeline import story_style
    assert story_style("story_pop") and not story_style("bold_pop") and not story_style("nope")
