"""Rights and safety gate (plan prompt 6). Runs before render and again before publish.

Every check returns a reason; the caller writes it to the decision log. Not implemented yet, and
said so honestly in each reason: OCR of on-screen text and a frame classifier for graphic violence
(both need models this PC does not have yet). The transcript-based checks cover the same risks
from what is said.
"""
import json
import re
from dataclasses import dataclass
from datetime import date

from autoclip.integrations.llm import LLM, LLMError, is_local
from autoclip.pipeline.brief import Brief

GAMBLING = re.compile(r"\b(casino|slots?|roulette|blackjack|stake\.com|gambl\w*|sportsbook|betting|"
                      r"bet365|draftkings|fanduel|crash game|plinko|rollbit)\b", re.I)
LEAK = re.compile(r"\b(leak(ed|s)?|pre-?release|unreleased|stolen build|datamine[d]?)\b", re.I)
VIOLENCE_WORDS = re.compile(r"\b(blood|stab\w*|shot dead|gore|beheaded|corpse|dismember\w*)\b", re.I)


@dataclass
class GateResult:
    ok: bool
    reason: str
    hold: bool = False  # True = needs the user's decision rather than a hard block


def check_source(video, campaign, today: date) -> GateResult:
    if not video.rights_basis:
        return GateResult(False, "no recorded rights basis for this source")
    if campaign is not None:
        if campaign.status not in ("joined",):
            return GateResult(False, f"campaign '{campaign.name}' is {campaign.status}, not joined")
        if campaign.ends and today > campaign.ends:
            return GateResult(False, f"campaign '{campaign.name}' ended on {campaign.ends}")
    return GateResult(True, f"source cleared: {video.rights_basis}")


def check_brief(brief: Brief | None, *, video, length_s: float, platform: str | None = None,
                caption: str | None = None, allow_ai_assisted: bool = False) -> GateResult:
    """Platform, length, footage date range and caption requirements."""
    if brief is None:
        return GateResult(True, "no campaign brief to enforce")
    if platform and brief.platforms and platform not in brief.platforms:
        return GateResult(False, f"brief does not pay on {platform} (allowed: {', '.join(brief.platforms)})")
    if brief.min_seconds and length_s < brief.min_seconds:
        return GateResult(False, f"clip is {length_s:.0f}s, brief minimum is {brief.min_seconds}s")
    if brief.max_seconds and length_s > brief.max_seconds:
        return GateResult(False, f"clip is {length_s:.0f}s, brief maximum is {brief.max_seconds}s")
    published = video.published_at.date() if getattr(video, "published_at", None) else None
    if published and brief.date_from and published < brief.date_from:
        return GateResult(False, f"footage from {published} is before the brief's range ({brief.date_from})")
    if published and brief.date_to and published > brief.date_to:
        return GateResult(False, f"footage from {published} is after the brief's range ({brief.date_to})")
    if brief.allowed_sources:
        # Match on the channel id or handle only: a display name can be copied by an impersonator.
        ids = [str(x).lower().lstrip("@") for x in (video.channel_id, getattr(video, "channel_handle", None)) if x]
        allowed = [s.lower().rstrip("/") for s in brief.allowed_sources]
        if not any(s == i or s.endswith("/" + i) or s.endswith("/@" + i) or s == "@" + i
                   for s in allowed for i in ids):
            return GateResult(False, "footage is not from a source the brief allows")
    if caption is not None:
        missing = [t for t in brief.required_hashtags if t.lower() not in caption.lower()]
        missing += [m for m in brief.required_mentions if m.lower() not in caption.lower()]
        if brief.name_in_caption and brief.name_in_caption.lower() not in caption.lower():
            missing.append(brief.name_in_caption)
        if brief.disclosure_text and brief.disclosure_text.lower() not in caption.lower():
            missing.append("disclosure text")
        if missing:
            return GateResult(False, f"caption is missing {', '.join(missing)}")
        banned = [w for w in brief.banned_caption_words if w.lower() in caption.lower()]
        if banned:
            return GateResult(False, f"caption uses banned words: {', '.join(banned)}")
        tags = [t for t in caption.split() if t.startswith("#")]
        if brief.max_hashtags and len(tags) > brief.max_hashtags:
            return GateResult(False, f"{len(tags)} hashtags, the brief allows at most {brief.max_hashtags}")
    if brief.ai_generated_allowed is False and not allow_ai_assisted:
        return GateResult(False, "brief forbids AI-generated content; autoclip clips are real footage but "
                                 "AI-selected and AI-captioned, so this needs your decision", hold=True)
    return GateResult(True, "meets the campaign brief")


