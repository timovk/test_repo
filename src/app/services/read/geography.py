"""REAL geography read models (provinces, municipalities, neighbourhoods) plus the FICTIONAL
apportionment built on it, and the GeoJSON layers served to the map.

Everything geographic here is REAL CBS/PDOK data or DERIVED from it (population aggregates,
estimated eligible voters, imputed demographic gaps — flagged); House seats, electoral votes,
districts and office holders are FICTIONAL.  Payloads carry a ``provenance`` map per field group.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import shapely
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_constitution
from app.core.errors import DataNotPreparedError, NotFoundError
from app.core.logging import Timer, get_logger
from app.core.settings import get_settings
from app.models import (
    Apportionment,
    ApportionmentSeat,
    DistrictAssignment,
    DistrictMunicipality,
    DistrictPlan,
    GeoUnit,
    GeoUnitDemographics,
    GeoVintage,
    HouseDistrict,
    Legislature,
    Municipality,
    MunicipalityDemographics,
    MunicipalityLineage,
    Province,
    ProvinceStats,
    SenateSeat,
)
from app.services._common import GEOGRAPHY_SOURCE_KEY, active_vintage, get_meta, loads
from app.services.read._base import (
    DERIVED,
    FICTIONAL,
    REAL,
    cached,
    current_holders,
    iso,
    province_maps,
    rnd,
)

log = get_logger(__name__)

#: Demographic indicator columns exposed by the API (REAL CBS; imputed ones listed per row).
DEMOGRAPHIC_FIELDS: tuple[str, ...] = (
    "pct_age_0_15",
    "pct_age_15_25",
    "pct_age_25_45",
    "pct_age_45_65",
    "pct_age_65_plus",
    "pct_single_households",
    "pct_households_with_children",
    "avg_household_size",
    "pct_origin_nl",
    "pct_origin_europe",
    "pct_origin_non_europe",
    "pct_education_low",
    "pct_education_mid",
    "pct_education_high",
    "income_per_capita_keur",
    "pct_owner_occupied",
)

GEO_PROVENANCE: dict[str, str] = {
    "code": REAL,
    "name": REAL,
    "population": REAL,
    "population_official": REAL,
    "area_km2": REAL,
    "land_area_km2": REAL,
    "density": DERIVED,
    "urbanity_class": REAL,
    "address_density": REAL,
    "eligible_voters_est": DERIVED,
    "demographics": REAL,
    "imputed_fields": DERIVED,
    "centroid": DERIVED,
    "house_seats": FICTIONAL,
    "electoral_votes": FICTIONAL,
    "senators": FICTIONAL,
    "districts": FICTIONAL,
    "governor": FICTIONAL,
    "mayor": FICTIONAL,
    "council_seats": FICTIONAL,
}

#: GeoJSON layers served by ``/api/geo/{layer}.geojson``.
GEO_LAYERS: tuple[str, ...] = ("provinces", "municipalities", "districts", "units/<PV>")


def _vintage(session: Session) -> GeoVintage:
    v = active_vintage(session)
    if v is None:
        raise DataNotPreparedError("no geography loaded in the database (run the setup first)")
    return v


def _active_apportionment(session: Session, vintage_id: int) -> Apportionment | None:
    return session.scalars(
        select(Apportionment)
        .where(Apportionment.vintage_id == vintage_id, Apportionment.is_active.is_(True))
        .order_by(Apportionment.id.desc())
        .limit(1)
    ).first()


def _seats_by_province(session: Session, appt: Apportionment | None) -> dict[int, ApportionmentSeat]:
    if appt is None:
        return {}
    return {
        s.province_id: s
        for s in session.scalars(
            select(ApportionmentSeat).where(ApportionmentSeat.apportionment_id == appt.id)
        )
    }


def _demographics(row: Any) -> dict[str, Any]:
    if row is None:
        return {f: None for f in DEMOGRAPHIC_FIELDS}
    return {f: rnd(getattr(row, f), 3) for f in DEMOGRAPHIC_FIELDS}


def _imputed(row: Any) -> list[str]:
    text = getattr(row, "imputed_fields", None) if row is not None else None
    return [x for x in (text or "").split(",") if x]


# =========================================================================== provinces
def provinces(session: Session) -> dict[str, Any]:
    """``GET /api/provinces`` — the 12 provinces with REAL statistics and FICTIONAL seats / EV."""
    v = _vintage(session)

    def build() -> dict[str, Any]:
        appt = _active_apportionment(session, v.id)
        seats = _seats_by_province(session, appt)
        cons = get_constitution()
        stats = {
            s.province_id: s
            for s in session.scalars(select(ProvinceStats).where(ProvinceStats.vintage_id == v.id))
        }
        governors = current_holders(session, prefix="GOV-")
        rows = []
        for p in session.scalars(select(Province).order_by(Province.sort_order, Province.code)):
            st = stats.get(p.id)
            seat = seats.get(p.id)
            rows.append(
                {
                    "code": p.code,
                    "cbs_code": p.cbs_code,
                    "name": p.name,
                    "name_en": p.name_en,
                    "capital": p.capital,
                    "population": None if st is None else st.population,
                    "population_official": None if st is None else st.population_official,
                    "eligible_voters_est": None if st is None else st.eligible_voters_est,
                    "area_km2": None if st is None else rnd(st.area_km2, 2),
                    "land_area_km2": None if st is None else rnd(st.land_area_km2, 2),
                    "density": None if st is None else rnd(st.density, 1),
                    "municipalities": None if st is None else st.municipality_count,
                    "unit_count": None if st is None else st.unit_count,
                    "centroid": None if st is None else [rnd(st.centroid_lon, 5), rnd(st.centroid_lat, 5)],
                    "house_seats": None if seat is None else seat.seats,
                    "senators": cons.senators_per_province,
                    "electoral_votes": None if seat is None else seat.electoral_votes,
                    "governor": governors.get(f"GOV-{p.code}"),
                }
            )
        return {
            "data_category": REAL,
            "provenance": GEO_PROVENANCE,
            "vintage": {"id": v.id, "year": v.year, "label": v.label},
            "apportionment_id": appt.id if appt is not None else None,
            "totals": {
                "population": int(sum(r["population"] or 0 for r in rows)),
                "house_seats": int(sum(r["house_seats"] or 0 for r in rows)),
                "electoral_votes": int(sum(r["electoral_votes"] or 0 for r in rows)),
            },
            "provinces": rows,
        }

    return cached(session, "provinces", (v.id, iso(v.built_at), _holders_token(session)), build)


def _holders_token(session: Session) -> tuple:
    """Cache token that changes whenever office holders change (new finalized elections)."""
    from sqlalchemy import func

    from app.models import OfficeHolder

    return tuple(session.execute(select(func.count(OfficeHolder.id), func.max(OfficeHolder.id))).one())


def province_detail(session: Session, code: str) -> dict[str, Any]:
    """``GET /api/provinces/{code}`` — statistics, population-weighted demographics, House
    districts, Senate seats and current governor / lieutenant governor."""
    v = _vintage(session)
    _ids, pmap = province_maps(session)
    code = code.upper()
    if code not in pmap:
        raise NotFoundError(f"province {code!r} not found")
    base = next(r for r in provinces(session)["provinces"] if r["code"] == code)
    pid = pmap[code]["id"]
    munis = session.execute(
        select(Municipality, MunicipalityDemographics)
        .outerjoin(MunicipalityDemographics, MunicipalityDemographics.municipality_id == Municipality.id)
        .where(Municipality.vintage_id == v.id, Municipality.province_id == pid)
        .order_by(Municipality.population.desc())
    ).all()
    demo = _weighted_demographics([(m.population, d) for m, d in munis])
    from app.districts.service import active_plan

    plan = active_plan(session)
    districts = []
    if plan is not None:
        holders = current_holders(session, prefix="HOUSE-")
        for d in session.scalars(
            select(HouseDistrict)
            .where(HouseDistrict.plan_id == plan.id, HouseDistrict.province_id == pid)
            .order_by(HouseDistrict.number)
        ):
            districts.append(
                {
                    "code": d.code,
                    "name": d.name,
                    "population": d.population,
                    "deviation_pct": rnd(d.deviation_pct, 3),
                    "holder": holders.get(f"HOUSE-{d.code}"),
                }
            )
    seat_holders = current_holders(session, prefix="SEN-")
    senate = [
        {
            "code": s.code,
            "seat_number": s.seat_number,
            "senate_class": s.senate_class,
            "holder": seat_holders.get(s.code),
        }
        for s in session.scalars(
            select(SenateSeat).where(SenateSeat.province_id == pid).order_by(SenateSeat.seat_number)
        )
    ]
    lt = current_holders(session, prefix=f"LTGOV-{code}")
    leg = session.scalar(
        select(Legislature).where(Legislature.level == "provincial", Legislature.jurisdiction_code == code)
    )
    return {
        **{k: base[k] for k in base},
        "data_category": REAL,
        "provenance": GEO_PROVENANCE,
        "demographics": demo,
        "demographics_provenance": DERIVED,
        "lieutenant_governor": lt.get(f"LTGOV-{code}"),
        "legislature": None
        if leg is None
        else {"name": leg.name, "seats": leg.seats, "electoral_system": leg.electoral_system},
        "districts": districts,
        "senate_seats": senate,
        "largest_municipalities": [
            {"code": m.cbs_code, "name": m.name, "population": m.population} for m, _d in munis[:10]
        ],
    }


def _weighted_demographics(rows: list[tuple[int, Any]]) -> dict[str, Any]:
    """Population-weighted means of municipal indicators (DERIVED)."""
    out: dict[str, Any] = {}
    pops = np.array([float(p or 0) for p, _ in rows])
    for f in DEMOGRAPHIC_FIELDS:
        vals = np.array(
            [np.nan if d is None or getattr(d, f) is None else float(getattr(d, f)) for _, d in rows]
        )
        ok = ~np.isnan(vals) & (pops > 0)
        out[f] = rnd(float(np.average(vals[ok], weights=pops[ok])), 3) if ok.any() else None
    return out


# =========================================================================== municipalities
def _municipality_row(m: Municipality, d: Any, pcode: str) -> dict[str, Any]:
    return {
        "code": m.cbs_code,
        "name": m.name,
        "province_code": pcode,
        "population": m.population,
        "population_official": m.population_official,
        "eligible_voters_est": m.eligible_voters_est,
        "area_km2": rnd(m.area_km2, 3),
        "land_area_km2": rnd(m.land_area_km2, 3),
        "density": rnd(m.density, 1),
        "urbanity_class": m.urbanity_class,
        "address_density": rnd(m.address_density, 1),
        "unit_count": m.unit_count,
        "centroid": [rnd(m.centroid_lon, 5), rnd(m.centroid_lat, 5)],
        "demographics": _demographics(d),
        "imputed_fields": _imputed(d),
    }


def municipalities(session: Session, province: str | None = None) -> dict[str, Any]:
    """``GET /api/municipalities?province=`` — REAL municipalities with demographics."""
    v = _vintage(session)
    ids, pmap = province_maps(session)
    if province is not None and province.upper() not in pmap:
        raise NotFoundError(f"province {province!r} not found")

    def build() -> list[dict[str, Any]]:
        rows = session.execute(
            select(Municipality, MunicipalityDemographics)
            .outerjoin(MunicipalityDemographics, MunicipalityDemographics.municipality_id == Municipality.id)
            .where(Municipality.vintage_id == v.id)
            .order_by(Municipality.cbs_code)
        ).all()
        return [_municipality_row(m, d, ids[m.province_id]) for m, d in rows]

    rows = cached(session, "municipalities", (v.id, iso(v.built_at)), build)
    if province is not None:
        rows = [r for r in rows if r["province_code"] == province.upper()]
    return {
        "data_category": REAL,
        "provenance": GEO_PROVENANCE,
        "vintage": {"id": v.id, "year": v.year},
        "count": len(rows),
        "municipalities": rows,
    }


def _municipality(session: Session, v: GeoVintage, code: str) -> Municipality:
    m = session.scalar(
        select(Municipality).where(Municipality.vintage_id == v.id, Municipality.cbs_code == code.upper())
    )
    if m is None:
        raise NotFoundError(f"municipality {code!r} not found in vintage {v.year}")
    return m


def municipality_detail(session: Session, code: str) -> dict[str, Any]:
    """``GET /api/municipalities/{code}`` — REAL statistics, overlapping House districts (with
    population shares), neighbourhoods (precincts), mayor and council, lineage."""
    v = _vintage(session)
    ids, _pmap = province_maps(session)
    m = _municipality(session, v, code)
    d = session.get(MunicipalityDemographics, m.id)
    out = _municipality_row(m, d, ids[m.province_id])
    out["demographics_source_years"] = (
        None if d is None else {"core": d.source_year_core, "supplement": d.source_year_supplement}
    )
    from app.districts.service import active_plan

    plan = active_plan(session)
    districts = []
    unit_district: dict[int, str] = {}
    if plan is not None:
        for dm, hd in session.execute(
            select(DistrictMunicipality, HouseDistrict)
            .join(HouseDistrict, HouseDistrict.id == DistrictMunicipality.district_id)
            .where(DistrictMunicipality.municipality_id == m.id, HouseDistrict.plan_id == plan.id)
            .order_by(DistrictMunicipality.population.desc())
        ).all():
            districts.append(
                {
                    "code": hd.code,
                    "name": hd.name,
                    "population": dm.population,
                    "unit_count": dm.n_units,
                    "share_of_municipality": rnd(dm.share_of_municipality, 4),
                    "share_of_district": rnd(dm.share_of_district, 4),
                }
            )
        unit_district = {
            int(u): c
            for u, c in session.execute(
                select(DistrictAssignment.geo_unit_id, HouseDistrict.code)
                .join(HouseDistrict, HouseDistrict.id == DistrictAssignment.district_id)
                .join(GeoUnit, GeoUnit.id == DistrictAssignment.geo_unit_id)
                .where(DistrictAssignment.plan_id == plan.id, GeoUnit.municipality_id == m.id)
            ).all()
        }
    units = [
        {
            "code": u.cbs_code,
            "name": u.name,
            "wijk_code": u.wijk_code,
            "population": u.population,
            "eligible_voters_est": u.eligible_voters_est,
            "land_area_km2": rnd(u.land_area_km2, 4),
            "density": rnd(u.density, 1),
            "urbanity_class": u.urbanity_class,
            "district_code": unit_district.get(u.id),
            "imputed_fields": _imputed(ud),
        }
        for u, ud in session.execute(
            select(GeoUnit, GeoUnitDemographics)
            .outerjoin(GeoUnitDemographics, GeoUnitDemographics.geo_unit_id == GeoUnit.id)
            .where(GeoUnit.municipality_id == m.id)
            .order_by(GeoUnit.cbs_code)
        ).all()
    ]
    council = session.scalar(
        select(Legislature).where(
            Legislature.level == "municipal", Legislature.jurisdiction_code == m.cbs_code
        )
    )
    lineage = [
        {
            "from_code": ln.from_code,
            "to_code": ln.to_code,
            "population_weight": rnd(ln.population_weight, 6),
            "event": ln.event,
            "effective_date": ln.effective_date,
        }
        for ln in session.scalars(
            select(MunicipalityLineage).where(
                (MunicipalityLineage.to_code == m.cbs_code) | (MunicipalityLineage.from_code == m.cbs_code)
            )
        )
    ]
    out.update(
        {
            "data_category": REAL,
            "provenance": {**GEO_PROVENANCE, "units": REAL},
            "districts": districts,
            "units": units,
            "mayor": current_holders(session, prefix=f"MAYOR-{m.cbs_code}").get(f"MAYOR-{m.cbs_code}"),
            "council": None
            if council is None
            else {"name": council.name, "seats": council.seats, "electoral_system": council.electoral_system},
            "lineage": lineage,
        }
    )
    return out


# =========================================================================== apportionment
def apportionment(session: Session) -> dict[str, Any]:
    """``GET /api/apportionment`` — House seats and electoral votes per province (FICTIONAL
    constitution applied to REAL population), the priority sequence and a method comparison."""
    from app.districts.apportionment import apportion, compare_methods

    v = _vintage(session)
    appt = _active_apportionment(session, v.id)
    if appt is None:
        raise NotFoundError("no active apportionment (run the setup first)")

    def build() -> dict[str, Any]:
        ids, pmap = province_maps(session)
        seats = sorted(
            _seats_by_province(session, appt).values(), key=lambda s: pmap[ids[s.province_id]]["sort_order"]
        )
        pops = {ids[s.province_id]: int(s.population) for s in seats}
        cons = get_constitution()
        res = apportion(pops, appt.total_seats, appt.method, appt.min_seats_per_province, constitution=cons)
        comparison = compare_methods(pops, appt.total_seats, appt.min_seats_per_province, constitution=cons)
        return {
            "data_category": FICTIONAL,
            "provenance": {"population": REAL, "seats": FICTIONAL, "electoral_votes": FICTIONAL},
            "id": appt.id,
            "method": appt.method,
            "population_basis": appt.population_basis,
            "total_seats": appt.total_seats,
            "min_seats_per_province": appt.min_seats_per_province,
            "senators_per_province": appt.senators_per_province,
            "total_electoral_votes": appt.total_electoral_votes,
            "total_population": int(sum(pops.values())),
            "average_persons_per_seat": rnd(sum(pops.values()) / appt.total_seats, 1)
            if appt.total_seats
            else None,
            "provinces": [
                {
                    "code": ids[s.province_id],
                    "name": pmap[ids[s.province_id]]["name"],
                    "population": s.population,
                    "quota": rnd(s.quota, 6),
                    "seats": s.seats,
                    "senators": appt.senators_per_province,
                    "electoral_votes": s.electoral_votes,
                    "persons_per_seat": rnd(s.persons_per_seat, 1),
                }
                for s in seats
            ],
            "priority_list": [
                {
                    "rank": p.rank,
                    "province": p.province,
                    "province_seat": p.province_seat,
                    "priority": rnd(p.priority, 3),
                }
                for p in res.priority_order
            ],
            "first_out": [
                {
                    "rank": p.rank,
                    "province": p.province,
                    "province_seat": p.province_seat,
                    "priority": rnd(p.priority, 3),
                }
                for p in res.first_out
            ],
            "method_comparison": [
                {"province": str(code), **{k: int(v) for k, v in row.items()}}
                for code, row in comparison.iterrows()
            ],
        }

    return cached(session, "apportionment", (appt.id,), build)


# =========================================================================== GeoJSON
_geo_lock = threading.Lock()


def _web_dir(year: int) -> Path:
    """``data/processed/geo_<year>/web`` (year 0 — the synthetic test country — gets ``geo_0``)."""
    return get_settings().processed_dir / f"geo_{int(year)}" / "web"


def _fingerprint_ok(path: Path, fingerprint: str) -> bool:
    """The cached file exists and starts with the expected fingerprint header."""
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as fh:
        head = fh.read(512)
    return f'"fingerprint":"{fingerprint}"' in head


def _write_collection(path: Path, header: dict[str, Any], features: list[str]) -> None:
    """Write a FeatureCollection (header keys first so the fingerprint can be checked cheaply)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    head = json.dumps(header, separators=(",", ":"), ensure_ascii=False)[:-1]
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(head)
        fh.write(',"features":[')
        fh.write(",".join(features))
        fh.write("]}")
    tmp.replace(path)


