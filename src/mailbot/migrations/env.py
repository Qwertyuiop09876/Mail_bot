"""Alembic environment.

Two entry points share this file:
* ``Database.upgrade()`` passes a ready SQLAlchemy engine via ``config.attributes["engine"]``;
* the ``alembic`` CLI (``make migrate-new``) builds one from ``MAILBOT_DATABASE_URL``.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Engine

from mailbot.config import Settings
from mailbot.db import create_db_engine
from mailbot.models import Base

config = context.config
target_metadata = Base.metadata


def _engine() -> Engine:
    engine = config.attributes.get("engine")
    if engine is not None:
        assert isinstance(engine, Engine)
        return engine
    return create_db_engine(Settings().database_url)


def run_migrations() -> None:
    engine = _engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",  # SQLite can't ALTER freely
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
