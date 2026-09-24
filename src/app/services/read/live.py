"""Bridge to the election-night service (:mod:`app.services.night`), imported lazily.

The night service replays a SIMULATED election's stored timeline and is — while a night is
live — the **only** source of results for that election (hidden-until-reported rule).  Its
interface::

    get_night_manager() -> NightManager
    NightManager(session_factory=None, ...)
    NightManager.state(election_id, detail="summary", now=None) -> dict  # {"election_id", "clock", "snapshot", "data_category", ...}
    NightManager.control(election_id, action, speed=None, now=None) -> dict
    NightManager.race_detail(election_id, race_code) -> dict
    NightManager.municipality_rows(election_id, race_code="PRES") -> list[dict]
    NightManager.municipality_rows_many(election_id, race_codes) -> dict[str, list[dict]]
    NightManager.municipality_detail(election_id, code) -> dict
    NightManager.override_call(election_id, race_code, status, line_key, reason) -> dict
    NightManager.is_live(election_id) -> bool

A night manager runs every call in its own transaction, so it must use **the same database** as
the request: :func:`manager_for` returns one manager per database URL (``session_factory``
bound to the URL).  The API's managers run the night service's background driver
(``background=True``): due reporting events are applied by a daemon thread while a night runs,
and read requests spend at most :data:`READ_BUDGET_S` on engine work, so the live state answers
quickly at every playback speed.  The module is imported on first use, so this package works
before the night service exists; when it is missing :func:`manager_for` raises
:class:`ServiceUnavailableError`.
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import NLFedError
from app.core.logging import get_logger
from app.core.settings import get_settings

log = get_logger(__name__)

NIGHT_MODULE = "app.services.night"
#: Seconds between import retries while the night service is unavailable.
_RETRY_S = 30.0
#: Engine work (seconds) one API read request may do; the background driver does the rest.
READ_BUDGET_S = 0.03

_lock = threading.Lock()
_module: Any = None
_last_attempt = -1e9
_bound: dict[str, Any] = {}


class ServiceUnavailableError(NLFedError):
    """An optional service (e.g. the election-night service) is not available (HTTP 503)."""


def _load() -> Any:
    global _module, _last_attempt
    with _lock:
        if _module is not None:
            return _module
        now = time.monotonic()
        if now - _last_attempt < _RETRY_S:
            return None
        _last_attempt = now
        try:
            mod = importlib.import_module(NIGHT_MODULE)
        except ImportError as exc:
            log.debug("night service unavailable: %s", exc)
            return None
        if not hasattr(mod, "get_night_manager"):
            log.warning("%s has no get_night_manager()", NIGHT_MODULE)
            return None
        _module = mod
        return _module


def reset_night_bridge() -> None:
    """Forget the cached import and the URL-bound managers, stopping their background drivers
    (tests, simulated restarts)."""
    global _module, _last_attempt
    with _lock:
        _module = None
        _last_attempt = -1e9
        managers = list(_bound.values())
        _bound.clear()
    for m in managers:
        close = getattr(m, "close", None)
        if callable(close):
            close()


def night_available() -> bool:
    """True when the night service can be imported."""
    return _load() is not None


def session_url(session: Session) -> str:
    """Full URL (password included) of the database behind ``session``."""
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    return "" if url is None else url.render_as_string(hide_password=False)


def _scope_for(url: str) -> Callable[[], AbstractContextManager[Session]]:
    from app.db.session import session_scope

    def factory() -> AbstractContextManager[Session]:
        return session_scope(url)

    return factory


def manager_for_url(url: str | None) -> Any:
    """The night manager of the database at ``url`` (``None``: the configured database), with
    the background driver running; raises :class:`ServiceUnavailableError`."""
    mod = _load()
    if mod is None:
        raise ServiceUnavailableError("the election-night service is not available")
    key = url or get_settings().db_url
    with _lock:
        m = _bound.get(key)
        if m is None:
            m = mod.NightManager(
                session_factory=_scope_for(key), background=True, read_budget_s=READ_BUDGET_S
            )
            _bound[key] = m
        return m


def close_manager_for_url(url: str | None) -> None:
    """Stop and forget the night manager of the database at ``url`` (application shutdown); the
    nights stay in the database and are rebuilt by the next manager."""
    key = url or get_settings().db_url
    with _lock:
        m = _bound.pop(key, None)
    close = getattr(m, "close", None)
    if callable(close):
        close()


def manager_for(session: Session) -> Any:
    """The night manager bound to ``session``'s database."""
    return manager_for_url(session_url(session))


def live_state(session: Session, election_id: int, detail: str = "summary") -> dict[str, Any]:
    """The night state ``{"election_id", "clock", "snapshot", "data_category", ...}``."""
    return manager_for(session).state(int(election_id), detail=detail)


def live_snapshot(session: Session, election_id: int, detail: str = "summary") -> dict[str, Any]:
    """Only the engine snapshot of :func:`live_state` (schema in :mod:`app.reporting.live`)."""
    state = live_state(session, election_id, detail)
    snap = state.get("snapshot") if isinstance(state, dict) else None
    return snap if isinstance(snap, dict) else {}


def live_races(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Compact live races of a snapshot by race code."""
    return {str(r.get("key")): r for r in snapshot.get("races") or [] if r.get("key") is not None}


def live_race_detail(session: Session, election_id: int, race_code: str) -> dict[str, Any]:
    return manager_for(session).race_detail(int(election_id), str(race_code))


def live_municipality_rows(
    session: Session, election_id: int, race_code: str = "PRES"
) -> list[dict[str, Any]]:
    return list(manager_for(session).municipality_rows(int(election_id), str(race_code)))


def live_municipality_rows_many(
    session: Session, election_id: int, race_codes: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Live map rows of several races at the same night position (one call)."""
    rows = manager_for(session).municipality_rows_many(int(election_id), [str(c) for c in race_codes])
    return {str(k): list(v) for k, v in rows.items()}


def live_municipality_detail(session: Session, election_id: int, code: str) -> dict[str, Any]:
    return manager_for(session).municipality_detail(int(election_id), str(code))