def _feature(wkb: bytes | None, props: dict[str, Any]) -> str | None:
    if not wkb:
        return None
    geom = shapely.from_wkb(wkb)
    geom = shapely.transform(geom, lambda c: np.round(c, 5))
    return (
        '{"type":"Feature","properties":'
        + json.dumps(props, separators=(",", ":"), ensure_ascii=False)
        + ',"geometry":'
        + shapely.to_geojson(geom)
        + "}"
    )


def districts_geojson(session: Session, plan_id: int | None = None) -> Path:
    """GeoJSON (EPSG:4326) of a House plan (default: the active one) built from the stored
    simplified district geometry and cached as ``data/processed/geo_<year>/web/districts_<plan_id>.geojson``.
    Properties: ``code, name, number, province_code, population, deviation_pct``."""
    from app.districts.service import active_plan

    plan = session.get(DistrictPlan, plan_id) if plan_id is not None else active_plan(session)
    if plan is None:
        raise NotFoundError("no district plan" if plan_id is None else f"district plan {plan_id} not found")
    vintage = session.get(GeoVintage, plan.vintage_id)
    year = vintage.year if vintage is not None else get_settings().geography_year
    path = _web_dir(year) / f"districts_{plan.id}.geojson"
    fp = f"plan:{plan.id}:{plan.config_hash}:{int(plan.seed)}:{iso(plan.created_at)}"
    with _geo_lock:
        if _fingerprint_ok(path, fp):
            return path
        ids, _pmap = province_maps(session)
        with Timer(log, f"districts GeoJSON of plan {plan.id}"):
            feats = []
            for d in session.scalars(
                select(HouseDistrict).where(HouseDistrict.plan_id == plan.id).order_by(HouseDistrict.code)
            ):
                f = _feature(
                    d.geometry_wkb,
                    {
                        "code": d.code,
                        "name": d.name,
                        "number": d.number,
                        "province_code": ids.get(d.province_id),
                        "population": d.population,
                        "deviation_pct": rnd(d.deviation_pct, 3),
                    },
                )
                if f is not None:
                    feats.append(f)
            if not feats:
                raise DataNotPreparedError(f"district plan {plan.id} has no stored geometry")
            _write_collection(
                path,
                {
                    "type": "FeatureCollection",
                    "fingerprint": fp,
                    "layer": "districts",
                    "plan_id": plan.id,
                    "data_category": FICTIONAL,
                    "crs": "EPSG:4326",
                },
                feats,
            )
    return path


