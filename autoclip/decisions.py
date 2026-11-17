from sqlalchemy.orm import Session

from autoclip.models import DecisionLog


def log_decision(session: Session, module: str, action: str, reason: str,
                 subject: str | None = None, data: dict | None = None) -> DecisionLog:
    """Record a decision with its reason. The caller commits."""
    if not reason:
        raise ValueError("a decision must have a reason")
    row = DecisionLog(module=module, action=action, reason=reason, subject=subject, data=data)
    session.add(row)
    session.flush()
    return row
