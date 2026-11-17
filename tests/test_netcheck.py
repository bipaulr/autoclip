from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from autoclip.integrations.telegram import MockNotifier
from autoclip.models import DecisionLog
from autoclip.services.netcheck import check, measure

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def test_measure_reports_bytes_per_second():
    ticks = iter([0.0, 0.5, 1.0, 2.0])
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 2_000_000)))
    speed = measure(client, clock=lambda: next(ticks))
    assert 900_000 < speed < 2_100_000


def test_measure_failure_is_zero_not_an_exception():
    def boom(request):
        raise httpx.ConnectError("down")
    assert measure(httpx.Client(transport=httpx.MockTransport(boom))) == 0.0
    assert measure(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))) == 0.0


def test_two_slow_readings_alert_once_then_recovery_is_reported(session):
    note = MockNotifier()
    assert check(session, note, NOW, 80_000) == "80 KB/s"
    assert note.sent == []  # one slow reading: no alert yet
    check(session, note, NOW, 75_000)
    assert len(note.sent) == 1 and "docker desktop restart" in note.sent[0]
    check(session, note, NOW, 70_000)
    assert len(note.sent) == 1  # no repeat while it stays slow
    check(session, note, NOW, 9_500_000)
    assert len(note.sent) == 2 and "back to normal" in note.sent[1]
    actions = [d.action for d in session.scalars(select(DecisionLog))]
    assert actions.count("slow") == 3 and "recovered" in actions


def test_fast_readings_stay_quiet_and_a_single_blip_resets(session):
    note = MockNotifier()
    check(session, note, NOW, 9_000_000)
    check(session, note, NOW, 50_000)
    check(session, note, NOW, 9_000_000)
    check(session, note, NOW, 50_000)
    assert note.sent == []  # never two slow readings in a row


def test_task_wires_measurement_and_alert(session_factory):
    from autoclip.config import Settings
    from autoclip.tasks import run_net_check
    note = MockNotifier()
    s = Settings(_env_file=None)
    assert run_net_check(s, session_factory=session_factory, notifier=note, speed=60_000, now=NOW) == \
        "download speed inside Docker: 60 KB/s"
    run_net_check(s, session_factory=session_factory, notifier=note, speed=60_000, now=NOW)
    assert len(note.sent) == 1
