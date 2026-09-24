"""Polling persistence (SQLAlchemy): pollsters, house-effect priors, polls and poll results.

Translates between ORM rows (:mod:`app.models.polling`) and the engine frames of
:mod:`app.polling.aggregate`.  All stored polls are FICTIONAL / SIMULATED
(``is_fictional = True``).  Functions flush but never commit — the caller owns the transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Party, Poll, PollResult, Pollster, PollsterHouseEffect, Province
from app.polling.config import PollsterConfig, load_polling_config, looks_like_real_pollster
from app.polling.types import (
    FICTIONAL_POLL_SOURCE,
    NATIONAL_GEO,
    POLL_COLUMNS,
    POLL_TYPES,
    POPULATIONS,
    RESULT_COLUMNS,
    PollsterLike,
    is_province_code,
    pollster_attr,
    province_of_geo,
    race_code_for,
)

log = get_logger(__name__)

#: Extra columns returned by :func:`polls_frames` besides :data:`POLL_COLUMNS`.
EXTRA_POLL_COLUMNS: tuple[str, ...] = ("margin_of_error", "pollster_rating", "race_id", "is_fictional")

# Column sizes of app.models.polling (PostgreSQL enforces them; SQLite does not).
_DISTRICT_CODE_LEN = 8
_METHOD_LEN = 24
_RATING_LABEL_LEN = 8
_LABEL_LEN = 120
_NAME_LEN = 120
#: Pollster ratings accepted by :class:`PollsterConfig` (0 = excluded from averages).
_RATING_RANGE = (0.0, 5.0)


def upsert_pollsters(
    session: Session, specs: Sequence[PollsterLike], party_code_to_id: Mapping[str, int]
) -> dict[str, Pollster]:
    """Insert or update pollsters (by name) and replace their prior house effects.

    House effects for party codes missing from ``party_code_to_id`` are skipped.  Returns
    ``{name: Pollster}``.
    """
    names = [p.name for p in specs]
    if len(set(names)) != len(names):
        raise ValueError("duplicate pollster names")
    for n in names:
        if looks_like_real_pollster(n):
            raise ValueError(f"pollster name {n!r} resembles a real polling organisation")
        if len(n) > _NAME_LEN:
            raise ValueError(f"pollster name {n!r} is longer than {_NAME_LEN} characters")
    existing = (
        {p.name: p for p in session.scalars(select(Pollster).where(Pollster.name.in_(names)))}
        if names
        else {}
    )
    out: dict[str, Pollster] = {}
    for spec in specs:
        row = existing.get(spec.name)
        if row is None:
            row = Pollster(name=spec.name)
            session.add(row)
        row.rating = float(spec.rating)
        label = pollster_attr(spec, "rating_label", None)
        row.rating_label = None if label is None else _fit(str(label), _RATING_LABEL_LEN)
        row.default_method = _fit(str(spec.method), _METHOD_LEN)
        row.is_fictional = True
        wanted = {
            int(party_code_to_id[code]): float(v)
            for code, v in dict(spec.house_effects).items()
            if code in party_code_to_id
        }
        skipped = sorted(set(dict(spec.house_effects)) - set(party_code_to_id))
        if skipped:
            log.debug("pollster %s: no party id for house effects %s", spec.name, skipped)
        current = {he.party_id: he for he in row.house_effects}
        for pid, he in current.items():
            if pid not in wanted:
                row.house_effects.remove(he)
        for pid, value in wanted.items():
            if pid in current:
                current[pid].effect_pct = value
            else:
                row.house_effects.append(PollsterHouseEffect(party_id=pid, effect_pct=value))
        out[spec.name] = row
    session.flush()
    return out


def store_polls(
    session: Session,
    election_id: int | None,
    polls: Iterable[Any],
    party_code_to_id: Mapping[str, int],
    race_code_to_id: Mapping[str, int] | None = None,
    province_code_to_id: Mapping[str, int] | None = None,
    *,
    pollsters: Sequence[PollsterLike] | None = None,
) -> list[Poll]:
    """Persist polls (e.g. :class:`~app.polling.generate.GeneratedPoll`) with their results.

    Each poll object needs the attributes ``pollster, poll_type, geo_code, start_date, end_date,
    sample_size, population, method, undecided_pct, results`` (``{key: pct}``) and may carry
    ``margin_of_error``, ``quality_rating``, ``race_code``, ``source``, ``notes``.

    Pollsters missing from the database are created through :func:`upsert_pollsters` from the
    matching entry of ``pollsters`` (e.g. the scenario's pollsters), else of ``config/polling.yaml``,
    else with the default rating 1.0 — so the stored rating (which the aggregate prefers over the
    configured one) is never silently reset.  Result keys that are party codes get ``party_id``;
    every result keeps its key as ``label``.

    Geography: national polls have no province.  ``province_id`` comes from
    ``province_code_to_id``, else from the ``province`` table.  Sub-province ``geo_code``s (House
    district ``NB-07``, Senate seat ``NB-1``) are stored in ``district_code``; a province-level
    ``geo_code`` whose province row does not exist is stored there too, so :func:`polls_frames`
    always returns the original ``geo_code``.
    """
    polls = list(polls)
    if not polls:
        return []
    # validate everything before touching the session
    checked = [_checked_poll(p) for p in polls]
    race_map = dict(race_code_to_id or {})
    prov_map = _province_ids(session, polls, province_code_to_id)
    _ensure_pollsters(session, polls, party_code_to_id, pollsters)
    names = sorted({str(p.pollster) for p in polls})
    pollster_rows = {p.name: p for p in session.scalars(select(Pollster).where(Pollster.name.in_(names)))}

    rows: list[Poll] = []
    for p, (poll_type, geo, population, results) in zip(polls, checked, strict=True):
        province_code = province_of_geo(geo)
        province_id = prov_map.get(province_code) if province_code else None
        if geo == NATIONAL_GEO or (is_province_code(geo) and province_id is not None):
            district_code = None
        else:
            district_code = geo
        race_code = getattr(p, "race_code", None) or race_code_for(poll_type, geo)
        race_id = race_map.get(race_code) if race_code else None
        if race_id is None and poll_type == "senate" and province_code:
            candidates = sorted(c for c in race_map if c.startswith(f"SEN-{province_code}-"))
            if len(candidates) == 1:
                race_id = race_map[candidates[0]]
        row = Poll(
            election_id=election_id,
            race_id=race_id,
            pollster_id=pollster_rows[str(p.pollster)].id,
            poll_type=poll_type,
            province_id=province_id,
            district_code=district_code,
            start_date=_as_date(p.start_date),
            end_date=_as_date(p.end_date),
            sample_size=int(p.sample_size),
            population=population,
            method=_fit(str(getattr(p, "method", "online") or "online"), _METHOD_LEN),
            margin_of_error=_opt_float(getattr(p, "margin_of_error", None)),
            undecided_pct=_opt_float(getattr(p, "undecided_pct", None)),
            source=str(getattr(p, "source", None) or FICTIONAL_POLL_SOURCE),
            quality_rating=_opt_float(getattr(p, "quality_rating", None)),
            is_fictional=True,
            notes=getattr(p, "notes", None) or "data_category=SIMULATED",
        )
        row.results = [
            PollResult(party_id=party_code_to_id.get(key), label=key, value_pct=value)
            for key, value in results.items()
        ]
        rows.append(row)
    session.add_all(rows)
    session.flush()
    log.info("stored %d fictional polls", len(rows), extra={"ctx": {"election_id": election_id}})
    return rows


def polls_frames(
    session: Session, election_id: int | None, poll_type: str | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load an election's polls as ``(polls_df, results_df)`` in the aggregation schema.

    ``geo_code`` is the stored ``district_code`` when present, else the province code, else
    ``"NL"``; result ``key`` is the party code when the result is linked to a party, else its label.
    Extra poll columns: ``margin_of_error, pollster_rating, race_id, is_fictional``.
    """
    stmt = (
        select(
            Poll.id,
            Pollster.name,
            Pollster.rating,
            Poll.poll_type,
            Poll.district_code,
            Province.code,
            Poll.start_date,
            Poll.end_date,
            Poll.sample_size,
            Poll.population,
            Poll.method,
            Poll.undecided_pct,
            Poll.quality_rating,
            Poll.margin_of_error,
            Poll.race_id,
            Poll.is_fictional,
        )
        .join(Pollster, Poll.pollster_id == Pollster.id)
        .outerjoin(Province, Poll.province_id == Province.id)
        .order_by(Poll.end_date, Poll.id)
    )
    stmt = (
        stmt.where(Poll.election_id.is_(None))
        if election_id is None
        else stmt.where(Poll.election_id == election_id)
    )
    if poll_type is not None:
        stmt = stmt.where(Poll.poll_type == poll_type)
    poll_rows = []
    for r in session.execute(stmt):
        geo = r[4] or r[5] or NATIONAL_GEO
        poll_rows.append(
            {
                "poll_id": r[0],
                "pollster": r[1],
                "poll_type": r[3],
                "geo_code": geo,
                "start_date": r[6],
                "end_date": r[7],
                "sample_size": r[8],
                "population": r[9],
                "method": r[10],
                "undecided_pct": r[11],
                "quality_rating": r[12],
                "margin_of_error": r[13],
                "pollster_rating": r[2],
                "race_id": r[14],
                "is_fictional": bool(r[15]),
            }
        )
    polls_df = pd.DataFrame(poll_rows, columns=[*POLL_COLUMNS, *EXTRA_POLL_COLUMNS])
    for col in ("start_date", "end_date"):
        polls_df[col] = pd.to_datetime(polls_df[col])
    for col in ("undecided_pct", "quality_rating", "margin_of_error", "pollster_rating"):
        polls_df[col] = polls_df[col].astype(float)
    if polls_df.empty:
        return polls_df, pd.DataFrame(columns=list(RESULT_COLUMNS))

    rstmt = (
        select(PollResult.poll_id, PollResult.label, Party.code, PollResult.value_pct)
        .join(Poll, PollResult.poll_id == Poll.id)
        .outerjoin(Party, PollResult.party_id == Party.id)
        .order_by(PollResult.poll_id, PollResult.id)
    )
    rstmt = (
        rstmt.where(Poll.election_id.is_(None))
        if election_id is None
        else rstmt.where(Poll.election_id == election_id)
    )
    if poll_type is not None:
        rstmt = rstmt.where(Poll.poll_type == poll_type)
    result_rows = [
        {"poll_id": r[0], "key": r[2] if r[2] is not None else r[1], "value_pct": float(r[3])}
        for r in session.execute(rstmt)
    ]
    results_df = pd.DataFrame(result_rows, columns=list(RESULT_COLUMNS))
    return polls_df, results_df


def load_pollster_configs(session: Session, names: Iterable[str] | None = None) -> list[PollsterConfig]:
    """Pollsters (with prior house effects keyed by party code) as :class:`PollsterConfig`.

    Pass the result as ``pollsters=`` to :func:`app.polling.aggregate.aggregate_polls` so the
    aggregate uses the (editable) database ratings and priors.
    """
    stmt = select(Pollster).order_by(Pollster.name)
    if names is not None:
        stmt = stmt.where(Pollster.name.in_(list(names)))
    rows = list(session.scalars(stmt))
    party_codes = dict(session.execute(select(Party.id, Party.code)).tuples().all()) if rows else {}
    out = []
    for row in rows:
        effects = {
            party_codes[he.party_id]: float(he.effect_pct)
            for he in row.house_effects
            if he.party_id in party_codes
        }
        rating = float(row.rating) if row.rating is not None else 1.0
        lo, hi = _RATING_RANGE
        if not lo <= rating <= hi:
            log.warning("pollster %r: stored rating %s outside [%s, %s]; clamped", row.name, rating, lo, hi)
            rating = min(max(rating, lo), hi)
        out.append(
            PollsterConfig(
                name=row.name,
                rating=rating,
                rating_label=row.rating_label,
                method=row.default_method or "online",
                house_effects=effects,
            )
        )
    return out


def _checked_poll(p: Any) -> tuple[str, str, str, dict[str, float]]:
    """Validated ``(poll_type, geo_code, population, results)`` of one poll to store."""
    poll_type = str(p.poll_type)
    if poll_type not in POLL_TYPES:
        raise ValueError(f"unknown poll type {poll_type!r}")
    geo = str(p.geo_code or NATIONAL_GEO)
    if len(geo) > _DISTRICT_CODE_LEN and geo != NATIONAL_GEO:
        raise ValueError(f"geo_code {geo!r} too long to store")
    population = str(getattr(p, "population", "LV") or "LV").strip().upper()
    if population not in POPULATIONS:
        raise ValueError(f"poll population must be one of {POPULATIONS}, got {population!r}")
    if int(p.sample_size) <= 0:
        raise ValueError(f"poll sample size must be positive, got {p.sample_size!r}")
    results = {str(key): float(value) for key, value in dict(p.results).items()}
    too_long = [k for k in results if len(k) > _LABEL_LEN]
    if too_long:
        raise ValueError(f"poll result keys longer than {_LABEL_LEN} characters: {too_long[:3]}")
    return poll_type, geo, population, results


def _province_ids(session: Session, polls: Sequence[Any], given: Mapping[str, int] | None) -> dict[str, int]:
    """Province code → id for every province referenced by ``polls``: ``given`` first, then the
    ``province`` table (province identity is stable across geography vintages)."""
    prov_map = {str(k): int(v) for k, v in dict(given or {}).items()}
    wanted = {pv for p in polls if (pv := province_of_geo(str(p.geo_code or NATIONAL_GEO)))}
    missing = sorted(wanted - set(prov_map))
    if missing:
        found = dict(
            session.execute(select(Province.code, Province.id).where(Province.code.in_(missing)))
            .tuples()
            .all()
        )
        prov_map.update(found)
        unresolved = sorted(set(missing) - set(found))
        if unresolved:
            log.debug("no province row for %s; province-level geo codes kept in district_code", unresolved)
    return prov_map


def _ensure_pollsters(
    session: Session,
    polls: Sequence[Any],
    party_code_to_id: Mapping[str, int],
    specs: Sequence[PollsterLike] | None,
) -> None:
    """Create the pollsters of ``polls`` that are not in the database yet."""
    names = sorted({str(p.pollster) for p in polls})
    existing = set(session.scalars(select(Pollster.name).where(Pollster.name.in_(names))))
    missing = [n for n in names if n not in existing]
    if not missing:
        return
    for name in missing:
        if looks_like_real_pollster(name):
            raise ValueError(f"pollster name {name!r} resembles a real polling organisation")
    known: dict[str, PollsterLike] = {p.name: p for p in load_polling_config().pollsters}
    known.update({p.name: p for p in specs or []})
    create: list[PollsterLike] = []
    for name in missing:
        spec = known.get(name)
        if spec is None:
            method = next(str(p.method) for p in polls if str(p.pollster) == name)
            log.warning("pollster %r is not configured; creating it with rating 1.0", name)
            spec = PollsterConfig(name=name, rating=1.0, method=method)
        create.append(spec)
    upsert_pollsters(session, create, party_code_to_id)


def _fit(value: str, length: int) -> str:
    """``value`` cut to a column's maximum length (logged when it had to be shortened)."""
    if len(value) <= length:
        return value
    log.debug("value %r shortened to %d characters for storage", value, length)
    return value[:length]


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f
