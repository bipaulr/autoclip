import json
import shutil
import subprocess

import pytest

from autoclip.integrations.llm import LLMError, MockLLM
from autoclip.pipeline.brief import Brief
from autoclip.pipeline.package import (is_duplicate, package_text, pick_thumbnail, similarity, video_phash)

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_similarity_and_dedupe_threshold():
    a = "0" * 128
    assert similarity(a, a) == 1.0
    ten_pct = ("f" * 12) + "0" * 116  # 48 of 512 bits differ -> 90.6% similar
    assert 0.90 < similarity(a, ten_pct) < 0.91
    dup, best = is_duplicate(a, [ten_pct])
    assert dup and best > 0.9
    far = ("f" * 13) + "0" * 115  # 52 bits differ -> 89.8%, just under the threshold
    assert not is_duplicate(a, [far])[0]
    assert not is_duplicate(a, [])[0]
    assert similarity(a, "abc") == 0.0


def test_package_text_from_llm_with_brief_requirements():
    llm = MockLLM([json.dumps({
        "youtube": {"title": "He Couldn't Believe It", "description": "Wild moment.", "hashtags": ["#funny", "#irl"]},
        "instagram": {"title": "He couldn't believe it 😭", "description": "Wait for it", "hashtags": ["funny"]}})])
    brief = Brief(required_hashtags=["#KaiCenat"], name_in_caption="Kai Cenat", disclosure_text="#ad")
    out = package_text(llm, title="t", hook="h", excerpt="e", creator="Kai Cenat", attribution=None, brief=brief)
    yt, ig = out["youtube"], out["instagram"]
    assert yt.title.endswith("#Shorts") and "#KaiCenat" in yt.hashtags and "#funny" in yt.hashtags
    cap = ig.caption()
    assert "#KaiCenat" in cap and "Kai Cenat" in cap and cap.count("#ad") == 1
    assert ig.disclosure == {"synthetic_media": False, "paid_partnership": True}


def test_package_falls_back_without_llm_and_keeps_attribution():
    out = package_text(MockLLM(error=LLMError("down")), title="Big moment", hook="h", excerpt="e", creator="c",
                       attribution="Source: x (CC BY)", brief=None)
    assert out["instagram"].title == "Big moment" and "CC BY" in out["instagram"].description
    assert out["youtube"].disclosure["paid_partnership"] is False


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("clips")

    def make(name, src):
        out = d / name
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", src, "-c:v", "libx264",
                        "-preset", "ultrafast", str(out)], check=True)
        return out

    return {"a": make("a.mp4", "testsrc2=size=540x960:rate=30:duration=4"),
            "a_again": make("a2.mp4", "testsrc2=size=540x960:rate=30:duration=4"),
            "b": make("b.mp4", "mandelbrot=size=540x960:rate=30,trim=duration=4")}


def test_video_hash_matches_same_video_and_not_a_different_one(clips):
    a, a2, b = (video_phash(clips[k]) for k in ("a", "a_again", "b"))
    assert len(a) == 128
    assert similarity(a, a2) > 0.95 and is_duplicate(a2, [a])[0]
    assert similarity(a, b) < 0.9 and not is_duplicate(b, [a])[0]


def test_thumbnail_written(clips, tmp_path):
    out = pick_thumbnail(clips["a"], tmp_path / "thumb.jpg")
    assert out and out.exists() and out.stat().st_size > 1000


def test_caption_rules_reach_the_writer_and_required_tags_lead():
    from autoclip.integrations.llm import MockLLM
    from autoclip.pipeline.brief import Brief
    from autoclip.pipeline.package import package_text
    seen = []

    def answer(prompt):
        seen.append(prompt)
        return ('{"youtube": {"title": "t", "description": "d", "hashtags": ["#Core", "#Lacy"]},'
                ' "instagram": {"title": "t", "description": "d", "hashtags": ["#Lacy"]}}')

    brief = Brief(required_hashtags=["CORE"], caption_rules="add a hashtag for each CORE member in the clip")
    pkg = package_text(MockLLM(answer), title="Lacy loses it", hook="h", excerpt="Lacy screams", creator="CORE",
                       attribution=None, brief=brief)
    assert "add a hashtag for each CORE member" in seen[0] and "never guess" in seen[0]
    assert pkg["youtube"].hashtags[:2] == ["#CORE", "#Lacy"]  # required first, the writer's #Core merged into it
    assert pkg["instagram"].hashtags[0] == "#CORE"


def test_no_caption_rules_leaves_the_prompt_unchanged():
    from autoclip.integrations.llm import MockLLM
    from autoclip.pipeline.package import package_text
    seen = []
    package_text(MockLLM(lambda p: seen.append(p) or "{}"), title="t", hook="h", excerpt="e", creator="c",
                 attribution=None, brief=None)
    assert "Campaign caption rules" not in seen[0]


def test_the_writer_sees_the_source_title_and_what_is_said_to_know_who_is_in_the_clip():
    from autoclip.integrations.llm import MockLLM
    from autoclip.pipeline.brief import Brief
    from autoclip.pipeline.package import package_text
    seen = []
    package_text(MockLLM(lambda p: seen.append(p) or "{}"), title="t", hook="h", excerpt="a reason",
                 creator="CORE", attribution=None, brief=Brief(caption_rules="a hashtag per CORE member"),
                 source_title="CORE JEOPARDY ft. Jason and Lacy", said="Jason you can't be serious " * 100)
    assert "Source video: CORE JEOPARDY ft. Jason and Lacy" in seen[0]
    assert "What is said in the clip: Jason you can't be serious" in seen[0]
    assert len(seen[0]) < 4000  # the transcript is capped
