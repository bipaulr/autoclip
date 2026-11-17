import json
import re

from autoclip.integrations.llm import FallbackLLM, LLMQuotaError, MockLLM
from autoclip.integrations.transcriber import Segment
from autoclip.pipeline.highlights import (Candidate, Score, candidate_windows, dedupe, pick_highlights,
                                          score_batch)
from autoclip.pipeline.profiles import GENERIC, get_profile, load_profiles
from autoclip.pipeline.signals import Timeline


def segs(n=120, step=5.0):
    return [Segment(i * step, (i + 1) * step, f"Sentence number {i}.") for i in range(n)]


def score_all(prompt, virality=None):
    ids = [int(i) for i in re.findall(r"^\[(\d+)\]", prompt, re.M)]
    return json.dumps({"scores": [{
        "id": i, "hook_strength": 7, "payoff": 6, "standalone_clarity": 8, "emotion": 5, "niche_fit": 7,
        "virality": (virality or {}).get(i, 40 + i % 7), "suggested_title": f"Clip {i}",
        "hook_headline": "one two three four five six seven eight nine ten", "hook_type": "weird",
        "context_line": "", "why": "strong opening"} for i in ids]})


def test_profiles_load_and_weights_normalise():
    p = load_profiles()
    assert set(p) == {"irl_streamer", "youtuber_challenge", "podcast_business", "brand_story"}
    assert abs(sum(p["irl_streamer"].weights.values()) - 1.0) < 1e-9
    assert get_profile("nope") is GENERIC and get_profile(None) is GENERIC


def test_windows_follow_sentence_boundaries_and_limits():
    s = segs()
    ws = candidate_windows(s, None, min_s=15, max_s=60)
    bounds = {x.start for x in s} | {x.end for x in s}
    assert ws and all(w.start in bounds and w.end in bounds for w in ws)
    assert all(15 <= w.end - w.start <= 60 for w in ws)
    assert [w.id for w in ws] == list(range(len(ws)))


def test_signal_peaks_seed_windows_and_rank_first():
    s = segs(240)
    db = [-40.0] * 1200
    db[700] = -5.0
    tl = Timeline(1200, db, [700])
    ws = candidate_windows(s, tl, get_profile("irl_streamer"), limit=6)
    assert any(w.start <= 700 <= w.end and "loud" in w.seeds for w in ws)
    assert max(ws, key=lambda w: w.signal_z).start <= 700


def test_window_start_snaps_to_nearby_shot_cut_only_if_still_a_sentence_start():
    s = segs(20)
    ws = candidate_windows(s, Timeline(100, shot_cuts=[30.5]), min_s=15, max_s=60)
    assert all(w.start in {x.start for x in s} for w in ws)


def test_score_batch_validates_and_retries_once_on_bad_json():
    llm = MockLLM(["sorry, here you go", score_all("[0] a\n[1] b")])
    ws = candidate_windows(segs(30), None)[:2]
    scores, used = score_batch(llm, ws, GENERIC, None, [])
    assert used == 2 and [x.id for x in scores] == [0, 1]
    assert len(scores[0].hook_headline.split()) == 8 and scores[0].hook_type == "bold_claim"
    assert "previous answer was not valid" in llm.prompts[1]


def test_score_batch_gives_up_after_two_invalid_answers():
    scores, used = score_batch(MockLLM(["nope", '{"scores": [{"id": 0}]}']), candidate_windows(segs(10), None)[:1],
                               GENERIC, None, [])
    assert scores == [] and used == 2


def test_out_of_range_scores_are_rejected():
    bad = json.loads(score_all("[0] a"))
    bad["scores"][0]["virality"] = 900
    scores, _ = score_batch(MockLLM([json.dumps(bad), json.dumps(bad)]), candidate_windows(segs(10), None)[:1],
                            GENERIC, None, [])
    assert scores == []


def test_pick_highlights_batches_up_to_10_and_returns_top_n_without_overlap():
    llm = MockLLM(lambda p: score_all(p, {3: 95, 4: 90, 10: 85}))
    chosen, stats = pick_highlights(llm, segs(), top_n=3, max_candidates=25)
    assert stats["candidates"] == 25 and stats["requests"] == 3 and stats["scored"] == 25
    assert all(p.count("\n[") <= 10 for p in llm.prompts)
    assert len(chosen) == 3 and chosen == sorted(chosen, key=lambda c: c.start)
    for a, b in zip(chosen, chosen[1:]):
        assert a.end <= b.start
    c = chosen[0]
    assert c.hook_headline and c.scores["virality"] >= 0 and "signal_z" in c.scores and c.reason == "strong opening"


def test_signal_summary_and_examples_reach_the_prompt():
    tl = Timeline(600, [-40.0] * 600, [100], [1] * 60, [100], {100: ["W", "LOL"]})
    llm = MockLLM(score_all)
    pick_highlights(llm, segs(), tl, get_profile("irl_streamer"),
                    examples=[{"label": "top", "excerpt": "he jumped", "outcome": "1.2M views"}])
    assert "chat said: LOL, W" in llm.prompts[0] or "chat said: W, LOL" in llm.prompts[0]
    assert "he jumped" in llm.prompts[0] and "irl_streamer" in llm.prompts[0]


