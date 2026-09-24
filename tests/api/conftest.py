"""Fixtures of the API tests.

The sandbox world (synthetic country; founding election FINAL, ``demo-2028`` SIMULATED) is built
once per session into a temporary SQLite file.  ``client`` serves a shared read-only copy;
``rw_client`` serves a fresh copy per test for tests that write (actions, scenarios, nights,
forecasts).  ``NLFED_DATA_DIR`` points at a temporary directory while the sandbox is built and
during every (non-realdata) API test, so generated GeoJSON caches never touch ``data/``; it is
restored after each test so later test packages see the real settings.  User scenarios are saved
to a temporary directory.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.settings import reset_settings_cache
from app.models import Base
from app.services.read._base import clear_read_cache
from app.services.sandbox import Sandbox, build_sandbox


@dataclass
class ApiWorld:
    path: Path
    sandbox: Sandbox
    data_dir: Path

    @property
    def final_id(self) -> int:
        return self.sandbox.founding_election_id

    @property
    def hidden_id(self) -> int:
        assert self.sandbox.second_election_id is not None
        return self.sandbox.second_election_id


@contextmanager
def _data_dir(data_dir: Path) -> Iterator[None]:
    """Point ``NLFED_DATA_DIR`` at ``data_dir`` (settings cache reset on entry and exit)."""
    old = os.environ.get("NLFED_DATA_DIR")
    os.environ["NLFED_DATA_DIR"] = str(data_dir)
    reset_settings_cache()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("NLFED_DATA_DIR", None)
        else:
            os.environ["NLFED_DATA_DIR"] = old
        reset_settings_cache()


@pytest.fixture(scope="session")
def api_world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ApiWorld]:
    base = tmp_path_factory.mktemp("api")
    data_dir = base / "data"
    data_dir.mkdir()
    path = base / "sandbox.db"
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with _data_dir(data_dir):
        session = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
        world = build_sandbox(session, seed=1)
        session.commit()
        session.close()
    engine.dispose()
    try:
        yield ApiWorld(path=path, sandbox=world, data_dir=data_dir)
    finally:
        clear_read_cache()


@pytest.fixture(autouse=True)
def _api_data_dir(request: pytest.FixtureRequest) -> Iterator[None]:
    """Every synthetic API test runs with ``NLFED_DATA_DIR`` at the sandbox's data directory;
    real-data tests keep the real settings."""
    if request.node.get_closest_marker("realdata") is not None:
        yield
        return
    world: ApiWorld = request.getfixturevalue("api_world")
    with _data_dir(world.data_dir):
        yield


def _client(db_path: Path, scen_dir: Path) -> TestClient:
    from app.api.main import create_app

    app = create_app(f"sqlite:///{db_path}", user_scenarios_dir=scen_dir, configure_logs=False)
    return TestClient(app)


@pytest.fixture(scope="session")
def client(api_world: ApiWorld, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """Client on a shared copy of the sandbox (tests using it must not write)."""
    d = tmp_path_factory.mktemp("ro")
    path = d / "ro.db"
    shutil.copyfile(api_world.path, path)
    with _client(path, d / "user_scenarios") as c:
        yield c


@pytest.fixture()
def rw_client(api_world: ApiWorld, tmp_path: Path) -> Iterator[TestClient]:
    """Client on a fresh copy of the sandbox (free to write)."""
    path = tmp_path / "rw.db"
    shutil.copyfile(api_world.path, path)
    clear_read_cache()
    with _client(path, tmp_path / "user_scenarios") as c:
        c.db_path = path  # type: ignore[attr-defined]
        c.scenario_dir = tmp_path / "user_scenarios"  # type: ignore[attr-defined]
        yield c
    clear_read_cache()
