"""Persistence round-trip: apportionment, plan, mapping, Senate seats (in-memory SQLite)."""

from __future__ import annotations

from collections import Counter

import numpy as np
import pytest
import shapely
from sqlalchemy import func, select

from app.core.constitution import ELECTORAL_VOTES, HOUSE_SEATS, SENATE_SEATS, OfficeType
from app.core.errors import DistrictingError, NotFoundError
from app.districts.service import (
    active_plan,
    apportionment_seats,
    create_apportionment,
    ensure_senate_seats,
    load_plan_mapping,
    store_plan,
)
from app.districts.stats import district_municipality_fragments
from app.geography.synthetic import frame_from_tables
from app.models import (
    Apportionment,
    DistrictAdjacency,
    DistrictAssignment,
    DistrictMunicipality,
    DistrictPlan,
    GeoUnit,
    GeoVintage,
    HouseDistrict,
    Municipality,
    Office,
    Province,
    SenateSeat,
)


def _load_geography(session, geo) -> GeoVintage:  # type: ignore[no-untyped-def]
    for i, row in enumerate(geo.provinces.itertuples()):
        session.add(Province(code=row.code, cbs_code=row.cbs_code, name=row.name, sort_order=i + 1))
    vintage = GeoVintage(year=2025, label="synthetic")
    session.add(vintage)
    session.flush()
    pid = dict(session.execute(select(Province.code, Province.id)).tuples().all())
    munis = geo.units.groupby("municipality_code").agg(
        name=("municipality_name", "first"),
        province_code=("province_code", "first"),
        population=("population", "sum"),
        area=("land_area_km2", "sum"),
        x=("centroid_x", "mean"),
        y=("centroid_y", "mean"),
    )
    for code, r in munis.iterrows():
        session.add(
            Municipality(
                vintage_id=vintage.id,
                cbs_code=code,
                name=r["name"],
                province_id=pid[r["province_code"]],
                population=int(r["population"]),
                area_km2=float(r["area"]),
                land_area_km2=float(r["area"]),
                density=float(r["population"] / r["area"]),
                centroid_lon=5.0,
                centroid_lat=52.0,
                centroid_x=float(r["x"]),
                centroid_y=float(r["y"]),
            )
        )
    session.flush()
    mid = dict(session.execute(select(Municipality.cbs_code, Municipality.id)).tuples().all())
    session.execute(
        GeoUnit.__table__.insert(),
        [
            {
                "vintage_id": vintage.id,
                "cbs_code": r.code,
                "name": r.name,
                "unit_kind": "buurt",
                "wijk_code": r.wijk_code,
                "municipality_id": mid[r.municipality_code],
                "province_id": pid[r.province_code],
                "population": int(r.population),
                "eligible_voters_est": int(r.eligible_voters_est),
                "area_km2": float(r.area_km2),
                "land_area_km2": float(r.land_area_km2),
                "density": float(r.density),
                "urbanity_class": int(r.urbanity_class),
                "centroid_lon": float(r.centroid_lon),
                "centroid_lat": float(r.centroid_lat),
                "centroid_x": float(r.centroid_x),
                "centroid_y": float(r.centroid_y),
            }
            for r in geo.units.itertuples()
        ],
    )
    session.flush()
    return vintage


