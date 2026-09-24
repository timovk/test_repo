"""Forecast persistence: SimulationRun + forecast_* rows round-trip through SQLite."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ElectionError, NotFoundError
from app.forecasting import ForecastResult
from app.forecasting.service import latest_forecast, list_forecasts, load_forecast, store_forecast
from app.models import (
    BallotCandidate,
    Election,
    ForecastAggregate,
    ForecastCombination,
    ForecastDistribution,
    ForecastSummary,
    GeoVintage,
    Race,
    SimulationRun,
)


def _election_rows(
    session: Session, result: ForecastResult
) -> tuple[int, dict[str, int], dict[str, dict[str, int]]]:
    vintage = GeoVintage(year=2025, label="synthetic test vintage")
    session.add(vintage)
    session.flush()
    election = Election(
        year=2028,
        election_date=date(2028, 11, 7),
        name="General Election 2028 (FICTIONAL)",
        election_type="general",
        seed=20280001,
        vintage_id=vintage.id,
    )
    session.add(election)
    session.flush()
    race_ids: dict[str, int] = {}
    line_ids: dict[str, dict[str, int]] = {}
    for key, rf in result.races.items():
        race = Race(election_id=election.id, race_type=rf.race_type, code=key, name=key)
        session.add(race)
        session.flush()
        race_ids[key] = race.id
        line_ids[key] = {}
        for order, ln in enumerate(rf.lines):
            bc = BallotCandidate(
                race_id=race.id,
                ballot_order=order,
                ballot_name=ln.label[:200],
                line_key=ln.key,
                party_code_snapshot=ln.party_code,
            )
            session.add(bc)
            session.flush()
            line_ids[key][ln.key] = bc.id
    return election.id, race_ids, line_ids


def test_store_and_load_round_trip(db_session: Session, full_forecast: ForecastResult) -> None:
    eid, race_ids, line_ids = _election_rows(db_session, full_forecast)
    run = store_forecast(db_session, eid, full_forecast, race_ids=race_ids, line_ids=line_ids)
    assert run.kind == "forecast" and run.status == "completed"
    assert run.seed == 11 and run.n_simulations == 2000
    assert run.config_hash == full_forecast.metadata["config_hash"]
    assert run.duration_s is not None and run.duration_s > 0
    assert json.loads(run.summary_json)["metadata"]["input_hash"] == full_forecast.metadata["input_hash"]

    n_lines = sum(len(r.lines) for r in full_forecast.races.values())
    assert db_session.scalar(select(func.count()).select_from(ForecastSummary)) == n_lines
    pres = full_forecast.president
    assert db_session.scalar(select(func.count()).select_from(ForecastCombination)) == len(pres.combinations)

    # per-ticket aggregates keyed by the ballot_candidate id of the national race
    top = pres.tickets[0]
    agg = db_session.scalars(
        select(ForecastAggregate).where(
            ForecastAggregate.subject == "ev", ForecastAggregate.key == str(line_ids["PRES"][top.key])
        )
    ).one()
    assert agg.mean == pytest.approx(top.ev.mean) and agg.prob_majority == pytest.approx(top.prob_majority)
    subjects = set(db_session.scalars(select(ForecastAggregate.subject).distinct()))
    assert subjects == {"ev", "popular_vote", "house_seats", "senate_seats", "governor_wins"}
    # every stored histogram sums to one
    rows = db_session.execute(
        select(
            ForecastDistribution.subject, ForecastDistribution.key, func.sum(ForecastDistribution.probability)
        ).group_by(ForecastDistribution.subject, ForecastDistribution.key)
    ).all()
    assert rows and all(total == pytest.approx(1.0) for _, _, total in rows)
    comb = db_session.scalars(select(ForecastCombination).order_by(ForecastCombination.rank)).first()
    assert comb.combination_key.startswith("GR:") and "|FR:" in comb.combination_key
    assert sum(json.loads(comb.ev_json).values()) == pytest.approx(174)

    loaded = load_forecast(db_session, run.id)
    assert loaded["data_category"] == "SIMULATED" and "prediction" in loaded["disclaimer"]
    assert loaded["result"] == json.loads(full_forecast.to_json())
    assert len(loaded["race_summaries"]) == n_lines
    assert loaded["aggregates"]["house_seats"]["VLP"]["mean"] == pytest.approx(
        full_forecast.house.parties["VLP"].seats.mean
    )
    assert loaded["combinations"][0]["rank"] == 1
    assert set(loaded["distributions"]) == {
        "ev",
        "popular_vote_share",
        "house_seats",
        "senate_seats",
        "governor_wins",
    }

    # a second run becomes the latest
    run2 = store_forecast(db_session, eid, full_forecast, race_ids=race_ids, line_ids=line_ids)
    latest = latest_forecast(db_session, eid)
    assert latest is not None and latest["run_id"] == run2.id
    assert [r["run_id"] for r in list_forecasts(db_session, eid)] == [run2.id, run.id]
    assert latest_forecast(db_session, eid + 1) is None


def test_missing_ids_and_unknown_runs(db_session: Session, full_forecast: ForecastResult) -> None:
    eid, race_ids, line_ids = _election_rows(db_session, full_forecast)
    partial = dict(race_ids)
    partial.pop("GOV-NB")
    with pytest.raises(ElectionError, match="GOV-NB"):
        store_forecast(db_session, eid, full_forecast, race_ids=partial, line_ids=line_ids)
    lines = {k: dict(v) for k, v in line_ids.items()}
    lines["PRES-NB"].popitem()
    with pytest.raises(ElectionError, match="PRES-NB/"):
        store_forecast(db_session, eid, full_forecast, race_ids=race_ids, line_ids=lines)
    assert db_session.scalar(select(func.count()).select_from(SimulationRun)) == 0
    with pytest.raises(NotFoundError):
        load_forecast(db_session, 12345)
