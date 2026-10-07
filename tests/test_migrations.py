from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from mailbot.db import Database
from mailbot.models import Base


def test_migrations_build_exactly_the_schema_the_models_describe(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = Database(f"sqlite:///{tmp_path}/m.db")
    db.upgrade()
    db.upgrade()  # idempotent
    with db.engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    assert diff == [], f"models and migrations drifted: {diff}; run `make migrate-new`"
    assert {"accounts", "contacts", "campaigns", "deliveries", "alembic_version"} <= set(
        inspect(db.engine).get_table_names()
    )
    db.dispose()


def test_sqlite_enforces_foreign_keys(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import pytest
    from sqlalchemy.exc import IntegrityError

    from mailbot.models import Delivery

    db = Database(f"sqlite:///{tmp_path}/fk.db")
    db.upgrade()
    with pytest.raises(IntegrityError), db.session() as s:
        s.add(Delivery(campaign_id=999, contact_id=999, email="x@y.ru"))
    db.dispose()
