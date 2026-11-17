from datetime import datetime, timezone

from sqlalchemy.orm import Session

from autoclip.models import Setting


def get_setting(session: Session, key: str, default=None):
    row = session.get(Setting, key)
    return row.value.get("v", default) if row else default


def set_setting(session: Session, key: str, value) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value={"v": value}))
    else:
        row.value, row.updated_at = {"v": value}, datetime.now(timezone.utc)
    session.flush()
