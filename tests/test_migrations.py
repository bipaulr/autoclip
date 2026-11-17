from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from autoclip.db import Base, make_engine


def test_migrations_create_the_same_tables_as_the_models(tmp_path):
    url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    engine = make_engine(url)
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        migrated = {c["name"] for c in insp.get_columns(table.name)}
        assert migrated == {c.name for c in table.columns}, table.name
    engine.dispose()

    command.downgrade(cfg, "base")
    engine = make_engine(url)
    assert "decision_log" not in inspect(engine).get_table_names()
    engine.dispose()
