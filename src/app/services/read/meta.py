"""Application meta, health, UI settings, data provenance and system validation read models."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import app
from app.core.config import get_constitution
from app.core.constitution import DECIDED_STATUSES, DataCategory, RaceStatus
from app.core.errors import ConfigError
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.models import (
    AppMeta,
    Apportionment,
    Candidate,
    DataSource,
    DistrictPlan,
    Election,
    Party,
    Poll,
    Scenario,
    SimulationRun,
)
from app.services._common import GEOGRAPHY_SOURCE_KEY, active_vintage, dumps, loads
from app.services.read._base import (
    DERIVED,
    FICTIONAL,
    REAL,
    SIMULATED,
    all_election_refs,
    cached,
    demo_election_id,
    iso,
    latest_election_id,
    party_index,
    rnd,
)
from app.services.read.live import night_available

log = get_logger(__name__)

APP_NAME = "NL Federal Election Simulator"

#: ``app_meta`` key of the persisted UI settings (JSON).
UI_SETTINGS_KEY = "ui_settings"

FICTIONAL_NOTICE = (
    "FICTIONAL constitutional system over the REAL geography of the Netherlands (CBS/PDOK). "
    "Parties, candidates, districts and every result are fictional simulations — not predictions "
    "of real Dutch politics."
)

DATA_CATEGORY_LEGEND: list[dict[str, str]] = [
    {
        "key": DataCategory.REAL.value,
        "label": "Real",
        "description": "Official Dutch open data (CBS / PDOK): provinces, municipalities, neighbourhoods, "
        "boundaries, population and demographic indicators.",
    },
    {
        "key": DataCategory.DERIVED.value,
        "label": "Derived",
        "description": "Computed deterministically from real data: estimated eligible voters, densities, "
        "imputed gaps (flagged), simplified display geometry, population-weighted aggregates.",
    },
    {
        "key": DataCategory.FICTIONAL.value,
        "label": "Fictional",
        "description": "The invented federal constitution, House districts, Senate classes, parties, "
        "candidates, pollsters and scenario assumptions.",
    },
    {
        "key": DataCategory.SIMULATED.value,
        "label": "Simulated",
        "description": "Model output: votes, turnout, election nights, race calls, recounts, polls, "
        "campaign effects and Monte Carlo forecasts. Never a prediction.",
    },
]


# =========================================================================== constitution
def constitution_payload() -> dict[str, Any]:
    """Constitutional totals, majorities and display labels (FICTIONAL)."""
    c = get_constitution()
    return {
        "data_category": FICTIONAL,
        "canonical": c.is_canonical(),
        "provinces": c.province_count,
        "electoral_votes": c.electoral_votes,
        "presidential_majority": c.presidential_majority,
        "house_seats": c.house_seats,
        "house_majority": c.house_majority,
        "senate_seats": c.senate_seats,
        "senate_majority": c.senate_majority,
        "senators_per_province": c.senators_per_province,
        "senate_classes": c.senate_classes,
        "seats_per_senate_class": c.seats_per_senate_class,
        "min_house_seats_per_province": c.min_house_seats_per_province,
        "terms": {
            "president": c.presidential_term_years,
            "house": c.house_term_years,
            "senate": c.senate_term_years,
            "governor": c.governor_term_years,
            "mayor": c.mayor_term_years,
        },
        "apportionment_method": c.apportionment_method,
        "ev_allocation": c.ev_allocation.value,
        "contingent_mode": c.contingent.mode.value,
        "lieutenant_governors": c.lieutenant_governors,
        "labels": {
            "president": f"{c.presidential_majority} TO WIN",
            "house": f"{c.house_majority} FOR CONTROL",
            "senate": f"{c.senate_majority} FOR CONTROL",
        },
    }


def _night_speeds() -> tuple[list[float], float]:
    try:
        from app.reporting.config import load_night_config

        cfg = load_night_config()
        speeds = [float(s) for s in cfg.playback.speeds]
        return speeds, float(cfg.playback.default_speed)
    except (ConfigError, AttributeError) as exc:
        log.debug("night config unavailable: %s", exc)
        return [1.0, 2.0, 5.0, 10.0, 25.0], 1.0


# =========================================================================== meta
def active_system(session: Session) -> dict[str, Any]:
    """Active geography vintage, apportionment and House plan (ids + key figures)."""
    v = active_vintage(session)
    vintage = None
    appt = None
    plan = None
    if v is not None:
        row = session.get(AppMeta, GEOGRAPHY_SOURCE_KEY)
        src = loads(row.value) if row is not None else {}
        vintage = {
            "id": v.id,
            "year": v.year,
            "label": v.label,
            "provinces": v.province_count,
            "municipalities": v.municipality_count,
            "units": v.unit_count,
            "population": v.population_total,
            "source": src.get("kind", "store"),
            "data_category": REAL if src.get("kind", "store") != "synthetic" else DERIVED,
        }
        a = session.scalars(
            select(Apportionment)
            .where(Apportionment.vintage_id == v.id, Apportionment.is_active.is_(True))
            .order_by(Apportionment.id.desc())
            .limit(1)
        ).first()
        if a is not None:
            appt = {
                "id": a.id,
                "method": a.method,
                "total_seats": a.total_seats,
                "total_electoral_votes": a.total_electoral_votes,
                "data_category": FICTIONAL,
            }
    from app.districts.service import active_plan

    p = active_plan(session)
    if p is not None:
        plan = {
            "id": p.id,
            "name": p.name,
            "seed": int(p.seed),
            "method": p.method,
            "config_hash": p.config_hash,
            "total_districts": p.total_districts,
            "max_abs_deviation_pct": rnd(p.max_abs_deviation_pct),
            "data_category": FICTIONAL,
        }
    return {"vintage": vintage, "apportionment": appt, "plan": plan}


def meta(session: Session) -> dict[str, Any]:
    """``GET /api/meta`` — everything the UI shell needs at start-up."""
    speeds, default_speed = _night_speeds()
    parties = party_index(session)
    return {
        "app": {"name": APP_NAME, "version": app.__version__},
        "notice": FICTIONAL_NOTICE,
        "provenance": {
            "constitution": FICTIONAL,
            "active.vintage": REAL,
            "active.apportionment": FICTIONAL,
            "active.plan": FICTIONAL,
            "elections": SIMULATED,
            "parties": FICTIONAL,
        },
        "constitution": constitution_payload(),
        "active": active_system(session),
        "elections": [r.brief() for r in all_election_refs(session)],
        "demo_election_id": demo_election_id(session),
        "latest_election_id": latest_election_id(session),
        "parties": [
            {k: p[k] for k in ("code", "name", "abbreviation", "color", "is_active")}
            for p in parties.values()
        ],
        "data_categories": DATA_CATEGORY_LEGEND,
        "race_statuses": [s.value for s in RaceStatus],
        "decided_statuses": sorted(s.value for s in DECIDED_STATUSES),
        "night": {"available": night_available(), "speeds": speeds, "default_speed": default_speed},
    }


# =========================================================================== health
def _database_status(url: str) -> dict[str, Any]:
    """Readiness of the database at ``url`` without creating a missing SQLite file."""
    from app.db.session import get_engine

    u = make_url(url)
    info: dict[str, Any] = {
        "backend": u.get_backend_name(),
        "database": Path(u.database).name if u.database else None,
        "exists": True,
        "schema": False,
        "revision": None,
        "ready": False,
        "error": None,
    }
    if (
        info["backend"] == "sqlite"
        and u.database
        and u.database != ":memory:"
        and not Path(u.database).exists()
    ):
        info["exists"] = False
        info["error"] = "database file does not exist (run `python -m app setup`)"
        return info
    try:
        engine = get_engine(url)
        with engine.connect() as conn:
            insp = inspect(conn)
            info["schema"] = bool(insp.has_table("election") and insp.has_table("province"))
            if insp.has_table("alembic_version"):
                row = conn.exec_driver_sql("SELECT version_num FROM alembic_version").first()
                info["revision"] = row[0] if row else None
        info["ready"] = info["schema"]
        if not info["schema"]:
            info["error"] = "database schema missing (run `python -m app db upgrade`)"
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def health(url: str, ui_index: Path | None = None) -> dict[str, Any]:
    """``GET /api/health`` — liveness plus database / geography readiness (never raises)."""
    from app.geography.store import is_prepared

    settings = get_settings()
    db = _database_status(url)
    geo: dict[str, Any] = {
        "store_year": settings.geography_year,
        "store_prepared": False,
        "loaded": False,
        "vintage_year": None,
        "source": None,
    }
    try:
        geo["store_prepared"] = bool(is_prepared(settings.geography_year))
    except Exception as exc:
        geo["error"] = str(exc)
    elections = None
    if db["ready"]:
        from app.db.session import get_sessionmaker

        try:
            with get_sessionmaker(url)() as s:
                v = active_vintage(s)
                if v is not None:
                    geo["loaded"] = True
                    geo["vintage_year"] = v.year
                    row = s.get(AppMeta, GEOGRAPHY_SOURCE_KEY)
                    geo["source"] = loads(row.value).get("kind") if row is not None else "store"
                elections = int(s.scalar(select(func.count()).select_from(Election)) or 0)
        except Exception as exc:
            db["error"] = f"{type(exc).__name__}: {exc}"
            db["ready"] = False
    ready = bool(db["ready"] and geo["loaded"])
    return {
        "status": "ok",
        "ready": ready,
        "version": app.__version__,
        "database": db,
        "geography": geo,
        "elections": elections,
        "night_service": night_available(),
        "ui_built": bool(ui_index is not None and ui_index.exists()),
    }


# =========================================================================== UI settings
class PartyColorOverrides(BaseModel):
    """Validated party colour overrides (code → ``#rrggbb``)."""

    colors: dict[str, str] = Field(default_factory=dict)

    @field_validator("colors")
    @classmethod
    def _hex(cls, v: dict[str, str]) -> dict[str, str]:
        import re

        pat = re.compile(r"^#[0-9A-Fa-f]{6}$")
        for code, color in v.items():
            if not pat.match(str(color)):
                raise ValueError(f"colour of {code} must be '#rrggbb', got {color!r}")
        return {k: str(c).upper() for k, c in v.items()}


class UISettings(BaseModel):
    """UI settings persisted in ``app_meta`` (``ui_settings``)."""

    theme: Literal["dark", "light"] = "dark"
    playback_speed: float = Field(1.0, gt=0)
    party_colors: dict[str, str] = Field(default_factory=dict)
    map_metric: Literal["margin", "share", "turnout", "swing", "reporting"] = "margin"
    show_provenance_badges: bool = True
    default_election_id: int | None = None

    @field_validator("party_colors")
    @classmethod
    def _colors(cls, v: dict[str, str]) -> dict[str, str]:
        return PartyColorOverrides(colors=v).colors


def ui_settings(session: Session) -> UISettings:
    """Stored UI settings merged over the defaults (the default speed comes from the night config)."""
    _speeds, default_speed = _night_speeds()
    row = session.get(AppMeta, UI_SETTINGS_KEY)
    data: dict[str, Any] = {"playback_speed": default_speed}
    if row is not None:
        stored = loads(row.value)
        if isinstance(stored, dict):
            data.update(stored)
    try:
        return UISettings.model_validate(data)
    except ValueError as exc:
        log.warning("stored UI settings invalid (%s); using defaults", exc)
        return UISettings(playback_speed=default_speed)


def settings_payload(session: Session) -> dict[str, Any]:
    """``GET /api/settings``."""
    s = ui_settings(session)
    speeds, default_speed = _night_speeds()
    parties = party_index(session)
    return {
        **s.model_dump(),
        "defaults": UISettings(playback_speed=default_speed).model_dump(),
        "options": {
            "themes": ["dark", "light"],
            "speeds": speeds,
            "map_metrics": ["margin", "share", "turnout", "swing", "reporting"],
            "parties": [
                {"code": p["code"], "name": p["name"], "color": p["color"]} for p in parties.values()
            ],
        },
        "provenance": {"party_colors": FICTIONAL},
    }


def store_ui_settings(session: Session, patch: dict[str, Any]) -> UISettings:
    """Merge ``patch`` into the stored settings, validate and persist (flushes; unknown party
    codes in ``party_colors`` are rejected)."""
    current = ui_settings(session).model_dump()
    merged = {**current, **{k: v for k, v in patch.items() if v is not None or k == "default_election_id"}}
    if "party_colors" in patch and patch["party_colors"] is not None:
        known = set(party_index(session))
        unknown = sorted(set(patch["party_colors"]) - known)
        if unknown:
            raise ValueError(f"unknown party codes in party_colors: {unknown}")
        merged["party_colors"] = dict(patch["party_colors"])
    speeds, _default = _night_speeds()
    s = UISettings.model_validate(merged)
    if float(s.playback_speed) not in speeds:
        raise ValueError(f"playback_speed must be one of {speeds}")
    if s.default_election_id is not None and session.get(Election, s.default_election_id) is None:
        raise ValueError(f"election {s.default_election_id} does not exist")
    row = session.get(AppMeta, UI_SETTINGS_KEY)
    text = dumps(s.model_dump())
    if row is None:
        session.add(AppMeta(key=UI_SETTINGS_KEY, value=text))
    else:
        row.value = text
    session.flush()
    return s


# =========================================================================== provenance
_DERIVED_TRANSFORMS: list[dict[str, str]] = [
    {
        "name": "Population aggregates",
        "definition": "Municipal and provincial population = sum of the CBS neighbourhood (buurt) figures; "
        "official municipal totals are kept alongside (CBS rounds buurt figures to 5).",
    },
    {
        "name": "Estimated eligible voters",
        "definition": "round(pop × ((100 − pct_0_15 − pct_15_25)/100 + 0.7 × pct_15_25/100) × 0.93), clipped to "
        "[0, pop] (config/geography.yaml, eligible_voters).",
    },
    {
        "name": "Density and urbanity",
        "definition": "Inhabitants per km² of land, ln(1 + density); urbanity = 6 − CBS stedelijkheid class "
        "(missing classes from the address density with the CBS thresholds).",
    },
    {
        "name": "Imputed demographic gaps",
        "definition": "Suppressed CBS cells filled from wijk → gemeente → province → nation references; every "
        "imputed value is listed in imputed_fields.",
    },
    {
        "name": "Province of a municipality",
        "definition": "Largest area overlap of the municipality polygon with the generalised province polygons.",
    },
    {
        "name": "Adjacency and water links",
        "definition": "Rook contiguity (≥ 20 m shared border within a 2 m snap buffer) plus documented water "
        "links that connect islands to their province.",
    },
    {
        "name": "Display geometry",
        "definition": "Coverage-simplified, reprojected to EPSG:4326 and snapped to 5 decimals; computation "
        "always uses the full-resolution EPSG:28992 store.",
    },
    {
        "name": "Province demographics (API)",
        "definition": "Population-weighted means of the municipal CBS indicators, computed on request.",
    },
]

_FICTIONAL_CONSTRUCTS: list[dict[str, str]] = [
    {
        "name": "Constitution",
        "where": "config/constitution.yaml, app.core.constitution",
        "description": "President + VP elected through a 174-vote Electoral College (88 to win), 150-seat House, "
        "24-seat Senate in three classes, governors, mayors.",
    },
    {
        "name": "Apportionment",
        "where": "apportionment, apportionment_seat",
        "description": "House seats per province from REAL population (Huntington–Hill by default); EV = seats + 2.",
    },
    {
        "name": "House districts",
        "where": "district_plan, house_district, district_assignment",
        "description": "150 single-member districts generated from CBS neighbourhoods with a seeded algorithm.",
    },
    {
        "name": "Senate classes",
        "where": "senate_seat",
        "description": "Two seats per province in different classes.",
    },
    {
        "name": "Parties and candidates",
        "where": "party, candidate, config/parties, scenarios",
        "description": "Invented parties (with lineage) and people with persistent careers.",
    },
    {
        "name": "Scenarios",
        "where": "config/scenarios, scenario",
        "description": "Political assumptions: baselines, environment, candidate quality, campaigns, polling.",
    },
    {
        "name": "Pollsters",
        "where": "pollster",
        "description": "Invented polling organisations with house effects.",
    },
]

_SIMULATED_OUTPUTS: list[dict[str, str]] = [
    {
        "name": "Election results",
        "where": "election_result, turnout_result",
        "description": "Votes per ballot line for every neighbourhood, aggregated exactly to every level.",
    },
    {
        "name": "Election nights",
        "where": "reporting_event, race_call, night_session",
        "description": "Reporting timelines replayed live with race calls based on the evidence available.",
    },
    {
        "name": "Recounts and contingent elections",
        "where": "recount, recount_adjustment, contingent_election",
        "description": "Audited recount corrections and contingent elections when nobody reaches 88 EV.",
    },
    {
        "name": "Polls",
        "where": "poll, poll_result",
        "description": "Fictional polls generated from the model.",
    },
    {
        "name": "Campaigns",
        "where": "campaign, campaign_allocation",
        "description": "Planned spending and its small, uncertain realised effects.",
    },
    {
        "name": "Forecasts",
        "where": "simulation_run (forecast), forecast_*",
        "description": "Monte Carlo model estimates — not predictions.",
    },
]


def _manifest_summary(year: int) -> dict[str, Any] | None:
    from app.core.errors import DataNotPreparedError
    from app.geography.store import is_prepared, manifest

    try:
        if not is_prepared(year):
            return None
        m = manifest(year)
    except DataNotPreparedError:
        return None
    return {
        "year": m.get("year"),
        "built_at": m.get("built_at"),
        "fingerprint": m.get("fingerprint"),
        "schema_version": m.get("schema_version"),
        "crs": m.get("crs"),
        "web_crs": m.get("web_crs"),
        "counts": m.get("counts"),
        "source_years": m.get("source_years"),
        "imputation": m.get("imputation"),
        "water_links": m.get("water_links"),
        "coverage_repaired_units": m.get("coverage_repaired_units"),
        "sources": [
            {
                k: s.get(k)
                for k in (
                    "key",
                    "name",
                    "publisher",
                    "url",
                    "license",
                    "sha256",
                    "retrieved_at",
                    "vintage",
                    "bytes",
                )
            }
            for s in (m.get("sources") or {}).values()
        ]
        if isinstance(m.get("sources"), dict)
        else m.get("sources"),
    }


def provenance(session: Session) -> dict[str, Any]:
    """``GET /api/data/provenance`` — REAL sources, DERIVED transforms, FICTIONAL constructs and
    SIMULATED outputs, with live counts."""
    v = active_vintage(session)
    sources = [
        {
            "key": s.key,
            "name": s.name,
            "publisher": s.publisher,
            "url": s.url,
            "license": s.license,
            "sha256": s.sha256,
            "size_bytes": s.size_bytes,
            "retrieved_at": iso(s.retrieved_at),
            "data_category": s.data_category,
            "notes": s.notes,
        }
        for s in session.scalars(select(DataSource).order_by(DataSource.key))
    ]
    year = v.year if v is not None else get_settings().geography_year

    def count(model: Any, *where: Any) -> int:
        q = select(func.count()).select_from(model)
        for w in where:
            q = q.where(w)
        return int(session.scalar(q) or 0)

    return {
        "legend": DATA_CATEGORY_LEGEND,
        "notice": FICTIONAL_NOTICE,
        "real": {
            "data_category": REAL,
            "attribution": "Centraal Bureau voor de Statistiek (CBS) / PDOK — CC BY 4.0",
            "vintage": None
            if v is None
            else {
                "year": v.year,
                "label": v.label,
                "provinces": v.province_count,
                "municipalities": v.municipality_count,
                "units": v.unit_count,
                "population": v.population_total,
                "built_at": iso(v.built_at),
            },
            "sources": sources,
            "store": _manifest_summary(year) if year else None,
            "precinct_note": "CBS neighbourhoods (buurten) are used as precincts: Dutch polling-station districts "
            "have no official polygons. Simulated precinct results are buurt results.",
        },
        "derived": {"data_category": DERIVED, "transforms": _DERIVED_TRANSFORMS},
        "fictional": {
            "data_category": FICTIONAL,
            "constructs": _FICTIONAL_CONSTRUCTS,
            "counts": {
                "parties": count(Party),
                "candidates": count(Candidate),
                "district_plans": count(DistrictPlan),
                "scenarios": count(Scenario),
            },
        },
        "simulated": {
            "data_category": SIMULATED,
            "outputs": _SIMULATED_OUTPUTS,
            "counts": {
                "elections": count(Election),
                "polls": count(Poll),
                "simulation_runs": count(SimulationRun),
                "forecast_runs": count(SimulationRun, SimulationRun.kind == "forecast"),
            },
        },
    }


# =========================================================================== validation
def validation_report(session: Session, *, elections: bool = False) -> dict[str, Any]:
    """``GET /api/data/validation`` — :func:`app.services.validation.validate_system` (cached per
    set of election states; reconciling every election is slow on the real country)."""
    from app.services.validation import validate_system

    state = tuple(
        (r.id, r.status, iso(r.finalized_at), iso(r.simulated_at)) for r in all_election_refs(session)
    )
    plan = active_system(session)["plan"]

    def build() -> dict[str, Any]:
        rep = validate_system(session, elections=elections)
        d = rep.to_dict()
        d["data_category"] = FICTIONAL
        d["elections_checked"] = elections
        return d

    return cached(session, "validation", (elections, state, plan["id"] if plan else None), build)