def check_text_rules(title: str, transcript_text: str, first_7s_text: str) -> GateResult:
    """Fast, free keyword checks before the LLM moderation pass."""
    if LEAK.search(title or ""):
        return GateResult(False, "source looks like a leak or pre-release footage")
    m = GAMBLING.search(f"{title} {transcript_text}")
    if m:
        return GateResult(False, f"gambling content ('{m.group(0)}')")
    m = VIOLENCE_WORDS.search(first_7s_text or "")
    if m:
        return GateResult(False, f"possible graphic violence in the first 7 seconds ('{m.group(0)}'); "
                                 "frame-level check not available yet")
    return GateResult(True, "no leak, gambling or early-violence keywords (OCR and frame checks not available yet)")


MOD_PROMPT = """You are a content safety reviewer for short clips posted to Instagram and YouTube.
For each numbered transcript, answer whether it contains: hate (attacks on protected groups),
sexual (explicit sexual content), self_harm (encouraging or depicting self-harm), gambling (promoting
gambling), or graphic_violence. Swearing, jokes and heated arguments are fine.
Answer JSON only: {"results": [{"id": 0, "flags": [], "note": ""}]}

"""


MOD_ATTEMPTS = 3
MOD_UNAVAILABLE = "moderation unavailable"
MOD_LOCAL_FLAG = "local model flagged"


def _results(reply: str) -> list[dict]:
    """The result list from {"results": [...]} or, as lighter models sometimes answer, a bare [...]."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    try:
        data = json.loads(text)
    except ValueError:
        data = json.loads(re.search(r"[\[{].*[\]}]", text, re.S).group(0))
    if isinstance(data, dict):
        data = data["results"]
    if not isinstance(data, list):
        raise TypeError("moderation reply is not a list of results")
    return data


def moderate(llm: LLM, texts: list[str]) -> list[GateResult]:
    """One LLM request for all clips of a video. If the LLM is unavailable, hold rather than pass."""
    if not texts:
        return []
    prompt = MOD_PROMPT + "\n\n".join(f"[{i}] {t[:1500]}" for i, t in enumerate(texts))
    by_id = None
    for n in range(MOD_ATTEMPTS):  # lighter fallback models sometimes return broken JSON: ask again
        try:
            # a changed prompt on retries, so a cached broken reply is not simply returned again
            asked = prompt if n == 0 else f"{prompt}\n\n(Retry {n}: reply with the JSON object only.)"
            by_id = {int(r["id"]): r for r in _results(llm.complete(asked))}
            break
        except (LLMError, AttributeError, ValueError, KeyError, TypeError) as e:
            err = e
    if by_id is None:
        reason = f"{MOD_UNAVAILABLE} ({type(err).__name__}); held until it can run"
        return [GateResult(False, reason, hold=True) for _ in texts]
    local = is_local(getattr(llm, "name", ""))
    out = []
    for i in range(len(texts)):
        r = by_id.get(i)
        if r is None:
            out.append(GateResult(False, f"{MOD_UNAVAILABLE} for this clip (no answer); held until it can run", hold=True))
        elif r.get("flags") and local:
            # the small local model over-flags (it called a lost bill "gambling"): hold, so Gemini re-checks
            out.append(GateResult(False, f"{MOD_LOCAL_FLAG}: {', '.join(map(str, r['flags']))}; held for a "
                                         "re-check by Gemini", hold=True))
        elif r.get("flags"):
            out.append(GateResult(False, f"moderation flagged: {', '.join(map(str, r['flags']))}"
                                         + (f" ({r.get('note')})" if r.get("note") else "")))
        else:
            out.append(GateResult(True, "passed moderation"))
    return out


def check_ranges(brief: Brief | None, start: float, end: float) -> GateResult:
    """Blocks a clip that touches any part of the footage the campaign says to avoid."""
    for a, b in (brief.avoid_ranges if brief else []):
        if start < b and end > a:
            return GateResult(False, f"clip ({start:.0f}-{end:.0f}s) overlaps footage the brief says to avoid "
                                     f"({a:.0f}-{b:.0f}s)")
    return GateResult(True, "no avoided footage")
