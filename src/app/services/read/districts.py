"""FICTIONAL electoral geography read models: the House district plan, districts and the Senate
seats (with their current holders and next election)."""

from __future__ import annotations

import statistics
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_constitution
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.districts.service import active_plan
from app.elections.calendar import ElectionCalendar
from app.models import (
    BallotCandidate,
    DistrictAdjacency,
    DistrictMunicipality,
    DistrictPlan,
    Election,
    HouseDistrict,
    Municipality,
    Race,
    SenateSeat,
)
from app.services._common import REPORTED_STATUSES, loads
from app.services.read._base import (
    DERIVED,
    FICTIONAL,
    REAL,
    SIMULATED,
    cached,
    current_holders,
    iso,
    party_codes_by_id,
    province_maps,
    rnd,
)

log = get_logger(__name__)

DISTRICT_PROVENANCE: dict[str, str] = {
    "code": FICTIONAL,
    "name": FICTIONAL,
    "population": REAL,
    "eligible_voters_est": DERIVED,
    "deviation_pct": DERIVED,
    "compactness": DERIVED,
    "holder": FICTIONAL,
    "history": SIMULATED,
}


def _plan(session: Session, plan_id: int | None = None) -> DistrictPlan:
    plan = session.get(DistrictPlan, plan_id) if plan_id is not None else active_plan(session)
    if plan is None:
        raise NotFoundError(
            "no House district plan" if plan_id is None else f"district plan {plan_id} not found"
        )
    return plan


def _district_row(d: HouseDistrict, pcode: str, holder: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "code": d.code,
        "number": d.number,
        "name": d.name,
        "province_code": pcode,
        "population": d.population,
        "eligible_voters_est": d.eligible_voters_est,
        "target_population": rnd(d.target_population, 1),
        "deviation_pct": rnd(d.deviation_pct, 3),
        "area_km2": rnd(d.area_km2, 2),
        "polsby_popper": rnd(d.polsby_popper, 4),
        "reock": rnd(d.reock, 4),
        "convex_hull_ratio": rnd(d.convex_hull_ratio, 4),
        "unit_count": d.n_units,
        "municipality_count": d.n_municipalities,
        "split_municipalities": d.n_split_municipalities,
        "urban_share": rnd(d.urban_share, 4),
        "rural_share": rnd(d.rural_share, 4),
        "is_contiguous": bool(d.is_contiguous),
        "components": d.n_components,
        "centroid": [rnd(d.centroid_lon, 5), rnd(d.centroid_lat, 5)],
        "holder": holder,
    }


def districts(session: Session, province: str | None = None, plan_id: int | None = None) -> dict[str, Any]:
    """``GET /api/districts?province=`` — districts of the active (or given) plan with statistics
    and the serving House member."""
    plan = _plan(session, plan_id)
    ids, pmap = province_maps(session)
    if province is not None and province.upper() not in pmap:
        raise NotFoundError(f"province {province!r} not found")
    holders = current_holders(session, prefix="HOUSE-")
    q = select(HouseDistrict).where(HouseDistrict.plan_id == plan.id)
    if province is not None:
        q = q.where(HouseDistrict.province_id == pmap[province.upper()]["id"])
    rows = [
        _district_row(d, ids[d.province_id], holders.get(f"HOUSE-{d.code}"))
        for d in session.scalars(q.order_by(HouseDistrict.code))
    ]
    rows.sort(key=lambda r: (pmap[r["province_code"]]["sort_order"], r["number"]))
    return {
        "data_category": FICTIONAL,
        "provenance": DISTRICT_PROVENANCE,
        "plan": {"id": plan.id, "name": plan.name, "seed": int(plan.seed), "is_active": bool(plan.is_active)},
        "count": len(rows),
        "districts": rows,
    }


