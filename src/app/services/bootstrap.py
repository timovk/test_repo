"""System bootstrap: database schema, REAL geography, apportionment, House plan, Senate seats and
offices — everything that must exist before an election can be created.

Every step is idempotent (re-running reuses what is already there) and the caller owns the
transaction (functions flush, never commit)::

    from app.db.session import session_scope
    from app.services.bootstrap import init_db, prepare_geography, setup_system

    init_db()                                   # Alembic upgrade to the latest revision
    prepare_geography()                         # download + build data/processed/geo_<year>
    with session_scope() as s:
        report = setup_system(s)                # geography → apportionment → plan → seats/offices

For tests and experiments :func:`setup_synthetic_system` does the same on the synthetic toy
country (:mod:`app.geography.synthetic`, offline, seconds).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_constitution
from app.core.constitution import ConstitutionConfig, OfficeType
from app.core.errors import DataNotPreparedError, DistrictingError, NotFoundError, ValidationError
from app.core.logging import Timer, get_logger, log_ctx
from app.core.rng import config_hash
from app.districts.apportionment import canonical_method
from app.districts.config import DistrictConfig, load_district_config
from app.districts.generator import generate_plan
from app.districts.overrides import OverrideEntry, load_overrides
from app.districts.service import (
    active_plan,
    apportionment_seats,
    create_apportionment,
    ensure_senate_seats,
    store_plan,
)
from app.districts.validation import validate_plan
from app.elections.calendar import ElectionCalendar
from app.elections.seats import municipal_council_size, provincial_legislature_size
from app.models import (
    Apportionment,
    DistrictPlan,
    GeoUnit,
    GeoVintage,
    GovernorSeat,
    HouseDistrict,
    Legislature,
    MayorSeat,
    Municipality,
    Office,
    Province,
    ProvinceStats,
    SenateSeat,
)
from app.services._common import (
    PRESIDENT_OFFICE,
    VICE_PRESIDENT_OFFICE,
    RunRecorder,
    active_vintage,
    governor_office_code,
    house_office_code,
    lt_governor_office_code,
    mayor_office_code,
)
from app.services.runtime import (
    GeographySource,
    clear_caches,
    register_geography_source,
    synthetic_world,
)

log = get_logger(__name__)

#: Deviation errors of the plan validator that are expected on the coarse synthetic geography.
_DEVIATION_ERROR = "population deviation"


@dataclass
class SetupReport:
    """What :func:`setup_system` / :func:`setup_synthetic_system` produced (ids and counts)."""

    vintage_id: int
    vintage_year: int
    apportionment_id: int
    plan_id: int
    plan_reused: bool
    provinces: int
    municipalities: int
    units: int
    districts: int
    senate_seats: int
    offices: int
    legislatures: int
    timings: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# =========================================================================== database
def init_db(url: str | None = None) -> None:
    """Bring the database at ``url`` (default: settings) to the latest Alembic revision."""
    from app.db.migrate import upgrade_db

    with Timer(log, "database migrations"):
        upgrade_db(url)


# =========================================================================== geography
def prepare_geography(year: int | None = None, download: bool = True) -> Any:
    """Download (optionally) and build the processed REAL geography store of ``year``.

    Returns the :class:`~app.geography.build.BuildReport`.  With ``download=False`` the raw
    sources must already be present (or network access must be allowed for the build step).
    """
    from app.geography.build import build_geography
    from app.geography.download import download_all

    if download:
        with Timer(log, "download geography sources"):
            download_all(year)
    with Timer(log, "build geography store"):
        report = build_geography(year)
    clear_caches()
    return report


def load_geography(session: Session, year: int | None = None) -> GeoVintage:
    """Load the REAL processed store of ``year`` into the database and register it as the frame
    source (idempotent).  Raises :class:`DataNotPreparedError` when the store is not built."""
    from app.geography.loader_db import load_into_db
    from app.geography.store import is_prepared, manifest

    if not is_prepared(year):
        raise DataNotPreparedError()
    vintage = load_into_db(session, year)
    register_geography_source(session, GeographySource("store", int(manifest(year)["year"])))
    return vintage


def _vintage(session: Session, vintage: GeoVintage | int | None) -> GeoVintage:
    if isinstance(vintage, GeoVintage):
        return vintage
    if vintage is None:
        v = active_vintage(session)
    else:
        v = session.scalar(select(GeoVintage).where(GeoVintage.year == int(vintage)))
        if v is None:
            v = session.get(GeoVintage, int(vintage))
    if v is None:
        raise DataNotPreparedError("no geography vintage loaded (run load_geography first)")
    return v


def province_populations(session: Session, vintage: GeoVintage) -> dict[str, int]:
    """Province code → population (sum of the vintage's CBS neighbourhoods), canonical order."""
    rows = session.execute(
        select(Province.code, func.sum(GeoUnit.population))
        .join(GeoUnit, GeoUnit.province_id == Province.id)
        .where(GeoUnit.vintage_id == vintage.id)
        .group_by(Province.code, Province.sort_order)
        .order_by(Province.sort_order)
    ).all()
    return {code: int(pop) for code, pop in rows}


def ensure_apportionment(
    session: Session, vintage: GeoVintage | int | None = None, method: str | None = None
) -> Apportionment:
    """The active House apportionment of ``vintage`` (created with the constitution's method,
    Huntington–Hill by default; idempotent per vintage and method)."""
    v = _vintage(session, vintage)
    cons = get_constitution()
    meth = canonical_method(method or cons.apportionment_method)
    return create_apportionment(session, v.id, province_populations(session, v), meth, cons)


# =========================================================================== House plan
def _expected_config_hash(cfg: DistrictConfig, overrides: list[OverrideEntry]) -> str:
    return config_hash(cfg.canonical_json([o.canonical() for o in overrides]))


def _plan_for(
    session: Session,
    vintage: GeoVintage,
    appt: Apportionment,
    seed: int,
    cfg_hash: str,
) -> DistrictPlan | None:
    """An existing House plan generated from exactly these inputs (active one preferred)."""
    return session.scalars(
        select(DistrictPlan)
        .where(
            DistrictPlan.chamber == "house",
            DistrictPlan.vintage_id == vintage.id,
            DistrictPlan.apportionment_id == appt.id,
            DistrictPlan.seed == int(seed),
            DistrictPlan.config_hash == cfg_hash,
        )
        .order_by(DistrictPlan.is_active.desc(), DistrictPlan.id.desc())
        .limit(1)
    ).first()


def _generate_and_store(
    session: Session,
    vintage: GeoVintage,
    appt: Apportionment,
    units: pd.DataFrame,
    adjacency: pd.DataFrame,
    *,
    year: int | None,
    seed: int,
    cfg: DistrictConfig,
    overrides: list[OverrideEntry],
    workers: int | None,
    strict: bool,
) -> tuple[DistrictPlan, list[str]]:
    seats = apportionment_seats(session, appt.id)
    names = dict(
        session.execute(
            select(Municipality.cbs_code, Municipality.name).where(Municipality.vintage_id == vintage.id)
        )
        .tuples()
        .all()
    )
    with RunRecorder(session, "districts", seed, config_hash=_expected_config_hash(cfg, overrides)) as rec:
        plan = generate_plan(
            units, adjacency, seats, cfg, seed, overrides or None, municipality_names=names, workers=workers
        )
        result = validate_plan(plan, units, seats)
        warnings = list(result.warnings)
        if strict:
            result.raise_if_invalid()
        else:
            hard = [e for e in result.errors if _DEVIATION_ERROR not in e]
            if hard:
                raise ValidationError("District plan failed validation", hard)
            warnings += [e for e in result.errors if _DEVIATION_ERROR in e]
        row = store_plan(session, plan, vintage.id, appt.id, year, units, set_active=True)
        rec.summary.update(
            {
                "plan_id": row.id,
                "districts": plan.n_districts,
                "max_abs_deviation_pct": round(plan.max_abs_deviation_pct, 3),
                "split_municipalities": len(plan.split_municipalities()),
                "warnings": len(warnings),
                "timings": {k: round(v, 3) for k, v in plan.timings.items()},
                "strict_validation": strict,
            }
        )
    return row, warnings


def ensure_district_plan(
    session: Session,
    year: int | None = None,
    seed: int = 2028,
    workers: int | None = 0,
    *,
    config: DistrictConfig | None = None,
) -> DistrictPlan:
    """The active House plan of the active REAL geography, generated only when needed.

    An existing plan is reused (and activated) when it was generated from the same vintage,
    apportionment, seed and configuration hash (district config + ``config/districts/overrides``);
    otherwise a new plan is generated (``generate_plan`` with the overrides and REAL municipality
    names), validated (``validate_plan(...).raise_if_invalid()``) and stored as the active plan.
    ``year`` is the first election year the plan applies to (stored on the plan).
    """
    vintage = _vintage(session, None)
    appt = ensure_apportionment(session, vintage)
    cfg = config or load_district_config()
    overrides = load_overrides(cfg.overrides_file) if cfg.overrides_file else []
    existing = _plan_for(session, vintage, appt, seed, _expected_config_hash(cfg, overrides))
    if existing is not None:
        _activate_plan(session, existing)
        return existing
    from app.geography.store import load_unit_adjacency, load_units_gdf

    with Timer(log, "load units for districting"):
        units = load_units_gdf(vintage.year)
        adjacency = load_unit_adjacency(vintage.year)
    row, warnings = _generate_and_store(
        session,
        vintage,
        appt,
        units,
        adjacency,
        year=year,
        seed=seed,
        cfg=cfg,
        overrides=overrides,
        workers=workers,
        strict=True,
    )
    if warnings:
        log.info("district plan %d stored with %d warnings", row.id, len(warnings))
    return row


def _activate_plan(session: Session, plan: DistrictPlan) -> None:
    if not plan.is_active:
        session.execute(
            update(DistrictPlan)
            .where(DistrictPlan.chamber == plan.chamber, DistrictPlan.id != plan.id)
            .values(is_active=False)
        )
        plan.is_active = True
        session.flush()


# =========================================================================== offices
def _upsert_office(
    offices: dict[str, Office],
    session: Session,
    code: str,
    office_type: OfficeType,
    name: str,
    term_years: int,
    **attrs: Any,
) -> Office:
    row = offices.get(code)
    if row is None:
        row = Office(
            office_type=office_type.value,
            code=code,
            name=name,
            term_years=term_years,
            is_active=True,
            **attrs,
        )
        session.add(row)
        offices[code] = row
    else:
        row.name = name
        row.term_years = term_years
        row.is_active = True
        for k, v in attrs.items():
            setattr(row, k, v)
    return row


def _statutory_population(official: int | None, counted: int) -> int:
    return int(official) if official else int(counted)


def ensure_offices(session: Session, constitution: ConstitutionConfig | None = None) -> dict[str, int]:
    """Create or update every elected office and seat (idempotent).

    * ``PRES`` and ``VP``; ``HOUSE-<district>`` for the active plan (House offices of districts no
      longer in the active plan are deactivated); the ``SEN-<PV>-<n>`` offices come from
      :func:`ensure_senate_seats`;
    * ``GOV-<PV>`` (+ ``LTGOV-<PV>`` when ``constitution.lieutenant_governors``) with a
      ``GovernorSeat`` per province; ``MAYOR-<GM>`` with a ``MayorSeat`` per municipality of the
      active vintage;
    * ``Legislature`` rows: 12 provincial legislatures (Provinciewet sizes) and one council per
      municipality (Gemeentewet sizes), sized by the official CBS population when known.

    Returns counts per kind.
    """
    cons = constitution or get_constitution()
    calendar = ElectionCalendar.from_config(constitution=cons)
    vintage = _vintage(session, None)
    plan = active_plan(session)
    if plan is None:
        raise NotFoundError("no active House plan (run ensure_district_plan first)")
    provinces = session.scalars(select(Province).order_by(Province.sort_order, Province.code)).all()
    offices = {o.code: o for o in session.scalars(select(Office))}
    counts: dict[str, int] = {}
    pres_term = cons.presidential_term_years
    _upsert_office(offices, session, PRESIDENT_OFFICE, OfficeType.PRESIDENT, "President", pres_term)
    _upsert_office(
        offices, session, VICE_PRESIDENT_OFFICE, OfficeType.VICE_PRESIDENT, "Vice-President", pres_term
    )

    # ---- House
    districts = session.scalars(select(HouseDistrict).where(HouseDistrict.plan_id == plan.id)).all()
    prov_name = {p.id: p.name for p in provinces}
    wanted_house = set()
    for d in districts:
        code = house_office_code(d.code)
        wanted_house.add(code)
        _upsert_office(
            offices,
            session,
            code,
            OfficeType.HOUSE,
            f"Member of the Tweede Kamer for {d.code} ({d.name or prov_name.get(d.province_id, '')})",
            cons.house_term_years,
            province_id=d.province_id,
            district_code=d.code,
        )
    for code, o in offices.items():
        if o.office_type == OfficeType.HOUSE.value and code not in wanted_house and o.is_active:
            o.is_active = False
    counts["house"] = len(wanted_house)

    # ---- Senate (offices + seats)
    counts["senate"] = len(ensure_senate_seats(session, constitution=cons))
    offices = {o.code: o for o in session.scalars(select(Office))}

    # ---- governors / lieutenant governors
    gov_seats = {g.province_id: g for g in session.scalars(select(GovernorSeat))}
    gov_first = calendar.governors.first_year
    for p in provinces:
        gov = _upsert_office(
            offices,
            session,
            governor_office_code(p.code),
            OfficeType.GOVERNOR,
            f"Governor of {p.name}",
            cons.governor_term_years,
            province_id=p.id,
        )
        lt = None
        if cons.lieutenant_governors:
            lt = _upsert_office(
                offices,
                session,
                lt_governor_office_code(p.code),
                OfficeType.LIEUTENANT_GOVERNOR,
                f"Lieutenant Governor of {p.name}",
                cons.governor_term_years,
                province_id=p.id,
            )
        elif lt_governor_office_code(p.code) in offices:
            offices[lt_governor_office_code(p.code)].is_active = False
        session.flush()
        seat = gov_seats.get(p.id)
        if seat is None:
            session.add(
                GovernorSeat(
                    province_id=p.id,
                    office_id=gov.id,
                    lt_office_id=lt.id if lt is not None else None,
                    first_election_year=gov_first,
                )
            )
        else:
            seat.office_id = gov.id
            seat.lt_office_id = lt.id if lt is not None else None
            seat.first_election_year = gov_first
    counts["governors"] = len(provinces)

    # ---- mayors
    munis = session.scalars(
        select(Municipality).where(Municipality.vintage_id == vintage.id).order_by(Municipality.cbs_code)
    ).all()
    mayor_seats = {m.municipality_code: m for m in session.scalars(select(MayorSeat))}
    muni_first = calendar.municipal.first_year
    current = set()
    for m in munis:
        code = mayor_office_code(m.cbs_code)
        current.add(code)
        _upsert_office(
            offices,
            session,
            code,
            OfficeType.MAYOR,
            f"Mayor of {m.name}",
            cons.mayor_term_years,
            province_id=m.province_id,
            municipality_code=m.cbs_code,
        )
    session.flush()
    for m in munis:
        office = offices[mayor_office_code(m.cbs_code)]
        seat = mayor_seats.get(m.cbs_code)
        if seat is None:
            session.add(
                MayorSeat(
                    municipality_code=m.cbs_code,
                    province_id=m.province_id,
                    office_id=office.id,
                    first_election_year=muni_first,
                )
            )
        else:
            seat.province_id = m.province_id
            seat.office_id = office.id
            seat.first_election_year = muni_first
    for code, o in offices.items():
        if o.office_type == OfficeType.MAYOR.value and code not in current and o.is_active:
            o.is_active = False  # municipality merged away in the active vintage
    counts["mayors"] = len(munis)

    # ---- legislatures
    legs = {(lg.level, lg.jurisdiction_code): lg for lg in session.scalars(select(Legislature))}
    stats = {
        s.province_id: s
        for s in session.scalars(select(ProvinceStats).where(ProvinceStats.vintage_id == vintage.id))
    }
    pops = {
        code: pop
        for code, pop in session.execute(
            select(Province.code, func.sum(GeoUnit.population))
            .join(GeoUnit, GeoUnit.province_id == Province.id)
            .where(GeoUnit.vintage_id == vintage.id)
            .group_by(Province.code)
        ).all()
    }

    def upsert_leg(level: str, code: str, province_id: int, name: str, seats: int, term: int) -> None:
        lg = legs.get((level, code))
        values = {
            "province_id": province_id,
            "name": name,
            "seats": int(seats),
            "electoral_system": "proportional_dhondt",
            "size_rule": "population_brackets",
            "term_years": int(term),
        }
        if lg is None:
            lg = Legislature(level=level, jurisdiction_code=code, **values)
            session.add(lg)
            legs[(level, code)] = lg
        else:
            for k, v in values.items():
                setattr(lg, k, v)

    for p in provinces:
        st = stats.get(p.id)
        pop = _statutory_population(st.population_official if st else None, int(pops.get(p.code, 0) or 0))
        upsert_leg(
            "provincial",
            p.code,
            p.id,
            f"Provincial Legislature of {p.name}",
            provincial_legislature_size(pop),
            cons.governor_term_years,
        )
    for m in munis:
        pop = _statutory_population(m.population_official, m.population)
        upsert_leg(
            "municipal",
            m.cbs_code,
            m.province_id,
            f"Municipal Council of {m.name}",
            municipal_council_size(pop),
            cons.mayor_term_years,
        )
    counts["legislatures"] = len(provinces) + len(munis)
    session.flush()
    counts["offices"] = int(
        session.scalar(select(func.count()).select_from(Office).where(Office.is_active.is_(True))) or 0
    )
    return counts


# =========================================================================== orchestration
def _report(
    session: Session,
    vintage: GeoVintage,
    appt: Apportionment,
    plan: DistrictPlan,
    reused: bool,
    counts: dict[str, int],
    timings: dict[str, float],
    warnings: list[str],
) -> SetupReport:
    n_munis = session.scalar(
        select(func.count()).select_from(Municipality).where(Municipality.vintage_id == vintage.id)
    )
    n_units = session.scalar(
        select(func.count()).select_from(GeoUnit).where(GeoUnit.vintage_id == vintage.id)
    )
    return SetupReport(
        vintage_id=vintage.id,
        vintage_year=vintage.year,
        apportionment_id=appt.id,
        plan_id=plan.id,
        plan_reused=reused,
        provinces=int(session.scalar(select(func.count()).select_from(Province)) or 0),
        municipalities=int(n_munis or 0),
        units=int(n_units or 0),
        districts=int(plan.total_districts),
        senate_seats=int(session.scalar(select(func.count()).select_from(SenateSeat)) or 0),
        offices=int(counts.get("offices", 0)),
        legislatures=int(counts.get("legislatures", 0)),
        timings={k: round(v, 3) for k, v in timings.items()},
        warnings=warnings,
    )


def setup_system(
    session: Session,
    *,
    year: int | None = None,
    district_seed: int = 2028,
    workers: int | None = 0,
) -> SetupReport:
    """Load the REAL geography and build the whole fictional system on it (idempotent):
    geography → apportionment → House plan (generated only when missing) → Senate seats →
    offices, seats and legislatures."""
    timings: dict[str, float] = {}
    t = time.perf_counter()
    vintage = load_geography(session, year)
    timings["geography"] = time.perf_counter() - t
    t = time.perf_counter()
    appt = ensure_apportionment(session, vintage)
    timings["apportionment"] = time.perf_counter() - t
    t = time.perf_counter()
    last_plan_id = int(session.scalar(select(func.max(DistrictPlan.id))) or 0)
    plan = ensure_district_plan(session, vintage.year, seed=district_seed, workers=workers)
    reused = plan.id <= last_plan_id
    timings["district_plan"] = time.perf_counter() - t
    t = time.perf_counter()
    counts = ensure_offices(session)
    timings["offices"] = time.perf_counter() - t
    report = _report(session, vintage, appt, plan, reused, counts, timings, [])
    log.info("system ready", extra=log_ctx(**{k: v for k, v in report.to_dict().items() if k != "warnings"}))
    return report


def synthetic_district_config(restarts: int = 2) -> DistrictConfig:
    """District configuration used on the synthetic geography: ``config/districts.yaml`` with
    ``restarts`` restarts, serial, and without the merge-split recombination (which searches in
    vain for population equality on the synthetic grid, whose city cells exceed a district)."""
    data = load_district_config().model_dump()
    data.update({"restarts": int(restarts), "workers": 1})
    if isinstance(data.get("merge_split"), dict):
        data["merge_split"] = {**data["merge_split"], "enabled": False}
    return DistrictConfig.model_validate(data)


def setup_synthetic_system(
    session: Session,
    seed: int = 7,
    *,
    district_seed: int | None = None,
    cells: int = 10,
    cell_m: float = 4000.0,
    muni_block: int = 2,
    population_scale: float = 1.0,
    restarts: int = 2,
) -> SetupReport:
    """The same system on the synthetic toy country (:func:`app.geography.synthetic.synthetic_geography`
    with the given parameters, vintage year 0), for tests and offline experiments.

    The House plan uses the Huntington–Hill apportionment of the synthetic populations and the
    district configuration of :func:`synthetic_district_config`.  The synthetic "cities" are single
    cells holding more people than a district, so population-deviation errors of the plan
    validator are recorded as warnings here (every structural check stays fatal).
    """
    from app.geography.loader_db import load_tables_into_db

    timings: dict[str, float] = {}
    t = time.perf_counter()
    geo = synthetic_world(seed, cells, cell_m, muni_block, population_scale)
    year = int(geo.frame.year)
    vintage = load_tables_into_db(
        session,
        year,
        geo.provinces,
        geo.municipalities,
        geo.units,
        label=f"Synthetic test geography (seed {seed})",
    )
    register_geography_source(
        session,
        GeographySource(
            "synthetic",
            year,
            (int(seed), int(cells), float(cell_m), int(muni_block), float(population_scale)),
        ),
    )
    timings["geography"] = time.perf_counter() - t
    t = time.perf_counter()
    appt = ensure_apportionment(session, vintage)
    timings["apportionment"] = time.perf_counter() - t
    t = time.perf_counter()
    cfg = synthetic_district_config(restarts)
    dseed = int(seed if district_seed is None else district_seed)
    chash = _expected_config_hash(cfg, [])
    existing = _plan_for(session, vintage, appt, dseed, chash)
    warnings: list[str] = []
    if existing is not None:
        _activate_plan(session, existing)
        plan, reused = existing, True
    else:
        plan, warnings = _generate_and_store(
            session,
            vintage,
            appt,
            geo.units,
            geo.unit_adjacency,
            year=None,
            seed=dseed,
            cfg=cfg,
            overrides=[],
            workers=1,
            strict=False,
        )
        reused = False
    if plan.total_districts != get_constitution().house_seats:
        raise DistrictingError(f"synthetic plan has {plan.total_districts} districts")
    timings["district_plan"] = time.perf_counter() - t
    t = time.perf_counter()
    counts = ensure_offices(session)
    timings["offices"] = time.perf_counter() - t
    return _report(session, vintage, appt, plan, reused, counts, timings, warnings)
