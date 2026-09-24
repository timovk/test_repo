"""Fixtures of the services tests.

The sandbox world (synthetic country, founding election FINAL, ``demo-2028`` SIMULATED) is built
once per session into a temporary SQLite file; tests that change it work on a copy of that file
(:func:`world`), so every test starts from the same state.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.services.sandbox import Sandbox, build_sandbox


def sqlite_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


@dataclass
class World:
    """A copy of the sandbox world."""

    session: Session
    sandbox: Sandbox
    path: Path

    @property
    def founding(self) -> int:
        return self.sandbox.founding_election_id

    @property
    def second(self) -> int:
        assert self.sandbox.second_election_id is not None
        return self.sandbox.second_election_id


@dataclass
class SandboxFile:
    path: Path
    sandbox: Sandbox
    seconds: float  # wall clock
    cpu_seconds: float  # CPU time of this process (robust on a shared, busy machine)


@pytest.fixture(scope="session")
def sandbox_file(tmp_path_factory: pytest.TempPathFactory) -> SandboxFile:
    path = tmp_path_factory.mktemp("services") / "sandbox.db"
    engine = sqlite_engine(path)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
    t0, c0 = time.perf_counter(), time.process_time()
    world = build_sandbox(session, seed=1)
    session.commit()
    seconds, cpu = time.perf_counter() - t0, time.process_time() - c0
    session.close()
    engine.dispose()
    return SandboxFile(path, world, seconds, cpu)


@pytest.fixture()
def world(sandbox_file: SandboxFile, tmp_path: Path) -> Iterator[World]:
    path = tmp_path / "world.db"
    shutil.copyfile(sandbox_file.path, path)
    engine = sqlite_engine(path)
    session = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
    try:
        yield World(session, sandbox_file.sandbox, path)
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def fresh_session(tmp_path: Path) -> Iterator[Session]:
    """An empty database file with the full schema."""
    engine = sqlite_engine(tmp_path / "fresh.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
