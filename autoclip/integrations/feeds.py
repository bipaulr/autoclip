"""Channel feeds for discovery: YouTube RSS (free, no quota), Twitch Helix, Kick (best effort)."""
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Protocol

import httpx

from autoclip.rights import Video


class FeedError(Exception):
    """Message never contains credentials."""


# ---------------------------------------------------------------- YouTube RSS
class YouTubeFeed(Protocol):
    def latest(self, channel_id: str) -> list[str]: ...


class YouTubeRSS:
    URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
    NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=20, headers={"User-Agent": "autoclip/1.0"})

    def latest(self, channel_id: str) -> list[str]:
        try:
            r = self.client.get(self.URL.format(channel_id))
        except httpx.HTTPError as e:
            raise FeedError(f"YouTube RSS unreachable ({type(e).__name__})") from None
        if r.status_code != 200:
            raise FeedError(f"YouTube RSS HTTP {r.status_code} for {channel_id}")
        try:
            root = ET.fromstring(r.text)
        except ET.ParseError:
            raise FeedError("YouTube RSS returned invalid XML") from None
        return [e.findtext("yt:videoId", namespaces=self.NS) for e in root.findall("a:entry", self.NS)
                if e.findtext("yt:videoId", namespaces=self.NS)]


class FeedWithFallback:
    """RSS first (free, no quota); when it fails, the Data API's uploads playlist (1 unit per channel).
    YouTube's RSS feeds returned 404 for every channel on 16 Nov 2026."""

    def __init__(self, rss: YouTubeFeed, api):
        self.rss, self.api = rss, api

    def latest(self, channel_id: str) -> list[str]:
        try:
            return self.rss.latest(channel_id)
        except FeedError as rss_error:
            try:
                return self.api.channel_uploads(channel_id)
            except Exception as e:  # noqa: BLE001 - YouTubeAPIError and friends; messages carry no secrets
                raise FeedError(f"{rss_error}; Data API fallback failed too ({type(e).__name__}: {str(e)[:120]})") from None


class MockYouTubeFeed:
    def __init__(self, feeds: dict[str, list[str]] | None = None):
        self.feeds, self.calls = feeds or {}, []

    def latest(self, channel_id: str) -> list[str]:
        self.calls.append(channel_id)
        return self.feeds.get(channel_id, [])


# ---------------------------------------------------------------- Twitch Helix
def parse_twitch_duration(text: str) -> int:
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text or "")
    if not m or not text:
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


class Twitch(Protocol):
    def vods(self, login: str, first: int = 10) -> list[Video]: ...
    def live_logins(self, logins: list[str]) -> set[str]: ...


