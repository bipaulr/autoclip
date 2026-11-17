"""Packaging and dedupe (plan prompt 9).

Per-platform title, description and hashtags; best thumbnail frame; disclosure flags; a perceptual
video hash, and blocking of clips over 90% similar to anything posted in the last 60 days.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

from autoclip.integrations.llm import LLM, LLMError
from autoclip.pipeline.brief import Brief

FRAMES = 8
SIMILARITY_BLOCK = 0.90


# ---------------------------------------------------------------- perceptual hash
def _dct_hash(gray32) -> int:
    """64-bit pHash of a 32x32 grayscale image (numpy array)."""
    import cv2
    import numpy as np
    d = cv2.dct(np.float32(gray32))[:8, :8]
    med = np.median(d.flatten()[1:])
    bits = (d > med).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def video_phash(path: Path, frames: int = FRAMES) -> str:
    """Hex string of `frames` 64-bit frame hashes sampled evenly through the clip."""
    import cv2
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    hashes = []
    try:
        for k in range(frames):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / frames))
            ok, frame = cap.read()
            if not ok:
                hashes.append(0)
                continue
            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (32, 32), interpolation=cv2.INTER_AREA)
            hashes.append(_dct_hash(small))
    finally:
        cap.release()
    return "".join(f"{h:016x}" for h in hashes)


def similarity(a: str, b: str) -> float:
    """1.0 = identical. Compares frame hashes position by position."""
    if not a or not b or len(a) != len(b):
        return 0.0
    bits = len(a) * 4
    diff = bin(int(a, 16) ^ int(b, 16)).count("1")
    return 1 - diff / bits


def is_duplicate(phash: str, recent: list[str], threshold: float = SIMILARITY_BLOCK) -> tuple[bool, float]:
    best = max((similarity(phash, r) for r in recent), default=0.0)
    return best > threshold, best


# ---------------------------------------------------------------- thumbnail
def pick_thumbnail(path: Path, out: Path, detector=None, samples: int = 12) -> Path | None:
    """Sharpest frame, with a bonus for a visible face."""
    import cv2
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    best, best_score = None, -1.0
    try:
        for k in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / samples))
            ok, frame = cap.read()
            if not ok:
                continue
            sharp = cv2.Laplacian(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
            face = 1.5 if detector and detector.detect(frame) else 1.0
            if sharp * face > best_score:
                best, best_score = frame, sharp * face
    finally:
        cap.release()
    if best is None:
        return None
    cv2.imwrite(str(out), best)
    return out


# ---------------------------------------------------------------- text
@dataclass
class PlatformPackage:
    title: str
    description: str
    hashtags: list[str]
    disclosure: dict

    def caption(self) -> str:
        return " ".join(x for x in [self.title, self.description, " ".join(self.hashtags)] if x).strip()


PROMPT = """Write post text for one short vertical clip, for YouTube Shorts and Instagram Reels.
Clip title idea: {title}
Hook: {hook}
What happens: {excerpt}
Creator: {creator}
Source video: {source_title}
What is said in the clip: {said}
Return JSON only: {{"youtube": {{"title": "under 90 chars", "description": "1-2 sentences", "hashtags": ["#..."]}},
"instagram": {{"title": "first line of caption, under 120 chars", "description": "1-2 sentences",
"hashtags": ["#..."]}}}}. Up to 5 hashtags each, no spammy tags.{rules}"""
RULES = ("\nCampaign caption rules (the post is rejected if they are broken): {r}\nFollow them in the hashtags. "
         "Only tag people who are named in the clip title, the source video title, what happens or what is said; "
         "never guess who appears.")


def _clean_tags(tags, required: list[str], limit: int = 8) -> list[str]:
    out = []
    for t in required + list(tags or []):
        t = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if len(t) > 1 and t.lower() not in [o.lower() for o in out]:
            out.append(t)
    return out[:limit]


def package_text(llm: LLM | None, *, title: str, hook: str, excerpt: str, creator: str,
                 attribution: str | None, brief: Brief | None, source_title: str = "",
                 said: str = "") -> dict[str, PlatformPackage]:
    req_tags = list(brief.required_hashtags) if brief else []
    extra = []
    if brief:
        extra += brief.required_mentions + brief.required_links
        if brief.name_in_caption:
            extra.append(brief.name_in_caption)
        if brief.disclosure_text:
            extra.insert(0, brief.disclosure_text)
    data = {}
    if llm is not None:
        try:
            rules = RULES.format(r=brief.caption_rules) if brief and brief.caption_rules else ""
            text = llm.complete(PROMPT.format(title=title, hook=hook, excerpt=excerpt[:600], creator=creator,
                                              source_title=source_title[:200], said=said[:1200],
                                              rules=rules))
            data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        except (LLMError, AttributeError, ValueError):
            data = {}
    out = {}
    for platform, limit in (("youtube", 95), ("instagram", 2000)):
        d = data.get(platform) if isinstance(data.get(platform), dict) else {}
        t = str(d.get("title") or title)[:limit]
        if platform == "youtube" and "#shorts" not in t.lower() and len(t) <= limit - 8:
            t = f"{t} #Shorts"
        desc = " ".join(x for x in [str(d.get("description") or ""), " ".join(extra), attribution or ""] if x)
        out[platform] = PlatformPackage(
            title=t, description=desc.strip(), hashtags=_clean_tags(d.get("hashtags"), req_tags, (brief.max_hashtags if brief and brief.max_hashtags else 8)),
            # Real footage, cut and captioned: not synthetic media under YouTube's or Meta's labels.
            disclosure={"synthetic_media": False, "paid_partnership": bool(brief and brief.disclosure_text)})
    return out