def _db_layer(session: Session, layer: str, v: GeoVintage) -> Path:
    """Provinces / municipalities GeoJSON from the database geometry (used when the processed
    store has no web layer, e.g. the synthetic test country)."""
    path = _web_dir(v.year) / f"{layer}_db_{v.id}.geojson"
    fp = f"{layer}:{v.id}:{v.year}:{iso(v.built_at)}"
    with _geo_lock:
        if _fingerprint_ok(path, fp):
            return path
        ids, _pmap = province_maps(session)
        feats = []
        if layer == "provinces":
            rows = session.execute(
                select(Province, ProvinceStats.geometry_wkb)
                .join(ProvinceStats, ProvinceStats.province_id == Province.id)
                .where(ProvinceStats.vintage_id == v.id)
                .order_by(Province.sort_order)
            ).all()
            for p, wkb in rows:
                f = _feature(wkb, {"code": p.code, "name": p.name, "province_code": p.code})
                if f is not None:
                    feats.append(f)
        else:
            for m in session.scalars(
                select(Municipality).where(Municipality.vintage_id == v.id).order_by(Municipality.cbs_code)
            ):
                f = _feature(
                    m.geometry_wkb, {"code": m.cbs_code, "name": m.name, "province_code": ids[m.province_id]}
                )
                if f is not None:
                    feats.append(f)
        if not feats:
            raise DataNotPreparedError(f"no stored {layer} geometry for vintage {v.year}")
        _write_collection(
            path,
            {
                "type": "FeatureCollection",
                "fingerprint": fp,
                "layer": layer,
                "data_category": REAL,
                "crs": "EPSG:4326",
            },
            feats,
        )
    return path


