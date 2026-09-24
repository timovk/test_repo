"""Datasets of ``python -m app export``: stored rows shaped into the stable export schemas of
:mod:`app.export` (results through :func:`app.services.results.results_frame` and the export
builders; race calls, timeline, polls, forecasts, plan and apportionment from their tables).

Result datasets follow the hidden-until-reported rule: they exist only for FINAL elections.  The
call log exists as soon as a night has published calls.  Everything is SIMULATED / FICTIONAL except
the REAL geography columns (codes, names, populations).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constitution import RaceType
from app.core.errors import ElectionError, NotFoundError
from app.export.builders import (
    electoral_votes_export,
    governor_results_export,
    house_results_export,
    results_export,
    senate_results_export,
)
from app.export.schemas import column_names, conform
from app.models import (
    ApportionmentSeat,
    BallotCandidate,
    Election,
    ForecastDistribution,
    ForecastSummary,
    GeoUnit,
    HouseDistrict,
    Municipality,
    Province,
    Race,
    RaceCall,
    ReportingEvent,
    SenateSeat,
    SimulationRun,
)
from app.services._common import REPORTED_STATUSES
from app.services.results import results_frame

Builder = Callable[[Session, Election], pd.DataFrame]


@dataclass(frozen=True)
class Dataset:
    """An exportable dataset: its export schema and how to build it."""

    name: str
    schema: str
    build: Builder
    description: str


# =========================================================================== helpers
def _require_reported(el: Election) -> None:
    if el.status not in REPORTED_STATUSES:
        raise ElectionError(
            f"the results of election {el.id} ({el.year}) are hidden until it is final (status {el.status}); "
            "finish its election night or run `finalize` first"
        )


def _previous_with(session: Session, el: Election, race_type: RaceType) -> Election | None:
    """The most recent earlier reported election holding races of ``race_type``."""
    return session.scalars(
        select(Election)
        .join(Race, Race.election_id == Election.id)
        .where(
            Election.election_date < el.election_date,
            Election.status.in_(list(REPORTED_STATUSES)),
            Race.race_type == race_type.value,
        )
        .order_by(Election.election_date.desc(), Election.id.desc())
        .limit(1)
    ).first()


def _frame(session: Session, el: Election, level: str, race_type: RaceType | None = None) -> pd.DataFrame:
    _require_reported(el)
    return results_frame(
        session, [el.id], levels=(level,), race_types=None if race_type is None else [race_type]
    )


def _level(level: str) -> Builder:
    def build(session: Session, el: Election) -> pd.DataFrame:
        frame = _frame(session, el, level)
        mapping = None
        if level == "unit":
            mapping = dict(
                session.execute(
                    select(GeoUnit.cbs_code, Municipality.cbs_code)
                    .join(Municipality, Municipality.id == GeoUnit.municipality_id)
                    .where(GeoUnit.vintage_id == el.vintage_id)
                )
                .tuples()
                .all()
            )
        return results_export(frame, level, unit_municipality=mapping)

    return build


def _with_previous(
    session: Session, el: Election, level: str, race_type: RaceType
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    curr = _frame(session, el, level, race_type)
    prev_el = _previous_with(session, el, race_type)
    prev = (
        None
        if prev_el is None
        else results_frame(session, [prev_el.id], levels=(level,), race_types=[race_type])
    )
    return curr, prev


def _house(session: Session, el: Election) -> pd.DataFrame:
    curr, prev = _with_previous(session, el, "district", RaceType.HOUSE)
    return house_results_export(curr, prev)


def _senate(session: Session, el: Election) -> pd.DataFrame:
    curr, prev = _with_previous(session, el, "province", RaceType.SENATE)
    rows = session.execute(
        select(Race.code, SenateSeat.senate_class, Race.is_special)
        .join(SenateSeat, SenateSeat.id == Race.senate_seat_id)
        .where(Race.election_id == el.id)
    ).all()
    classes = {code: int(cls) for code, cls, _ in rows}
    special = [code for code, _, sp in rows if sp]
    return senate_results_export(curr, prev, senate_classes=classes, special_races=special)


def _governors(session: Session, el: Election) -> pd.DataFrame:
    curr, prev = _with_previous(session, el, "province", RaceType.GOVERNOR)
    return governor_results_export(curr, prev)


def _electoral_votes(session: Session, el: Election) -> pd.DataFrame:
    frame = _frame(session, el, "province", RaceType.PRESIDENT_PROVINCE)
    ev = dict(
        session.execute(
            select(Province.code, ApportionmentSeat.electoral_votes)
            .join(Province, Province.id == ApportionmentSeat.province_id)
            .where(ApportionmentSeat.apportionment_id == el.apportionment_id)
        )
        .tuples()
        .all()
    )
    decided = {
        code.removeprefix("PRES-"): by
        for code, by in session.execute(
            select(Race.code, Race.decided_by).where(
                Race.election_id == el.id, Race.race_type == RaceType.PRESIDENT_PROVINCE.value
            )
        ).all()
        if by
    }
    return electoral_votes_export(frame, ev, decided_by=decided)


def _timeline(session: Session, el: Election) -> pd.DataFrame:
    _require_reported(el)
    rows = session.execute(
        select(
            ReportingEvent.seq,
            ReportingEvent.sim_time_s,
            ReportingEvent.timestamp,
            Municipality.cbs_code,
            Municipality.name,
            Province.code,
            ReportingEvent.kind,
            ReportingEvent.ballots_in_batch,
            ReportingEvent.municipality_fraction_after,
        )
        .join(Municipality, Municipality.id == ReportingEvent.municipality_id)
        .join(Province, Province.id == ReportingEvent.province_id)
        .where(ReportingEvent.election_id == el.id)
        .order_by(ReportingEvent.seq)
    ).all()
    df = pd.DataFrame(
        rows,
        columns=[
            "seq",
            "sim_time_s",
            "time",
            "municipality_code",
            "municipality_name",
            "province_code",
            "kind",
            "ballots_in_batch",
            "municipality_fraction_after",
        ],
    )
    df.insert(0, "election_id", el.id)
    total = float(df["ballots_in_batch"].sum()) if len(df) else 0.0
    df["cumulative_ballots"] = df["ballots_in_batch"].cumsum()
    df["national_fraction"] = df["cumulative_ballots"] / total if total > 0 else 0.0
    return conform(df, "reporting_timeline")


def _calls(session: Session, el: Election) -> pd.DataFrame:
    rows = session.execute(
        select(
            RaceCall.seq,
            RaceCall.sim_time_s,
            RaceCall.called_at,
            Race.code,
            Race.race_type,
            RaceCall.status,
            BallotCandidate.line_key,
            BallotCandidate.ballot_name,
            BallotCandidate.party_code_snapshot,
            RaceCall.reporting_pct,
            RaceCall.leader_margin_pct,
            RaceCall.win_probability,
            RaceCall.is_manual,
            RaceCall.superseded,
        )
        .join(Race, Race.id == RaceCall.race_id)
        .outerjoin(BallotCandidate, BallotCandidate.id == RaceCall.ballot_candidate_id)
        .where(RaceCall.election_id == el.id)
        .order_by(RaceCall.seq, RaceCall.id)
    ).all()
    df = pd.DataFrame(
        rows,
        columns=[
            "seq",
            "sim_time_s",
            "called_at",
            "race_code",
            "race_type",
            "status",
            "line_key",
            "candidate",
            "party_code",
            "reporting_pct",
            "leader_margin_pct",
            "win_probability",
            "is_manual",
            "superseded",
        ],
    )
    df.insert(0, "election_id", el.id)
    return conform(df, "race_calls")


def _latest_forecast_run(session: Session, el: Election) -> SimulationRun:
    run = session.scalars(
        select(SimulationRun)
        .where(
            SimulationRun.kind == "forecast",
            SimulationRun.election_id == el.id,
            SimulationRun.status == "completed",
        )
        .order_by(SimulationRun.id.desc())
        .limit(1)
    ).first()
    if run is None:
        raise NotFoundError(f"election {el.id} has no forecast (run `forecast --election {el.id}` first)")
    return run


def _mc_summary(session: Session, el: Election) -> pd.DataFrame:
    run = _latest_forecast_run(session, el)
    rows = session.execute(
        select(
            Race.code,
            Race.race_type,
            BallotCandidate.line_key,
            BallotCandidate.ballot_name,
            BallotCandidate.party_code_snapshot,
            ForecastSummary.win_probability,
            ForecastSummary.mean_share,
            ForecastSummary.p05_share,
            ForecastSummary.p50_share,
            ForecastSummary.p95_share,
            ForecastSummary.mean_votes,
        )
        .join(Race, Race.id == ForecastSummary.race_id)
        .join(BallotCandidate, BallotCandidate.id == ForecastSummary.ballot_candidate_id)
        .where(ForecastSummary.run_id == run.id)
        .order_by(Race.id, BallotCandidate.ballot_order)
    ).all()
    df = pd.DataFrame(
        rows,
        columns=[
            "race_code",
            "race_type",
            "line_key",
            "candidate",
            "party_code",
            "win_probability",
            "mean_share",
            "p05_share",
            "p50_share",
            "p95_share",
            "mean_votes",
        ],
    )
    df.insert(0, "n_simulations", run.n_simulations)
    df.insert(0, "seed", int(run.seed))
    df.insert(0, "election_id", el.id)
    df.insert(0, "run_id", run.id)
    return conform(df, "montecarlo_summary")


def _mc_distribution(session: Session, el: Election) -> pd.DataFrame:
    run = _latest_forecast_run(session, el)
    rows = session.execute(
        select(
            ForecastDistribution.subject,
            ForecastDistribution.key,
            ForecastDistribution.value,
            ForecastDistribution.probability,
        )
        .where(ForecastDistribution.run_id == run.id)
        .order_by(ForecastDistribution.subject, ForecastDistribution.key, ForecastDistribution.value)
    ).all()
    df = pd.DataFrame(rows, columns=["subject", "key", "value", "probability"])
    df.insert(0, "election_id", el.id)
    df.insert(0, "run_id", run.id)
    return conform(df, "montecarlo_distribution")


def _polls(session: Session, el: Election) -> pd.DataFrame:
    from app.polling.service import polls_frames

    polls, results = polls_frames(session, el.id)
    if len(polls) == 0 or len(results) == 0:
        return conform(pd.DataFrame({c: pd.Series(dtype=object) for c in column_names("polls")}), "polls")
    df = results.merge(polls, on="poll_id", how="left")
    df["election_id"] = el.id
    geo = df["geo_code"].astype(str)
    df["district_code"] = df["geo_code"].where(geo.str.fullmatch(r"[A-Z]{2}-\d{2}"), None)
    df["label"] = df["key"].astype(str)
    df["party_code"] = df["key"].where(df["key"].astype(str).str.fullmatch(r"[A-Z][A-Z0-9]{1,11}"), None)
    return conform(df, "polls", allow_unknown=True)


def _plan(session: Session, el: Election) -> pd.DataFrame:
    if el.district_plan_id is None:
        raise NotFoundError(f"election {el.id} has no House plan")
    rows = session.execute(
        select(HouseDistrict, Province.code)
        .join(Province, Province.id == HouseDistrict.province_id)
        .where(HouseDistrict.plan_id == el.district_plan_id)
        .order_by(Province.sort_order, HouseDistrict.number)
    ).all()
    cols = [
        "district_code",
        "province_code",
        "number",
        "name",
        "population",
        "eligible_voters_est",
        "target_population",
        "deviation_pct",
        "area_km2",
        "polsby_popper",
        "reock",
        "convex_hull_ratio",
        "n_units",
        "n_municipalities",
        "n_split_municipalities",
        "urban_share",
        "rural_share",
        "is_contiguous",
        "n_components",
        "centroid_lon",
        "centroid_lat",
    ]
    data = [[d.code, pv, *(getattr(d, c) for c in cols[2:])] for d, pv in rows]
    df = pd.DataFrame(data, columns=cols)
    df.insert(0, "plan_id", el.district_plan_id)
    return conform(df, "districts")


def _apportionment(session: Session, el: Election) -> pd.DataFrame:
    from app.models import Apportionment

    appt = session.get(Apportionment, el.apportionment_id) if el.apportionment_id else None
    if appt is None:
        raise NotFoundError(f"election {el.id} has no apportionment")
    rows = session.execute(
        select(
            Province.code,
            Province.name,
            ApportionmentSeat.population,
            ApportionmentSeat.quota,
            ApportionmentSeat.seats,
            ApportionmentSeat.electoral_votes,
            ApportionmentSeat.persons_per_seat,
        )
        .join(Province, Province.id == ApportionmentSeat.province_id)
        .where(ApportionmentSeat.apportionment_id == appt.id)
        .order_by(Province.sort_order)
    ).all()
    df = pd.DataFrame(
        rows,
        columns=[
            "province_code",
            "province_name",
            "population",
            "quota",
            "seats",
            "electoral_votes",
            "persons_per_seat",
        ],
    )
    df.insert(0, "method", appt.method)
    df.insert(0, "apportionment_id", appt.id)
    df["senators"] = appt.senators_per_province
    return conform(df, "apportionment")


DATASETS: dict[str, Dataset] = {
    d.name: d
    for d in (
        Dataset("national", "national_results", _level("national"), "national results per race and line"),
        Dataset("provinces", "province_results", _level("province"), "province-level results"),
        Dataset(
            "municipalities", "municipality_results", _level("municipality"), "municipality-level results"
        ),
        Dataset("units", "unit_results", _level("unit"), "precinct (CBS buurt) results — large"),
        Dataset("district_lines", "district_results", _level("district"), "House-district-level results"),
        Dataset("house", "house_results", _house, "one row per House race (winner, margin, flip)"),
        Dataset("senate", "senate_results", _senate, "one row per Senate race"),
        Dataset("governors", "governor_results", _governors, "one row per governor race"),
        Dataset(
            "electoral_votes", "electoral_votes", _electoral_votes, "province Electoral College contests"
        ),
        Dataset("timeline", "reporting_timeline", _timeline, "election-night reporting events"),
        Dataset("calls", "race_calls", _calls, "race-call log of the election night"),
        Dataset(
            "montecarlo_summary", "montecarlo_summary", _mc_summary, "latest forecast: per race and line"
        ),
        Dataset(
            "montecarlo_distribution",
            "montecarlo_distribution",
            _mc_distribution,
            "latest forecast: histograms",
        ),
        Dataset("polls", "polls", _polls, "FICTIONAL polls of the election"),
        Dataset("district_plan", "districts", _plan, "the election's House plan (FICTIONAL districts)"),
        Dataset("apportionment", "apportionment", _apportionment, "the election's apportionment"),
    )
}

#: Datasets exported by ``--dataset all`` (the large unit table only on request).
DEFAULT_ALL: tuple[str, ...] = tuple(n for n in DATASETS if n != "units")


def build_dataset(session: Session, election_id: int, name: str) -> tuple[Dataset, pd.DataFrame]:
    """The dataset ``name`` of an election (conformed to its export schema)."""
    ds = DATASETS.get(name)
    if ds is None:
        raise NotFoundError(f"unknown dataset {name!r}; available: {', '.join(DATASETS)}")
    el = session.get(Election, int(election_id))
    if el is None:
        raise NotFoundError(f"election {election_id} not found")
    return ds, ds.build(session, el)
