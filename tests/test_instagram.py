from datetime import datetime, timedelta, timezone

import httpx
import pytest

from autoclip.integrations.instagram import GraphInstagramAuth, InstagramAuthError

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def auth(handler, host="graph.facebook.com"):
    return GraphInstagramAuth("APP", "SECRET", host,
                              client=httpx.Client(transport=httpx.MockTransport(handler)),
                              now=lambda: NOW)


def test_facebook_inspect_reads_expiry():
    exp = int((NOW + timedelta(days=40)).timestamp())

    def handler(request):
        assert request.url.path == "/debug_token"
        assert request.url.params["access_token"] == "APP|SECRET"
        assert request.url.params["input_token"] == "TOKEN"
        return httpx.Response(200, json={"data": {"is_valid": True, "expires_at": exp}})

    info = auth(handler).inspect("TOKEN")
    assert info.is_valid and not info.never_expires
    assert info.expires_at == NOW + timedelta(days=40)


def test_facebook_inspect_never_expires():
    info = auth(lambda r: httpx.Response(200, json={"data": {"is_valid": True, "expires_at": 0}})).inspect("T")
    assert info.never_expires and info.expires_at is None


def test_facebook_refresh_uses_exchange():
    def handler(request):
        assert request.url.path == "/oauth/access_token"
        p = request.url.params
        assert (p["grant_type"], p["client_id"], p["client_secret"], p["fb_exchange_token"]) == (
            "fb_exchange_token", "APP", "SECRET", "OLD")
        return httpx.Response(200, json={"access_token": "NEW", "expires_in": 86400 * 60})

    assert auth(handler).refresh("OLD") == ("NEW", NOW + timedelta(days=60))


def test_refresh_without_expires_in():
    token, exp = auth(lambda r: httpx.Response(200, json={"access_token": "NEW"})).refresh("OLD")
    assert (token, exp) == ("NEW", None)


def test_instagram_login_host_uses_its_own_endpoints():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.url.host == "graph.instagram.com"
        if request.url.path == "/me":
            return httpx.Response(200, json={"user_id": "1"})
        assert request.url.params["grant_type"] == "ig_refresh_token"
        return httpx.Response(200, json={"access_token": "NEW", "expires_in": 100})

    a = auth(handler, host="graph.instagram.com")
    info = a.inspect("T")
    assert info.is_valid and info.expires_at is None and not info.never_expires
    assert a.refresh("T")[0] == "NEW"
    assert paths == ["/me", "/refresh_access_token"]


def test_errors_never_leak_the_token():
    def api_error(request):
        return httpx.Response(400, json={"error": {"message": "Session has expired"}})

    with pytest.raises(InstagramAuthError) as e:
        auth(api_error).refresh("SUPERSECRETTOKEN")
    assert "Session has expired" in str(e.value) and "SUPERSECRETTOKEN" not in str(e.value)

    def network_error(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(InstagramAuthError) as e:
        auth(network_error).inspect("SUPERSECRETTOKEN")
    assert "SUPERSECRETTOKEN" not in str(e.value) and "SECRET" not in str(e.value)
