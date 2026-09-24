"""Shared helpers of the services layer (private to :mod:`app.services`).

* application metadata (``app_meta``) access,
* chunked bulk inserts (``session.execute(insert(table), rows)``),
* :class:`RunRecorder` — the ``simulation_run`` audit record of every stochastic step,
* office / race code conventions and small conversions.

Services own transactions: nothing here commits; functions flush when ids are needed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

import numpy as np
from sqlalchemy import Table, insert, select
from sqlalchemy.orm import Session

import app
from app.core.constitution import ElectionStatus
from app.core.logging import get_logger
from app.models import AppMeta, GeoVintage, SimulationRun, utcnow

log = get_logger(__name__)

#: Rows per ``INSERT`` statement of the bulk helpers.
CHUNK_ROWS = 5000

#: ``app_meta`` key describing where the frame of the active geography comes from (JSON).
GEOGRAPHY_SOURCE_KEY = "geography_source"

#: Office codes of the national executive.
PRESIDENT_OFFICE = "PRES"
VICE_PRESIDENT_OFFICE = "VP"

#: Election statuses whose results may be shown (the read layer hides everything else).
REPORTED_STATUSES: frozenset[str] = frozenset({ElectionStatus.FINAL.value, ElectionStatus.CERTIFIED.value})

#: Line colour used for independents (no party colour).
INDEPENDENT_COLOR = "#8A8A8A"


# --------------------------------------------------------------------------- codes
def house_office_code(district_code: str) -> str:
    """``NB-07`` → ``HOUSE-NB-07`` (House offices are keyed by district code)."""
    return f"HOUSE-{district_code}"


def governor_office_code(province_code: str) -> str:
    return f"GOV-{province_code}"


def lt_governor_office_code(province_code: str) -> str:
    return f"LTGOV-{province_code}"


def mayor_office_code(municipality_code: str) -> str:
    return f"MAYOR-{municipality_code}"


# --------------------------------------------------------------------------- app_meta
def get_meta(session: Session, key: str) -> str | None:
    """Value of an ``app_meta`` key (None when unset)."""
    row = session.get(AppMeta, key)
    return None if row is None else row.value


def set_meta(session: Session, key: str, value: str) -> None:
    """Create or update an ``app_meta`` key (flushes)."""
    row = session.get(AppMeta, key)
    if row is None:
        session.add(AppMeta(key=key, value=value))
    else:
        row.value = value
    session.flush()


def active_vintage(session: Session) -> GeoVintage | None:
    """The active geography vintage (most recently built if several are flagged active)."""
    return session.scalars(
        select(GeoVintage)
        .where(GeoVintage.is_active.is_(True))
        .order_by(GeoVintage.built_at.desc(), GeoVintage.id.desc())
        .limit(1)
    ).first()


# --------------------------------------------------------------------------- bulk inserts
def table_of(model: Any) -> Table:
    """The Core ``Table`` of an ORM model (or the table itself)."""
    return model if isinstance(model, Table) else model.__table__


def bulk_insert(
    session: Session, model: Any, rows: Sequence[Mapping[str, Any]], chunk: int = CHUNK_ROWS
) -> int:
    """Insert ``rows`` (dicts with identical keys) in chunks with Core ``INSERT`` statements."""
    table = table_of(model)
    n = len(rows)
    for i in range(0, n, chunk):
        part = rows[i : i + chunk]
        if part:
            session.execute(insert(table), list(part))
    return n


def rows_from_columns(columns: Mapping[str, Iterable[Any]]) -> list[dict[str, Any]]:
    """Column arrays (equal length; NumPy arrays or lists) → list of row dicts with Python scalars."""
    names = list(columns)
    cols = [c.tolist() if isinstance(c, np.ndarray) else list(c) for c in columns.values()]
    if not cols:
        return []
    n = len(cols[0])
    for name, c in zip(names, cols, strict=True):
        if len(c) != n:
            raise ValueError(f"column {name!r} has {len(c)} values, expected {n}")
    return [dict(zip(names, vals, strict=True)) for vals in zip(*cols, strict=True)]


# --------------------------------------------------------------------------- JSON
def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, set | frozenset | tuple):
        return list(obj)
    return str(obj)


def dumps(obj: Any) -> str:
    """Compact, deterministic JSON (NumPy scalars/arrays, dates and sets supported)."""
    return json.dumps(obj, default=_json_default, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def loads(text: str | None) -> Any:
    """Inverse of :func:`dumps` (``None``/empty → ``{}``)."""
    return json.loads(text) if text else {}


# --------------------------------------------------------------------------- run audit
class RunRecorder:
    """Context manager writing one ``simulation_run`` row around a stochastic step.

    The row is inserted (status ``running``) on entry and completed on a clean exit with the
    duration and ``summary`` (a dict filled by the caller).  On an exception the row is marked
    ``failed``; whether that survives depends on the caller's transaction.
    """

    def __init__(
        self,
        session: Session,
        kind: str,
        seed: int,
        *,
        election_id: int | None = None,
        scenario_id: int | None = None,
        config_hash: str | None = None,
        n_simulations: int | None = None,
    ) -> None:
        self.session = session
        self.summary: dict[str, Any] = {}
        self.run = SimulationRun(
            kind=kind,
            election_id=election_id,
            scenario_id=scenario_id,
            seed=int(seed),
            n_simulations=n_simulations,
            config_hash=config_hash,
            status="running",
            code_version=app.__version__,
        )

    def __enter__(self) -> RunRecorder:
        self._t0 = time.perf_counter()
        self.session.add(self.run)
        self.session.flush()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.run.duration_s = round(time.perf_counter() - self._t0, 4)
        self.run.finished_at = utcnow()
        self.run.status = "failed" if exc_type is not None else "completed"
        if exc_type is not None:
            self.summary.setdefault("error", str(exc))
        self.run.summary_json = dumps(self.summary)
        if exc_type is None:
            self.session.flush()


def latest_run(session: Session, election_id: int, kind: str) -> SimulationRun | None:
    """Most recent completed ``simulation_run`` of ``kind`` for an election."""
    return session.scalars(
        select(SimulationRun)
        .where(
            SimulationRun.election_id == election_id,
            SimulationRun.kind == kind,
            SimulationRun.status == "completed",
        )
        .order_by(SimulationRun.id.desc())
        .limit(1)
    ).first()


def seed62(value: int) -> int:
    """Clamp a 64-bit derived seed into the stored (signed BigInteger, < 2**62) range."""
    return int(value) % (2**62)
