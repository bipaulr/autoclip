"""Campaign listings from Content Rewards' public Discover page.

Checked 14 Nov 2026: robots.txt allows /c/discover (only /api/ and account pages are disallowed) and the
Creator Terms say nothing against reading listings. The page is server-rendered and carries each
campaign as structured data, so one polite request every few hours is enough. /api/ is never called.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

# The GTA Clipper network posts its GTA 6 campaigns on its own Content Rewards page (same page format;
# robots.txt allows it). It had 0 campaigns on 16 Nov 2026; launch-week campaigns are expected there.
DISCOVER_URLS = ("https://contentrewards.com/c/discover", "https://contentrewards.com/c/discover/clipping",
                 "https://contentrewards.com/gtaclipper")
USER_AGENT = "autoclip-personal/1.0 (checks public campaign listings a few times a day)"


class MarketplaceError(Exception):
    pass


@dataclass
class Listing:
    id: str
    title: str
    brand: str
    category: str
    description: str
    platforms: list[str]
    rate_per_1k: float | None
    budget_total: float | None
    budget_left: float | None
    requires_application: bool
    kind: str  # cpm, ...
    url: str
    channels: list[dict] = field(default_factory=list)  # footage sources found in the description


CHANNEL_PATTERNS = (
    ("twitch", re.compile(r"twitch\.tv/([A-Za-z0-9_]{3,25})", re.I)),
    ("kick", re.compile(r"kick\.com/([A-Za-z0-9_-]{2,40})", re.I)),
    ("youtube_handle", re.compile(r"youtube\.com/@([A-Za-z0-9_.-]{2,40})", re.I)),
)
NOT_CHANNELS = {"videos", "video", "directory", "search", "settings", "p", "c"}


def channels_in(text: str) -> list[dict]:
    out, seen = [], set()
    for platform, rx in CHANNEL_PATTERNS:
        for m in rx.finditer(text or ""):
            cid = m.group(1).lower() if platform != "youtube_handle" else m.group(1)
            if cid.lower() in NOT_CHANNELS or (platform, cid.lower()) in seen:
                continue
            seen.add((platform, cid.lower()))
            out.append({"platform": platform, "channel_id": cid})
    return out


def _payload(page: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', page, re.S)
    try:
        return "".join(json.loads('"' + c + '"') for c in chunks)
    except ValueError as e:
        raise MarketplaceError(f"page format changed ({e})") from None


def parse_discover(page: str) -> list[Listing]:
    text, dec, found = _payload(page), json.JSONDecoder(), {}
    for m in re.finditer(r'"campaigns":\[', text):
        try:
            arr, _ = dec.raw_decode(text, m.end() - 1)
        except ValueError:
            continue
        for c in arr:
            if not isinstance(c, dict) or not c.get("id") or not c.get("title"):
                continue
            desc = c.get("description") or ""
            found[c["id"]] = Listing(
                id=c["id"], title=c["title"].replace("$$", "$").strip(), brand=c.get("brand") or "",
                category=c.get("category") or "", description=desc,
                platforms=[p.lower() for p in c.get("platforms") or []],
                rate_per_1k=c.get("payoutSortRaw") if c.get("type") == "cpm" else None,
                budget_total=c.get("budgetTotalRaw"), budget_left=c.get("availableBudgetRaw"),
                requires_application=bool(c.get("requiresApplication")), kind=c.get("type") or "",
                url=f"https://contentrewards.com/c/campaigns/{c['id']}", channels=channels_in(desc))
    return list(found.values())


def parse_campaign_page(page: str, campaign_id: str) -> Listing | None:
    """One campaign's public page -> Listing, or None when it is not active (ended, paused, draft)."""
    text = _payload(page)
    m = re.search(r'"queryKey":\["campaigns","detail","%s"\]' % re.escape(campaign_id), text)
    start = text.rfind('"data":{', 0, m.start()) if m else -1
    if start < 0:
        raise MarketplaceError(f"campaign {campaign_id}: page format changed")
    try:
        c, _ = json.JSONDecoder().raw_decode(text, start + 7)
    except ValueError as e:
        raise MarketplaceError(f"campaign {campaign_id}: {e}") from None
    if c.get("status") != "active":
        return None
    cfg = c.get("configuration") or {}
    payouts = cfg.get("payoutModel") or []
    rates = {p["platform"]: p.get("rate") or 0 for p in payouts if p.get("platform")}
    # Some campaigns pay nothing for views from some countries (e.g. India/Pakistan/Bangladesh).
    zero_geo = sorted({f"{t.get('label', '?')} on {t.get('platform')}" for t in (cfg.get("geoRates") or {}).get("tiers") or []
                       if not t.get("rate")})
    budget = (cfg.get("budget") or 0) / 100 or None
    spent = ((c.get("metrics") or {}).get("budgetSpentCents") or 0) / 100
    cats = (cfg.get("content") or {}).get("contentCategories") or []
    desc = (c.get("description") or "").strip()
    if zero_geo:
        desc += "\n[pays $0 for views from: " + "; ".join(zero_geo) + "]"
    ours = [rates[p] for p in ("instagram", "youtube") if rates.get(p)]
    return Listing(
        id=campaign_id, title=(c.get("name") or "").strip(), brand=(c.get("organization") or {}).get("name") or "",
        category=cats[0]["name"] if cats else "", description=desc, platforms=sorted(rates),
        rate_per_1k=max(ours) / 100 if ours else (max(rates.values()) / 100 if rates and max(rates.values()) else None),
        budget_total=budget, budget_left=(budget - spent) if budget else None,
        requires_application=bool(c.get("requiresApplication") or (cfg.get("requirement") or {}).get("preApproval")),
        kind=c.get("type") or "", url=f"https://contentrewards.com/c/campaigns/{campaign_id}",
        channels=channels_in(c.get("description") or ""))