def test_round_trip(db_session, fine_geo, fine_plan) -> None:  # type: ignore[no-untyped-def]
    s = db_session
    vintage = _load_geography(s, fine_geo)
    pops = fine_geo.units.groupby("province_code")["population"].sum().astype(int).to_dict()
    # ---- apportionment (idempotent per vintage + method) -------------------------------
    appt = create_apportionment(s, vintage.id, pops)
    assert appt.method == "huntington_hill" and appt.total_seats == HOUSE_SEATS
    assert appt.total_electoral_votes == ELECTORAL_VOTES
    assert len(appt.seats) == 12 and sum(x.seats for x in appt.seats) == HOUSE_SEATS
    assert sum(x.electoral_votes for x in appt.seats) == ELECTORAL_VOTES
    assert create_apportionment(s, vintage.id, pops).id == appt.id
    assert apportionment_seats(s, appt.id) == fine_geo.seats
    ham = create_apportionment(s, vintage.id, pops, method="hamilton", set_active=False)
    assert ham.id != appt.id and not ham.is_active and appt.is_active
    assert s.scalar(select(func.count()).select_from(Apportionment)) == 2
    # ---- plan ---------------------------------------------------------------------------
    plan_row = store_plan(s, fine_plan, vintage.id, appt.id, 2028, fine_geo.units)
    assert plan_row.total_districts == HOUSE_SEATS and plan_row.is_active
    assert plan_row.config_hash == fine_plan.config_hash and plan_row.seed == fine_plan.seed
    assert s.scalar(select(func.count()).select_from(HouseDistrict)) == HOUSE_SEATS
    assert s.scalar(select(func.count()).select_from(DistrictAssignment)) == len(fine_geo.units)
    n_frag = len(district_municipality_fragments(fine_plan))
    assert s.scalar(select(func.count()).select_from(DistrictMunicipality)) == n_frag
    assert s.scalar(select(func.count()).select_from(DistrictAdjacency)) > HOUSE_SEATS
    d = s.scalars(select(HouseDistrict).where(HouseDistrict.code == fine_plan.district_codes[0])).one()
    assert d.population == int(fine_plan.district_population()[0])
    assert d.geometry_wkb is not None and shapely.from_wkb(d.geometry_wkb).is_valid
    assert d.polsby_popper is not None and 0 < d.polsby_popper <= 1
    assert active_plan(s).id == plan_row.id
    # ---- storing another plan deactivates the previous one ----------------------------
    second = store_plan(s, fine_plan, vintage.id, appt.id, 2030, fine_geo.units, name="second")
    s.refresh(plan_row)
    assert not plan_row.is_active and second.is_active
    assert active_plan(s).id == second.id
    assert active_plan(s, chamber="provincial_legislature") is None
    # ---- mapping aligned to a frame -----------------------------------------------------
    frame = frame_from_tables(fine_geo.units, fine_geo.municipalities, fine_geo.provinces, year=2025)
    mapping = load_plan_mapping(s, second.id, frame)
    assert (mapping.unit_district >= 0).all()
    assert mapping.n_districts == HOUSE_SEATS
    got = np.asarray(mapping.district_codes, dtype=object)[mapping.unit_district]
    want = dict(zip(fine_plan.unit_codes, fine_plan.unit_district_codes(), strict=True))
    assert all(got[i] == want[c] for i, c in enumerate(frame.unit_codes))
    assert [frame.province_codes[i] for i in mapping.district_province] == [
        c[:2] for c in mapping.district_codes
    ]
    code = mapping.district_codes[5]
    assert len(mapping.units_of(code)) == int((fine_plan.unit_district_codes() == code).sum())
    assert s.scalar(select(func.count()).select_from(DistrictPlan)) == 2
    with pytest.raises(NotFoundError):
        load_plan_mapping(s, 999, frame)


def test_store_plan_requires_units(db_session, fine_geo, fine_plan) -> None:  # type: ignore[no-untyped-def]
    for i, row in enumerate(fine_geo.provinces.itertuples()):
        db_session.add(Province(code=row.code, cbs_code=row.cbs_code, name=row.name, sort_order=i + 1))
    v = GeoVintage(year=2025, label="empty")
    db_session.add(v)
    db_session.flush()
    with pytest.raises(DistrictingError, match="not in geo_unit"):
        store_plan(db_session, fine_plan, v.id, None, 2028, fine_geo.units)


def test_senate_seats(db_session, fine_geo) -> None:  # type: ignore[no-untyped-def]
    for i, row in enumerate(fine_geo.provinces.itertuples()):
        db_session.add(Province(code=row.code, cbs_code=row.cbs_code, name=row.name, sort_order=i + 1))
    db_session.flush()
    seats = ensure_senate_seats(db_session)
    assert len(seats) == SENATE_SEATS
    assert Counter(x.senate_class for x in seats) == {1: 8, 2: 8, 3: 8}
    assert seats[0].code == "SEN-GR-1"
    by_prov: dict[int, set[int]] = {}
    for x in seats:
        by_prov.setdefault(x.province_id, set()).add(x.senate_class)
    assert all(len(v) == 2 for v in by_prov.values())
    offices = db_session.scalars(select(Office).where(Office.office_type == OfficeType.SENATE.value)).all()
    assert len(offices) == SENATE_SEATS
    assert {o.code for o in offices} == {x.code for x in seats}
    assert all(o.term_years == 6 and o.senate_class in (1, 2, 3) for o in offices)
    again = ensure_senate_seats(db_session)
    assert [x.id for x in again] == [x.id for x in seats]
    assert db_session.scalar(select(func.count()).select_from(SenateSeat)) == SENATE_SEATS
    assert db_session.scalar(select(func.count()).select_from(Office)) == SENATE_SEATS
