"""Declarative base, naming conventions and shared column helpers.

The schema is written against SQLAlchemy 2.0 typed declarative mappings and uses only
portable column types so the same models run on SQLite (default) and PostgreSQL.
Geometries are stored as WKB (``LargeBinary``, EPSG:4326, simplified for display);
full-resolution geometry lives in the processed GeoParquet store (see docs/DATABASE.md).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, MetaData, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict: JSON,
        list: JSON,
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        ident = getattr(self, "id", None)
        code = getattr(self, "code", None) or getattr(self, "cbs_code", None)
        return f"<{type(self).__name__} id={ident}{' ' + str(code) if code else ''}>"


def pk() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True, autoincrement=True)


def created_col() -> Mapped[datetime]:
    return mapped_column(DateTime, default=utcnow, nullable=False)


def seed_col(nullable: bool = False) -> Mapped[int]:
    """Seeds are 64-bit unsigned in NumPy; stored as signed BigInteger (seeds are kept < 2**63)."""
    return mapped_column(BigInteger, nullable=nullable)


def text_col(nullable: bool = True) -> Mapped[str | None]:
    return mapped_column(Text, nullable=nullable)