def test_dedupe():
    a, b, c = (Candidate(0, 30, "a", "", "", "", "r", 90, {}), Candidate(20, 50, "b", "", "", "", "r", 80, {}),
               Candidate(60, 90, "c", "", "", "", "r", 70, {}))
    assert [x.title for x in dedupe([c, b, a], 5)] == ["a", "c"]


def test_rechunks_into_small_batches_when_falling_back():
    primary = MockLLM(error=LLMQuotaError("daily", daily=True), name="gemini:m")
    fallback = MockLLM(score_all, name="ollama:t", max_chars=1500)
    long = [Segment(i * 5.0, (i + 1) * 5.0, "word " * 40) for i in range(120)]
    chosen, stats = pick_highlights(FallbackLLM(primary, fallback), long, top_n=4)
    assert stats["llm"] == "ollama:t" and len(chosen) == 4
    assert all(p.count("\n[") <= 3 for p in fallback.prompts)


def test_empty_transcript_makes_no_requests():
    llm = MockLLM(["{}"])
    assert pick_highlights(llm, [])[0] == [] and llm.prompts == []


def test_overlay_text_names_the_creator_not_the_streamer():
    from autoclip.pipeline.highlights import name_people
    assert name_people("The streamer can't believe it", "JasonTheWeen") == "JasonTheWeen can't believe it"
    assert name_people("streamer loses it", "Lacy") == "Lacy loses it"
    assert name_people("No change here", "Lacy") == "No change here" and name_people("x", "") == "x"
    llm = MockLLM(lambda p: score_all(p).replace("one two three four five six seven eight nine ten",
                                                 "the streamer goes wild"))
    chosen, _ = pick_highlights(llm, segs(), creator="JasonTheWeen")
    assert "JasonTheWeen" in llm.prompts[0] and all("streamer" not in c.hook_headline.lower() for c in chosen)
    assert all(c.hook_headline.startswith("JasonTheWeen") for c in chosen)


def test_without_signals_the_score_is_the_llms_score():
    llm = MockLLM(lambda p: score_all(p, {i: 90 for i in range(60)}))
    chosen, _ = pick_highlights(llm, segs(), None)
    assert all(c.score == 90 for c in chosen)


def test_chat_spike_gets_a_window_with_setup_before_and_payoff_after():
    s = segs(60)  # 5 s sentences, 300 s
    tl = Timeline(300, chat_per_10s=[2] * 30, chat_spikes=[200])
    ws = [w for w in candidate_windows(s, tl, min_s=15, max_s=30) if "chat_moment" in w.seeds]
    assert len(ws) == 1
    w = ws[0]
    assert w.start <= 200 - 15 and w.end >= 200 + 12 and 15 <= w.end - w.start <= 30


def test_chat_moment_windows_always_reach_the_llm_even_with_a_tiny_limit():
    s = segs(120)
    tl = Timeline(600, chat_per_10s=[2] * 60, chat_spikes=[100, 400])
    ws = candidate_windows(s, tl, min_s=15, max_s=30, limit=2)
    assert sorted(w.start for w in ws if "chat_moment" in w.seeds) == [85.0, 385.0]


def cand(a, b, score):
    return Candidate(a, b, "t", "h", "reaction", "", "r", score, {})


def test_each_chat_spike_keeps_its_best_clip_before_higher_scored_others():
    cands = [cand(0, 25, 90), cand(40, 65, 88), cand(185, 212, 55), cand(190, 215, 60)]
    chosen = dedupe(cands, 2, spikes=[200])
    assert [(c.start, c.score) for c in chosen] == [(0, 90), (190, 60)]


def test_strongest_spike_wins_when_there_are_more_spikes_than_places():
    cands = [cand(90, 115, 50), cand(190, 215, 50), cand(290, 315, 50)]
    chosen = dedupe(cands, 1, spikes=[300, 100, 200])  # ordered strongest first
    assert [c.start for c in chosen] == [290]


def test_pick_highlights_end_to_end_keeps_the_spike_moment():
    s = segs(60)
    tl = Timeline(300, chat_per_10s=[2] * 20 + [40] + [2] * 9, chat_spikes=[200])
    # the LLM prefers early windows; the spike window must still be chosen
    llm = MockLLM(lambda p: score_all(p, {i: 95 - i for i in range(40)}))
    chosen, _ = pick_highlights(llm, s, tl, GENERIC, min_s=15, max_s=30, top_n=2)
    assert any(c.start <= 205 <= c.end for c in chosen)


def test_scores_carry_emphasis_words_and_a_short_reaction_tag():
    from autoclip.pipeline.highlights import Score
    base = dict(id=0, hook_strength=5, payoff=5, standalone_clarity=5, emotion=5, niche_fit=5, virality=50,
                suggested_title="t", hook_headline="h", why="w")
    s = Score.model_validate({**base, "emphasis": ["bill", "$12,000", "a", "b", "c"], "reaction_tag": "shocked!! face 2"})
    assert s.emphasis == ["bill", "$12,000", "a", "b"] and s.reaction_tag == "shocked face"
    assert Score.model_validate({**base, "emphasis": "bill, leak"}).emphasis == ["bill", "leak"]
    old = Score.model_validate(base)  # answers without the new fields still parse
    assert old.emphasis == [] and old.reaction_tag == ""
