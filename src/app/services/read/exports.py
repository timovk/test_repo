"""Stable-schema exports of an election (:mod:`app.export`): results at every level, race
summaries, electoral votes, swing, reporting timeline, race calls, Monte Carlo output, polls and
polling averages, the district plan and the apportionment.

Result datasets exist only for reported elections (hidden-until-reported rule); polls, the
district plan, the apportionment and forecasts are available for every election.  CSV and JSON
envelopes are produced by :func:`app.export.writers.to_csv_text` /
:func:`app.export.writers.to_json_envelope` (deterministic, schema-conformed, key-sorted)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constitution import RaceType
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.export.builders import (
    electoral_votes_export,
    governor_results_export,
    house_results_export,
    results_export,
    senate_results_export,
    swing_export,
)
from app.export.schemas import conform, get_schema, list_schemas
from app.export.writers import to_csv_text, to_json_envelope
from app.models import (
    ApportionmentSeat,
    BallotCandidate,
    Candidate,
    ForecastDistribution,
    ForecastSummary,
    HouseDistrict,
    Party,
    Race,
    RaceCall,
    SenateSeat,
    SimulationRun,
)
from app.services.read._base import ElectionRef, province_maps, require_reported
from app.services.read.elections import lineage_between, previous_with
from app.services.results import results_frame

log = get_logger(__name__)

FORMATS: tuple[str, ...] = ("csv", "json")

_LEVEL_DATASETS: dict[str, str] = {
    "national_results": "national",
    "province_results": "province",
    "municipality_results": "municipality",
    "unit_results": "unit",
    "district_results": "district",
}
#: Datasets that need a reported election.
_RESULT_DATASETS: frozenset[str] = frozenset(
    {
        *_LEVEL_DATASETS,
        "house_results",
        "senate_results",
        "governor_results",
        "electoral_votes",
        "swing",
        "reporting_timeline",
        "race_calls",
    }
)


def datasets() -> dict[str, Any]:
    """``GET /api/export/schemas`` — every dataset with its schema (version, category, key,
    columns) and aliases."""
    out = []
    for name in list_schemas():
        s = get_schema(name)
        out.append(
            {
                "name": s.name,
                "version": s.version,
                "data_category": s.data_category.value,
                "aliases": list(s.aliases),
                "key": list(s.key),
                "fingerprint": s.fingerprint(),
                "description": s.description,
                "requires_reported_election": s.name in _RESULT_DATASETS,
                "columns": [c.to_dict(s.data_category) for c in s.columns],
            }
        )
    return {"formats": list(FORMATS), "datasets": out}


def _race_types(race_type: str | None) -> list[RaceType] | None:
    if race_type is None:
        return None
    try:
        return [RaceType(race_type.upper())]
    except ValueError:
        raise ValidationError(f"unknown race_type {race_type!r}") from None


def _incumbents(session: Session, ref: ElectionRef, rt: RaceType) -> pd.DataFrame:
    rows = session.execute(
        select(Race.code, Party.code, Candidate.full_name, Race.is_open_seat)
        .outerjoin(Party, Party.id == Race.incumbent_party_id)
        .outerjoin(Candidate, Candidate.id == Race.incumbent_candidate_id)
        .where(Race.election_id == ref.id, Race.race_type == rt.value)
    ).all()
    return pd.DataFrame(rows, columns=["race_code", "incumbent_party", "incumbent_candidate", "is_open_seat"])


def _prev_frame(session: Session, ref: ElectionRef, rt: RaceType, level: str) -> pd.DataFrame | None:
    prev = previous_with(session, ref, (rt,))
    if prev is None:
        return None
    return results_frame(session, [prev.id], levels=[level], race_types=[rt])


def build(
    session: Session, ref: ElectionRef, dataset: str, **opts: Any
) -> tuple[str, pd.DataFrame, dict[str, Any]]:
    """``(schema name, conformed frame, metadata)`` of an export dataset (name or alias)."""
    schema = get_schema(dataset).name
    if schema in _RESULT_DATASETS:
        require_reported(ref)
    meta: dict[str, Any] = {"election_id": ref.id, "year": ref.year, "election": ref.name, "seed": ref.seed}
    rts = _race_types(opts.get("race_type"))
    if schema in _LEVEL_DATASETS:
        level = _LEVEL_DATASETS[schema]
        frame = results_frame(session, [ref.id], levels=[level], race_types=rts)
        if opts.get("race"):
            frame = frame[frame["race_code"] == str(opts["race"]).upper()]
        um = None
        if level == "unit":
            from app.services.read.geography import unit_frame

            uf = unit_frame(session, ref.vintage_id)
            um = dict(zip(uf["unit_code"], uf["municipality_code"], strict=True))
        return schema, results_export(frame, level, unit_municipality=um), meta
    if schema == "house_results":
        frame = results_frame(session, [ref.id], levels=["district"], race_types=[RaceType.HOUSE])
        prev = _prev_frame(session, ref, RaceType.HOUSE, "district")
        return schema, house_results_export(frame, prev, _incumbents(session, ref, RaceType.HOUSE)), meta
    if schema == "senate_results":
        frame = results_frame(session, [ref.id], levels=["province"], race_types=[RaceType.SENATE])
        prev = _prev_frame(session, ref, RaceType.SENATE, "province")
        seats = session.execute(
            select(Race.code, SenateSeat.senate_class, Race.is_special)
            .join(SenateSeat, SenateSeat.id == Race.senate_seat_id)
            .where(Race.election_id == ref.id)
        ).all()
        return (
            schema,
            senate_results_export(
                frame,
                prev,
                _incumbents(session, ref, RaceType.SENATE),
                senate_classes={c: int(k) for c, k, _s in seats},
                special_races=[c for c, _k, s in seats if s],
            ),
            meta,
        )
    if schema == "governor_results":
        frame = results_frame(session, [ref.id], levels=["province"], race_types=[RaceType.GOVERNOR])
        prev = _prev_frame(session, ref, RaceType.GOVERNOR, "province")
        return (
            schema,
            governor_results_export(frame, prev, _incumbents(session, ref, RaceType.GOVERNOR)),
            meta,
        )
    if schema == "electoral_votes":
        frame = results_frame(
            session, [ref.id], levels=["province"], race_types=[RaceType.PRESIDENT_PROVINCE]
        )
        ids, _ = province_maps(session)
        rows = session.execute(
            select(Race.province_id, Race.electoral_votes, Race.decided_by).where(
                Race.election_id == ref.id, Race.race_type == RaceType.PRESIDENT_PROVINCE.value
            )
        ).all()
        if not rows:
            raise NotFoundError(f"election {ref.id} has no presidential race")
        ev = {ids[p]: int(v or 0) for p, v, _d in rows}
        allowed = {"popular_vote", "lot", "recount", "contingent"}
        decided = {ids[p]: d for p, _v, d in rows if d in allowed}
        return schema, electoral_votes_export(frame, ev, decided_by=decided), meta
    if schema == "swing":
        family = str(opts.get("race") or "PRES").upper()
        level = str(opts.get("level") or "municipality")
        rt = {
            "PRES": RaceType.PRESIDENT,
            "HOUSE": RaceType.HOUSE,
            "SEN": RaceType.SENATE,
            "GOV": RaceType.GOVERNOR,
        }.get(family)
        if rt is None:
            raise ValidationError("swing exports support race=PRES|HOUSE|SEN|GOV")
        prev = previous_with(session, ref, (rt,))
        if prev is None:
            raise NotFoundError(f"no reported election with {family} races before election {ref.id}")
        curr_f = results_frame(session, [ref.id], levels=[level], race_types=[rt])
        prev_f = results_frame(session, [prev.id], levels=[level], race_types=[rt])
        meta["previous_election_id"] = prev.id
        return (
            schema,
            swing_export(prev_f, curr_f, level, race_type=rt, lineage=lineage_between(session, prev, ref)),
            meta,
        )
    if schema == "reporting_timeline":
        from app.services.read.elections import timeline as timeline_view

        page = timeline_view(session, ref, detail="events", limit=10**7)
        df = pd.DataFrame(page["events"])
        if df.empty:
            return schema, conform(pd.DataFrame(columns=list(get_schema(schema).names)), schema), meta
        df = df.rename(columns={"ballots": "ballots_in_batch"})
        df["election_id"] = ref.id
        df["time"] = pd.to_datetime(df["time"])
        return schema, conform(df, schema, allow_unknown=True), meta
    if schema == "race_calls":
        rows = session.execute(
            select(RaceCall, Race.code, Race.race_type, BallotCandidate)
            .join(Race, Race.id == RaceCall.race_id)
            .outerjoin(BallotCandidate, BallotCandidate.id == RaceCall.ballot_candidate_id)
            .where(RaceCall.election_id == ref.id)
        ).all()
        df = pd.DataFrame(
            [
                {
                    "election_id": ref.id,
                    "seq": c.seq,
                    "sim_time_s": c.sim_time_s,
                    "called_at": c.called_at,
                    "race_code": code,
                    "race_type": rt,
                    "status": c.status,
                    "line_key": None if b is None else b.line_key,
                    "candidate": None if b is None else b.ballot_name,
                    "party_code": None if b is None else b.party_code_snapshot,
                    "reporting_pct": c.reporting_pct,
                    "leader_margin_pct": c.leader_margin_pct,
                    "win_probability": c.win_probability,
                    "is_manual": bool(c.is_manual),
                    "superseded": bool(c.superseded),
                }
                for c, code, rt, b in rows
            ],
            columns=list(get_schema(schema).names),
        )
        return schema, conform(df, schema), meta
    if schema in ("montecarlo_summary", "montecarlo_distribution"):
        run = _forecast_run(session, ref, opts.get("run_id"))
        meta["run_id"] = run.id
        if schema == "montecarlo_summary":
            rows = session.execute(
                select(ForecastSummary, Race.code, Race.race_type, BallotCandidate)
                .join(Race, Race.id == ForecastSummary.race_id)
                .join(BallotCandidate, BallotCandidate.id == ForecastSummary.ballot_candidate_id)
                .where(ForecastSummary.run_id == run.id)
            ).all()
            df = pd.DataFrame(
                [
                    {
                        "run_id": run.id,
                        "election_id": ref.id,
                        "seed": int(run.seed),
                        "n_simulations": run.n_simulations,
                        "race_code": code,
                        "race_type": rt,
                        "line_key": b.line_key or str(b.id),
                        "candidate": b.ballot_name,
                        "party_code": b.party_code_snapshot,
                        "win_probability": s.win_probability,
                        "mean_share": s.mean_share,
                        "p05_share": s.p05_share,
                        "p50_share": s.p50_share,
                        "p95_share": s.p95_share,
                        "mean_votes": s.mean_votes,
                    }
                    for s, code, rt, b in rows
                ],
                columns=list(get_schema(schema).names),
            )
        else:
            df = pd.DataFrame(
                [
                    {
                        "run_id": run.id,
                        "election_id": ref.id,
                        "subject": d.subject,
                        "key": d.key,
                        "value": d.value,
                        "probability": d.probability,
                    }
                    for d in session.scalars(
                        select(ForecastDistribution).where(ForecastDistribution.run_id == run.id)
                    )
                ],
                columns=list(get_schema(schema).names),
            )
        return schema, conform(df, schema), meta
    if schema == "polls":
        return schema, _polls_df(session, ref), meta
    if schema == "polling_averages":
        return schema, _averages_df(session, ref, opts.get("as_of")), meta
    if schema == "districts":
        if ref.district_plan_id is None:
            raise NotFoundError(f"election {ref.id} has no district plan")
        ids, _ = province_maps(session)
        ds = session.scalars(select(HouseDistrict).where(HouseDistrict.plan_id == ref.district_plan_id)).all()
        df = pd.DataFrame(
            [
                {
                    "plan_id": d.plan_id,
                    "district_code": d.code,
                    "province_code": ids[d.province_id],
                    "number": d.number,
                    "name": d.name,
                    "population": d.population,
                    "eligible_voters_est": d.eligible_voters_est,
                    "target_population": d.target_population,
                    "deviation_pct": d.deviation_pct,
                    "area_km2": d.area_km2,
                    "polsby_popper": d.polsby_popper,
                    "reock": d.reock,
                    "convex_hull_ratio": d.convex_hull_ratio,
                    "n_units": d.n_units,
                    "n_municipalities": d.n_municipalities,
                    "n_split_municipalities": d.n_split_municipalities,
                    "urban_share": d.urban_share,
                    "rural_share": d.rural_share,
                    "is_contiguous": bool(d.is_contiguous),
                    "n_components": d.n_components,
                    "centroid_lon": d.centroid_lon,
                    "centroid_lat": d.centroid_lat,
                }
                for d in ds
            ],
            columns=list(get_schema(schema).names),
        )
        meta["plan_id"] = ref.district_plan_id
        return schema, conform(df, schema), meta
    if schema == "apportionment":
        from app.models import Apportionment

        if ref.apportionment_id is None:
            raise NotFoundError(f"election {ref.id} has no apportionment")
        appt = session.get(Apportionment, ref.apportionment_id)
        ids, pmap = province_maps(session)
        df = pd.DataFrame(
            [
                {
                    "apportionment_id": appt.id,
                    "method": appt.method,
                    "province_code": ids[s.province_id],
                    "province_name": pmap[ids[s.province_id]]["name"],
                    "population": s.population,
                    "quota": s.quota,
                    "seats": s.seats,
                    "senators": appt.senators_per_province,
                    "electoral_votes": s.electoral_votes,
                    "persons_per_seat": s.persons_per_seat,
                }
                for s in session.scalars(
                    select(ApportionmentSeat).where(ApportionmentSeat.apportionment_id == appt.id)
                )
            ],
            columns=list(get_schema(schema).names),
        )
        return schema, conform(df, schema), meta
    raise NotFoundError(f"dataset {dataset!r} is not exported per election")


def _forecast_run(session: Session, ref: ElectionRef, run_id: Any) -> SimulationRun:
    q = select(SimulationRun).where(
        SimulationRun.kind == "forecast",
        SimulationRun.election_id == ref.id,
        SimulationRun.status == "completed",
    )
    if run_id is not None:
        q = q.where(SimulationRun.id == int(run_id))
    run = session.scalars(q.order_by(SimulationRun.id.desc()).limit(1)).first()
    if run is None:
        raise NotFoundError(f"election {ref.id} has no forecast run" + (f" {run_id}" if run_id else ""))
    return run


def _polls_df(session: Session, ref: ElectionRef) -> pd.DataFrame:
    from app.polling.service import polls_frames
    from app.polling.types import NATIONAL_GEO, province_of_geo

    pdf, rdf = polls_frames(session, ref.id)
    cols = list(get_schema("polls").names)
    if pdf.empty:
        return conform(pd.DataFrame(columns=cols), "polls")
    parties = set(session.scalars(select(Party.code)))
    m = rdf.merge(pdf, on="poll_id", how="inner")
    geo = m["geo_code"].astype(str)
    df = pd.DataFrame(
        {
            "poll_id": m["poll_id"],
            "election_id": ref.id,
            "pollster": m["pollster"],
            "poll_type": m["poll_type"],
            "geo_code": [None if g == NATIONAL_GEO else province_of_geo(g) for g in geo],
            "district_code": [None if g == NATIONAL_GEO or len(g) == 2 else g for g in geo],
            "start_date": m["start_date"].dt.date,
            "end_date": m["end_date"].dt.date,
            "sample_size": m["sample_size"],
            "population": m["population"],
            "method": m["method"],
            "margin_of_error": m["margin_of_error"],
            "undecided_pct": m["undecided_pct"],
            "label": m["key"],
            "party_code": [k if k in parties else None for k in m["key"]],
            "value_pct": m["value_pct"],
            "is_fictional": m["is_fictional"].astype(bool),
        },
        columns=cols,
    )
    return conform(df, "polls")


def _averages_df(session: Session, ref: ElectionRef, as_of: Any) -> pd.DataFrame:
    from app.polling.aggregate import aggregate_polls
    from app.polling.service import load_pollster_configs, polls_frames
    from app.polling.types import NATIONAL_GEO

    cols = list(get_schema("polling_averages").names)
    pdf, rdf = polls_frames(session, ref.id)
    day = pd.Timestamp(as_of).date() if as_of else ref.election_date - timedelta(days=1)
    avgs = (
        aggregate_polls(pdf, rdf, None, day, pollsters=load_pollster_configs(session))
        if not pdf.empty
        else {}
    )
    parties = set(session.scalars(select(Party.code)))
    rows = []
    for avg in avgs.values():
        tr = avg.trend
        for k in avg.keys:
            slope = None
            if tr is not None and not tr.empty and len(tr) >= 8 and k in tr.columns:
                slope = float(tr[k].iloc[-1] - tr[k].iloc[-8])
            rows.append(
                {
                    "election_id": ref.id,
                    "as_of": avg.as_of,
                    "poll_type": avg.poll_type,
                    "geo_code": None if avg.geo_code == NATIONAL_GEO else avg.geo_code,
                    "label": k,
                    "party_code": k if k in parties else None,
                    "average_pct": avg.mean[k],
                    "lower_pct": avg.lower[k],
                    "upper_pct": avg.upper[k],
                    "n_polls": avg.n_polls,
                    "effective_sample_size": avg.effective_n,
                    "trend_pct_per_week": slope,
                }
            )
    return conform(pd.DataFrame(rows, columns=cols), "polling_averages")


def export(session: Session, ref: ElectionRef, dataset: str, fmt: str, **opts: Any) -> tuple[Any, str, str]:
    """``GET /api/export/{id}/{dataset}.{csv|json}`` → ``(content, media type, filename)``: CSV
    text or the JSON envelope (dict)."""
    fmt = fmt.lower()
    if fmt not in FORMATS:
        raise ValidationError(f"format must be one of {list(FORMATS)}")
    schema, df, meta = build(session, ref, dataset, **opts)
    filename = f"nlfed_{ref.year}_e{ref.id}_{schema}.{fmt}"
    if fmt == "csv":
        return to_csv_text(df, schema), "text/csv; charset=utf-8", filename
    return to_json_envelope(df, schema, meta), "application/json", filename


__all__ = ["FORMATS", "build", "datasets", "export"]
