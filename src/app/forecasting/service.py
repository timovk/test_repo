"""Persistence of Monte Carlo forecasts (SQLAlchemy).

Translates a :class:`~app.forecasting.result.ForecastResult` into the ``simulation_run`` (kind
``forecast``) and ``forecast_*`` tables and back.  Every stored number is a SIMULATED estimate of
the FICTIONAL model (never a prediction); the full result is kept as JSON in
``simulation_run.summary_json`` and the queryable parts are materialised relationally:

* ``forecast_summary`` — per race × ballot line: win probability, share mean / p05 / p50 / p95,
  mean votes;
* ``forecast_distribution`` — histograms: ``ev`` and ``popular_vote_share`` per ticket,
  ``house_seats`` / ``senate_seats`` / ``governor_wins`` per party (non-zero bins only);
* ``forecast_aggregate`` — ``ev`` / ``popular_vote`` per ticket and ``house_seats`` /
  ``senate_seats`` / ``governor_wins`` per party: mean, median, p05, p25, p75, p95, probability of
  a majority and of a (strict) plurality;
* ``forecast_combination`` — the most common Electoral College maps.

Ticket keys in the distribution / aggregate tables are the ``ballot_candidate`` id of the ticket
on the national ``PRES`` ballot (as a string), falling back to the ticket's party code.  Functions
flush but never commit — the caller owns the transaction.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.core.constitution import DataCategory
from app.core.errors import ElectionError, NotFoundError
from app.core.logging import Timer, get_logger
from app.forecasting.result import DISCLAIMER, ForecastResult, SeatForecast, Summary
from app.models import (
    ForecastAggregate,
    ForecastCombination,
    ForecastDistribution,
    ForecastSummary,
    SimulationRun,
)
from app.models.base import utcnow

log = get_logger(__name__)

RUN_KIND = "forecast"
STATUS_COMPLETED = "completed"
_CHUNK = 5000
_KEY_LEN = 24  # forecast_distribution.key / forecast_aggregate.key
_SUBJECT_LEN = 24


# --------------------------------------------------------------------------- helpers
def _bulk(session: Session, model: type, rows: Sequence[dict[str, Any]]) -> None:
    for i in range(0, len(rows), _CHUNK):
        session.execute(insert(model), list(rows[i : i + _CHUNK]))


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _ticket_keys(result: ForecastResult, line_ids: Mapping[str, Mapping[str, int]]) -> dict[str, str]:
    """DB key per ticket: ballot_candidate id on the national race (or a province contest),
    else the party code (else the line key), made unique and ≤ 24 characters."""
    pres = result.president
    if pres is None:
        return {}
    ids: Mapping[str, int] = line_ids.get("PRES", {})
    if not ids:
        for code in sorted(line_ids):
            if code.startswith("PRES-"):
                ids = line_ids[code]
                break
    out: dict[str, str] = {}
    used: set[str] = set()
    for t in pres.tickets:
        if t.key in ids:
            k = str(int(ids[t.key]))
        else:
            k = (t.party_code or t.key)[:_KEY_LEN]
        base, n = k, 2
        while k in used:
            suffix = f"~{n}"
            k = base[: _KEY_LEN - len(suffix)] + suffix
            n += 1
        used.add(k)
        out[t.key] = k
    return out


def _ticket_labels(result: ForecastResult) -> dict[str, str]:
    """Human-readable ticket label for combination keys: the party code when unique."""
    pres = result.president
    if pres is None:
        return {}
    parties = [t.party_code for t in pres.tickets]
    return {
        t.key: t.party_code if t.party_code and parties.count(t.party_code) == 1 else t.key
        for t in pres.tickets
    }


def _aggregate_row(
    run_id: int, subject: str, key: str, s: Summary, maj: float | None, plu: float | None
) -> dict:
    return {
        "run_id": run_id,
        "subject": subject[:_SUBJECT_LEN],
        "key": key[:_KEY_LEN],
        "mean": s.mean,
        "median": s.median,
        "p05": s.p05,
        "p25": s.p25,
        "p75": s.p75,
        "p95": s.p95,
        "prob_majority": maj,
        "prob_plurality": plu,
    }


def _histogram_rows(run_id: int, subject: str, key: str, hist: Iterable[float]) -> list[dict]:
    return [
        {
            "run_id": run_id,
            "subject": subject,
            "key": key[:_KEY_LEN],
            "value": float(v),
            "probability": float(p),
        }
        for v, p in enumerate(hist)
        if p > 0
    ]


def _seat_rows(
    run_id: int, subject: str, parties: Mapping[str, SeatForecast]
) -> tuple[list[dict], list[dict]]:
    agg, dist = [], []
    for key, sf in parties.items():
        agg.append(_aggregate_row(run_id, subject, key, sf.seats, sf.prob_majority, sf.prob_plurality))
        dist.extend(_histogram_rows(run_id, subject, key, sf.histogram))
    return agg, dist


# --------------------------------------------------------------------------- store
def store_forecast(
    session: Session,
    election_id: int,
    result: ForecastResult,
    *,
    race_ids: Mapping[str, int],
    line_ids: Mapping[str, Mapping[str, int]],
    scenario_id: int | None = None,
    code_version: str | None = None,
) -> SimulationRun:
    """Persist ``result`` for ``election_id``; returns the new ``SimulationRun`` (kind ``forecast``).

    ``race_ids`` maps race codes to ``race.id`` and ``line_ids`` race code → line key →
    ``ballot_candidate.id``; every race and line of the result must be mapped (``ElectionError``
    otherwise).
    """
    missing_races = [k for k in result.races if k not in race_ids]
    missing_lines = [
        f"{k}/{ln.key}"
        for k, r in result.races.items()
        if k in race_ids
        for ln in r.lines
        if ln.key not in line_ids.get(k, {})
    ]
    if missing_races or missing_lines:
        raise ElectionError(
            "forecast races/lines without database ids: "
            + ", ".join((missing_races + missing_lines)[:10])
            + (" …" if len(missing_races) + len(missing_lines) > 10 else "")
        )
    runtime = result.runtime or {}
    timing = runtime.get("timing") or {}
    with Timer(log, f"store forecast ({len(result.races)} races)"):
        run = SimulationRun(
            kind=RUN_KIND,
            election_id=election_id,
            scenario_id=scenario_id,
            seed=int(result.seed),
            n_simulations=int(result.n_simulations),
            config_hash=str(result.metadata.get("config_hash", ""))[:16] or None,
            status=STATUS_COMPLETED,
            started_at=_dt(runtime.get("started_at")) or utcnow(),
            finished_at=_dt(runtime.get("finished_at")),
            duration_s=timing.get("total_s"),
            summary_json=result.to_json(),
            code_version=code_version,
        )
        session.add(run)
        session.flush()
        rid = int(run.id)
        summaries = [
            {
                "run_id": rid,
                "race_id": int(race_ids[k]),
                "ballot_candidate_id": int(line_ids[k][ln.key]),
                "win_probability": ln.win_probability,
                "mean_share": ln.share_mean,
                "p05_share": ln.share_p05,
                "p50_share": ln.share_p50,
                "p95_share": ln.share_p95,
                "mean_votes": ln.mean_votes,
            }
            for k, r in result.races.items()
            for ln in r.lines
        ]
        aggregates: list[dict] = []
        distributions: list[dict] = []
        combinations: list[dict] = []
        pres = result.president
        if pres is not None:
            tkeys = _ticket_keys(result, line_ids)
            labels = _ticket_labels(result)
            for t in pres.tickets:
                k = tkeys[t.key]
                aggregates.append(_aggregate_row(rid, "ev", k, t.ev, t.prob_majority, t.prob_plurality))
                aggregates.append(_aggregate_row(rid, "popular_vote", k, t.pv, None, t.prob_pv_plurality))
                distributions.extend(_histogram_rows(rid, "ev", k, t.ev_histogram))
                distributions.extend(
                    {"run_id": rid, "subject": "popular_vote_share", "key": k, "value": c, "probability": p}
                    for c, p in t.pv_histogram
                )
            for c in pres.combinations:
                combinations.append(
                    {
                        "run_id": rid,
                        "rank": c.rank,
                        "combination_key": "|".join(
                            f"{pv}:{labels.get(t, t)}" for pv, t in c.winners.items()
                        ),
                        "frequency": c.frequency,
                        "ev_json": json.dumps({t: round(v, 3) for t, v in c.ev.items()}, sort_keys=True),
                    }
                )
        for subject, chamber in (
            ("house_seats", result.house),
            ("senate_seats", result.senate),
            ("governor_wins", result.governors),
        ):
            if chamber is not None:
                a, d = _seat_rows(rid, subject, chamber.parties)
                aggregates.extend(a)
                distributions.extend(d)
        _bulk(session, ForecastSummary, summaries)
        _bulk(session, ForecastAggregate, aggregates)
        _bulk(session, ForecastDistribution, distributions)
        _bulk(session, ForecastCombination, combinations)
        session.flush()
    log.info(
        "stored forecast run %d: %d summaries, %d aggregates, %d histogram rows, %d maps",
        rid,
        len(summaries),
        len(aggregates),
        len(distributions),
        len(combinations),
    )
    return run


# --------------------------------------------------------------------------- load
def _run_dict(run: SimulationRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "election_id": run.election_id,
        "scenario_id": run.scenario_id,
        "kind": run.kind,
        "status": run.status,
        "seed": run.seed,
        "n_simulations": run.n_simulations,
        "config_hash": run.config_hash,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_s": run.duration_s,
        "code_version": run.code_version,
        "data_category": DataCategory.SIMULATED.value,
        "disclaimer": DISCLAIMER,
    }


def load_forecast(session: Session, run_id: int) -> dict[str, Any]:
    """A stored forecast: run metadata, the full result (``result``) and the relational rows
    (``aggregates`` by subject and key, ``distributions`` as ``[value, probability]`` pairs,
    ``combinations``, ``race_summaries``).  Raises :class:`NotFoundError`."""
    run = session.get(SimulationRun, run_id)
    if run is None or run.kind != RUN_KIND:
        raise NotFoundError(f"forecast run {run_id} not found")
    out = _run_dict(run)
    out["result"] = json.loads(run.summary_json) if run.summary_json else None
    aggregates: dict[str, dict[str, dict[str, float | None]]] = {}
    for a in session.scalars(select(ForecastAggregate).where(ForecastAggregate.run_id == run_id)):
        aggregates.setdefault(a.subject, {})[a.key] = {
            "mean": a.mean,
            "median": a.median,
            "p05": a.p05,
            "p25": a.p25,
            "p75": a.p75,
            "p95": a.p95,
            "prob_majority": a.prob_majority,
            "prob_plurality": a.prob_plurality,
        }
    distributions: dict[str, dict[str, list[list[float]]]] = {}
    rows = session.scalars(
        select(ForecastDistribution)
        .where(ForecastDistribution.run_id == run_id)
        .order_by(ForecastDistribution.subject, ForecastDistribution.key, ForecastDistribution.value)
    )
    for d in rows:
        distributions.setdefault(d.subject, {}).setdefault(d.key, []).append([d.value, d.probability])
    combinations = [
        {
            "rank": c.rank,
            "combination_key": c.combination_key,
            "frequency": c.frequency,
            "ev": json.loads(c.ev_json),
        }
        for c in session.scalars(
            select(ForecastCombination)
            .where(ForecastCombination.run_id == run_id)
            .order_by(ForecastCombination.rank)
        )
    ]
    summaries = [
        {
            "race_id": s.race_id,
            "ballot_candidate_id": s.ballot_candidate_id,
            "win_probability": s.win_probability,
            "mean_share": s.mean_share,
            "p05_share": s.p05_share,
            "p50_share": s.p50_share,
            "p95_share": s.p95_share,
            "mean_votes": s.mean_votes,
        }
        for s in session.scalars(
            select(ForecastSummary)
            .where(ForecastSummary.run_id == run_id)
            .order_by(ForecastSummary.race_id, ForecastSummary.ballot_candidate_id)
        )
    ]
    out.update(
        {
            "aggregates": aggregates,
            "distributions": distributions,
            "combinations": combinations,
            "race_summaries": summaries,
        }
    )
    return out


def list_forecasts(session: Session, election_id: int) -> list[dict[str, Any]]:
    """Forecast runs of an election (newest first; metadata only)."""
    runs = session.scalars(
        select(SimulationRun)
        .where(SimulationRun.kind == RUN_KIND, SimulationRun.election_id == election_id)
        .order_by(SimulationRun.id.desc())
    )
    return [_run_dict(r) for r in runs]


def latest_forecast(session: Session, election_id: int) -> dict[str, Any] | None:
    """The newest completed forecast of ``election_id`` (see :func:`load_forecast`), or ``None``."""
    run_id = session.scalar(
        select(SimulationRun.id)
        .where(
            SimulationRun.kind == RUN_KIND,
            SimulationRun.election_id == election_id,
            SimulationRun.status == STATUS_COMPLETED,
        )
        .order_by(SimulationRun.id.desc())
        .limit(1)
    )
    return None if run_id is None else load_forecast(session, int(run_id))