def plan_summary(session: Session, plan_id: int | None = None) -> dict[str, Any]:
    """``GET /api/districts/plan`` — plan metadata, generation audit (seed, method, config hash,
    timings), validation summary and deviation statistics (overall and per province)."""
    plan = _plan(session, plan_id)

    def build() -> dict[str, Any]:
        ids, pmap = province_maps(session)
        ds = session.scalars(select(HouseDistrict).where(HouseDistrict.plan_id == plan.id)).all()
        devs = [float(d.deviation_pct) for d in ds]
        pp = [float(d.polsby_popper) for d in ds if d.polsby_popper is not None]
        by_prov: dict[str, list[HouseDistrict]] = {}
        for d in ds:
            by_prov.setdefault(ids[d.province_id], []).append(d)
        run = None
        from app.models import SimulationRun

        for r in session.scalars(
            select(SimulationRun).where(SimulationRun.kind == "districts").order_by(SimulationRun.id.desc())
        ):
            summary = loads(r.summary_json)
            if summary.get("plan_id") == plan.id:
                run = {
                    "id": r.id,
                    "seed": int(r.seed),
                    "config_hash": r.config_hash,
                    "duration_s": r.duration_s,
                    "summary": summary,
                }
                break
        config = loads(plan.config_json) if plan.config_json else None
        cons = get_constitution()
        problems = []
        if len(ds) != cons.house_seats:
            problems.append(f"{len(ds)} districts (constitution: {cons.house_seats})")
        noncontig = [d.code for d in ds if not d.is_contiguous]
        if noncontig:
            problems.append(f"non-contiguous districts: {noncontig}")
        return {
            "data_category": FICTIONAL,
            "id": plan.id,
            "name": plan.name,
            "chamber": plan.chamber,
            "year": plan.year,
            "vintage_id": plan.vintage_id,
            "apportionment_id": plan.apportionment_id,
            "seed": int(plan.seed),
            "method": plan.method,
            "config_hash": plan.config_hash,
            "config": config,
            "is_active": bool(plan.is_active),
            "created_at": iso(plan.created_at),
            "generation_seconds": rnd(plan.generation_seconds, 2),
            "overrides_applied": plan.overrides_applied,
            "notes": plan.notes,
            "generation_run": run,
            "validation": {
                "ok": not problems,
                "problems": problems,
                "total_districts": plan.total_districts,
                "noncontiguous_districts": plan.noncontiguous_districts,
                "split_municipalities": plan.split_municipalities,
                "warnings": (run or {}).get("summary", {}).get("warnings"),
            },
            "deviation": {
                "max_abs_pct": rnd(plan.max_abs_deviation_pct, 4),
                "mean_abs_pct": rnd(plan.mean_abs_deviation_pct, 4),
                "min_pct": rnd(min(devs), 4) if devs else None,
                "max_pct": rnd(max(devs), 4) if devs else None,
                "stdev_pct": rnd(statistics.pstdev(devs), 4) if len(devs) > 1 else None,
            },
            "compactness": {
                "mean_polsby_popper": rnd(statistics.fmean(pp), 4) if pp else None,
                "min_polsby_popper": rnd(min(pp), 4) if pp else None,
            },
            "provinces": [
                {
                    "code": code,
                    "name": pmap[code]["name"],
                    "districts": len(lst),
                    "population": int(sum(d.population for d in lst)),
                    "target_population": rnd(lst[0].target_population, 1) if lst else None,
                    "max_abs_deviation_pct": rnd(max(abs(d.deviation_pct) for d in lst), 4),
                    "split_municipalities": int(sum(d.n_split_municipalities for d in lst)),
                }
                for code, lst in sorted(by_prov.items(), key=lambda kv: pmap[kv[0]]["sort_order"])
            ],
        }

    return cached(session, "plan", (plan.id, iso(plan.created_at)), build)


def district_detail(session: Session, code: str, plan_id: int | None = None) -> dict[str, Any]:
    """``GET /api/districts/{code}`` — statistics, municipal fragments, neighbours, the serving
    member and the House results of this district code in every reported election."""
    plan = _plan(session, plan_id)
    ids, pmap = province_maps(session)
    d = session.scalar(
        select(HouseDistrict).where(HouseDistrict.plan_id == plan.id, HouseDistrict.code == code.upper())
    )
    if d is None:
        raise NotFoundError(f"district {code!r} not found in plan {plan.id}")
    holder = current_holders(session, prefix=f"HOUSE-{d.code}").get(f"HOUSE-{d.code}")
    out = _district_row(d, ids[d.province_id], holder)
    out["province_name"] = pmap[ids[d.province_id]]["name"]
    out["municipalities"] = [
        {
            "code": m.cbs_code,
            "name": m.name,
            "population": dm.population,
            "unit_count": dm.n_units,
            "share_of_municipality": rnd(dm.share_of_municipality, 4),
            "share_of_district": rnd(dm.share_of_district, 4),
        }
        for dm, m in session.execute(
            select(DistrictMunicipality, Municipality)
            .join(Municipality, Municipality.id == DistrictMunicipality.municipality_id)
            .where(DistrictMunicipality.district_id == d.id)
            .order_by(DistrictMunicipality.population.desc())
        ).all()
    ]
    neigh = []
    for adj in session.scalars(
        select(DistrictAdjacency).where(
            (DistrictAdjacency.district_a_id == d.id) | (DistrictAdjacency.district_b_id == d.id)
        )
    ):
        other = adj.district_b_id if adj.district_a_id == d.id else adj.district_a_id
        neigh.append((other, adj))
    other_rows = (
        {
            h.id: h
            for h in session.scalars(select(HouseDistrict).where(HouseDistrict.id.in_([o for o, _ in neigh])))
        }
        if neigh
        else {}
    )
    out["neighbours"] = sorted(
        (
            {
                "code": other_rows[o].code,
                "name": other_rows[o].name,
                "shared_border_km": rnd(adj.shared_border_km, 3),
                "via_water_link": bool(adj.via_water_link),
            }
            for o, adj in neigh
            if o in other_rows
        ),
        key=lambda r: r["code"],
    )
    out["history"] = house_history(session, d.code)
    out["data_category"] = FICTIONAL
    out["provenance"] = DISTRICT_PROVENANCE
    out["plan"] = {"id": plan.id, "name": plan.name, "seed": int(plan.seed)}
    return out


