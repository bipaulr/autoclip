from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from autoclip.integrations.instagram import MockInstagramAuth, TokenInfo
from autoclip.integrations.telegram import MockNotifier
from autoclip.models import DecisionLog
from autoclip.services.ig_token import refresh_instagram_token
from autoclip.token_store import MemoryTokenStore

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def in_days(n):
    return NOW + timedelta(days=n)


def run(session, auth, token="OLD"):
    store = MemoryTokenStore(IG_ACCESS_TOKEN=token) if token else MemoryTokenStore()
    notifier = MockNotifier()
    result = refresh_instagram_token(auth, store, notifier, session, now=NOW, threshold_days=15)
    logs = session.scalars(select(DecisionLog)).all()
    return result, store, notifier, logs


def test_skips_when_plenty_of_time_left(session):
    auth = MockInstagramAuth(TokenInfo(True, in_days(50)))
    result, store, notifier, logs = run(session, auth)
    assert result.status == "skipped" and "50 days" in result.reason
    assert auth.refresh_calls == 0 and store.get("IG_ACCESS_TOKEN") == "OLD" and notifier.sent == []
    assert [(l.module, l.action, l.subject) for l in logs] == [("ig_token", "skipped", "IG_ACCESS_TOKEN")]


def test_refreshes_when_near_expiry(session):
    auth = MockInstagramAuth(TokenInfo(True, in_days(10)), refreshed=("NEW", in_days(60)))
    result, store, notifier, logs = run(session, auth)
    assert result.status == "refreshed" and "2027-01-11" in result.reason
    assert store.get("IG_ACCESS_TOKEN") == "NEW" and notifier.sent == []
    assert logs[0].action == "refreshed"


def test_refresh_without_expiry_inspects_new_token(session):
    auth = MockInstagramAuth(TokenInfo(True, in_days(10)), refreshed=("NEW", None),
                             refreshed_info=TokenInfo(True, in_days(60)))
    result, store, _, _ = run(session, auth)
    assert result.status == "refreshed" and store.get("IG_ACCESS_TOKEN") == "NEW"


def test_alerts_and_keeps_old_token_when_expiry_not_extended(session):
    auth = MockInstagramAuth(TokenInfo(True, in_days(10)), refreshed=("NEW", in_days(10)))
    result, store, notifier, logs = run(session, auth)
    assert result.status == "failed" and "did not extend" in result.reason
    assert store.get("IG_ACCESS_TOKEN") == "OLD"
    assert len(notifier.sent) == 1 and "setup_wizard.py --only 4" in notifier.sent[0]
    assert logs[0].action == "failed"


def test_alerts_when_refresh_call_fails(session):
    auth = MockInstagramAuth(TokenInfo(True, in_days(3)), fail_refresh="HTTP 400: nope")
    result, store, notifier, _ = run(session, auth)
    assert result.status == "failed" and "3 days" in result.reason and "nope" in result.reason
    assert store.get("IG_ACCESS_TOKEN") == "OLD" and len(notifier.sent) == 1


def test_alerts_when_token_invalid(session):
    result, _, notifier, _ = run(session, MockInstagramAuth(TokenInfo(False)))
    assert result.status == "failed" and "invalid" in result.reason and len(notifier.sent) == 1


def test_never_expiring_token_is_left_alone(session):
    auth = MockInstagramAuth(TokenInfo(True, never_expires=True))
    result, _, notifier, _ = run(session, auth)
    assert result.status == "skipped" and auth.refresh_calls == 0 and notifier.sent == []


def test_unknown_expiry_refreshes(session):
    auth = MockInstagramAuth(TokenInfo(True), refreshed=("NEW", in_days(60)))
    result, store, _, _ = run(session, auth)
    assert result.status == "refreshed" and store.get("IG_ACCESS_TOKEN") == "NEW"


def test_no_token_configured(session):
    result, _, notifier, logs = run(session, MockInstagramAuth(TokenInfo(True)), token=None)
    assert result.status == "skipped" and notifier.sent == [] and len(logs) == 1


def test_alert_never_contains_the_token(session):
    auth = MockInstagramAuth(TokenInfo(True, in_days(2)), fail_refresh="HTTP 400: bad")
    _, _, notifier, logs = run(session, auth, token="SUPERSECRETTOKEN")
    assert "SUPERSECRETTOKEN" not in notifier.sent[0] and "SUPERSECRETTOKEN" not in logs[0].reason
