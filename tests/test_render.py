import shutil
import subprocess
from pathlib import Path

import pytest

from autoclip.integrations.transcriber import Word
from autoclip.pipeline.render import (COLD_OPEN_S, Face, RenderError, RenderJob, Sample, big_moment, build_filter,
                                      choose_layout, crop_commands, facecam_box, probe, render, track_crop)
from autoclip.pipeline.style import (GREEN, PINK, PRESETS, RED, accent, build_ass, group_words, merge_amounts,
                                     zoom_expression)

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
REVIEW = Path(__file__).resolve().parents[1] / "media" / "review"


# ------------------------------------------------------------ pure logic
def test_layout_choice():
    big = [Sample(t, [Face(0.5, 0.4, 0.2, 0.3)]) for t in range(10)]
    cam = [Sample(t, [Face(0.88, 0.85, 0.06, 0.1)]) for t in range(10)]
    none = [Sample(t) for t in range(10)]
    assert choose_layout(big) == "talking_head"
    assert choose_layout(cam) == "gameplay_facecam"
    assert choose_layout(none) == "gameplay" and choose_layout([]) == "gameplay"


def test_crop_follows_face_smoothly_and_jumps_only_at_cuts():
    samples = [Sample(t / 2, [Face(0.2 if t < 10 else 0.8, 0.5, 0.2, 0.3)]) for t in range(20)]
    no_cut = track_crop(samples, [], 1920, 1080)
    xs = [x for _, x in no_cut]
    assert max(abs(a - b) for a, b in zip(xs, xs[1:])) < 90  # glides: no frame moves far
    assert xs[-1] > 1100  # ...but it does arrive at the face
    with_cut = dict(track_crop(samples, [5.0], 1920, 1080))
    assert with_cut[5.0] - with_cut[round(5.0 - 1 / 30, 3)] > 900  # at the cut it goes straight to the face
    cw = int(1080 * 9 / 16)
    assert all(0 <= x <= 1920 - cw and x % 2 == 0 for x in with_cut.values())


def test_detection_jitter_leaves_the_frame_still():
    # a person talking in place, detected with +-3% noise every sample: the crop must not twitch
    noise = [0.0, 0.03, -0.02, 0.025, -0.03, 0.01, -0.015, 0.03, -0.025, 0.02] * 3
    samples = [Sample(i / 2, [Face(0.5 + n, 0.4, 0.2, 0.3)]) for i, n in enumerate(noise)]
    xs = {x for _, x in track_crop(samples, [], 1920, 1080)}
    assert max(xs) - min(xs) <= 4


def test_the_crop_stays_with_the_same_person_when_two_are_on_screen():
    # two people; the one on the right is detected bigger in every other frame
    samples = [Sample(i / 2, [Face(0.25, 0.4, 0.15, 0.2), Face(0.75, 0.4, 0.16 if i % 2 else 0.14, 0.2)])
               for i in range(20)]
    xs = [x for _, x in track_crop(samples, [], 1920, 1080)]
    assert max(xs) - min(xs) <= 4  # never flips between them


def test_crop_expression_is_piecewise():
    assert crop_commands([(0.0, 10), (0.5, 12), (1.0, 30)]) == "0.000 crop@cam x 10;\n0.500 crop@cam x 12;\n1.000 crop@cam x 30;\n"
    assert crop_commands([(0.0, 10), (0.033, 11)]) == "0.000 crop@cam x 10;\n"
    assert crop_commands([(0.0, 42)]) == "0.000 crop@cam x 42;\n"


def test_facecam_box_is_inside_frame():
    samples = [Sample(t, [Face(0.9, 0.85, 0.08, 0.12)]) for t in range(6)]
    x, y, w, h = facecam_box(samples, 1920, 1080)
    assert x >= 0 and y >= 0 and x + w <= 1920 and y + h <= 1080 and w > 0 and h > 0


def test_caption_lines_have_max_three_words_and_break_on_sentences():
    words = [Word(w, i * 0.4, i * 0.4 + 0.35) for i, w in enumerate("one two three four. five six".split())]
    assert [[x.w for x in line] for line in group_words(words)] == [["one", "two", "three"], ["four."],
                                                                    ["five", "six"]]


