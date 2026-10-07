"""Engine/session management and schema migrations."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def _alembic_config(engine: Engine) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    # env.py reuses this engine instead of building one from a URL, so in-memory DBs work.
    cfg.attributes["engine"] = engine
    return cfg


def create_db_engine(url: str) -> Engine:
    parsed = make_url(url)
    kwargs: dict[str, Any] = {}
    if parsed.get_backend_name() == "sqlite":
        in_memory = parsed.database in (None, "", ":memory:")
        if in_memory:
            # One shared connection, otherwise every checkout would see a fresh empty database.
            kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
        else:
            assert parsed.database is not None
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
            kwargs.update(connect_args={"timeout": 30})
    engine = create_engine(url, **kwargs)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if not in_memory:
                cur.execute("PRAGMA journal_mode=WAL")  # worker writes while others read
                cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_db_engine(url)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commit on success, roll back on any exception."""
        session = self._factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def upgrade(self, revision: str = "head") -> None:
        """Bring the schema to ``revision`` using the bundled Alembic migrations."""
        command.upgrade(_alembic_config(self.engine), revision)

    def dispose(self) -> None:
        self.engine.dispose()