def sitemap_campaign_ids(xml: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"/c/campaigns/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", xml)))


class Marketplace(Protocol):
    name: str

    def listings(self, seen: set[str] | None = None) -> list[Listing]: ...


class ContentRewardsPublic:
    """Discover pages, plus campaigns found only in the sitemap (opened a few dozen at a time, slowly)."""
    name = "content_rewards"

    def __init__(self, client: httpx.Client | None = None, urls=DISCOVER_URLS, *, per_run: int = 60,
                 pause_s: float = 2.0, sleep=None):
        import time
        self.client = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self.urls, self.per_run, self.pause_s, self.sleep = urls, per_run, pause_s, sleep or time.sleep
        self.inactive: list[str] = []  # campaign pages that turned out ended/paused: never opened again

    def listings(self, seen: set[str] | None = None) -> list[Listing]:
        out = self._discover()
        seen = set(seen or ()) | set(out)
        try:
            r = self.client.get("https://contentrewards.com/sitemap.xml")
            ids = sitemap_campaign_ids(r.text) if r.status_code == 200 else []
        except httpx.HTTPError:
            ids = []
        for cid in [i for i in ids if i not in seen][:self.per_run]:
            self.sleep(self.pause_s)
            try:
                r = self.client.get(f"https://contentrewards.com/c/campaigns/{cid}")
                item = parse_campaign_page(r.text, cid) if r.status_code == 200 else None
            except (httpx.HTTPError, MarketplaceError):
                continue  # try again next run
            if item is None:
                self.inactive.append(cid)
            else:
                out[cid] = item
        return list(out.values())

    def _discover(self) -> dict[str, Listing]:
        out: dict[str, Listing] = {}
        for k, url in enumerate(self.urls):
            try:
                r = self.client.get(url)
                if r.status_code != 200:
                    raise MarketplaceError(f"Content Rewards HTTP {r.status_code} for {url}")
                items = parse_discover(r.text)
            except (httpx.HTTPError, MarketplaceError) as e:
                if k < 2:  # the main Discover pages must work; an extra page (GTA Clipper) may fail alone
                    if isinstance(e, MarketplaceError):
                        raise
                    raise MarketplaceError(f"Content Rewards unreachable ({type(e).__name__})") from None
                continue
            for item in items:
                out[item.id] = item
        if not out:
            raise MarketplaceError("no campaigns found on the Discover page (layout changed?)")
        return out


class MockMarketplace:
    name = "content_rewards"

    def __init__(self, items: list[Listing]):
        self.items, self.inactive = items, []

    def listings(self, seen: set[str] | None = None) -> list[Listing]:
        return list(self.items)