def test_ass_highlights_current_word_and_adds_hook_and_context():
    words = [Word("hello", 10.0, 10.4), Word("big", 10.5, 10.9), Word("world", 11.0, 11.5)]
    ass = build_ass(words, PRESETS["bold_pop"], clip_start=10.0, duration=20.0,
                    hook_headline="He did what?", context_line="This was his first stream in Japan")
    events = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    caps = [e for e in events if ",Cap," in e]
    assert len(caps) == 3 and caps[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.50")
    assert "\\c&H0000E6FF}HELLO" in caps[0] and "\\c&H0000E6FF}BIG" in caps[1]
    assert any(",Hook," in e and "HE DID WHAT?" in e and "0:00:02.50" in e for e in events)
    assert any(",Ctx," in e and "first stream in Japan" in e for e in events)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass


def test_zoom_expression():
    assert zoom_expression([], 1.12) == "1" and zoom_expression([3.0], 1.0) == "1"
    z = zoom_expression([3.0], 1.12)
    assert z.startswith("1+0.120*min(1,") and "between(t,3.00,3.15)" in z


def test_seven_presets_differ():
    assert len(PRESETS) == 7
    assert len({(p.font, p.highlight, p.margin_v) for p in PRESETS.values()}) == 7
    assert [p.name for p in PRESETS.values() if p.story] == ["story_pop"]


# ------------------------------------------------------------ story style
def test_amounts_are_merged_and_written_out():
    words = [Word("the", 0, .2), Word("bill", .2, .5), Word("was", .5, .7), Word("1.6", .7, 1.0),
             Word("million", 1.0, 1.4), Word("yen,", 1.4, 1.7), Word("$12,000", 2.0, 2.6), Word("3", 3.0, 3.1),
             Word("50k", 3.2, 3.5)]
    out = [w.w for w in merge_amounts(words)]
    assert out == ["the", "bill", "was", "¥1,600,000", "$12,000", "3", "50,000"]
    merged = merge_amounts(words)[3]
    assert (merged.start, merged.end) == (0.7, 1.7)


def test_accent_colours_amounts_and_key_words():
    assert accent("$12,000", set()) == GREEN and accent("-$12,000", set()) == RED
    assert accent("$500", set(), prev="lost") == RED
    assert accent("LEAKED!", {"leaked"}) == PINK and accent("the", {"leaked"}) is None


def test_story_captions_are_one_big_centred_word_with_a_reaction_tag():
    words = [Word(w, i * 0.4, i * 0.4 + 0.35) for i, w in enumerate(
        "he spent 500 dollars on the bill and everyone screamed".split())]
    ass = build_ass(words, PRESETS["story_pop"], clip_start=0, duration=6, emphasis=["bill"],
                    reaction_tag="shocked!!", tag_at=3.2)
    caps = [e for e in ass.splitlines() if ",Cap," in e]
    assert all(len(e.rsplit("}", 1)[1].split()) == 1 for e in caps)  # one word per event
    assert any(f"\\c{GREEN}}}$500" in e for e in caps) and any(f"\\c{PINK}}}BILL" in e for e in caps)
    assert "Style: Cap,Anton,150" in ass and ",5,40,40,0,1" in ass  # alignment 5: middle of the screen
    tag = [e for e in ass.splitlines() if ",Tag," in e]
    assert len(tag) == 1 and tag[0].endswith("*SHOCKED*") and "0:00:03.20" in tag[0]
    assert not any("0:00:03.20" <= e.split(",")[1] < "0:00:04.20" for e in caps)  # the tag owns that second


def test_story_style_prefers_a_face_crop_and_flashes_on_peaks():
    small = [Sample(t, [Face(0.3 + 0.1 * t, 0.4, 0.08, 0.1)]) for t in range(10)]
    assert choose_layout(small) != "talking_head" and choose_layout(small, prefer_crop=True) == "talking_head"
    graph, _ = build_filter("gameplay", 1920, 1080, [], [], PRESETS["story_pop"], [3.0, 7.0], True, 10)
    assert "drawbox" in graph and "between(t,3.00,3.25)" in graph
    plain, _ = build_filter("gameplay", 1920, 1080, [], [], PRESETS["bold_pop"], [3.0], True, 10)
    assert "drawbox" not in plain
    assert big_moment([1.0, 4.0, 8.0, 9.5], 10.0) == 8.0 and big_moment([1.0], 10.0) is None


def test_filters_for_each_layout():
    samples = [Sample(t, [Face(0.5, 0.4, 0.2, 0.3)]) for t in range(5)]
    for layout in ("talking_head", "gameplay_facecam", "gameplay"):
        graph, _ = build_filter(layout, 1920, 1080, samples, [], PRESETS["neon_box"], [2.0], True, 10)
        assert "[v]" in graph and "[a]" in graph and "loudnorm=I=-14" in graph and "subtitles=captions.ass" in graph
        assert "adelay=2000|2000" in graph
    silent, _ = build_filter("gameplay", 1920, 1080, [], [], PRESETS["clean_white"], [], False, 10)
    assert "anullsrc" in silent and "amix" not in silent


def test_bad_inputs():
    with pytest.raises(RenderError, match="unknown layout"):
        render(RenderJob(Path("x.mp4"), 0, 10, Path("out"), layout="weird"))
    with pytest.raises(RenderError, match="no duration"):
        render(RenderJob(Path("x.mp4"), 5, 5, Path("out")))


# ------------------------------------------------------------ real ffmpeg
@pytest.fixture(scope="module")
def fixture_video(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("src") / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=12",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=12", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(out)], check=True)
    return out


