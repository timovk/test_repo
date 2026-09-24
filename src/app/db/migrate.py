"""Database migrations (Alembic, configured programmatically — no alembic.ini needed).

``upgrade_db()`` brings any database (SQLite or PostgreSQL) to the latest schema revision.
``python -m app db revision -m "..."`` autogenerates a new revision after model changes.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.core.logging import get_logger
from app.core.settings import get_settings
from app.db.session import get_engine

log = get_logger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url or get_settings().db_url)
    cfg.set_main_option("version_path_separator", "os")
    return cfg


def upgrade_db(url: str | None = None, revision: str = "head") -> None:
    """Apply migrations up to ``revision`` (default: latest)."""
    engine = get_engine(url)
    cfg = alembic_config(url)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)
    log.info("database schema at %s", revision)


def current_revision(url: str | None = None) -> str | None:
    engine = get_engine(url)
    with engine.connect() as conn:
        if not inspect(conn).has_table("alembic_version"):
            return None
        row = conn.exec_driver_sql("SELECT version_num FROM alembic_version").first()
        return row[0] if row else None


def make_revision(message: str, url: str | None = None, autogenerate: bool = True) -> None:
    cfg = alembic_config(url)
    command.revision(cfg, message=message, autogenerate=autogenerate)


def downgrade_db(revision: str, url: str | None = None) -> None:
    engine = get_engine(url)
    cfg = alembic_config(url)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)
