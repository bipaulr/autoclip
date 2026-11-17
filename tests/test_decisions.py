import pytest
from sqlalchemy import select

from autoclip.decisions import log_decision
from autoclip.models import DecisionLog


def test_log_decision_persists_with_reason(session):
    log_decision(session, "picker", "skipped", "score below threshold", subject="clip-1", data={"score": 0.2})
    session.commit()
    row = session.scalars(select(DecisionLog)).one()
    assert (row.module, row.action, row.subject, row.reason) == (
        "picker", "skipped", "clip-1", "score below threshold")
    assert row.data == {"score": 0.2}
    assert row.created_at is not None


def test_reason_is_required(session):
    with pytest.raises(ValueError):
        log_decision(session, "picker", "skipped", "")
