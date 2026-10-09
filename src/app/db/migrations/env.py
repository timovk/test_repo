"""Alembic environment (programmatic; see app.db.migrate)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.settings import get_settings
from app.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().db_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        section = config.get_section(config.config_ini_section) or {}
        section["sqlalchemy.url"] = _url()
        engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
        with engine.connect() as connection:
            _run(connection)
        engine.dispose()
    else:
        _run(connectable)


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    sqlite = connection.dialect.name == "sqlite"
    if sqlite:
        # Batch migrations rebuild tables (copy, drop, rename); with foreign keys enforced, dropping
        # a table that other rows reference fails.  The PRAGMA only works outside a transaction,
        # so it is issued before the migration transaction starts and restored afterwards.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=sqlite,
        compare_type=True,
    )
    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
