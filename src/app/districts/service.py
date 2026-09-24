"""Persistence of apportionments, House district plans and Senate seats (SQLAlchemy layer).

This is the only module of :mod:`app.districts` that touches the database.  It translates the pure
engine results (:class:`~app.districts.apportionment.ApportionmentResult`,
:class:`~app.districts.plan.GeneratedPlan`) into ORM rows and back
(:func:`load_plan_mapping` → :class:`PlanMapping` aligned to a
:class:`~app.geography.frame.GeographyFrame`).  Callers own the transaction (commit/rollback).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.core.config import get_constitution
from app.core.constitution import ConstitutionConfig, OfficeType
from app.core.errors import ApportionmentError, DistrictingError, NotFoundError
from app.core.logging import Timer, get_logger
from app.districts.apportionment import ApportionmentResult, apportion, canonical_method
from app.districts.plan import SOURCE_GENERATED, SOURCE_OVERRIDE, GeneratedPlan
from app.districts.senate import SenateConfig, assign_senate_classes, senate_seat_code
from app.districts.stats import (
    district_adjacency,
    district_geometries,
    district_municipality_fragments,
    district_stats,
    geometries_wkb,
    has_geometry,
)
from app.geography.frame import GeographyFrame
from app.models import (
    Apportionment,
    ApportionmentSeat,
    DistrictAdjacency,
    DistrictAssignment,
    DistrictMunicipality,
    DistrictPlan,
    GeoUnit,
    HouseDistrict,
    Municipality,
    Office,
    Province,
    SenateSeat,
)

log = get_logger(__name__)

_BATCH = 5000


# =========================================================================== apportionment
def _province_ids(session: Session) -> dict[str, int]:
    return {code: pid for pid, code in session.execute(select(Province.id, Province.code))}


def create_apportionment(
    session: Session,
    vintage_id: int,
    populations_by_code: Mapping[str, int],
    method: str | None = None,
    constitution: ConstitutionConfig | None = None,
    *,
    set_active: bool = True,
    notes: str | None = None,
) -> Apportionment:
    """Compute and store the House apportionment of a geography vintage.

    Idempotent per (vintage, method): an existing apportionment with the same population basis is
    returned unchanged; one with a different basis is recomputed in place.  With ``set_active``
    the apportionment becomes the vintage's active one (others are deactivated).

    Raises:
        NotFoundError: a province is not in the database.
        ApportionmentError: not exactly the constitution's provinces, or an invalid apportionment.
    """
    cons = constitution or get_constitution()
    meth = canonical_method(method or cons.apportionment_method)
    pids = _province_ids(session)
    unknown = sorted(set(populations_by_code) - set(pids))
    if unknown:
        raise NotFoundError(f"provinces not in the database: {unknown}")
    if len(populations_by_code) != cons.province_count:
        # a partial apportionment would silently change the EV total (seats + 2 × provinces)
        raise ApportionmentError(
            f"apportionment needs the populations of all {cons.province_count} provinces, "
            f"got {len(populations_by_code)}"
        )
    result = apportion(
        populations_by_code, cons.house_seats, meth, cons.min_house_seats_per_province, constitution=cons
    )
    existing = session.scalars(
        select(Apportionment).where(Apportionment.vintage_id == vintage_id, Apportionment.method == meth)
    ).first()
    wanted = {pids[c]: (result.populations[c], result.seats[c]) for c in result.province_codes}
    if existing is not None:
        have = {s.province_id: (s.population, s.seats) for s in existing.seats}
        same = (
            have == wanted
            and existing.total_seats == result.total_seats
            and existing.min_seats_per_province == result.min_seats
            and existing.senators_per_province == result.senators_per_province
        )
        if same:
            appt = existing
        else:
            log.warning(
                "apportionment %s of vintage %s recomputed (population basis changed)", meth, vintage_id
            )
            existing.seats.clear()
            session.flush()
            appt = existing
            _fill_apportionment(appt, result, pids)
    else:
        appt = Apportionment(
            vintage_id=vintage_id,
            method=meth,
            population_basis="population",
            notes=notes,
            is_active=bool(set_active),
        )
        _fill_apportionment(appt, result, pids)
        session.add(appt)
    session.flush()
    if set_active:
        session.execute(
            update(Apportionment)
            .where(Apportionment.vintage_id == vintage_id, Apportionment.id != appt.id)
            .values(is_active=False)
        )
        appt.is_active = True
    session.flush()
    return appt


def _fill_apportionment(appt: Apportionment, result: ApportionmentResult, pids: Mapping[str, int]) -> None:
    appt.total_seats = result.total_seats
    appt.min_seats_per_province = result.min_seats
    appt.senators_per_province = result.senators_per_province
    appt.total_electoral_votes = result.total_electoral_votes
    for c in result.province_codes:
        appt.seats.append(
            ApportionmentSeat(
                province_id=pids[c],
                population=int(result.populations[c]),
                quota=float(result.quotas[c]),
                seats=int(result.seats[c]),
                electoral_votes=int(result.electoral_votes[c]),
                persons_per_seat=float(result.persons_per_seat[c]),
            )
        )


def apportionment_seats(session: Session, apportionment_id: int) -> dict[str, int]:
    """Province code → House seats of a stored apportionment (canonical province order)."""
    rows = session.execute(
        select(Province.code, ApportionmentSeat.seats)
        .join(Province, Province.id == ApportionmentSeat.province_id)
        .where(ApportionmentSeat.apportionment_id == apportionment_id)
        .order_by(Province.sort_order, Province.code)
    ).all()
    if not rows:
        raise NotFoundError(f"apportionment {apportionment_id} has no seats")
    return {code: int(seats) for code, seats in rows}


# =========================================================================== plans
def store_plan(
    session: Session,
    plan: GeneratedPlan,
    vintage_id: int,
    apportionment_id: int | None,
    year: int | None,
    units_gdf: pd.DataFrame,
    set_active: bool = True,
    *,
    name: str | None = None,
    chamber: str = "house",
    simplify_tolerance_m: float = 80.0,
) -> DistrictPlan:
    """Persist a generated plan: ``DistrictPlan`` + ``HouseDistrict`` (stats, WKB geometry) +
    ``DistrictAssignment`` (bulk) + ``DistrictMunicipality`` + ``DistrictAdjacency``.

    With ``set_active`` the previously active plan of the same chamber is deactivated.  Requires
    the ``Province``, ``GeoUnit`` and ``Municipality`` rows of the vintage.

    Raises:
        DistrictingError: plan units or municipalities missing from the database.
    """
    pids = _province_ids(session)
    missing_p = sorted(set(plan.district_province) - set(pids))
    if missing_p:
        raise DistrictingError(f"provinces not in the database: {missing_p}")
    unit_ids = dict(
        session.execute(select(GeoUnit.cbs_code, GeoUnit.id).where(GeoUnit.vintage_id == vintage_id))
        .tuples()
        .all()
    )
    muni_ids = dict(
        session.execute(
            select(Municipality.cbs_code, Municipality.id).where(Municipality.vintage_id == vintage_id)
        )
        .tuples()
        .all()
    )
    unit_codes = plan.unit_codes.astype(str)
    uid = pd.Series(unit_ids, dtype="float64").reindex(unit_codes).to_numpy()
    if np.isnan(uid).any():
        n = int(np.isnan(uid).sum())
        raise DistrictingError(
            f"{n} plan units are not in geo_unit for vintage {vintage_id} (e.g. {unit_codes[np.isnan(uid)][:3].tolist()})"
        )
    with Timer(log, "store plan: statistics and geometry"):
        geoms = district_geometries(plan, units_gdf) if has_geometry(units_gdf) else None  # type: ignore[arg-type]
        stats = district_stats(plan, units_gdf, geoms)
        wkb = geometries_wkb(geoms, simplify_tolerance_m) if geoms is not None else {}
        frags = district_municipality_fragments(plan)
        adj = district_adjacency(plan)
    missing_m = sorted(set(frags["municipality_code"]) - set(muni_ids))
    if missing_m:
        raise DistrictingError(
            f"municipalities not in the database for vintage {vintage_id}: {missing_m[:5]}"
        )
    summary = plan.summary()
    dp = DistrictPlan(
        name=(name or f"House plan {year or ''} seed {plan.seed}".replace("  ", " "))[:120],
        chamber=chamber,
        vintage_id=vintage_id,
        apportionment_id=apportionment_id,
        year=year,
        seed=int(plan.seed),
        method=plan.method,
        config_json=plan.config_json,
        config_hash=plan.config_hash,
        total_districts=plan.n_districts,
        max_abs_deviation_pct=float(summary["max_abs_deviation_pct"]),
        mean_abs_deviation_pct=float(summary["mean_abs_deviation_pct"]),
        split_municipalities=int(summary["split_municipalities"]),
        noncontiguous_districts=int(summary["noncontiguous_districts"]),
        overrides_applied=plan.overrides_applied,
        is_active=bool(set_active),
        is_fictional=True,
        generation_seconds=float(plan.timings.get("total", 0.0)) or None,
        notes=json.dumps(
            {"seats_by_province": plan.seats_by_province, "warnings": plan.warnings[:200]}, ensure_ascii=False
        ),
    )
    session.add(dp)
    session.flush()
    with Timer(log, "store plan: rows"):
        districts: list[HouseDistrict] = []
        for rec in stats.to_dict(orient="records"):
            districts.append(
                HouseDistrict(
                    plan_id=dp.id,
                    code=rec["code"],
                    province_id=pids[rec["province_code"]],
                    number=int(rec["number"]),
                    name=str(rec["name"])[:160],
                    population=int(rec["population"]),
                    eligible_voters_est=int(rec["eligible_voters_est"]),
                    target_population=float(rec["target_population"]),
                    deviation_pct=float(rec["deviation_pct"]),
                    area_km2=_num(rec["area_km2"], 0.0),
                    polsby_popper=_num(rec["polsby_popper"]),
                    reock=_num(rec["reock"]),
                    convex_hull_ratio=_num(rec["convex_hull_ratio"]),
                    n_units=int(rec["n_units"]),
                    n_municipalities=int(rec["n_municipalities"]),
                    n_split_municipalities=int(rec["n_split_municipalities"]),
                    urban_share=_num(rec["urban_share"]),
                    rural_share=_num(rec["rural_share"]),
                    is_contiguous=bool(rec["is_contiguous"]),
                    n_components=int(rec["n_components"]),
                    centroid_lon=_num(rec["centroid_lon"], 0.0),
                    centroid_lat=_num(rec["centroid_lat"], 0.0),
                    geometry_wkb=wkb.get(rec["code"]),
                )
            )
        session.add_all(districts)
        session.flush()
        did = np.asarray([d.id for d in districts], dtype=np.int64)
        code_to_id = {d.code: d.id for d in districts}
        sources = np.where(plan.unit_overridden, SOURCE_OVERRIDE, SOURCE_GENERATED)
        rows = [
            {"plan_id": dp.id, "geo_unit_id": int(u), "district_id": int(d), "source": str(s)}
            for u, d, s in zip(uid.astype(np.int64), did[plan.unit_district], sources, strict=True)
        ]
        _bulk_insert(session, DistrictAssignment, rows)
        _bulk_insert(
            session,
            DistrictMunicipality,
            [
                {
                    "district_id": code_to_id[r["district_code"]],
                    "municipality_id": muni_ids[r["municipality_code"]],
                    "population": int(r["population"]),
                    "n_units": int(r["n_units"]),
                    "share_of_municipality": float(r["share_of_municipality"]),
                    "share_of_district": float(r["share_of_district"]),
                }
                for r in frags.to_dict(orient="records")
            ],
        )
        _bulk_insert(
            session,
            DistrictAdjacency,
            [
                {
                    "plan_id": dp.id,
                    "district_a_id": code_to_id[r["district_a"]],
                    "district_b_id": code_to_id[r["district_b"]],
                    "shared_border_km": float(r["shared_border_km"]),
                    "via_water_link": bool(r["via_water_link"]),
                }
                for r in adj.to_dict(orient="records")
            ],
        )
    if set_active:
        session.execute(
            update(DistrictPlan)
            .where(DistrictPlan.chamber == chamber, DistrictPlan.id != dp.id)
            .values(is_active=False)
        )
    session.flush()
    log.info("stored district plan %s (%d districts, %d assignments)", dp.id, len(districts), len(rows))
    return dp


def _num(v: object, default: float | None = None) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return f if np.isfinite(f) else default


def _bulk_insert(session: Session, model: type, rows: list[dict]) -> None:
    for i in range(0, len(rows), _BATCH):
        chunk = rows[i : i + _BATCH]
        if chunk:
            session.execute(insert(model), chunk)


def active_plan(session: Session, chamber: str = "house") -> DistrictPlan | None:
    """The active plan of ``chamber`` (most recent if several are flagged active)."""
    return session.scalars(
        select(DistrictPlan)
        .where(DistrictPlan.chamber == chamber, DistrictPlan.is_active.is_(True))
        .order_by(DistrictPlan.created_at.desc(), DistrictPlan.id.desc())
        .limit(1)
    ).first()


@dataclass
class PlanMapping:
    """A stored plan aligned to a :class:`GeographyFrame` (engine-ready)."""

    plan_id: int
    unit_district: np.ndarray  #: (U,) int64 district index per frame unit (−1 = not in the plan)
    district_codes: list[str]
    district_ids: np.ndarray  #: (D,) int64 ``house_district.id``
    district_province: np.ndarray  #: (D,) int64 index into ``frame.province_codes``
    district_names: list[str]

    @property
    def n_districts(self) -> int:
        return len(self.district_codes)

    def district_index(self, code: str) -> int:
        return self.district_codes.index(code)

    def units_of(self, code: str) -> np.ndarray:
        """Sorted frame unit indices of district ``code``."""
        return np.flatnonzero(self.unit_district == self.district_index(code))


def load_plan_mapping(session: Session, plan_id: int, frame: GeographyFrame) -> PlanMapping:
    """Load a plan's unit → district assignment aligned to ``frame``.

    Districts are ordered by the frame's province order and then by district number.  Frame units
    absent from the plan get −1 (logged); plan units absent from the frame are ignored (logged).
    """
    if session.get(DistrictPlan, plan_id) is None:
        raise NotFoundError(f"district plan {plan_id} not found")
    drows = session.execute(
        select(HouseDistrict.id, HouseDistrict.code, HouseDistrict.number, HouseDistrict.name, Province.code)
        .join(Province, Province.id == HouseDistrict.province_id)
        .where(HouseDistrict.plan_id == plan_id)
    ).all()
    p_index = {c: i for i, c in enumerate(frame.province_codes)}
    drows = sorted(drows, key=lambda r: (p_index.get(r[4], len(p_index)), r[2], r[1]))
    unknown = sorted({r[4] for r in drows} - set(p_index))
    if unknown:
        raise DistrictingError(f"plan {plan_id} has districts in provinces absent from the frame: {unknown}")
    d_ids = np.asarray([r[0] for r in drows], dtype=np.int64)
    d_index = {int(i): k for k, i in enumerate(d_ids.tolist())}
    arows = session.execute(
        select(GeoUnit.cbs_code, DistrictAssignment.district_id)
        .join(GeoUnit, GeoUnit.id == DistrictAssignment.geo_unit_id)
        .where(DistrictAssignment.plan_id == plan_id)
    ).all()
    unit_district = np.full(frame.n_units, -1, dtype=np.int64)
    if arows:
        codes = [r[0] for r in arows]
        didx = np.asarray([d_index[int(r[1])] for r in arows], dtype=np.int64)
        pos = pd.Index(frame.unit_codes).get_indexer(codes)
        ok = pos >= 0
        if (~ok).any():
            log.warning("plan %s: %d assigned units are not in the frame", plan_id, int((~ok).sum()))
        unit_district[pos[ok]] = didx[ok]
    n_missing = int((unit_district < 0).sum())
    if n_missing:
        log.warning("plan %s: %d frame units have no district", plan_id, n_missing)
    return PlanMapping(
        plan_id=plan_id,
        unit_district=unit_district,
        district_codes=[r[1] for r in drows],
        district_ids=d_ids,
        district_province=np.asarray([p_index[r[4]] for r in drows], dtype=np.int64),
        district_names=[r[3] or r[1] for r in drows],
    )


# =========================================================================== Senate
def ensure_senate_seats(
    session: Session,
    config: SenateConfig | None = None,
    constitution: ConstitutionConfig | None = None,
) -> list[SenateSeat]:
    """Create (idempotently) the Senate seats and their ``Office`` rows.

    Every province gets ``senators_per_province`` seats ``SEN-XX-n`` in the classes of
    :func:`~app.districts.senate.assign_senate_classes`, each with an ``Office`` (office_type
    SENATE, same code).  Existing rows are reused; a changed class assignment is updated.
    Returns the seats in province order, then seat number.
    """
    cons = constitution or get_constitution()
    provinces = session.scalars(select(Province).order_by(Province.sort_order, Province.code)).all()
    if not provinces:
        raise NotFoundError("no provinces in the database")
    if len(provinces) != cons.province_count:
        raise ApportionmentError(
            f"{len(provinces)} provinces in the database, constitution expects {cons.province_count}"
        )
    mapping = assign_senate_classes([p.code for p in provinces], config, constitution=cons)
    seats = {(s.province_id, s.seat_number): s for s in session.scalars(select(SenateSeat))}
    offices = {
        o.code: o
        for o in session.scalars(select(Office).where(Office.office_type == OfficeType.SENATE.value))
    }
    out: list[SenateSeat] = []
    for p in provinces:
        for n, cl in enumerate(mapping[p.code], start=1):
            code = senate_seat_code(p.code, n)
            office = offices.get(code)
            if office is None:
                office = Office(
                    office_type=OfficeType.SENATE.value,
                    code=code,
                    name=f"Senator for {p.name} (seat {n})",
                    province_id=p.id,
                    senate_class=int(cl),
                    term_years=cons.senate_term_years,
                    is_active=True,
                )
                session.add(office)
                session.flush()
                offices[code] = office
            elif office.senate_class != int(cl):
                log.warning("Senate office %s moved from class %s to %s", code, office.senate_class, cl)
                office.senate_class = int(cl)
            seat = seats.get((p.id, n))
            if seat is None:
                seat = SenateSeat(
                    code=code, province_id=p.id, seat_number=n, senate_class=int(cl), office_id=office.id
                )
                session.add(seat)
                seats[(p.id, n)] = seat
            else:
                if seat.senate_class != int(cl):
                    log.warning("Senate seat %s moved from class %s to %s", code, seat.senate_class, cl)
                    seat.senate_class = int(cl)
                if seat.office_id != office.id:
                    seat.office_id = office.id
            out.append(seat)
    session.flush()
    return out
