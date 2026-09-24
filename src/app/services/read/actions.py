"""Write actions exposed by the API, each one a committed unit of work: create / simulate /
finalize an election, persist UI settings and add a manual (FICTIONAL) poll.

The election mathematics stays in :mod:`app.services.elections` (and the engines); these
functions validate the request, call the service, commit and clear the read cache."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constitution import ElectionStatus
from app.core.errors import ElectionError, ValidationError
from app.core.logging import get_logger, log_ctx
from app.models import Party, Race
from app.polling.types import NATIONAL_GEO, POLL_TYPES
from app.services import elections as election_service
from app.services.read._base import ElectionRef, clear_read_cache, ref_of, resolve_election
from app.services.read.meta import settings_payload, store_ui_settings

log = get_logger(__name__)


def _commit(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        clear_read_cache()


def create_election(
    session: Session,
    scenario: str,
    *,
    seed: int | None = None,
    year: int | None = None,
    strict: bool | None = None,
    simulate: bool = False,
    user_dir: Path | None = None,
) -> dict[str, Any]:
    """``POST /api/elections`` — create a SCHEDULED election from a built-in or user scenario
    (optionally simulate it at once).  ``strict`` (default: True on the REAL geography, False on
    the synthetic test country) rejects scenario references to places missing from the geography."""
    from app.services.read.elections import election_detail
    from app.services.read.scenarios import load_for_election
    from app.services.runtime import geography_source

    doc = load_for_election(session, scenario, user_dir)
    if strict is None:
        strict = geography_source(session).kind == "store"
    try:
        el = election_service.create_election(session, doc, seed=seed, year=year, strict=strict)
        if simulate:
            election_service.simulate_election(session, el.id)
        _commit(session)
    except Exception:
        session.rollback()
        clear_read_cache()
        raise
    log.info("election created via API", extra=log_ctx(election_id=el.id, scenario=scenario))
    return election_detail(session, ref_of(el))


def simulate_election(session: Session, ref: ElectionRef, *, seed: int | None = None) -> dict[str, Any]:
    """``POST /api/elections/{id}/simulate`` — simulate (or re-simulate) the hidden result and
    the election-night timeline.  LIVE and reported elections are refused (409)."""
    from app.services.read.elections import election_detail

    try:
        election_service.simulate_election(session, ref.id, seed=seed)
        _commit(session)
    except Exception:
        session.rollback()
        clear_read_cache()
        raise
    return election_detail(session, resolve_election(session, ref.id))


def finalize_election(session: Session, ref: ElectionRef) -> dict[str, Any]:
    """``POST /api/elections/{id}/finalize`` — instant finish without an election night
    (simulates first when needed).  A LIVE election's night must be finished instead (409)."""
    from app.services.read.elections import election_detail

    if ref.status == ElectionStatus.LIVE.value:
        raise ElectionError(
            f"election {ref.id} is live: finish its election night (POST /api/night/{ref.id}/control "
            '{"action": "finish"})'
        )
    try:
        election_service.instant_finalize(session, ref.id)
        _commit(session)
    except Exception:
        session.rollback()
        clear_read_cache()
        raise
    return election_detail(session, resolve_election(session, ref.id))


def update_settings(session: Session, patch: Mapping[str, Any]) -> dict[str, Any]:
    """``PUT /api/settings`` — merge, validate and persist UI settings."""
    try:
        store_ui_settings(session, dict(patch))
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return settings_payload(session)


def add_poll(session: Session, ref: ElectionRef, poll: Mapping[str, Any]) -> dict[str, Any]:
    """``POST /api/polls/{id}`` — add a manual FICTIONAL poll to an unreported election.

    ``poll``: ``pollster`` (an invented name; new pollsters get rating 1.0), ``poll_type``,
    ``geo_code`` (``NL`` / province / district / seat), ``start_date``, ``end_date`` (≤ election
    day), ``sample_size``, ``results`` (key → percent; party codes link to parties), optional
    ``population``, ``method``, ``undecided_pct``, ``margin_of_error``, ``notes``."""
    from app.polling.service import store_polls

    if ref.reported:
        raise ElectionError(f"election {ref.id} is {ref.status}: polls can no longer be added")
    ptype = str(poll.get("poll_type") or "")
    if ptype not in POLL_TYPES:
        raise ValidationError(f"poll_type must be one of {list(POLL_TYPES)}")
    start = poll.get("start_date")
    end = poll.get("end_date")
    start_d = start if isinstance(start, date) else date.fromisoformat(str(start))
    end_d = end if isinstance(end, date) else date.fromisoformat(str(end))
    if start_d > end_d:
        raise ValidationError("start_date must not be after end_date")
    if end_d > ref.election_date:
        raise ValidationError(f"end_date must be on or before election day {ref.election_date.isoformat()}")
    results = {str(k): float(v) for k, v in dict(poll.get("results") or {}).items()}
    if not results:
        raise ValidationError("results must list at least one option")
    if any(v < 0 or v > 100 for v in results.values()) or sum(results.values()) > 100.5:
        raise ValidationError("results are percentages (0–100) that sum to at most 100")
    party_ids = dict(session.execute(select(Party.code, Party.id)).tuples().all())
    race_ids = dict(
        session.execute(select(Race.code, Race.id).where(Race.election_id == ref.id)).tuples().all()
    )
    obj = SimpleNamespace(
        pollster=str(poll.get("pollster") or "").strip(),
        poll_type=ptype,
        geo_code=str(poll.get("geo_code") or NATIONAL_GEO).upper(),
        start_date=start_d,
        end_date=end_d,
        sample_size=int(poll.get("sample_size") or 0),
        population=str(poll.get("population") or "LV"),
        method=str(poll.get("method") or "online"),
        undecided_pct=poll.get("undecided_pct"),
        margin_of_error=poll.get("margin_of_error"),
        results=results,
        notes=poll.get("notes") or "manual poll (FICTIONAL, entered via the API)",
        source="Manual FICTIONAL poll entered in the NL Federal Election Simulator (SIMULATED data)",
    )
    if not obj.pollster:
        raise ValidationError("pollster is required")
    if obj.sample_size < 1:
        raise ValidationError("sample_size must be positive")
    try:
        rows = store_polls(session, ref.id, [obj], party_ids, race_ids)
        _commit(session)
    except ValueError as exc:
        session.rollback()
        raise ValidationError(str(exc)) from exc
    row = rows[0]
    return {
        "data_category": "SIMULATED",
        "id": row.id,
        "election_id": ref.id,
        "pollster": obj.pollster,
        "poll_type": ptype,
        "geo_code": obj.geo_code,
        "start_date": start_d.isoformat(),
        "end_date": end_d.isoformat(),
        "sample_size": obj.sample_size,
        "results": results,
    }
