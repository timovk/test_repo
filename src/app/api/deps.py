"""Shared API plumbing: the ``orjson`` response class, the per-request database session and the
``{election_id}`` resolver (numbers, ``latest``, ``demo``)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import orjson
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import DataNotPreparedError
from app.services.read._base import ElectionRef, resolve_election

_ORJSON_OPTIONS = orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS | orjson.OPT_NAIVE_UTC


class ApiJSON(JSONResponse):
    """JSON response rendered with ``orjson`` (NumPy scalars, dates; NaN → null)."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content, option=_ORJSON_OPTIONS)


def respond(data: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> ApiJSON:
    """Wrap a read-model payload (skips FastAPI's generic encoder: payloads are JSON-ready)."""
    return ApiJSON(data, status_code=status_code, headers=headers)


class Database:
    """The application's database: URL, session factory and a cached readiness check that never
    creates a missing SQLite file."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._maker: sessionmaker[Session] | None = None
        self._ready = False
        self._checked = -1e9
        self._reason: str | None = None
        self._lock = threading.Lock()

    def ready(self) -> tuple[bool, str | None]:
        if self._ready:
            return True, None
        with self._lock:
            if time.monotonic() - self._checked < 2.0:
                return self._ready, self._reason
            from app.services.read.meta import _database_status

            st = _database_status(self.url)
            self._ready = bool(st["ready"])
            self._reason = st.get("error")
            self._checked = time.monotonic()
            return self._ready, self._reason

    def sessionmaker(self) -> sessionmaker[Session]:
        if self._maker is None:
            from app.db.session import get_sessionmaker

            self._maker = get_sessionmaker(self.url)
        return self._maker


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency: a session on the application's database (503 when not prepared)."""
    db: Database = request.app.state.db
    ok, reason = db.ready()
    if not ok:
        raise DataNotPreparedError(reason or "database not initialised (run `python -m app setup`)")
    session = db.sessionmaker()()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_db)]


def get_election(election_id: str, session: SessionDep) -> ElectionRef:
    """Path dependency: ``{election_id}`` (number, ``latest`` or ``demo``) → :class:`ElectionRef`."""
    return resolve_election(session, election_id)


ElectionDep = Annotated[ElectionRef, Depends(get_election)]


def user_scenarios_dir(request: Request) -> Path:
    return request.app.state.user_scenarios_dir


def db_url(request: Request) -> str:
    return request.app.state.db.url
