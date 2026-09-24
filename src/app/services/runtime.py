"""Engine inputs reconstructed from the database, with caching of immutable data.

Services persist elections relationally; the pure engines need NumPy/dataclass inputs.  This
module is the bridge:

* :func:`get_frame` — the :class:`~app.geography.frame.GeographyFrame` of the active geography
  with ``province_ids`` / ``muni_ids`` / ``unit_ids`` aligned to the database rows.  The frame
  comes from the REAL processed store, from the synthetic test geography registered for the
  database (``app_meta.geography_source``), or — as a fallback — from the database rows.
* :func:`plan_mapping` — the stored House plan aligned to the frame (cached).
* :func:`get_model` — the calibrated :class:`~app.simulation.structural.StructuralModel`, cached by
  (frame content, scenario document hash).
* :func:`election_inputs` — everything an engine needs for one stored election
  (:class:`ElectionInputs`: races as :class:`~app.elections.types.RaceSpec`, the
  :class:`~app.simulation.voting.ElectionContext`, race metadata for the election night, …).
* :func:`load_final_race_votes` / :func:`load_timeline` — the persisted SIMULATED result and
  reporting timeline back as engine types (exact round trips).

The context of an election is rebuilt from what was stored when it was created (ballot lines with
quality snapshots, incumbents on the race rows, the president's party and the Senate holdovers in
the ``election-setup`` run, campaign allocations), so the same election always yields the same
inputs, however the office holders and candidates evolve afterwards.
"""

from __future__ import annotations

import dataclasses
import hashlib
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from itertools import chain
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns.config import NATIONAL_CODE, config_from_spec
from app.campaigns.engine import combine_effects
from app.campaigns.types import CampaignEffects, CombinedCampaignEffects
from app.core.config import get_constitution
from app.core.constitution import ConstitutionConfig, ElectoralSystem, RaceType
from app.core.errors import DataNotPreparedError, ElectionError, NotFoundError
from app.core.logging import Timer, get_logger
from app.core.rng import config_hash
from app.districts.service import PlanMapping, active_plan, load_plan_mapping
from app.elections.calendar import CycleContents, ElectionCalendar
from app.elections.types import BallotLine, RaceSpec, RaceVotes
from app.geography.frame import DEMOGRAPHIC_VARIABLES, GeographyFrame
from app.geography.synthetic import SyntheticGeography, frame_from_tables, synthetic_geography
from app.models import (
    ApportionmentSeat,
    BallotCandidate,
    Campaign,
    CampaignAllocation,
    Candidate,
    DistrictPlan,
    Election,
    ElectionResult,
    GeoUnit,
    GeoUnitDemographics,
    GeoVintage,
    Municipality,
    Party,
    Province,
    Race,
    ReportingEvent,
    ReportingEventUnit,
    Scenario,
    TurnoutResult,
)
from app.reporting.live import RaceMeta
from app.reporting.timeline import Timeline
from app.scenarios.loader import load_scenario_text
from app.scenarios.schema import CandidateSpec, ScenarioDocument
from app.services._common import (
    GEOGRAPHY_SOURCE_KEY,
    INDEPENDENT_COLOR,
    active_vintage,
    dumps,
    get_meta,
    latest_run,
    loads,
    set_meta,
)
from app.simulation.structural import StructuralModel
from app.simulation.voting import (
    ElectionContext,
    IncumbentInfo,
    expected_party_state,
    prepare_race,
)

log = get_logger(__name__)

_lock = threading.RLock()

#: Run kinds written by :mod:`app.services.elections` (read back here).
RUN_SETUP = "election-setup"
RUN_SIMULATE = "election"
RUN_FINALIZE = "election-final"

#: Race types elected province-wide / municipality-wide.
PROVINCE_WIDE: frozenset[RaceType] = frozenset(
    {RaceType.PRESIDENT_PROVINCE, RaceType.SENATE, RaceType.GOVERNOR, RaceType.PROVINCIAL_LEGISLATURE}
)
MUNICIPALITY_WIDE: frozenset[RaceType] = frozenset({RaceType.MAYOR, RaceType.MUNICIPAL_COUNCIL})
#: Race types whose results are also aggregated by House district.
DISTRICT_LEVEL_TYPES: frozenset[RaceType] = frozenset(
    {RaceType.HOUSE, RaceType.PRESIDENT, RaceType.PRESIDENT_PROVINCE}
)


# =========================================================================== geography source
@dataclass(frozen=True)
class GeographySource:
    """Where the frame of a database's geography comes from.

    ``kind``: ``store`` (REAL processed CBS store of ``year``), ``synthetic`` (the synthetic test
    country with ``synthetic`` = (seed, cells, cell_m, muni_block, population_scale)) or
    ``database`` (rebuilt from the database rows).
    """

    kind: str
    year: int
    synthetic: tuple[int, int, float, int, float] | None = None

    def to_json(self) -> str:
        return dumps({"kind": self.kind, "year": self.year, "synthetic": self.synthetic})

    @classmethod
    def from_json(cls, text: str) -> GeographySource:
        data = loads(text)
        syn = data.get("synthetic")
        return cls(
            kind=str(data["kind"]),
            year=int(data["year"]),
            synthetic=None
            if syn is None
            else (int(syn[0]), int(syn[1]), float(syn[2]), int(syn[3]), float(syn[4])),
        )


def register_geography_source(session: Session, source: GeographySource) -> None:
    """Record how :func:`get_frame` must build the frame of this database."""
    set_meta(session, GEOGRAPHY_SOURCE_KEY, source.to_json())


def geography_source(session: Session) -> GeographySource:
    """The registered geography source, else the REAL store of the active vintage when it is
    prepared, else the database rows."""
    text = get_meta(session, GEOGRAPHY_SOURCE_KEY)
    if text:
        return GeographySource.from_json(text)
    vintage = active_vintage(session)
    if vintage is None:
        raise DataNotPreparedError("no geography loaded in the database (run the setup first)")
    from app.geography.store import is_prepared

    if is_prepared(vintage.year):
        return GeographySource("store", vintage.year)
    return GeographySource("database", vintage.year)


