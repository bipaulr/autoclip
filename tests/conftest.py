import pytest
from sqlalchemy.orm import sessionmaker

import autoclip.models  # noqa: F401
from autoclip.db import Base, make_engine


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s