def words_for(start, end):
    text = "this is the moment everybody was waiting for and nobody saw it coming at all".split()
    step = (end - start) / len(text)
    return [Word(w, start + i * step, start + (i + 1) * step - 0.05) for i, w in enumerate(text)]


class NoFaces:
    def detect(self, frame):
        return []


@pytest.mark.parametrize("layout,preset", [("talking_head", "bold_pop"), ("gameplay_facecam", "neon_box"),
                                           ("gameplay", "impact_yellow")])
def test_render_each_layout(fixture_video, tmp_path, layout, preset):
    job = RenderJob(fixture_video, 2.0, 9.0, tmp_path / layout, words_for(2.0, 9.0),
                    hook_headline="Nobody saw this coming", context_line="He had one chance left",
                    preset=preset, layout=layout, peaks=[4.0], cuts=[6.0])
    r = render(job, detector=NoFaces())
    info = probe(r.path)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert abs(info["duration"] - 7.0) < 0.3 and info["has_audio"]
    assert len(r.frames) == 3 and all(f.exists() and f.stat().st_size > 1000 for f in r.frames)
    review = REVIEW / f"{layout}_{preset}"
    review.mkdir(parents=True, exist_ok=True)
    for f in r.frames:
        shutil.copy(f, review / f.name)


def test_story_render_opens_on_the_big_moment(fixture_video, tmp_path):
    job = RenderJob(fixture_video, 1.0, 9.0, tmp_path / "story", words_for(1.0, 9.0), preset="story_pop",
                    layout="gameplay", peaks=[6.5], emphasis=["moment"], reaction_tag="shocked")
    r = render(job, detector=NoFaces())
    info = probe(r.path)
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    assert abs(info["duration"] - (8.0 + COLD_OPEN_S)) < 0.3  # the 1.5 s teaser sits in front
    review = REVIEW / "story_pop"
    review.mkdir(parents=True, exist_ok=True)
    for f in r.frames:
        shutil.copy(f, review / f.name)