@lru_cache(maxsize=4)
def synthetic_world(
    seed: int = 7,
    cells: int = 10,
    cell_m: float = 4000.0,
    muni_block: int = 2,
    population_scale: float = 1.0,
) -> SyntheticGeography:
    """Cached :func:`app.geography.synthetic.synthetic_geography` (deterministic in its arguments)."""
    return synthetic_geography(
        seed=seed, cells=cells, cell_m=cell_m, muni_block=muni_block, population_scale=population_scale
    )


_db_frames: OrderedDict[tuple, GeographyFrame] = OrderedDict()
_frames: OrderedDict[tuple, GeographyFrame] = OrderedDict()
_models: OrderedDict[tuple[str, str], StructuralModel] = OrderedDict()
_mappings: OrderedDict[tuple, PlanMapping] = OrderedDict()
_scenarios: OrderedDict[str, ScenarioDocument] = OrderedDict()


def _cache_put(cache: OrderedDict, key: Any, value: Any, size: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > size:
        cache.popitem(last=False)


def _cache_get(cache: OrderedDict, key: Any) -> Any:
    value = cache.get(key)
    if value is not None:
        cache.move_to_end(key)
    return value


def clear_caches() -> None:
    """Forget every cached frame, model, plan mapping and parsed scenario (tests, rebuilds)."""
    with _lock:
        for c in (_db_frames, _frames, _models, _mappings, _scenarios):
            c.clear()
        synthetic_world.cache_clear()


def _base_frame(session: Session, source: GeographySource, vintage: GeoVintage) -> GeographyFrame:
    if source.kind == "store":
        from app.geography.store import load_frame

        return load_frame(source.year)
    if source.kind == "synthetic":
        if source.synthetic is None:
            raise DataNotPreparedError("synthetic geography source without parameters")
        return synthetic_world(*source.synthetic).frame
    key = (vintage.year, vintage.unit_count, vintage.population_total, str(vintage.built_at))
    with _lock:
        frame = _cache_get(_db_frames, key)
    if frame is None:
        frame = frame_from_database(session, vintage)
        with _lock:
            _cache_put(_db_frames, key, frame, 2)
    return frame


def frame_from_database(session: Session, vintage: GeoVintage) -> GeographyFrame:
    """Rebuild a :class:`GeographyFrame` from the rows of a loaded vintage (fallback when neither
    the processed store nor a registered synthetic geography is available).

    ``log_density`` = ln(1 + density) and ``urbanity`` = 6 − CBS urbanity class are derived like
    the geography pipeline does; missing indicators stay NaN (the model treats them as the mean).
    """
    with Timer(log, f"frame from database (vintage {vintage.year})"):
        provinces = pd.DataFrame(
            session.execute(
                select(Province.code, Province.name, Province.cbs_code).order_by(
                    Province.sort_order, Province.code
                )
            ).all(),
            columns=["code", "name", "cbs_code"],
        )
        munis = pd.DataFrame(
            session.execute(
                select(Municipality.cbs_code, Municipality.name, Province.code)
                .join(Province, Province.id == Municipality.province_id)
                .where(Municipality.vintage_id == vintage.id)
            ).all(),
            columns=["code", "name", "province_code"],
        )
        demo_cols = [c for c in DEMOGRAPHIC_VARIABLES if hasattr(GeoUnitDemographics, c)]
        stmt = (
            select(
                GeoUnit.cbs_code,
                GeoUnit.name,
                GeoUnit.wijk_code,
                Municipality.cbs_code,
                Province.code,
                GeoUnit.population,
                GeoUnit.eligible_voters_est,
                GeoUnit.area_km2,
                GeoUnit.land_area_km2,
                GeoUnit.density,
                GeoUnit.urbanity_class,
                GeoUnit.address_density,
                GeoUnit.centroid_x,
                GeoUnit.centroid_y,
                GeoUnit.centroid_lon,
                GeoUnit.centroid_lat,
                *[getattr(GeoUnitDemographics, c) for c in demo_cols],
                GeoUnitDemographics.imputed_fields,
            )
            .join(Municipality, Municipality.id == GeoUnit.municipality_id)
            .join(Province, Province.id == GeoUnit.province_id)
            .outerjoin(GeoUnitDemographics, GeoUnitDemographics.geo_unit_id == GeoUnit.id)
            .where(GeoUnit.vintage_id == vintage.id)
        )
        cols = [
            "code",
            "name",
            "wijk_code",
            "municipality_code",
            "province_code",
            "population",
            "eligible_voters_est",
            "area_km2",
            "land_area_km2",
            "density",
            "urbanity_class",
            "address_density",
            "centroid_x",
            "centroid_y",
            "centroid_lon",
            "centroid_lat",
            *demo_cols,
            "imputed_fields",
        ]
        units = pd.DataFrame(session.execute(stmt).all(), columns=cols)
        if units.empty:
            raise DataNotPreparedError(f"vintage {vintage.year} has no geographic units in the database")
        units["log_density"] = np.log1p(pd.to_numeric(units["density"], errors="coerce").clip(lower=0))
        urb = pd.to_numeric(units["urbanity_class"], errors="coerce")
        units["urbanity"] = 6.0 - urb
        for c in DEMOGRAPHIC_VARIABLES:
            if c not in units.columns:
                units[c] = np.nan
            units[c] = pd.to_numeric(units[c], errors="coerce").astype(float)
        units["imputed_fields"] = units["imputed_fields"].fillna("")
        return frame_from_tables(units, munis, provinces, year=vintage.year)


# =========================================================================== frame
def frame_token(frame: GeographyFrame) -> str:
    """Content hash of a frame (year, codes, populations, eligible voters, demographics); cached
    on the frame object.  Used as the model cache key so different geographies never collide."""
    tok = frame.__dict__.get("_nlfed_token")
    if tok is None:
        h = hashlib.blake2b(digest_size=12)
        h.update(str(frame.year).encode())
        for seq in (frame.province_codes, frame.muni_codes, frame.unit_codes):
            h.update("\x1f".join(seq).encode("utf-8"))
            h.update(b"\x1e")
        for arr in (
            frame.unit_muni,
            frame.unit_province,
            frame.unit_population,
            frame.unit_eligible,
            frame.unit_urbanity_class,
            frame.unit_xy,
            frame.unit_demo,
        ):
            h.update(np.ascontiguousarray(arr).tobytes())
        tok = h.hexdigest()
        frame.__dict__["_nlfed_token"] = tok
    return tok


def _digest(arr: np.ndarray) -> str:
    return hashlib.blake2b(np.ascontiguousarray(arr).tobytes(), digest_size=10).hexdigest()


def get_frame(session: Session) -> GeographyFrame:
    """The active geography frame with ``province_ids``/``muni_ids``/``unit_ids`` = database ids.

    Raises :class:`DataNotPreparedError` when no geography is loaded or the database rows do not
    match the frame (e.g. a store rebuilt without reloading the database).
    """
    vintage = active_vintage(session)
    if vintage is None:
        raise DataNotPreparedError("no geography loaded in the database (run the setup first)")
    source = geography_source(session)
    base = _base_frame(session, source, vintage)
    pid = dict(session.execute(select(Province.code, Province.id)).tuples().all())
    mid = dict(
        session.execute(
            select(Municipality.cbs_code, Municipality.id).where(Municipality.vintage_id == vintage.id)
        )
        .tuples()
        .all()
    )
    uid_rows = session.execute(
        select(GeoUnit.cbs_code, GeoUnit.id).where(GeoUnit.vintage_id == vintage.id)
    ).all()
    province_ids = np.array([pid.get(c, 0) for c in base.province_codes], dtype=np.int64)
    muni_ids = np.array([mid.get(c, 0) for c in base.muni_codes], dtype=np.int64)
    if uid_rows:
        codes, ids = zip(*uid_rows, strict=True)
        pos = pd.Index(codes).get_indexer(base.unit_codes)
        id_arr = np.asarray(ids, dtype=np.int64)
        unit_ids = np.where(pos >= 0, id_arr[np.maximum(pos, 0)], 0)
    else:
        unit_ids = np.zeros(base.n_units, dtype=np.int64)
    problems = []
    for name, arr in (("provinces", province_ids), ("municipalities", muni_ids), ("units", unit_ids)):
        missing = int((arr <= 0).sum())
        if missing:
            problems.append(f"{missing} {name}")
    if problems:
        raise DataNotPreparedError(
            f"the database does not match the {source.kind} geography {source.year}: missing "
            + ", ".join(problems)
            + " (reload the geography)"
        )
    key = (id(base), _digest(province_ids), _digest(muni_ids), _digest(unit_ids))
    with _lock:
        cached = _cache_get(_frames, key)
        if cached is not None and cached.__dict__.get("_nlfed_base") is base:
            return cached
        frame = dataclasses.replace(base, province_ids=province_ids, muni_ids=muni_ids, unit_ids=unit_ids)
        frame.__dict__["_nlfed_base"] = base
        frame.__dict__["_nlfed_token"] = frame_token(base)
        _cache_put(_frames, key, frame, 4)
    return frame


def unit_index_of_ids(frame: GeographyFrame, ids: np.ndarray) -> np.ndarray:
    """Frame unit index of ``geo_unit`` ids (−1 for ids outside the frame)."""
    if frame.unit_ids is None:
        raise DataNotPreparedError("frame has no database ids (use get_frame)")
    lookup = frame.__dict__.get("_nlfed_unit_lookup")
    if lookup is None:
        lookup = np.full(int(frame.unit_ids.max()) + 1, -1, dtype=np.int64)
        lookup[frame.unit_ids] = np.arange(frame.n_units)
        frame.__dict__["_nlfed_unit_lookup"] = lookup
    ids = np.asarray(ids, dtype=np.int64)
    out = np.full(len(ids), -1, dtype=np.int64)
    ok = (ids >= 0) & (ids < len(lookup))
    out[ok] = lookup[ids[ok]]
    return out


def units_by_group(index: np.ndarray, n_groups: int) -> list[np.ndarray]:
    """Sorted member indices of every group ``0..n_groups-1`` of an (N,) group index."""
    order = np.argsort(index, kind="stable")
    bounds = np.searchsorted(index[order], np.arange(n_groups + 1))
    return [order[bounds[g] : bounds[g + 1]] for g in range(n_groups)]


def _groups(frame: GeographyFrame, name: str) -> list[np.ndarray]:
    cache = frame.__dict__.get("_nlfed_groups")
    if cache is None:
        cache = {}
        frame.__dict__["_nlfed_groups"] = cache
    if name not in cache:
        if name == "province":
            cache[name] = units_by_group(frame.unit_province, frame.n_provinces)
        else:
            cache[name] = units_by_group(frame.unit_muni, frame.n_munis)
    return cache[name]


def province_units(frame: GeographyFrame, code: str) -> np.ndarray:
    """Sorted unit indices of a province."""
    return _groups(frame, "province")[frame.province_index(code)]


def municipality_units(frame: GeographyFrame, code: str) -> np.ndarray:
    """Sorted unit indices of a municipality."""
    return _groups(frame, "muni")[frame.muni_index(code)]


# =========================================================================== scenario / model
def scenario_hash(doc: ScenarioDocument) -> str:
    """Stable hash of a scenario document's content."""
    return config_hash(doc.model_dump_json())


def parse_stored_scenario(document: str) -> ScenarioDocument:
    """Parse a stored scenario YAML document (cached by text hash)."""
    key = config_hash(document)
    with _lock:
        doc = _cache_get(_scenarios, key)
    if doc is None:
        doc = load_scenario_text(document)
        with _lock:
            _cache_put(_scenarios, key, doc, 16)
    return doc


def get_model(frame: GeographyFrame, scenario_doc: ScenarioDocument) -> StructuralModel:
    """The calibrated structural model of ``scenario_doc`` on ``frame`` (cached by frame content
    and scenario hash; references to places missing from the frame are warnings, see
    ``StructuralModel.build(strict=False)``)."""
    key = (frame_token(frame), scenario_hash(scenario_doc))
    with _lock:
        model = _cache_get(_models, key)
    if model is None:
        model = StructuralModel.build(frame, scenario_doc, strict=False)
        with _lock:
            _cache_put(_models, key, model, 6)
    return model


def plan_mapping(
    session: Session, plan_id: int | None = None, frame: GeographyFrame | None = None
) -> PlanMapping:
    """A stored House plan (default: the active one) aligned to the frame (cached)."""
    plan = session.get(DistrictPlan, plan_id) if plan_id is not None else active_plan(session)
    if plan is None:
        raise NotFoundError("no district plan" if plan_id is None else f"district plan {plan_id} not found")
    frame = frame or get_frame(session)
    key = (
        frame_token(frame),
        plan.id,
        str(plan.created_at),
        plan.config_hash,
        int(plan.seed),
        plan.total_districts,
    )
    with _lock:
        mapping = _cache_get(_mappings, key)
    if mapping is None:
        mapping = load_plan_mapping(session, plan.id, frame)
        if (mapping.unit_district < 0).any():
            raise ElectionError(
                f"district plan {plan.id} leaves {(mapping.unit_district < 0).sum()} units unassigned"
            )
        with _lock:
            _cache_put(_mappings, key, mapping, 4)
    return mapping


def district_units(mapping: PlanMapping) -> list[np.ndarray]:
    """Sorted frame unit indices of every district of ``mapping`` (in mapping order; cached on
    the mapping)."""
    groups = mapping.__dict__.get("_nlfed_district_units")
    if groups is None:
        groups = units_by_group(mapping.unit_district, mapping.n_districts)
        mapping.__dict__["_nlfed_district_units"] = groups
    return groups


# =========================================================================== candidates / campaigns
def candidate_spec_from_row(
    row: Candidate,
    party_code: str | None,
    home_province_code: str | None,
    *,
    incumbent_office: str | None = None,
) -> CandidateSpec:
    """A :class:`CandidateSpec` for a stored (FICTIONAL) person."""
    return CandidateSpec(
        key=row.key,
        first_name=row.first_name,
        last_name=row.last_name,
        gender=row.gender,
        birth_date=row.birth_date,
        party=party_code,
        home_municipality=row.home_municipality_code,
        home_province=home_province_code,
        quality=float(np.clip(row.quality or 0.0, -3.0, 3.0)),
        campaign_strength=float(np.clip(row.campaign_strength or 0.0, -3.0, 3.0)),
        fundraising=max(float(row.fundraising or 0.0), 0.0),
        favorability=row.favorability,
        bio=row.bio,
        incumbent_office=incumbent_office,
    )


def campaign_context_effects(
    doc: ScenarioDocument, effects: Iterable[CampaignEffects], parties: Iterable[str]
) -> tuple[CombinedCampaignEffects, dict[Any, dict[str, float]]]:
    """Combine per-party campaign effects and map them onto ``ElectionContext.campaign_effects``.

    National effects become ``("national", "NL")``, province and municipality effects the
    matching geography keys, district effects the race key ``HOUSE-<district>``.  A target's value
    is its capped total including the national effect minus the national effect, so national +
    target never exceeds the cap.  A party's supporter-turnout effect ``t`` enters as the
    equivalent vote-share shift of mobilising its supporters, ``t · (1 − turnout_base)`` (the
    context has no geography-specific party turnout).
    """
    cfg = config_from_spec(doc.campaigns)
    combined = combine_effects(list(effects), cfg)
    known = set(parties)
    mobilisation = 1.0 - float(doc.environment.turnout_base)
    keys = sorted(set(combined.persuasion) | set(combined.turnout))
    out: dict[Any, dict[str, float]] = {}
    national = ("national", NATIONAL_CODE)

    def total(level: str, code: str, party: str) -> float:
        p = combined.effect(level, code, party, "persuasion", include_national=True)
        t = combined.effect(level, code, party, "turnout", include_national=True)
        return p + mobilisation * t

    for party in sorted(combined.parties):
        if party not in known:
            continue
        base = total(*national, party) if national in keys else 0.0
        if base:
            out.setdefault(national, {})[party] = base
        for level, code in keys:
            if level == "national":
                continue
            has = party in combined.persuasion.get((level, code), {}) or party in combined.turnout.get(
                (level, code), {}
            )
            if not has:
                continue
            v = total(level, code, party) - base
            if v == 0.0:
                continue
            key: Any = f"HOUSE-{code}" if level == "district" else (level, code)
            out.setdefault(key, {})[party] = v
    return combined, out


def stored_campaign_effects(
    session: Session, election_id: int, party_codes: Mapping[int, str]
) -> list[CampaignEffects]:
    """Per-party realised campaign effects rebuilt from ``campaign_allocation`` rows (the sum of the
    per-allocation shares is the target's realised effect)."""
    rows = session.execute(
        select(
            Campaign.party_id,
            Campaign.seed,
            CampaignAllocation.target_level,
            CampaignAllocation.target_code,
            CampaignAllocation.realized_effect,
            CampaignAllocation.turnout_effect,
            CampaignAllocation.amount,
        )
        .join(Campaign, Campaign.id == CampaignAllocation.campaign_id)
        .where(Campaign.election_id == election_id)
        .order_by(Campaign.id, CampaignAllocation.id)
    ).all()
    per: dict[str, dict[str, Any]] = {}
    for party_id, seed, level, code, real, turn, amount in rows:
        party = party_codes.get(party_id) if party_id is not None else None
        if party is None:
            continue
        d = per.setdefault(party, {"p": {}, "t": {}, "s": {}, "seed": int(seed)})
        k = (str(level), str(code))
        d["p"][k] = d["p"].get(k, 0.0) + float(real or 0.0)
        d["t"][k] = d["t"].get(k, 0.0) + float(turn or 0.0)
        d["s"][k] = d["s"].get(k, 0.0) + float(amount or 0.0)
    return [
        CampaignEffects(
            party=party,
            persuasion=d["p"],
            turnout=d["t"],
            expected_persuasion={},
            expected_turnout={},
            spend=d["s"],
            allocations=[],
            seed=d["seed"],
        )
        for party, d in sorted(per.items())
    ]


# =========================================================================== election inputs
@dataclass
class ElectionInputs:
    """Engine inputs of one stored election (see :func:`election_inputs`)."""

    election_id: int
    year: int
    election_type: str
    election_date: date
    status: str
    seed: int
    frame: GeographyFrame
    scenario: ScenarioDocument
    scenario_hash: str
    model: StructuralModel
    context: ElectionContext
    races: dict[str, RaceSpec]
    race_ids: dict[str, int]
    line_ids: dict[str, dict[str, int]]
    race_meta: dict[str, RaceMeta]
    ev_by_province: dict[str, int]
    holdover_senate: dict[str, int]
    holdover_seats: dict[str, str | None]
    unit_district: np.ndarray
    district_codes: list[str]
    district_ids: list[int]
    plan_id: int | None
    constitution: ConstitutionConfig
    cycle: CycleContents
    party_ids: dict[str, int] = field(default_factory=dict)
    president_party: str | None = None

    @property
    def race_types(self) -> dict[str, RaceType]:
        return {k: RaceType(r.race_type) for k, r in self.races.items()}

    def races_of(self, *types: RaceType) -> list[str]:
        """Race codes of the given types (in creation order)."""
        wanted = set(types)
        return [k for k, r in self.races.items() if RaceType(r.race_type) in wanted]


def election_scenario(session: Session, election: Election) -> tuple[ScenarioDocument, Scenario]:
    """The scenario document an election was created from."""
    if election.scenario_id is None:
        raise ElectionError(f"election {election.id} has no scenario")
    row = session.get(Scenario, election.scenario_id)
    if row is None:
        raise NotFoundError(f"scenario {election.scenario_id} not found")
    return parse_stored_scenario(row.document), row


def get_election(session: Session, election_id: int) -> Election:
    """The election row or :class:`NotFoundError`."""
    el = session.get(Election, election_id)
    if el is None:
        raise NotFoundError(f"election {election_id} not found")
    return el


def election_inputs(
    session: Session,
    election_id: int,
    *,
    frame: GeographyFrame | None = None,
    setup_snapshot: Mapping[str, Any] | None = None,
) -> ElectionInputs:
    """Rebuild every engine input of a stored election (deterministic).

    ``setup_snapshot`` overrides the stored ``election-setup`` snapshot (president's party, Senate
    holdovers); only :func:`app.services.elections.create_election` passes it, while the election
    is still being created.
    """
    el = get_election(session, election_id)
    frame = frame or get_frame(session)
    doc, _row = election_scenario(session, el)
    model = get_model(frame, doc)
    mapping = plan_mapping(session, el.district_plan_id, frame) if el.district_plan_id else None
    parties = {p.id: p for p in session.scalars(select(Party))}
    party_code = {pid: p.code for pid, p in parties.items()}
    prov_code = dict(session.execute(select(Province.id, Province.code)).tuples().all())
    race_rows = session.scalars(select(Race).where(Race.election_id == el.id).order_by(Race.id)).all()
    if not race_rows:
        raise ElectionError(f"election {el.id} has no races")
    race_ids = [r.id for r in race_rows]
    ballots = session.scalars(
        select(BallotCandidate)
        .where(BallotCandidate.race_id.in_(race_ids))
        .order_by(BallotCandidate.race_id, BallotCandidate.ballot_order)
    ).all()
    by_race: dict[int, list[BallotCandidate]] = {}
    for b in ballots:
        by_race.setdefault(b.race_id, []).append(b)
    cand_ids = {c for b in ballots for c in (b.candidate_id, b.running_mate_id) if c is not None}
    cand_ids |= {r.incumbent_candidate_id for r in race_rows if r.incumbent_candidate_id is not None}
    cands = (
        {c.id: c for c in session.scalars(select(Candidate).where(Candidate.id.in_(cand_ids)))}
        if cand_ids
        else {}
    )
    muni_ids = {r.municipality_id for r in race_rows if r.municipality_id is not None}
    muni_code = (
        dict(
            session.execute(
                select(Municipality.id, Municipality.cbs_code).where(Municipality.id.in_(muni_ids))
            )
            .tuples()
            .all()
        )
        if muni_ids
        else {}
    )
    dist_code: dict[int, str] = {}
    if mapping is not None:
        dist_code = {int(i): c for i, c in zip(mapping.district_ids, mapping.district_codes, strict=True)}
    doc_cands = {c.key: c for c in doc.candidates}

    def home_prov(c: Candidate | None) -> str | None:
        return None if c is None or c.home_province_id is None else prov_code.get(c.home_province_id)

    extra: dict[str, CandidateSpec] = {}
    for c in cands.values():
        if c.key in doc_cands or c.key in extra:
            continue
        pcode = party_code.get(c.party_id) if c.party_id is not None else None
        if pcode is not None and pcode not in model.party_index:
            pcode = None
        extra[c.key] = candidate_spec_from_row(c, pcode, home_prov(c))

    races: dict[str, RaceSpec] = {}
    line_ids: dict[str, dict[str, int]] = {}
    race_meta: dict[str, RaceMeta] = {}
    incumbents: dict[str, IncumbentInfo] = {}
    d_units = district_units(mapping) if mapping is not None else []
    d_index = {c: i for i, c in enumerate(mapping.district_codes)} if mapping is not None else {}
    for r in race_rows:
        rt = RaceType(r.race_type)
        pcode = prov_code.get(r.province_id) if r.province_id is not None else None
        dcode = dist_code.get(r.district_id) if r.district_id is not None else None
        mcode = muni_code.get(r.municipality_id) if r.municipality_id is not None else None
        if rt == RaceType.PRESIDENT:
            units = np.arange(frame.n_units, dtype=np.int64)
        elif rt in PROVINCE_WIDE:
            units = province_units(frame, str(pcode))
        elif rt == RaceType.HOUSE:
            if dcode is None or dcode not in d_index:
                raise ElectionError(f"{r.code}: district not in the election's plan")
            units = d_units[d_index[dcode]]
        elif rt in MUNICIPALITY_WIDE:
            units = municipality_units(frame, str(mcode))
        else:  # pragma: no cover - every RaceType is handled above
            raise ElectionError(f"unsupported race type {rt}")
        lines: list[BallotLine] = []
        lids: dict[str, int] = {}
        labels: dict[str, str] = {}
        lparties: dict[str, str | None] = {}
        colors: dict[str, str] = {}
        for b in by_race.get(r.id, []):
            c = cands.get(b.candidate_id) if b.candidate_id is not None else None
            mate = cands.get(b.running_mate_id) if b.running_mate_id is not None else None
            key = b.line_key or (c.key if c is not None else b.party_code_snapshot)
            if key is None:
                raise ElectionError(f"{r.code}: ballot line {b.id} has no key")
            spec = doc_cands.get(c.key) if c is not None else None
            quality = (
                float(b.quality_snapshot)
                if b.quality_snapshot is not None
                else (float(c.quality) if c is not None else 0.0)
            )
            lines.append(
                BallotLine(
                    key=key,
                    party_code=b.party_code_snapshot,
                    candidate_key=c.key if c is not None else None,
                    running_mate_key=mate.key if mate is not None else None,
                    label=b.ballot_name,
                    quality=quality,
                    incumbent=bool(b.is_incumbent),
                    home_province=(
                        spec.home_province if spec is not None and spec.home_province else home_prov(c)
                    ),
                    home_municipality=c.home_municipality_code if c is not None else None,
                    running_mate_home_province=home_prov(mate),
                    withdrawn=bool(b.withdrawn),
                )
            )
            lids[key] = b.id
            labels[key] = b.ballot_name
            lparties[key] = b.party_code_snapshot
            colors[key] = b.party_color_snapshot or INDEPENDENT_COLOR
        inc_party = party_code.get(r.incumbent_party_id) if r.incumbent_party_id is not None else None
        spec = RaceSpec(
            key=r.code,
            race_type=rt,
            unit_index=units,
            lines=lines,
            electoral_system=ElectoralSystem(r.electoral_system),
            seats=int(r.seats),
            electoral_votes=r.electoral_votes,
            province_code=pcode,
            district_code=dcode,
            municipality_code=mcode,
            incumbent_party=inc_party,
            is_open_seat=bool(r.is_open_seat),
            is_special=bool(r.is_special),
        )
        races[r.code] = spec
        line_ids[r.code] = lids
        if r.incumbent_candidate_id is not None or inc_party is not None:
            inc = cands.get(r.incumbent_candidate_id) if r.incumbent_candidate_id is not None else None
            incumbents[r.code] = IncumbentInfo(
                party=inc_party,
                candidate_key=inc.key if inc is not None else None,
                running=any(ln.incumbent for ln in lines),
            )
        race_meta[r.code] = RaceMeta(
            race_type=rt,
            electoral_votes=r.electoral_votes,
            province_code=pcode,
            district_code=dcode,
            municipality_code=mcode,
            line_labels=labels,
            line_parties=lparties,
            line_colors=colors,
            parent="PRES" if rt == RaceType.PRESIDENT_PROVINCE else None,
            incumbent_party=inc_party,
            name=r.name,
        )

    if setup_snapshot is not None:
        snapshot = dict(setup_snapshot)
    else:
        setup = latest_run(session, el.id, RUN_SETUP)
        snapshot = loads(setup.summary_json) if setup is not None else {}
    president_party = snapshot.get("president_party")
    if president_party is not None and president_party not in model.party_index:
        president_party = None
    holdover_seats: dict[str, str | None] = dict(snapshot.get("holdover_senate", {}))
    holdover: dict[str, int] = {}
    for p in holdover_seats.values():
        key = p if p is not None else "independent"
        holdover[key] = holdover.get(key, 0) + 1
    effects = stored_campaign_effects(session, el.id, party_code)
    _, ctx_effects = campaign_context_effects(doc, effects, model.party_codes) if effects else (None, {})
    context = ElectionContext.from_scenario(
        doc,
        president_party=president_party,
        incumbents=incumbents,
        extra_candidates=list(extra.values()),
        campaign_effects=ctx_effects,
    )
    if president_party is None:
        # the setup snapshot records the president's party; None there means "no president"
        context.president_party = None if "president_party" in snapshot else context.president_party
    ev = {}
    if el.apportionment_id is not None:
        ev_rows = session.execute(
            select(Province.code, ApportionmentSeat.electoral_votes)
            .join(Province, Province.id == ApportionmentSeat.province_id)
            .where(ApportionmentSeat.apportionment_id == el.apportionment_id)
        ).all()
        ev_map = {c: int(v) for c, v in ev_rows}
        ev = {c: ev_map[c] for c in frame.province_codes if c in ev_map}
    calendar = ElectionCalendar.from_config()
    return ElectionInputs(
        election_id=el.id,
        year=el.year,
        election_type=el.election_type,
        election_date=el.election_date,
        status=el.status,
        seed=int(el.seed),
        frame=frame,
        scenario=doc,
        scenario_hash=scenario_hash(doc),
        model=model,
        context=context,
        races=races,
        race_ids={r.code: r.id for r in race_rows},
        line_ids=line_ids,
        race_meta=race_meta,
        ev_by_province=ev,
        holdover_senate=holdover,
        holdover_seats=holdover_seats,
        unit_district=mapping.unit_district if mapping is not None else np.full(frame.n_units, -1),
        district_codes=list(mapping.district_codes) if mapping is not None else [],
        district_ids=[int(i) for i in mapping.district_ids] if mapping is not None else [],
        plan_id=mapping.plan_id if mapping is not None else None,
        constitution=get_constitution(),
        cycle=calendar.cycle(el.year),
        party_ids={p.code: pid for pid, p in parties.items()},
        president_party=context.president_party,
    )


# =========================================================================== expectations
def race_expectations(
    inputs: ElectionInputs, codes: Iterable[str] | None = None
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Deterministic pre-election expectation ``(expected_shares (n, L), expected_turnout (n,))``
    per race — exactly what :func:`app.simulation.voting.simulate_election` stores in
    ``RaceVotes.expected_*`` (the national ``PRES`` race is stitched from its province contests)."""
    wanted = list(inputs.races) if codes is None else list(codes)
    model, ctx = inputs.model, inputs.context
    state = expected_party_state(model, ctx)
    children = [k for k, r in inputs.races.items() if RaceType(r.race_type) == RaceType.PRESIDENT_PROVINCE]
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    need = set(wanted)
    if "PRES" in need and children:
        need |= set(children)
    for code in inputs.races:
        if code not in need:
            continue
        spec = inputs.races[code]
        if RaceType(spec.race_type) == RaceType.PRESIDENT and children:
            continue
        plan = prepare_race(model, spec, ctx, state)
        out[code] = (plan.expected_shares, plan.expected_turnout)
    if "PRES" in need and children and "PRES" in inputs.races:
        parent = inputs.races["PRES"]
        col = {k: i for i, k in enumerate(parent.line_keys)}
        n = len(parent.unit_index)
        pos = np.full(inputs.frame.n_units, -1, dtype=np.int64)
        pos[parent.unit_index] = np.arange(n)
        exp = np.zeros((n, len(col)))
        tur = np.zeros(n)
        for code in children:
            spec = inputs.races[code]
            rows = pos[spec.unit_index]
            es, et = out[code]
            for j, lk in enumerate(spec.line_keys):
                if lk in col:
                    exp[rows, col[lk]] = es[:, j]
            tur[rows] = et
        out["PRES"] = (exp, tur)
    return {k: out[k] for k in wanted if k in out}


# =========================================================================== final results
def int_rows(rows: Sequence[Sequence[Any]], width: int) -> np.ndarray:
    """``(len(rows), width)`` int64 matrix of SQL result rows.

    Converting SQLAlchemy ``Row`` objects with ``np.asarray`` probes every row for the array
    protocols (≈ 10 µs per row); flattening them through their iterators is ~40× faster."""
    if not rows:
        return np.zeros((0, width), dtype=np.int64)
    flat = np.fromiter(chain.from_iterable(rows), dtype=np.int64, count=len(rows) * width)
    return flat.reshape(-1, width)


def map_ids(ids: np.ndarray, mapping: Mapping[int, int]) -> np.ndarray:
    """Vectorised ``[mapping[i] for i in ids]`` (each distinct id is looked up once)."""
    ids = np.asarray(ids, dtype=np.int64)
    if ids.size == 0:
        return np.zeros(0, dtype=np.int64)
    uniq, inverse = np.unique(ids, return_inverse=True)
    try:
        values = np.fromiter((mapping[int(u)] for u in uniq), dtype=np.int64, count=len(uniq))
    except KeyError as exc:
        raise ElectionError(f"stored rows reference unknown id {exc.args[0]}") from None
    return values[inverse.reshape(-1)]


def load_final_race_votes(
    session: Session,
    election_id: int,
    inputs: ElectionInputs,
    *,
    with_expectation: bool = True,
    codes: Sequence[str] | None = None,
) -> dict[str, RaceVotes]:
    """The persisted unit-level result of every race as :class:`RaceVotes` (after recounts, if
    any), with ``expected_shares`` / ``expected_turnout`` recomputed deterministically.

    Raises :class:`ElectionError` when the election has not been simulated.
    """
    frame = inputs.frame
    wanted = list(inputs.races) if codes is None else [c for c in codes if c in inputs.races]
    if not wanted:
        return {}
    rid_to_code = {inputs.race_ids[c]: c for c in wanted}
    race_pos = {c: i for i, c in enumerate(wanted)}
    line_pos: dict[int, tuple[int, int]] = {}
    for c in wanted:
        for j, key in enumerate(inputs.races[c].line_keys):
            line_pos[inputs.line_ids[c][key]] = (race_pos[c], j)
    rids = list(rid_to_code)
    vote_rows: list[tuple[int, int, int, int]] = []
    turn_rows: list[tuple[int, int, int, int, int, int]] = []
    for i in range(0, len(rids), 400):
        part = rids[i : i + 400]
        vote_rows += (
            session.execute(
                select(
                    ElectionResult.race_id,
                    ElectionResult.ballot_candidate_id,
                    ElectionResult.geo_unit_id,
                    ElectionResult.votes,
                ).where(ElectionResult.race_id.in_(part), ElectionResult.level == "unit")
            )
            .tuples()
            .all()
        )
        turn_rows += (
            session.execute(
                select(
                    TurnoutResult.race_id,
                    TurnoutResult.geo_unit_id,
                    TurnoutResult.eligible_voters,
                    TurnoutResult.ballots_cast,
                    TurnoutResult.blank_votes,
                    TurnoutResult.invalid_votes,
                ).where(TurnoutResult.race_id.in_(part), TurnoutResult.level == "unit")
            )
            .tuples()
            .all()
        )
    if not turn_rows:
        raise ElectionError(f"election {election_id} has no stored results (simulate it first)")
    v = int_rows(vote_rows, 4)
    t = int_rows(turn_rows, 6)
    race_of_id = {rid: race_pos[code] for rid, code in rid_to_code.items()}
    v_race = map_ids(v[:, 0], race_of_id)
    lp = np.column_stack(
        [
            map_ids(v[:, 1], {b: pos[0] for b, pos in line_pos.items()}),
            map_ids(v[:, 1], {b: pos[1] for b, pos in line_pos.items()}),
        ]
    )
    v_unit = unit_index_of_ids(frame, v[:, 2])
    t_race = map_ids(t[:, 0], race_of_id)
    t_unit = unit_index_of_ids(frame, t[:, 1])
    if (v_unit < 0).any() or (t_unit < 0).any():
        raise ElectionError(f"election {election_id}: stored results reference units outside the frame")
    v_order = np.argsort(v_race, kind="stable")
    t_order = np.argsort(t_race, kind="stable")
    v_bounds = np.searchsorted(v_race[v_order], np.arange(len(wanted) + 1))
    t_bounds = np.searchsorted(t_race[t_order], np.arange(len(wanted) + 1))
    expectations = race_expectations(inputs, wanted) if with_expectation else {}
    out: dict[str, RaceVotes] = {}
    for c in wanted:
        k = race_pos[c]
        spec = inputs.races[c]
        units = np.asarray(spec.unit_index, dtype=np.int64)
        n, L = len(units), len(spec.lines)
        ts = t_order[t_bounds[k] : t_bounds[k + 1]]
        if len(ts) != n:
            raise ElectionError(f"{c}: {len(ts)} unit turnout rows stored, expected {n}")
        rows_t = np.searchsorted(units, t_unit[ts])
        if not np.array_equal(units[np.minimum(rows_t, n - 1)], t_unit[ts]):
            raise ElectionError(f"{c}: stored units differ from the race's jurisdiction")
        eligible = np.zeros(n, dtype=np.int64)
        cast = np.zeros(n, dtype=np.int64)
        blank = np.zeros(n, dtype=np.int64)
        invalid = np.zeros(n, dtype=np.int64)
        eligible[rows_t] = t[ts, 2]
        cast[rows_t] = t[ts, 3]
        blank[rows_t] = t[ts, 4]
        invalid[rows_t] = t[ts, 5]
        vs = v_order[v_bounds[k] : v_bounds[k + 1]]
        if len(vs) != n * L:
            raise ElectionError(f"{c}: {len(vs)} unit vote rows stored, expected {n * L}")
        votes = np.zeros((n, L), dtype=np.int64)
        votes[np.searchsorted(units, v_unit[vs]), lp[vs, 1]] = v[vs, 3]
        es, et = expectations.get(c, (None, None))
        rv = RaceVotes(
            race_key=c,
            line_keys=spec.line_keys,
            unit_index=units.copy(),
            votes=votes,
            ballots_cast=cast,
            blank=blank,
            invalid=invalid,
            eligible=eligible,
            expected_shares=es,
            expected_turnout=et,
        )
        try:
            rv.check()
        except AssertionError as exc:
            raise ElectionError(f"{c}: stored unit results are inconsistent") from exc
        out[c] = rv
    return out


# =========================================================================== timeline
def timeline_meta(session: Session, election_id: int) -> dict[str, Any]:
    """Metadata of the stored reporting timeline (reference close, timezone, seed, config
    fingerprint, per-municipality closing offsets) from the ``election`` simulation run."""
    run = latest_run(session, election_id, RUN_SIMULATE)
    meta = loads(run.summary_json).get("timeline") if run is not None else None
    if not meta:
        raise ElectionError(f"election {election_id} has no reporting timeline (simulate it first)")
    return meta


def local_time(meta: Mapping[str, Any], sim_time_s: float) -> datetime:
    """Naive local wall-clock time of a simulation time (as stored in ``reporting_event``)."""
    tz = ZoneInfo(str(meta["timezone"]))
    ref = datetime.fromisoformat(str(meta["reference_close"]))
    utc = ref.astimezone(ZoneInfo("UTC")) + timedelta(seconds=float(sim_time_s))
    return utc.astimezone(tz).replace(tzinfo=None)


def load_timeline(session: Session, election_id: int, frame: GeographyFrame) -> Timeline:
    """The persisted election-night timeline, bit-for-bit identical to the generated one."""
    meta = timeline_meta(session, election_id)
    ev_rows = session.execute(
        select(
            ReportingEvent.id,
            ReportingEvent.seq,
            ReportingEvent.sim_time_s,
            ReportingEvent.municipality_id,
            ReportingEvent.ballots_in_batch,
            ReportingEvent.municipality_fraction_after,
        )
        .where(ReportingEvent.election_id == election_id)
        .order_by(ReportingEvent.seq)
    ).all()
    if not ev_rows:
        raise ElectionError(f"election {election_id} has no reporting events")
    if frame.muni_ids is None:
        raise DataNotPreparedError("frame has no database ids (use get_frame)")
    muni_code_of = dict(zip(frame.muni_ids.tolist(), frame.muni_codes, strict=True))
    events = [
        {
            "seq": int(seq),
            "sim_time_s": float(t),
            "municipality_code": muni_code_of[int(m)],
            "ballots_in_batch": int(b),
            "municipality_fraction_after": float(f),
        }
        for _id, seq, t, m, b, f in ev_rows
    ]
    seq_of_event = {int(r[0]): int(r[1]) for r in ev_rows}
    unit_rows = session.execute(
        select(ReportingEventUnit.event_id, ReportingEventUnit.geo_unit_id, ReportingEventUnit.fraction)
        .join(ReportingEvent, ReportingEvent.id == ReportingEventUnit.event_id)
        .where(ReportingEvent.election_id == election_id)
    ).all()
    arr = np.asarray([(seq_of_event[int(e)], int(u)) for e, u, _ in unit_rows], dtype=np.int64).reshape(-1, 2)
    fractions = [float(x) for _, _, x in unit_rows]
    uidx = unit_index_of_ids(frame, arr[:, 1])
    if (uidx < 0).any():
        raise ElectionError(f"election {election_id}: timeline references units outside the frame")
    codes = [frame.unit_codes[i] for i in uidx.tolist()]
    rows = list(zip(arr[:, 0].tolist(), codes, fractions, strict=True))
    tl = Timeline.from_records(
        frame,
        events,
        rows,
        reference_close=datetime.fromisoformat(str(meta["reference_close"])),
        timezone=str(meta["timezone"]),
        seed=int(meta["seed"]),
        checkpoint_every=int(meta.get("checkpoint_every", 500)),
        config_fingerprint=str(meta.get("config_fingerprint", "")),
    )
    offsets = meta.get("muni_close_offset_s")
    if offsets is not None and len(offsets) == frame.n_munis:
        tl.muni_close_offset_s = np.asarray(offsets, dtype=np.float64)
    return tl
