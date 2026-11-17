"""Highlight engine (plan prompt 5).

1. Candidate windows (15-60s) seeded from signal peaks and from the transcript, snapped to
   sentence boundaries (and to shot cuts when one is close), never mid-word.
2. The LLM scores candidates in batches of up to 10 with structured JSON, validated with pydantic
   and retried once on invalid output.
3. Final score blends the LLM's virality with signal z-scores using the profile's weights;
   overlapping windows are de-duplicated and the top N returned.
"""
import json
import math
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError, field_validator

from autoclip.integrations.llm import LLM, LLMSwitched
from autoclip.integrations.transcriber import Segment
from autoclip.pipeline.profiles import GENERIC, Profile
from autoclip.pipeline.signals import Timeline, zscores

BATCH = 10
HOOK_TYPES = ("question", "bold_claim", "reaction", "story", "number", "conflict", "reveal")


@dataclass
class Window:
    id: int
    start: float
    end: float
    text: str
    signal_z: float = 0.0  # combined, profile-weighted signal strength
    seeds: list[str] = field(default_factory=list)


class Score(BaseModel):
    id: int
    hook_strength: int = Field(ge=0, le=10)
    payoff: int = Field(ge=0, le=10)
    standalone_clarity: int = Field(ge=0, le=10)
    emotion: int = Field(ge=0, le=10)
    niche_fit: int = Field(ge=0, le=10)
    virality: int = Field(ge=0, le=100)
    suggested_title: str = Field(max_length=120)
    hook_headline: str
    hook_type: str = "bold_claim"
    context_line: str = ""
    why: str
    emphasis: list[str] = []  # up to 4 words from the transcript that carry the punch (coloured on screen)
    reaction_tag: str = ""  # 1-2 words shown like *SHOCKED* on the big moment

    @field_validator("emphasis", mode="before")
    @classmethod
    def few_words(cls, v) -> list[str]:
        items = v.split(",") if isinstance(v, str) else (v or [])
        return [str(x).strip()[:30] for x in items if str(x).strip()][:4]

    @field_validator("reaction_tag", mode="before")
    @classmethod
    def short_tag(cls, v) -> str:
        return " ".join(re.sub(r"[^A-Za-z ]", "", str(v or "")).split()[:2])[:16]

    @field_validator("hook_headline")
    @classmethod
    def max_eight_words(cls, v: str) -> str:
        return " ".join(v.split()[:8])

    @field_validator("hook_type")
    @classmethod
    def known_type(cls, v: str) -> str:
        return v if v in HOOK_TYPES else "bold_claim"


class Batch(BaseModel):
    scores: list[Score]


@dataclass
class Candidate:
    start: float
    end: float
    title: str
    hook_headline: str
    hook_type: str
    context_line: str
    reason: str
    score: int  # final 0-100
    scores: dict


# ---------------------------------------------------------------- step 1: windows
def _snap_to_cut(t: float, cuts: list[float], tolerance: float = 1.5) -> float:
    near = [c for c in cuts if abs(c - t) <= tolerance]
    return min(near, key=lambda c: abs(c - t)) if near else t


def _window_from(segments: list[Segment], i: int, min_s: int, max_s: int, target_s: float) -> tuple[int, int] | None:
    """Starting at sentence i, extend whole sentences until the target length (within limits)."""
    start = segments[i].start
    j = i
    while j + 1 < len(segments) and segments[j + 1].end - start <= max_s and segments[j].end - start < target_s:
        j += 1
    if segments[j].end - start < min_s or segments[j].end - start > max_s:
        return None
    return i, j


CHAT_MOMENT = "chat_moment"
SETUP_S, PAYOFF_S = 15, 12


