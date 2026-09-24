"""Fixtures of the CLI tests.

The synthetic sandbox world (founding election FINAL, ``demo-2028`` SIMULATED) is built once per
session into a temporary SQLite file; every test runs the CLI (``typer.testing.CliRunner``)
against its own copy through ``NLFED_DATABASE_URL``.  Logging handlers, settings, engines and
the night manager are restored afterwards.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from app.core.settings import reset_settings_cache
from app.db.session import reset_engines
from app.models import Base
from app.services.night import reset_night_manager
from app.services.sandbox import build_sandbox


@dataclass(frozen=True)
class SandboxDB:
    path: Path
    founding: int
    second: int


@dataclass(frozen=True)
class CliDB:
    path: Path
    url: str
    founding: int
    second: int


@pytest.fixture(scope="session")
def cli_sandbox(tmp_path_factory: pytest.TempPathFactory) -> SandboxDB:
    path = tmp_path_factory.mktemp("cli") / "sandbox.db"
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
    world = build_sandbox(session, seed=1)
    session.commit()
    session.close()
    engine.dispose()
    assert world.second_election_id is not None
    return SandboxDB(path, world.founding_election_id, world.second_election_id)


def _reset_runtime() -> None:
    reset_settings_cache()
    reset_engines()
    reset_night_manager()


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture()
def cli_db(cli_sandbox: SandboxDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CliDB]:
    path = tmp_path / "cli.db"
    shutil.copyfile(cli_sandbox.path, path)
    url = f"sqlite:///{path}"
    monkeypatch.setenv("NLFED_DATABASE_URL", url)
    monkeypatch.setenv("COLUMNS", "200")
    _reset_runtime()
    yield CliDB(path, url, cli_sandbox.founding, cli_sandbox.second)
    monkeypatch.delenv("NLFED_DATABASE_URL", raising=False)
    _reset_runtime()


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()