def house_history(session: Session, district_code: str) -> list[dict[str, Any]]:
    """Reported House results of a district code across elections (the same code may cover a
    different territory after a redistricting)."""
    pcode = party_codes_by_id(session)
    rows = session.execute(
        select(Race, Election, BallotCandidate)
        .join(Election, Election.id == Race.election_id)
        .outerjoin(BallotCandidate, BallotCandidate.id == Race.winner_ballot_candidate_id)
        .where(Race.code == f"HOUSE-{district_code}", Election.status.in_(list(REPORTED_STATUSES)))
        .order_by(Election.election_date)
    ).all()
    out = []
    for r, el, win in rows:
        out.append(
            {
                "election_id": el.id,
                "year": el.year,
                "race_code": r.code,
                "winner": None if win is None else win.ballot_name,
                "winner_party": pcode.get(r.winner_party_id) if r.winner_party_id else None,
                "margin_pp": rnd(r.margin_pct, 3),
                "turnout_pct": rnd(r.turnout_pct, 3),
                "flipped": r.flipped,
                "incumbent_party": pcode.get(r.incumbent_party_id) if r.incumbent_party_id else None,
                "open_seat": bool(r.is_open_seat),
                "district_plan_id": el.district_plan_id,
                "data_category": SIMULATED,
            }
        )
    return out


def senate_seats(session: Session) -> dict[str, Any]:
    """``GET /api/senate/seats`` — the 24 Senate seats: province, seat number, class, current
    holder and the year of the seat's next regular election."""
    ids, pmap = province_maps(session)
    holders = current_holders(session, prefix="SEN-")
    cal = ElectionCalendar.from_config()
    last = session.scalar(
        select(Election.year)
        .where(Election.status.in_(list(REPORTED_STATUSES)))
        .order_by(Election.election_date.desc())
        .limit(1)
    )
    after = int(last) if last is not None else None
    seats = []
    for s in session.scalars(select(SenateSeat)):
        nxt = None
        if after is not None:
            nxt = cal.next_senate_election(s.senate_class, after)
        seats.append(
            {
                "code": s.code,
                "province_code": ids[s.province_id],
                "province_name": pmap[ids[s.province_id]]["name"],
                "seat_number": s.seat_number,
                "senate_class": s.senate_class,
                "holder": holders.get(s.code),
                "next_election_year": nxt,
            }
        )
    seats.sort(key=lambda r: (pmap[r["province_code"]]["sort_order"], r["seat_number"]))
    cons = get_constitution()
    comp: dict[str, int] = {}
    for s in seats:
        party = (s["holder"] or {}).get("party") or ("vacant" if s["holder"] is None else "independent")
        comp[party] = comp.get(party, 0) + 1
    return {
        "data_category": FICTIONAL,
        "provenance": {"seats": FICTIONAL, "holder": SIMULATED},
        "seats_total": cons.senate_seats,
        "majority": cons.senate_majority,
        "label": f"{cons.senate_majority} FOR CONTROL",
        "classes": {
            str(c): sum(1 for s in seats if s["senate_class"] == c) for c in range(1, cons.senate_classes + 1)
        },
        "as_of_year": after,
        "composition": dict(sorted(comp.items(), key=lambda kv: (-kv[1], kv[0]))),
        "seats": seats,
    }