def _spike_window(segments: list[Segment], t: float, min_s: int, max_s: int) -> tuple[int, int] | None:
    """Whole sentences covering [t - SETUP_S, t + PAYOFF_S], widened to min_s and trimmed (from the
    front, keeping the moment) to max_s."""
    a = next((k for k, s in enumerate(segments) if s.end > t - SETUP_S), None)
    if a is None:
        return None
    b = next((k for k in range(a, len(segments)) if segments[k].end >= t + PAYOFF_S), len(segments) - 1)
    while segments[b].end - segments[a].start < min_s and (b + 1 < len(segments) or a > 0):
        if b + 1 < len(segments):
            b += 1
        else:
            a -= 1
    while segments[b].end - segments[a].start > max_s and a < b and segments[a + 1].start < t:
        a += 1
    while segments[b].end - segments[a].start > max_s and b > a:
        b -= 1
    length = segments[b].end - segments[a].start
    return (a, b) if min_s <= length <= max_s else None


def signal_strength(tl: Timeline | None, start: float, end: float, profile: Profile) -> float:
    """Profile-weighted mean z-score of loudness and chat inside a window."""
    if tl is None:
        return 0.0
    s, e = int(start), max(int(start) + 1, int(math.ceil(end)))
    total, weight = 0.0, 0.0
    if tl.loudness_db:
        z = zscores(tl.loudness_db)[s:e]
        if z:
            total += profile.weights.get("loudness", 0) * max(z)
            weight += profile.weights.get("loudness", 0)
    if tl.chat_per_10s:
        z = zscores([float(c) for c in tl.chat_per_10s])[s // 10:e // 10 + 1]
        if z:
            total += profile.weights.get("chat", 0) * max(z)
            weight += profile.weights.get("chat", 0)
    return total / weight if weight else 0.0


def candidate_windows(segments: list[Segment], tl: Timeline | None, profile: Profile = GENERIC, *,
                      min_s: int = 15, max_s: int = 60, limit: int = 40) -> list[Window]:
    if not segments:
        return []
    max_s = min(max_s, profile.max_seconds)
    starts: dict[int, list[str]] = {}

    # Seeds from signals: start one to three sentences before each peak, so the moment has a setup.
    peaks = ([("loud", p) for p in tl.loud_peaks] + [("chat", w) for w in tl.chat_spikes]) if tl else []
    for kind, t in peaks:
        idx = next((k for k, s in enumerate(segments) if s.end >= t), None)
        if idx is not None:
            for back in (1, 3):
                starts.setdefault(max(0, idx - back), []).append(kind)
    # Chat moments: a window built around each chat spike (spike times are already shifted back by the chat
    # lag, so the moment is at about t..t+10): ~15 s of setup before it and ~12 s of payoff after it.
    spike_spans: set[tuple[int, int]] = set()
    for t in (tl.chat_spikes if tl else []):
        span = _spike_window(segments, t, min_s, max_s)
        if span:
            spike_spans.add(span)
    # Seeds from the transcript: a window at roughly every 30 seconds of speech.
    last = -1e9
    for k, s in enumerate(segments):
        if s.start - last >= 30:
            starts.setdefault(k, []).append("transcript")
            last = s.start

    windows, wid = [], 0
    for a, b in sorted(spike_spans):
        start, end = segments[a].start, segments[b].end
        text = " ".join(s.text for s in segments[a:b + 1])
        windows.append(Window(wid, round(start, 2), round(end, 2), text,
                              signal_strength(tl, start, end, profile), [CHAT_MOMENT]))
        wid += 1
    for i in sorted(starts):
        for target in (min_s + 7, min_s + 20):  # ~22 s and ~35 s: top campaign clips run 20-25 s
            span = _window_from(segments, i, min_s, max_s, target)
            if not span:
                continue
            a, b = span
            start = _snap_to_cut(segments[a].start, tl.shot_cuts if tl else [])
            end = segments[b].end
            if start > segments[a].start + 0.01 or end - start < min_s:  # never cut into a sentence
                start = segments[a].start
            text = " ".join(s.text for s in segments[a:b + 1])
            windows.append(Window(wid, round(start, 2), round(end, 2), text,
                                  signal_strength(tl, start, end, profile), starts[i]))
            wid += 1

    # Drop near-duplicates, then keep the strongest seeds so the number of LLM requests stays small.
    unique: dict[tuple[int, int], Window] = {}
    for w in windows:
        unique.setdefault((int(w.start), int(w.end)), w)
    # chat moments always reach the LLM; then the strongest signal seeds
    ranked = sorted(unique.values(), key=lambda w: (CHAT_MOMENT not in w.seeds,
                                                    -(w.signal_z + (0.5 if "transcript" not in w.seeds else 0)),
                                                    w.start))
    kept = sorted(ranked[:limit], key=lambda w: w.start)
    for n, w in enumerate(kept):
        w.id = n
    return kept


# ---------------------------------------------------------------- step 2: LLM scoring
def build_prompt(batch: list[Window], profile: Profile, tl: Timeline | None, examples: list[dict],
                 creator: str = "") -> str:
    shots = ""
    if examples:
        shots = "\nEXAMPLES OF PAST CLIPS AND HOW THEY PERFORMED (learn from these):\n" + "\n".join(
            f"- {e.get('label', 'clip')}: \"{e.get('excerpt', '')[:200]}\" -> {e.get('outcome', '')}"
            for e in examples[:16]) + "\n"
    items = "\n\n".join(
        f"[{w.id}] ({w.end - w.start:.0f}s) signals: {tl.summary(w.start, w.end) if tl else 'none'}\n{w.text}"
        for w in batch)
    return (
        f"You score candidate moments for vertical short clips. Niche: {profile.name}.\n"
        + (f"The clips are about {creator}. In hook_headline and context_line always call them {creator}, "
           f"never 'the streamer', 'streamer', 'the host' or 'he'.\n" if creator else "") +
        f"What works in this niche: {profile.focus}\n"
        f"Hook headline tone: {profile.hook_tone}\n{shots}\n"
        "Sponsor reads, ads, promo codes, 'subscribe/follow' plugs and intros/outros are never good clips: "
        "give them virality 0.\n"
        "For EVERY candidate below, score 0-10: hook_strength (do the first 3 seconds grab attention), "
        "payoff (does it end on a satisfying point), standalone_clarity (makes sense alone), emotion, "
        "niche_fit; and virality 0-100. Also write: suggested_title (under 80 chars), hook_headline "
        "(max 8 words, shown on screen in the first 2.5s, must not just repeat the first sentence), "
        f"hook_type (one of {', '.join(HOOK_TYPES)}), context_line (optional, max 15 words: information "
        "the viewer cannot see, like why this matters or the stakes; NEVER describe what is on screen; "
        "empty string if nothing useful), why (one sentence), emphasis (up to 4 single words copied exactly "
        "from the transcript that carry the punch, like amounts, numbers, names or the shocking word; they "
        "get coloured on screen), and reaction_tag (1-2 words for the peak reaction, shown like *SHOCKED*, "
        "e.g. SHOCKED, SCREAMING, SPEECHLESS, CRYING LAUGHING; empty if there is no clear reaction).\n"
        'Answer with JSON only: {"scores": [{"id": 0, "hook_strength": 7, "payoff": 6, '
        '"standalone_clarity": 8, "emotion": 5, "niche_fit": 7, "virality": 64, "suggested_title": "...", '
        '"hook_headline": "...", "hook_type": "reaction", "context_line": "", "why": "...", '
        '"emphasis": ["bill", "$12,000"], "reaction_tag": "SHOCKED"}]}\n\n'
        "CANDIDATES:\n" + items)


def _extract_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", text or "", re.S)
        return json.loads(m.group(0)) if m else None


def score_batch(llm: LLM, batch: list[Window], profile: Profile, tl: Timeline | None,
                examples: list[dict], creator: str = "") -> tuple[list[Score], int]:
    """Returns valid scores for this batch and the number of requests used (1, or 2 after a retry)."""
    prompt = build_prompt(batch, profile, tl, examples, creator)
    ids = {w.id for w in batch}
    for attempt in (1, 2):
        text = llm.complete(prompt if attempt == 1 else
                            prompt + "\n\nYour previous answer was not valid JSON in the required shape. "
                                     "Answer again with JSON only.")
        try:
            data = _extract_json(text)
            if isinstance(data, list):
                data = {"scores": data}
            parsed = Batch.model_validate(data)
        except (ValidationError, ValueError):
            continue
        valid = [s for s in parsed.scores if s.id in ids]
        if valid:
            return valid, attempt
    return [], 2
    return [], 2


# ---------------------------------------------------------------- step 3: blend and select
def name_people(text: str, creator: str) -> str:
    """Overlay text must name the person the clip is about, never 'the streamer'."""
    if not creator or not text:
        return text
    out = re.sub(r"\b(the|this|our|a)\s+(streamer|host|creator)\b", creator, text, flags=re.I)
    return re.sub(r"\bstreamer\b", creator, out, flags=re.I)


def blend(score: Score, window: Window, profile: Profile, has_signals: bool = True) -> int:
    if not has_signals:  # no loudness peaks or chat in this video: the LLM's judgement is all we have
        return int(score.virality)
    w_llm = profile.weights.get("llm", 0.7)
    signal = 100 / (1 + math.exp(-window.signal_z))  # z-score -> 0..100
    return round(w_llm * score.virality + (1 - w_llm) * signal)


def dedupe(cands: list[Candidate], n: int, spikes: list[float] = ()) -> list[Candidate]:
    """Best non-overlapping candidates. Each chat spike first gets its best-scoring candidate that
    contains it (strongest spikes first, up to n): a moment the audience reacted to is never dropped
    for a moment Gemini merely liked. The remaining places go by score."""
    chosen: list[Candidate] = []

    def free(c):
        return all(c.end <= o.start or c.start >= o.end for o in chosen)

    for t in spikes:
        if len(chosen) >= n:
            break
        if any(o.start <= t + 5 <= o.end for o in chosen):
            continue
        hits = [c for c in cands if c.start <= t + 5 <= c.end and free(c)]
        if hits:
            chosen.append(max(hits, key=lambda c: c.score))
    for c in sorted(cands, key=lambda c: -c.score):
        if len(chosen) < n and c not in chosen and free(c):
            chosen.append(c)
    return sorted(chosen, key=lambda c: c.start)


def _spikes_by_strength(tl: Timeline | None) -> list[float]:
    if not tl or not tl.chat_spikes:
        return []
    counts = tl.chat_per_10s or []
    return sorted(tl.chat_spikes, key=lambda w: -(counts[w // 10] if w // 10 < len(counts) else 0))


def pick_highlights(llm: LLM, segments: list[Segment], tl: Timeline | None = None, profile: Profile = GENERIC, *,
                    min_s: int = 15, max_s: int = 60, top_n: int = 5, examples: list[dict] | None = None,
                    max_candidates: int = 40, creator: str = "") -> tuple[list[Candidate], dict]:
    has_signals = bool(tl and (tl.loud_peaks or tl.chat_spikes))
    windows = candidate_windows(segments, tl, profile, min_s=min_s, max_s=max_s, limit=max_candidates)
    by_id = {w.id: w for w in windows}
    batch_size = BATCH
    for attempt in range(2):
        cands, requests, scored = [], 0, 0
        try:
            for k in range(0, len(windows), batch_size):
                batch = windows[k:k + batch_size]
                scores, used = score_batch(llm, batch, profile, tl, examples or [], creator)
                requests += used
                scored += len(scores)
                for s in scores:
                    w = by_id[s.id]
                    cands.append(Candidate(
                        w.start, w.end, s.suggested_title.strip() or "Untitled",
                        name_people(s.hook_headline.strip(), creator),
                        s.hook_type, name_people(s.context_line.strip(), creator), s.why.strip() or "no reason given",
                        blend(s, w, profile, has_signals),
                        {**s.model_dump(exclude={"id", "suggested_title", "hook_headline", "why",
                                                 "hook_type", "context_line"}),
                         "signal_z": round(w.signal_z, 2), "seeds": w.seeds}))
        except LLMSwitched:
            if attempt == 0:
                batch_size = 3  # the local fallback model has a much smaller context
                continue
            raise
        return dedupe(cands, top_n, _spikes_by_strength(tl)), {"candidates": len(windows), "scored": scored, "requests": requests,
                                      "llm": llm.name}
    return [], {}