def test_loudness_is_normalised(fixture_video, tmp_path):
    r = render(RenderJob(fixture_video, 1.0, 9.0, tmp_path / "loud", preset="clean_white", layout="gameplay"),
               detector=NoFaces())
    p = subprocess.run(["ffmpeg", "-v", "info", "-i", str(r.path), "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    integrated = float(p.stderr.rsplit("I:", 1)[1].split("LUFS")[0])
    assert -16.5 <= integrated <= -12.5


def test_long_hook_headline_shrinks_and_wraps():
    from autoclip.pipeline.style import hook_size
    assert hook_size("Short one") == 84 and 60 <= hook_size("x" * 60) < 84
    ass = build_ass([], PRESETS["clean_white"], clip_start=0, duration=10,
                    hook_headline="He really thought nobody would ever notice that")
    assert "WrapStyle: 0" in ass


def test_detector_chain_never_crashes(monkeypatch):
    import autoclip.pipeline.render as r
    monkeypatch.setattr(r, "_detector", None)
    for name in ("YuNetFaceDetector", "MediaPipeFaceDetector", "HaarFaceDetector"):
        monkeypatch.setattr(r, name, lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("gone")))
    d = r.default_detector()
    assert isinstance(d, r.NoFaceDetector) and d.detect(None) == []


def test_face_sampling_failure_falls_back_to_full_frame(fixture_video, tmp_path):
    class Boom:
        def detect(self, frame):
            raise RuntimeError("detector exploded")

    res = render(RenderJob(fixture_video, 1.0, 5.0, tmp_path / "x", preset="clean_white"), detector=Boom())
    assert res.layout == "gameplay" and probe(res.path)["width"] == 1080


def test_clip_inside_a_muted_stretch_still_renders(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    src = tmp_path / "muted.mp4"  # audio stops after 4 s, like a Twitch muted section
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=4", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(src)], check=True)
    r = render(RenderJob(src, 6.0, 10.0, tmp_path / "out", preset="clean_white", layout="gameplay"), detector=NoFaces())
    info = probe(r.path)
    assert info["has_audio"] and abs(info["duration"] - 4.0) < 0.3


def test_long_talking_head_clip_renders(fixture_video, tmp_path):
    """60 s of face-tracked crop used to exceed ffmpeg's expression nesting limit."""
    class MovingFace:
        n = 0

        def detect(self, frame):
            MovingFace.n += 1
            return [Face(0.3 + 0.4 * ((MovingFace.n // 3) % 2), 0.4, 0.2, 0.3)]

    long_src = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=62",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=62", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(long_src)], check=True)
    r = render(RenderJob(long_src, 1.0, 60.0, tmp_path / "th", preset="bold_pop", layout="talking_head"),
               detector=MovingFace())
    assert abs(probe(r.path)["duration"] - 59.0) < 0.5 and (tmp_path / "th" / "crop.cmd").exists()


def test_streamer_facecam_wins_over_big_faces_in_reacted_content():
    import random as _r
    rng = _r.Random(3)
    cam = Face(0.85, 0.15, 0.08, 0.12)  # streamer, top-right, every frame
    samples = []
    for t in range(20):
        faces = [cam]
        if t % 3:  # someone in the video he is watching: big and moving around
            faces.append(Face(rng.uniform(0.2, 0.6), rng.uniform(0.3, 0.7), 0.3, 0.4))
        samples.append(Sample(t, faces))
    assert choose_layout(samples) == "gameplay_facecam"
    x, y, w, h = facecam_box(samples, 1920, 1080)
    assert x <= 0.85 * 1920 <= x + w and y <= 0.15 * 1080 <= y + h  # the box sits on the streamer


# ------------------------------------------------------------ dead air
def test_long_pauses_are_cut_but_reactions_near_loud_moments_stay():
    from autoclip.pipeline.render import keep_spans, map_time
    words = [(0.0, 1.0), (1.2, 2.0), (4.0, 5.0), (8.0, 9.0)]  # a 2 s pause, then a 3 s pause
    spans = keep_spans(words, 10.0)
    assert spans == [(0.0, 2.12), (3.88, 5.12), (7.88, 9.12)]  # the 1 s tail after the last word goes too
    assert map_time(4.0, spans) == pytest.approx(2.12 + 0.12) and map_time(8.5, spans) == pytest.approx(3.98)
    protected = keep_spans(words, 10.0, protect=[6.0])  # a scream at 6 s: the 5-8 s pause stays
    assert (3.88, 9.12) in protected or any(a <= 5.2 and b >= 7.9 for a, b in protected)
    assert keep_spans([(0.0, 1.0), (1.3, 2.0)], 2.2) == []  # nothing worth cutting
    assert keep_spans([], 10.0) == []


def test_render_cuts_dead_air(fixture_video, tmp_path):
    words = [Word("one", 1.0, 1.5), Word("two", 1.6, 2.0), Word("three", 6.0, 6.5), Word("four", 6.6, 7.0)]
    r = render(RenderJob(fixture_video, 1.0, 8.0, tmp_path / "cut", words, preset="clean_white", layout="gameplay"),
               detector=NoFaces())
    assert probe(r.path)["duration"] < 4.0  # 7 s with a 4 s silence -> about 2.5 s of speech
    # audio is cut too (ffmpeg 8 kept the full-length audio with the old select/aselect filter)
    lengths = [float(subprocess.run(["ffprobe", "-v", "error", "-select_streams", s, "-show_entries",
                                     "stream=duration", "-of", "csv=p=0", str(r.path)],
                                    capture_output=True, text=True, check=True).stdout) for s in ("v:0", "a:0")]
    assert abs(lengths[0] - lengths[1]) < 0.2
