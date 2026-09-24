"""Shared pytest fixtures.

* ``synthetic`` — a toy country (real province codes, synthetic geometry) with the same schema as
  the processed CBS store; fast and offline.
* ``db_session`` — a fresh in-memory SQLite session with the full schema.
* ``real_frame`` — the REAL CBS geography frame; tests using it are marked ``realdata`` and are
  skipped automatically when the processed store has not been built.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.geography.synthetic import SyntheticGeography, synthetic_geography
from app.models import Base


@pytest.fixture(scope="session")
def synthetic() -> SyntheticGeography:
    return synthetic_geography(seed=7)


@pytest.fixture()
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _real_store_ready() -> bool:
    try:
        from app.geography.store import is_prepared

        return bool(is_prepared())
    except Exception:
        return False


@pytest.fixture(scope="session")
def real_frame():  # type: ignore[no-untyped-def]
    if not _real_store_ready():
        pytest.skip("processed CBS geography not available (run geography download/build)")
    from app.geography.store import load_frame

    return load_frame()
