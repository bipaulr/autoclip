"""YouTube Data API v3 (read side), behind an interface with a mock."""
import re
from datetime import datetime
from typing import Protocol

import httpx

from autoclip.rights import Video

SEARCH_COST, LIST_COST = 100, 1  # quota units per call (daily budget: 10,000)


class YouTubeAPIError(Exception):
    """Message never contains credentials."""


class YouTubeQuotaError(YouTubeAPIError):
    pass


class YouTubeAPI(Protocol):
    units_used: int

    def search_cc(self, query: str, published_after: datetime, max_results: int = 25,
                  language: str | None = None) -> list[str]: ...
    def channel_uploads(self, channel_id: str, max_results: int = 15) -> list[str]: ...
    def videos(self, ids: list[str]) -> list[Video]: ...


def parse_duration(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


class MockYouTube:
    def __init__(self, videos: list[Video] | None = None, search: dict[str, list[str]] | None = None,
                 uploads: dict[str, list[str]] | None = None, quota_exhausted: bool = False):
        self._videos = {v.video_id: v for v in videos or []}
        self._search, self._uploads = search or {}, uploads or {}
        self.quota_exhausted = quota_exhausted
        self.units_used = 0
        self.searched: list[str] = []
        self.search_languages: list[str | None] = []
        self.fetched: list[str] = []

    def search_cc(self, query, published_after, max_results=25, language=None):
        self.search_languages.append(language)
        if self.quota_exhausted:
            raise YouTubeQuotaError("quota exceeded")
        self.units_used += SEARCH_COST
        self.searched.append(query)
        return self._search.get(query, [])[:max_results]

    def channel_uploads(self, channel_id, max_results=15):
        self.units_used += LIST_COST
        return self._uploads.get(channel_id, [])[:max_results]

    def videos(self, ids):
        self.units_used += LIST_COST * ((len(ids) + 49) // 50)
        self.fetched += ids
        return [self._videos[i] for i in ids if i in self._videos]


class YouTubeDataAPI:
    BASE = "https://www.googleapis.com/youtube/v3"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 client: httpx.Client | None = None):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.client = client or httpx.Client(timeout=30)
        self._access: str | None = None
        self.units_used = 0

    def _token(self) -> str:
        if not self._access:
            try:
                r = self.client.post(self.TOKEN_URL, data={
                    "client_id": self.client_id, "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
                body = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise YouTubeAPIError(f"token request failed ({type(e).__name__})") from None
            if r.status_code != 200 or "access_token" not in body:
                raise YouTubeAPIError(f"token refresh rejected: HTTP {r.status_code} {body.get('error', '')}")
            self._access = body["access_token"]
        return self._access

    def _get(self, path: str, params: dict, cost: int) -> dict:
        token = self._token()
        try:
            r = self.client.get(self.BASE + path, params=params, headers={"Authorization": f"Bearer {token}"})
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise YouTubeAPIError(f"request failed ({type(e).__name__})") from None
        self.units_used += cost
        if r.status_code == 200:
            return body
        err = body.get("error") or {}
        reason = ((err.get("errors") or [{}])[0]).get("reason", "")
        if r.status_code == 429 or reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            raise YouTubeQuotaError(f"quota: {reason or 'HTTP 429'}")
        raise YouTubeAPIError(f"HTTP {r.status_code}: {err.get('message', 'unknown error')}")

    def search_cc(self, query: str, published_after: datetime, max_results: int = 25,
                  language: str | None = None) -> list[str]:
        params = {
            "part": "id", "type": "video", "q": query, "videoLicense": "creativeCommon",
            "videoDuration": "long", "order": "viewCount", "maxResults": min(max_results, 50),
            "publishedAfter": published_after.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if language:
            params["relevanceLanguage"] = language  # a hint only; discovery re-checks each video
        body = self._get("/search", params, SEARCH_COST)
        return [i["id"]["videoId"] for i in body.get("items", []) if i.get("id", {}).get("videoId")]

    def channel_uploads(self, channel_id: str, max_results: int = 15) -> list[str]:
        if not channel_id.startswith("UC"):
            raise YouTubeAPIError(f"channel id must start with UC: {channel_id}")
        body = self._get("/playlistItems", {
            "part": "contentDetails", "playlistId": "UU" + channel_id[2:],
            "maxResults": min(max_results, 50)}, LIST_COST)
        return [i["contentDetails"]["videoId"] for i in body.get("items", [])]

    def videos(self, ids: list[str]) -> list[Video]:
        out: list[Video] = []
        for start in range(0, len(ids), 50):
            body = self._get("/videos", {
                "part": "snippet,contentDetails,status,statistics",
                "id": ",".join(ids[start:start + 50])}, LIST_COST)
            for it in body.get("items", []):
                sn = it.get("snippet", {})
                published = sn.get("publishedAt")
                out.append(Video(
                    platform="youtube", video_id=it["id"], channel_id=sn.get("channelId", ""),
                    channel_title=sn.get("channelTitle", ""), title=sn.get("title", ""),
                    duration_s=parse_duration(it.get("contentDetails", {}).get("duration", "")),
                    license=it.get("status", {}).get("license", ""),
                    views=int(it.get("statistics", {}).get("viewCount", 0)),
                    published_at=datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None,
                    language=sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or ""))
        return out