class TwitchHelix:
    def __init__(self, client_id: str, client_secret: str, client: httpx.Client | None = None,
                 clock=time.time):
        self.client_id, self.client_secret = client_id, client_secret
        self.client = client or httpx.Client(timeout=20)
        self.clock = clock
        self._token, self._expires = None, 0.0
        self._ids: dict[str, str] = {}

    def _auth(self) -> dict:
        if not self._token or self.clock() > self._expires - 60:
            try:
                r = self.client.post("https://id.twitch.tv/oauth2/token", data={
                    "client_id": self.client_id, "client_secret": self.client_secret,
                    "grant_type": "client_credentials"})
                body = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise FeedError(f"Twitch token request failed ({type(e).__name__})") from None
            if r.status_code != 200 or "access_token" not in body:
                raise FeedError(f"Twitch token rejected: HTTP {r.status_code}")
            self._token, self._expires = body["access_token"], self.clock() + float(body.get("expires_in", 3600))
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params, _retry: bool = True) -> dict:
        try:
            r = self.client.get(f"https://api.twitch.tv/helix{path}", params=params, headers=self._auth())
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise FeedError(f"Twitch request failed ({type(e).__name__})") from None
        if r.status_code == 401 and _retry:
            # Twitch can revoke an app token before it expires; a long-running process must not stay
            # blind until the old expiry time. Fetch a new token and try once more.
            self._token, self._expires = None, 0.0
            return self._get(path, params, _retry=False)
        if r.status_code != 200:
            raise FeedError(f"Twitch HTTP {r.status_code}: {body.get('message', '')}")
        return body

    def user_id(self, login: str) -> str:
        if login not in self._ids:
            data = self._get("/users", {"login": login}).get("data", [])
            if not data:
                raise FeedError(f"Twitch user '{login}' not found")
            self._ids[login] = data[0]["id"]
        return self._ids[login]

    def vods(self, login: str, first: int = 10) -> list[Video]:
        data = self._get("/videos", {"user_id": self.user_id(login), "type": "archive", "first": first}).get("data", [])
        return [Video(platform="twitch", video_id=v["id"], channel_id=login, channel_title=v.get("user_name", login),
                      title=v.get("title", ""), duration_s=parse_twitch_duration(v.get("duration", "")),
                      license="", views=int(v.get("view_count", 0)),
                      published_at=datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
                      if v.get("created_at") else None, language=v.get("language", "") or "")
                for v in data]

    def live_logins(self, logins: list[str]) -> set[str]:
        return set(self.live_streams(logins))

    def live_streams(self, logins: list[str]) -> dict[str, dict]:
        """login -> {"game": ..., "title": ...} for each channel that is live now."""
        if not logins:
            return {}
        data = self._get("/streams", [("user_login", l) for l in logins[:100]]).get("data", [])
        return {s["user_login"].lower(): {"game": s.get("game_name") or "", "title": s.get("title") or ""}
                for s in data if s.get("type") == "live"}


class MockTwitch:
    def __init__(self, vods: dict[str, list[Video]] | None = None, live: set[str] | None = None):
        self._vods, self.live = vods or {}, set(live or [])

    def vods(self, login: str, first: int = 10) -> list[Video]:
        return self._vods.get(login, [])[:first]

    def live_logins(self, logins: list[str]) -> set[str]:
        return {l for l in logins if l in self.live}

    def live_streams(self, logins: list[str]) -> dict[str, dict]:
        games = getattr(self, "games", {})
        return {l.lower(): {"game": games.get(l, ""), "title": ""} for l in logins if l in self.live}


# ---------------------------------------------------------------- Kick (unofficial, best effort)
class KickPublic:
    """Kick has no official public VOD API; this reads the endpoint its website uses.
    It may be blocked by Cloudflare at any time, in which case discovery logs and skips Kick."""

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=20, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

    def vods(self, slug: str, first: int = 10) -> list[Video]:
        try:
            r = self.client.get(f"https://kick.com/api/v2/channels/{slug}/videos")
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise FeedError(f"Kick unreachable or blocked ({type(e).__name__})") from None
        if r.status_code != 200 or not isinstance(data, list):
            raise FeedError(f"Kick HTTP {r.status_code} (often Cloudflare blocking)")
        out = []
        for v in data[:first]:
            vid = (v.get("video") or {}).get("uuid") or v.get("uuid")
            if not vid:
                continue
            created = v.get("created_at") or (v.get("video") or {}).get("created_at")
            out.append(Video(platform="kick", video_id=str(vid), channel_id=slug, channel_title=slug,
                             title=v.get("session_title", ""), duration_s=int((v.get("duration") or 0) / 1000),
                             license="", views=int(v.get("views") or v.get("viewer_count") or 0),
                             published_at=datetime.fromisoformat(created.replace("Z", "+00:00").replace(" ", "T"))
                             if created else None, language=(v.get("language") or "")[:2]))
        return out

    def live_logins(self, logins: list[str]) -> set[str]:
        live = set()
        for slug in logins:
            try:
                r = self.client.get(f"https://kick.com/api/v2/channels/{slug}")
                if r.status_code == 200 and (r.json().get("livestream") or None):
                    live.add(slug)
            except (httpx.HTTPError, ValueError):
                continue
        return live
