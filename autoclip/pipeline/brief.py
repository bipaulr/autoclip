"""Brief engine (plan prompt 19): campaign rules text -> strict, enforceable constraints."""
import json
import re
from datetime import date

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from autoclip.integrations.llm import LLM

PLATFORMS = ("instagram", "youtube", "tiktok", "x", "facebook")


class Brief(BaseModel):
    allowed_sources: list[str] = Field(default_factory=list)  # channel URLs/handles the footage must come from
    date_from: date | None = None
    date_to: date | None = None
    platforms: list[str] = Field(default_factory=list)
    min_seconds: int | None = None
    max_seconds: int | None = None
    required_hashtags: list[str] = Field(default_factory=list)
    required_mentions: list[str] = Field(default_factory=list)
    required_links: list[str] = Field(default_factory=list)
    name_in_caption: str | None = None
    disclosure_text: str | None = None
    banned_content: list[str] = Field(default_factory=list)
    no_logos: bool = False
    no_negative_portrayal: bool = False
    ai_generated_allowed: bool | None = None  # None = brief does not say
    niche: str | None = None
    audience_geo: list[str] = Field(default_factory=list)
    audience_geo_min_share: float | None = None
    rate_per_1k: float | None = None
    min_payout: float | None = None
    max_payout: float | None = None
    budget_remaining: float | None = None
    submit_within_minutes: int | None = None
    min_views_to_qualify: int | None = None
    avoid_ranges: list[list[float]] = Field(default_factory=list)  # [[start_s, end_s]] never in a clip
    priority_ranges: list[list[float]] = Field(default_factory=list)  # moments the campaign wants first
    max_hashtags: int | None = None
    banned_caption_words: list[str] = Field(default_factory=list)
    # caption instructions that depend on the clip, e.g. "a hashtag for each member in the video"
    caption_rules: str | None = None

    @model_validator(mode="before")
    @classmethod
    def null_means_not_stated(cls, data):
        # the prompt asks for null when the brief is silent; for lists and flags that is their default
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None or cls.model_fields.get(k) is None
                    or cls.model_fields[k].default is None}
        return data

    @field_validator("platforms", mode="before")
    @classmethod
    def norm_platforms(cls, v):
        out = []
        for p in v or []:
            p = str(p).lower()
            p = {"reels": "instagram", "instagram reels": "instagram", "shorts": "youtube",
                 "youtube shorts": "youtube", "twitter": "x"}.get(p, p)
            if p in PLATFORMS and p not in out:
                out.append(p)
        return out

    @field_validator("required_hashtags", mode="before")
    @classmethod
    def norm_tags(cls, v):
        return ["#" + str(t).lstrip("#").strip() for t in v or [] if str(t).strip("# ")]


PROMPT = """Extract the rules of this clipping campaign brief into JSON with exactly these keys:
allowed_sources (list of channel URLs or handles the footage must come from), date_from and date_to
(YYYY-MM-DD or null: the footage date range), platforms (subset of instagram, youtube, tiktok, x, facebook),
min_seconds, max_seconds, required_hashtags, required_mentions, required_links, name_in_caption (the
creator's name if it must appear in the caption, else null), disclosure_text (exact ad/sponsor disclosure
if required, else null), banned_content (list of short phrases), no_logos (bool), no_negative_portrayal
(bool), ai_generated_allowed (true, false, or null if not mentioned), niche, audience_geo (list of country
codes like US, GB), audience_geo_min_share (0-1 or null), rate_per_1k, min_payout, max_payout,
budget_remaining, submit_within_minutes, min_views_to_qualify, caption_rules (caption instructions that
depend on each clip and cannot be a fixed hashtag list, in one short sentence, e.g. "add a hashtag for each
member who appears in the clip"; null if none). Rules after "[Before you submit]" are the campaign's
submission requirements: put their fixed hashtags in required_hashtags. Use null when the brief does not say.
Do not invent rules. Answer with JSON only.

BRIEF:
"""


INFO_FIELDS = {"rate_per_1k", "min_payout", "max_payout", "budget_remaining", "min_views_to_qualify",
               "audience_geo_min_share", "niche"}


class BriefError(Exception):
    pass


def parse_brief(llm: LLM, rules_text: str) -> Brief:
    """Parses with the LLM, validates with pydantic, retries once on invalid output."""
    prompt = PROMPT + rules_text.strip()
    for attempt in (1, 2):
        text = llm.complete(prompt if attempt == 1 else prompt + "\n\nReturn valid JSON only.")
        try:
            m = re.search(r"\{.*\}", text or "", re.S)
            data = json.loads(m.group(0) if m else text)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            return Brief.model_validate(data)
        except ValidationError as e:
            # an odd money/stats field (e.g. a rate written as "$1.5") should not lose every rule: drop it.
            # A rule field that cannot be read still fails: dropping it would silently relax the brief.
            bad = {err["loc"][0] for err in e.errors() if err.get("loc")}
            if not bad <= INFO_FIELDS:
                continue
            try:
                return Brief.model_validate({k: v for k, v in data.items() if k not in bad})
            except ValidationError:
                continue
    raise BriefError("could not parse the brief into the required schema")