def geojson_layer(session: Session, layer: str, *, plan_id: int | None = None) -> Path:
    """File of a GeoJSON layer: ``provinces``, ``municipalities`` (REAL; the processed store's
    web layer, else built from the database geometry), ``districts`` (FICTIONAL, active plan or
    ``plan_id``) or ``units/<PV>`` (REAL neighbourhoods of a province; store only)."""
    name = layer.strip().strip("/")
    if name.endswith(".geojson"):
        name = name[: -len(".geojson")]
    if name == "districts":
        return districts_geojson(session, plan_id)
    v = _vintage(session)
    src = loads(get_meta(session, GEOGRAPHY_SOURCE_KEY) or "")
    from_store = src.get("kind", "store") == "store"
    from app.geography.store import is_prepared, web_geojson_path

    if name in ("provinces", "municipalities"):
        if from_store and is_prepared(v.year):
            return web_geojson_path(v.year, name)
        return _db_layer(session, name, v)
    if name.startswith("units/") or name == "units":
        if not (from_store and is_prepared(v.year)):
            raise DataNotPreparedError("neighbourhood layers need the processed CBS geography store")
        try:
            return web_geojson_path(v.year, name)
        except ValueError as exc:
            raise NotFoundError(str(exc)) from None
    raise NotFoundError(f"unknown GeoJSON layer {layer!r}; expected one of {list(GEO_LAYERS)}")


def unit_frame(session: Session, vintage_id: int) -> pd.DataFrame:
    """Neighbourhood code → municipality code / name of a vintage (for unit exports)."""
    rows = session.execute(
        select(GeoUnit.cbs_code, Municipality.cbs_code)
        .join(Municipality, Municipality.id == GeoUnit.municipality_id)
        .where(GeoUnit.vintage_id == vintage_id)
    ).all()
    return pd.DataFrame(rows, columns=["unit_code", "municipality_code"])


__all__ = [
    "DEMOGRAPHIC_FIELDS",
    "GEO_LAYERS",
    "apportionment",
    "districts_geojson",
    "geojson_layer",
    "municipalities",
    "municipality_detail",
    "province_detail",
    "provinces",
    "unit_frame",
]
