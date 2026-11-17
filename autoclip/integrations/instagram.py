"""Instagram token inspection and refresh, behind an interface with a mock."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx


class InstagramAuthError(Exception):
    """Message never contains a token (request URLs carry tokens, so they are not echoed)."""


@dataclass
class TokenInfo:
    is_valid: bool
    expires_at: datetime | None = None  # None with never_expires=False means "unknown"
    never_expires: bool = False


class InstagramAuthAPI(Protocol):
    def inspect(self, token: str) -> TokenInfo: ...
    def refresh(self, token: str) -> tuple[str, datetime | None]: ...


class MockInstagramAuth:
    def __init__(self, info: TokenInfo, refreshed: tuple[str, datetime | None] | None = None,
                 refreshed_info: TokenInfo | None = None, fail_refresh: str | None = None):
        self.info, self.refreshed = info, refreshed
        self.refreshed_info, self.fail_refresh = refreshed_info, fail_refresh
        self.refresh_calls = 0

    def inspect(self, token: str) -> TokenInfo:
        if self.refreshed and token == self.refreshed[0] and self.refreshed_info:
            return self.refreshed_info
        return self.info

    def refresh(self, token: str) -> tuple[str, datetime | None]:
        self.refresh_calls += 1
        if self.fail_refresh or not self.refreshed:
            raise InstagramAuthError(self.fail_refresh or "refresh not available")
        return self.refreshed


class GraphInstagramAuth:
    """Real client.

    host graph.facebook.com  -> Facebook-Login tokens: debug_token + fb_exchange_token
    host graph.instagram.com -> Instagram-Login tokens: /me + refresh_access_token
    """

    def __init__(self, app_id: str, app_secret: str, host: str = "graph.facebook.com",
                 client: httpx.Client | None = None, now=lambda: datetime.now(timezone.utc)):
        self.app_id, self.app_secret, self.host = app_id, app_secret, host
        self.client = client or httpx.Client(timeout=30)
        self.now = now

    @property
    def _is_ig_login(self) -> bool:
        return self.host == "graph.instagram.com"

    def _get(self, path: str, params: dict) -> tuple[int, dict]:
        try:
            r = self.client.get(f"https://{self.host}{path}", params=params)
            return r.status_code, r.json()
        except httpx.HTTPError as e:
            raise InstagramAuthError(f"network error ({type(e).__name__})") from None
        except ValueError:
            raise InstagramAuthError("non-JSON response") from None

    @staticmethod
    def _err(status: int, body: dict) -> str:
        return f"HTTP {status}: {(body.get('error') or {}).get('message', 'unknown error')}"

    def inspect(self, token: str) -> TokenInfo:
        if self._is_ig_login:
            status, body = self._get("/me", {"fields": "user_id", "access_token": token})
            return TokenInfo(is_valid=status == 200)
        status, body = self._get("/debug_token", {
            "input_token": token, "access_token": f"{self.app_id}|{self.app_secret}"})
        if status != 200:
            raise InstagramAuthError(self._err(status, body))
        d = body.get("data", {})
        exp = d.get("expires_at")
        return TokenInfo(
            is_valid=bool(d.get("is_valid")),
            expires_at=datetime.fromtimestamp(exp, timezone.utc) if exp else None,
            never_expires=exp == 0,
        )

    def refresh(self, token: str) -> tuple[str, datetime | None]:
        if self._is_ig_login:
            status, body = self._get("/refresh_access_token", {
                "grant_type": "ig_refresh_token", "access_token": token})
        else:
            status, body = self._get("/oauth/access_token", {
                "grant_type": "fb_exchange_token", "client_id": self.app_id,
                "client_secret": self.app_secret, "fb_exchange_token": token})
        if status != 200 or "access_token" not in body:
            raise InstagramAuthError(self._err(status, body))
        expires_in = body.get("expires_in")
        return body["access_token"], (self.now() + timedelta(seconds=expires_in)) if expires_in else None
