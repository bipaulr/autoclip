"""Campaign listings: import, ranking and status.

No marketplace (Content Rewards, Vyro, Clipster, Reach) publishes an official listings API that I
could find, and scraping their sites would break their terms, so listings come from
config/campaigns.yaml, which I (Claude) or you keep up to date from the marketplace pages.
"""
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoclip.decisions import log_decision
from autoclip.models import Campaign
from autoclip.pipeline.brief import Brief

MODULE = "campaigns"
FIELDS = ("name", "url", "creator", "niche", "rate_per_1k", "budget_total", "budget_remaining", "min_payout",
          "max_payout", "allowed_platforms", "rules_text", "source_channels", "starts", "ends",
          "approved_in_principle", "source_videos")


NOTICE_TAG = "[Before you submit]"


def with_notice(rules: str | None, current: str | None) -> str | None:
    """A "Before you submit" notice saved by auto-submit is only visible to joined members, so it is not in
    campaigns.yaml: an import keeps it after the new rules instead of erasing it."""
    if not current or NOTICE_TAG not in current or (rules and NOTICE_TAG in rules):
        return rules
    return f"{rules or ''}\n\n{current[current.index(NOTICE_TAG):]}".strip()


def import_campaigns(path: str | Path, session: Session) -> int:
    """Upserts campaigns from YAML. Status (joined, paused...) is never changed by an import,
    except that a campaign past its end date becomes 'ended'."""
    p = Path(path)
    if not p.exists():
        return 0
    entries = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("campaigns") or []
    today = date.today()
    for e in entries:
        if not e.get("marketplace") or not e.get("id"):
            raise ValueError("config/campaigns.yaml: every campaign needs 'marketplace' and 'id'")
        c = session.scalars(select(Campaign).where(Campaign.marketplace == e["marketplace"],
                                                   Campaign.external_id == str(e["id"]))).first()
        if c is None:
            c = Campaign(marketplace=e["marketplace"], external_id=str(e["id"]), name=e.get("name", e["id"]),
                         url=e.get("url", ""), status="open")
            session.add(c)
        if "rules_text" in e:
            e = {**e, "rules_text": with_notice(e["rules_text"], c.rules_text)}
            if e["rules_text"] != c.rules_text:
                c.brief = None  # rules changed: the brief engine parses them again
        for f in FIELDS:
            if f in e:
                if f == "url" and "/c/campaigns/" in (c.url or "") and "/c/campaigns/" not in (e[f] or ""):
                    continue  # keep the exact campaign page found by the campaign feed
                setattr(c, f, e[f])
        if e.get("brief"):  # a brief written by hand in campaigns.yaml wins over the LLM's reading
            c.brief = Brief.model_validate(e["brief"]).model_dump(mode="json")
        if e.get("joined") and c.status == "open":
            c.status = "joined"
            log_decision(session, MODULE, "joined", f"marked joined in campaigns.yaml: {c.name}", subject=c.url)
        if c.ends and c.ends < today and c.status != "ended":
            c.status = "ended"
            log_decision(session, MODULE, "ended", f"end date {c.ends} has passed", subject=c.url)
        c.updated_at = datetime.now(timezone.utc)
    session.flush()
    return len(entries)
