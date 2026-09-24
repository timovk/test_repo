"""Internals of :func:`app.services.elections.create_election` (private).

Turns a scenario document plus the stored system (geography, plan, seats, offices, office
holders, earlier elections) into the relational rows of a new election: parties (with lineage
events and model parameters), candidates (with affiliations), races, ballot lines with party
snapshots, links to the previous race of each office, campaigns and fictional polls.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.campaigns.config import NATIONAL_CODE, config_from_spec
from app.campaigns.engine import build_targets, run_campaigns
from app.campaigns.types import Target
from app.core.constitution import ElectoralSystem, OfficeType, RaceType
from app.core.errors import ElectionError
from app.core.logging import get_logger
from app.core.rng import derive_seed, make_rng
from app.districts.service import PlanMapping
from app.elections.calendar import CycleContents, ElectionCalendar
from app.elections.types import BallotLine, RaceSpec
from app.geography.frame import GeographyFrame
from app.models import (
    BallotCandidate,
    Campaign,
    CampaignAllocation,
    Candidate,
    CandidateAffiliation,
    Election,
    HouseDistrict,
    Legislature,
    Municipality,
    Office,
    OfficeHolder,
    Party,
    PartyEvent,
    PartyModifier,
    Province,
    Race,
    SenateSeat,
)
from app.polling.config import resolve_pollsters
from app.polling.generate import generate_polls
from app.polling.service import store_polls
from app.scenarios.schema import CandidateSpec, DownBallotSpec, ScenarioDocument
from app.services._common import (
    INDEPENDENT_COLOR,
    PRESIDENT_OFFICE,
    VICE_PRESIDENT_OFFICE,
    bulk_insert,
    governor_office_code,
    house_office_code,
    lt_governor_office_code,
    mayor_office_code,
    seed62,
)
from app.services.runtime import (
    ElectionInputs,
    candidate_spec_from_row,
    municipality_units,
    province_units,
)
from app.simulation.candidates import (
    PROFESSIONS,
    RaceCandidates,
    RaceSlot,
    fictional_name,
    generate_down_ballot,
    slugify,
)
from app.simulation.races import (
    governor_slots,
    house_slots,
    mayor_slots,
    presidential_races,
    races_from_candidates,
    senate_slots,
    ticket_lines,
)
from app.simulation.structural import StructuralModel
from app.simulation.voting import expected_party_state, prepare_race

log = get_logger(__name__)

#: A party fields a list in a party-list (council / provincial legislature) election when its
#: expected share in the jurisdiction reaches this value (at least two lists always stand).
LIST_MIN_EXPECTED_SHARE = 0.02
#: Scenario ``incumbent_office`` spellings of the Vice-Presidency.
_VP_ALIASES = {"VP", "VICE_PRESIDENT", "VICE-PRESIDENT"}
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


# =========================================================================== office holders
@dataclass(frozen=True)
class Holder:
    """The person serving in an office on a given date."""

    office_code: str
    office_id: int
    holder_id: int
    candidate_id: int
    candidate_key: str
    seat_party: str | None  # party the holder was elected for
    current_party: str | None  # the person's current party
    term_start: date
    term_end: date | None


def holders_at(session: Session, when: date) -> dict[str, Holder]:
    """Office code → holder serving on ``when`` (term started, not yet ended)."""
    party_code = dict(session.execute(select(Party.id, Party.code)).tuples().all())
    rows = session.execute(
        select(OfficeHolder, Office.code, Candidate.key, Candidate.party_id)
        .join(Office, Office.id == OfficeHolder.office_id)
        .join(Candidate, Candidate.id == OfficeHolder.candidate_id)
        .where(
            OfficeHolder.term_start <= when,
            or_(OfficeHolder.ended_on.is_(None), OfficeHolder.ended_on > when),
        )
        .order_by(OfficeHolder.term_start, OfficeHolder.id)
    ).all()
    out: dict[str, Holder] = {}
    for oh, code, key, cur_party in rows:
        out[code] = Holder(
            office_code=code,
            office_id=oh.office_id,
            holder_id=oh.id,
            candidate_id=oh.candidate_id,
            candidate_key=key,
            seat_party=party_code.get(oh.party_id) if oh.party_id is not None else None,
            current_party=party_code.get(cur_party) if cur_party is not None else None,
            term_start=oh.term_start,
            term_end=oh.term_end,
        )
    return out


# =========================================================================== parties
def upsert_parties(
    session: Session, doc: ScenarioDocument, year: int, *, historical: bool = False
) -> dict[str, Party]:
    """Insert/update the scenario's parties; record lineage events and model parameters.

    * scenario ``party_events`` become ``PartyEvent`` rows (idempotent);
    * a changed name/abbreviation or colour of an existing party without a matching scenario
      event is recorded as a ``renamed`` / ``recolored`` event of ``year``;
    * the party's model parameters (demographic, urbanity, province, region and municipality
      effects) are stored as ``PartyModifier`` rows with ``source = 'scenario:<slug>'``.

    ``party`` rows hold the *current* identity: with ``historical`` (an election older than one
    already stored is being created) existing parties keep their identity and no change is
    detected.  Ballot rows keep their own snapshot of the identity used on that ballot, so history
    is never rewritten.
    """
    rows = {p.code: p for p in session.scalars(select(Party))}
    events_in_doc = {(e.party, e.year, e.event) for e in doc.party_events}
    new_events: list[dict[str, Any]] = []
    old_identity: dict[str, tuple[str, str, str]] = {}
    for spec in doc.parties:
        row = rows.get(spec.code)
        if row is None:
            row = Party(code=spec.code, name=spec.name, abbreviation=spec.abbreviation, color=spec.color)
            session.add(row)
            rows[spec.code] = row
        elif historical:
            continue
        else:
            old_identity[spec.code] = (row.name, row.abbreviation, row.color)
            renamed = (row.name, row.abbreviation) != (spec.name, spec.abbreviation)
            if renamed and not any(
                e[0] == spec.code and e[2] == "renamed" and e[1] <= year for e in events_in_doc
            ):
                new_events.append(
                    {
                        "party": spec.code,
                        "year": year,
                        "event_type": "renamed",
                        "old_value": f"{row.name} ({row.abbreviation})"[:160],
                        "new_value": f"{spec.name} ({spec.abbreviation})"[:160],
                        "notes": "detected when the scenario was loaded",
                    }
                )
            if row.color.lower() != spec.color.lower() and not any(
                e[0] == spec.code and e[2] == "recolored" and e[1] <= year for e in events_in_doc
            ):
                new_events.append(
                    {
                        "party": spec.code,
                        "year": year,
                        "event_type": "recolored",
                        "old_value": row.color,
                        "new_value": spec.color,
                        "notes": "detected when the scenario was loaded",
                    }
                )
        row.name = spec.name
        row.abbreviation = spec.abbreviation
        row.color = spec.color
        row.color_secondary = spec.color_secondary
        row.family = spec.family
        row.description = spec.description
        row.ideology_economic = spec.ideology.economic
        row.ideology_social = spec.ideology.social
        row.ideology_europe = spec.ideology.europe
        row.base_share = spec.base_share
        row.turnout_propensity = spec.turnout_propensity
        row.founded_year = spec.founded_year
        row.dissolved_year = spec.dissolved_year
        row.is_active = spec.dissolved_year is None or spec.dissolved_year > year
        row.is_fictional = bool(spec.fictional)
    session.flush()
    for spec in doc.parties:
        rows[spec.code].successor_party_id = rows[spec.successor].id if spec.successor in rows else None
    # ---- lineage events
    existing = {
        (e.party_id, e.year, e.event_type, e.new_value)
        for e in session.scalars(
            select(PartyEvent).where(PartyEvent.party_id.in_([p.id for p in rows.values()]))
        )
    }
    for ev in doc.party_events:
        party = rows.get(ev.party)
        if party is None:
            continue
        old = old_identity.get(ev.party)
        if ev.event == "renamed":
            new_value = f"{ev.new_name or party.name} ({ev.new_abbreviation or party.abbreviation})"
            old_value = (
                f"{old[0]} ({old[1]})"
                if old and (old[0], old[1]) != (party.name, party.abbreviation)
                else None
            )
        elif ev.event == "recolored":
            new_value = ev.new_color or party.color
            old_value = old[2] if old and old[2] != party.color else None
        elif ev.related_party is not None:
            new_value, old_value = ev.related_party, None
        else:
            new_value, old_value = None, None
        key = (party.id, ev.year, ev.event, new_value[:160] if new_value else None)
        if key in existing:
            continue
        existing.add(key)
        related = rows.get(ev.related_party) if ev.related_party else None
        session.add(
            PartyEvent(
                party_id=party.id,
                year=ev.year,
                event_type=ev.event,
                related_party_id=related.id if related is not None else None,
                old_value=old_value[:160] if old_value else None,
                new_value=new_value[:160] if new_value else None,
                notes=f"scenario {doc.scenario.slug}",
            )
        )
    for e in new_events:
        party = rows[e.pop("party")]
        key = (party.id, e["year"], e["event_type"], e["new_value"])
        if key not in existing:
            existing.add(key)
            session.add(PartyEvent(party_id=party.id, **e))
    # ---- model parameters
    source = f"scenario:{doc.scenario.slug}"[:80]
    ids = [rows[s.code].id for s in doc.parties]
    session.execute(
        delete(PartyModifier).where(PartyModifier.party_id.in_(ids), PartyModifier.source == source)
    )
    mods: list[dict[str, Any]] = []
    for spec in doc.parties:
        pid = rows[spec.code].id
        for scope, values in (
            ("demographic", spec.demographics),
            ("urbanity", spec.urbanity),
            ("province", spec.provinces),
            ("region", spec.regions),
            ("municipality", spec.municipalities),
        ):
            for k, v in values.items():
                mods.append(
                    {"party_id": pid, "scope": scope, "key": str(k)[:64], "value": float(v), "source": source}
                )
    bulk_insert(session, PartyModifier, mods)
    session.flush()
    return rows


# =========================================================================== candidates
def _portrait_key(key: str) -> str:
    return f"avatar-{derive_seed(0, 'portrait', key) % 48:02d}"


def _candidate_values(
    spec: CandidateSpec,
    party_ids: Mapping[str, int],
    province_ids: Mapping[str, int],
    frame: GeographyFrame,
    party_ideology: Mapping[str, tuple[float, float]],
) -> dict[str, Any]:
    home_p = spec.home_province
    if home_p is None and spec.home_municipality is not None:
        m = frame.muni_index_or_none(spec.home_municipality)
        if m is not None:
            home_p = frame.province_codes[int(frame.muni_province[m])]
    if spec.ideology is not None:
        econ, soc = spec.ideology.economic, spec.ideology.social
    else:
        econ, soc = party_ideology.get(spec.party or "", (0.0, 0.0))
    return {
        "first_name": spec.first_name[:60],
        "last_name": spec.last_name[:80],
        "full_name": f"{spec.first_name} {spec.last_name}"[:160],
        "gender": spec.gender,
        "birth_date": spec.birth_date,
        "home_municipality_code": spec.home_municipality,
        "home_province_id": province_ids.get(home_p) if home_p else None,
        "party_id": party_ids.get(spec.party) if spec.party else None,
        "ideology_economic": float(econ),
        "ideology_social": float(soc),
        "quality": float(spec.quality),
        "campaign_strength": float(spec.campaign_strength),
        "fundraising": float(spec.fundraising),
        "favorability": spec.favorability,
        "bio": spec.bio,
        "is_fictional": True,
    }


def upsert_candidates(
    session: Session,
    specs: Iterable[CandidateSpec],
    year: int,
    party_ids: Mapping[str, int],
    province_ids: Mapping[str, int],
    frame: GeographyFrame,
    party_ideology: Mapping[str, tuple[float, float]],
    *,
    update_existing: bool,
) -> dict[str, int]:
    """Insert new people (bulk) and, with ``update_existing``, refresh the attributes of known
    ones; maintain the party-affiliation history.  Returns ``{key: candidate id}``."""
    specs = list({s.key: s for s in specs}.values())
    if not specs:
        return {}
    keys = [s.key for s in specs]
    existing: dict[str, Candidate] = {}
    for i in range(0, len(keys), 500):
        part = keys[i : i + 500]
        existing.update({c.key: c for c in session.scalars(select(Candidate).where(Candidate.key.in_(part)))})
    new_rows = []
    for s in specs:
        values = _candidate_values(s, party_ids, province_ids, frame, party_ideology)
        row = existing.get(s.key)
        if row is None:
            new_rows.append({"key": s.key, "portrait_key": _portrait_key(s.key), **values})
        elif update_existing:
            old_party = row.party_id
            for k, v in values.items():
                setattr(row, k, v)
            if old_party != row.party_id:
                open_aff = session.scalars(
                    select(CandidateAffiliation).where(
                        CandidateAffiliation.candidate_id == row.id, CandidateAffiliation.to_year.is_(None)
                    )
                ).all()
                for a in open_aff:
                    a.to_year = year
                session.add(CandidateAffiliation(candidate_id=row.id, party_id=row.party_id, from_year=year))
    bulk_insert(session, Candidate, new_rows)
    session.flush()
    ids: dict[str, int] = {}
    for i in range(0, len(keys), 500):
        part = keys[i : i + 500]
        ids.update(
            dict(
                session.execute(select(Candidate.key, Candidate.id).where(Candidate.key.in_(part)))
                .tuples()
                .all()
            )
        )
    bulk_insert(
        session,
        CandidateAffiliation,
        [
            {"candidate_id": ids[r["key"]], "party_id": r["party_id"], "from_year": year, "to_year": None}
            for r in new_rows
        ],
    )
    return ids


def _key_suffix(seed: int, *keys: str) -> str:
    v = derive_seed(seed, "candidate-key", *keys)
    out = ""
    for _ in range(4):
        v, r = divmod(v, 36)
        out += _B36[r]
    return out


def running_mate(
    model: StructuralModel,
    seed: int,
    race_key: str,
    line: BallotLine,
    province_code: str,
    office_label: str,
    rules: DownBallotSpec,
    existing: set[str],
) -> CandidateSpec:
    """A FICTIONAL running mate (e.g. Lieutenant Governor) for ``line``: same party, a home
    municipality drawn by eligible voters in the province; keyed stream
    ``(seed, "running-mate", race_key, line.key)``."""
    f = model.frame
    rng = make_rng(seed, "running-mate", race_key, line.key)
    cc = model.config.candidates
    gender = "F" if rng.random() < cc.female_share else "M"
    units = province_units(f, province_code)
    w = model.eligible[units]
    p = w / w.sum() if w.sum() > 0 else np.full(len(units), 1.0 / len(units))
    u = int(units[int(rng.choice(len(units), p=p))])
    home = f.muni_codes[int(f.unit_muni[u])]
    first, last = fictional_name(rng, gender, province=province_code, heritage_prob=0.08)
    base = slugify(f"{first} {last}")
    key = f"{base}-{_key_suffix(seed, race_key, 'mate', line.key)}"
    n = 2
    while key in existing:
        key = f"{base}-{_key_suffix(seed, race_key, 'mate', line.key, str(n))}"
        n += 1
    existing.add(key)
    lo, hi = cc.age_range.get(RaceType.GOVERNOR.value, (35, 68))
    age = round(rng.triangular(lo, (lo + hi) / 2 + 2, hi))
    year = model.scenario.scenario.year
    birth = date(year - age, int(rng.integers(1, 13)), int(rng.integers(1, 29)))
    quality = float(np.clip(rng.normal(0.0, rules.candidate_quality_sd), -3.0, 3.0))
    profession = PROFESSIONS[int(rng.integers(len(PROFESSIONS)))]
    muni_name = f.muni_names[f.muni_index(home)]
    who = (
        f"Fictional {model.scenario.party(line.party_code).name} candidate"
        if line.party_code is not None
        else "Fictional independent candidate"
    )
    return CandidateSpec(
        key=key,
        first_name=first,
        last_name=last,
        gender=gender,
        birth_date=birth,
        party=line.party_code,
        home_municipality=home,
        home_province=province_code,
        quality=quality,
        campaign_strength=float(np.clip(rng.normal(0.0, 0.5), -3.0, 3.0)),
        fundraising=float(np.round(rng.lognormal(0.0, 0.4), 3)),
        bio=f"{who} for {office_label}; {profession} from {muni_name}.",
    )


# =========================================================================== race planning
@dataclass
class PlannedLine:
    line: BallotLine
    candidate: CandidateSpec | None
    running_mate: CandidateSpec | None


@dataclass
class PlannedRace:
    code: str
    race_type: RaceType
    name: str
    spec: RaceSpec
    lines: list[PlannedLine]
    office_code: str | None = None
    senate_seat_code: str | None = None
    parent: str | None = None
    holder: Holder | None = None
    legislature: tuple[str, str] | None = None


@dataclass
class SetupState:
    """Everything the race planner needs (read once from the database)."""

    session: Session
    doc: ScenarioDocument
    frame: GeographyFrame
    model: StructuralModel
    mapping: PlanMapping | None
    cycle: CycleContents
    calendar: ElectionCalendar
    year: int
    seed: int
    election_date: date
    holders: dict[str, Holder]
    ev_by_province: dict[str, int]
    existing_keys: set[str]
    candidate_specs: dict[str, CandidateSpec] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def scenario_incumbents(doc: ScenarioDocument) -> dict[str, CandidateSpec]:
    """Office code → scenario candidate declared as its holder (``incumbent_office``)."""
    out: dict[str, CandidateSpec] = {}
    for c in doc.candidates:
        if not c.incumbent_office:
            continue
        code = VICE_PRESIDENT_OFFICE if c.incumbent_office.upper() in _VP_ALIASES else c.incumbent_office
        out[code] = c
    return out


def _holder_spec(state: SetupState, holder: Holder) -> CandidateSpec | None:
    """The holder as a :class:`CandidateSpec` (scenario definition preferred), or None when the
    person cannot run (their party is not part of the scenario)."""
    if holder.candidate_key in state.candidate_specs:
        spec = state.candidate_specs[holder.candidate_key]
    else:
        row = state.session.get(Candidate, holder.candidate_id)
        if row is None:
            return None
        pcode = holder.current_party
        home_p = None
        if row.home_province_id is not None:
            prov = state.session.get(Province, row.home_province_id)
            home_p = prov.code if prov is not None else None
        spec = candidate_spec_from_row(row, pcode, home_p)
        state.candidate_specs[spec.key] = spec
    if spec.party is not None and spec.party not in state.model.party_index:
        return None
    return spec


def presidential_plan(state: SetupState, pres_party: str | None) -> list[PlannedRace]:
    doc, frame = state.doc, state.frame
    if not doc.president.tickets:
        state.warnings.append("presidential election due but the scenario has no tickets")
        return []
    lines = ticket_lines(doc, frame)
    holder = state.holders.get(PRESIDENT_OFFICE)
    if holder is not None:
        lines = [dataclasses.replace(ln, incumbent=ln.candidate_key == holder.candidate_key) for ln in lines]
    specs = presidential_races(frame, lines, state.ev_by_province, incumbent_party=pres_party)
    planned_lines = [
        PlannedLine(ln, doc.candidate(ln.candidate_key or ""), doc.candidate(ln.running_mate_key or ""))
        for ln in lines
    ]
    out: list[PlannedRace] = []
    for spec in specs:
        spec.is_open_seat = not any(ln.incumbent and not ln.withdrawn for ln in spec.lines)
        if spec.race_type == RaceType.PRESIDENT:
            out.append(
                PlannedRace(
                    "PRES", RaceType.PRESIDENT, "President", spec, list(planned_lines),
                    office_code=PRESIDENT_OFFICE, holder=holder,
                )
            )  # fmt: skip
        else:
            name = frame.province_names[frame.province_index(str(spec.province_code))]
            ev = state.ev_by_province.get(str(spec.province_code))
            out.append(
                PlannedRace(
                    spec.key,
                    RaceType.PRESIDENT_PROVINCE,
                    f"President: {name} ({ev} EV)",
                    spec,
                    list(planned_lines),
                    parent="PRES",
                )
            )
    # the national parent first (children reference it)
    out.sort(key=lambda r: r.race_type != RaceType.PRESIDENT)
    return out


def _district_names(session: Session, plan_id: int | None) -> dict[str, str]:
    if plan_id is None:
        return {}
    return {
        c: (n or c)
        for c, n in session.execute(
            select(HouseDistrict.code, HouseDistrict.name).where(HouseDistrict.plan_id == plan_id)
        ).all()
    }


def down_ballot_plan(
    state: SetupState,
    *,
    house: bool,
    senate_seats: Sequence[tuple[str, int, int, bool]],
    governors: bool,
    mayors: bool,
    blocked: set[str],
) -> list[PlannedRace]:
    """Single-member races with generated / incumbent candidates.

    ``senate_seats``: (province, seat number, class, is_special); ``blocked``: candidate keys that
    cannot run in these races (e.g. people on the presidential ticket).
    """
    frame, model = state.frame, state.model
    slots: list[RaceSlot] = []
    meta: dict[str, dict[str, Any]] = {}
    if house:
        if state.mapping is None:
            raise ElectionError("a House election needs an active district plan")
        codes = np.asarray(state.mapping.district_codes, dtype=object)[state.mapping.unit_district]
        names = _district_names(state.session, state.mapping.plan_id)
        for s in house_slots(frame, codes):
            d = s.key.removeprefix("HOUSE-")
            slots.append(s)
            meta[s.key] = {"office": house_office_code(d), "name": f"House {d}: {names.get(d, d)}"}
    if senate_seats:
        ss = senate_slots(frame, [(pv, n) for pv, n, _c, _sp in senate_seats])
        for s, (pv, n, cls, special) in zip(ss, senate_seats, strict=True):
            slots.append(s)
            pname = frame.province_names[frame.province_index(pv)]
            label = " (special election)" if special else ""
            meta[s.key] = {
                "office": s.key,
                "seat": s.key,
                "name": f"Senate: {pname} seat {n} (class {cls}){label}",
                "special": special,
            }
    if governors:
        for s in governor_slots(frame):
            pv = str(s.province_code)
            slots.append(s)
            meta[s.key] = {
                "office": governor_office_code(pv),
                "name": f"Governor of {frame.province_names[frame.province_index(pv)]}",
            }
    if mayors:
        for s in mayor_slots(frame):
            gm = s.key.removeprefix("MAYOR-")
            slots.append(s)
            meta[s.key] = {
                "office": mayor_office_code(gm),
                "name": f"Mayor of {frame.muni_names[frame.muni_index(gm)]}",
            }
    if not slots:
        return []
    incumbents: dict[str, CandidateSpec] = {}
    used: set[str] = set(blocked)
    for s in slots:
        holder = state.holders.get(meta[s.key]["office"])
        if holder is None or holder.candidate_key in used:
            continue
        spec = _holder_spec(state, holder)
        if spec is None:
            continue
        incumbents[s.key] = spec
        used.add(spec.key)
    fielded: dict[str, RaceCandidates] = generate_down_ballot(
        model, slots, state.seed, incumbents=incumbents, existing_keys=state.existing_keys
    )
    specs = races_from_candidates(slots, fielded)
    out: list[PlannedRace] = []
    lt_enabled = bool(get_lt_governors(state))
    for s, spec in zip(slots, specs, strict=True):
        rc = fielded[s.key]
        by_key = {c.key: c for c in rc.candidates}
        for c in rc.candidates:
            state.candidate_specs.setdefault(c.key, c)
        holder = state.holders.get(meta[s.key]["office"])
        spec.is_open_seat = not any(ln.incumbent for ln in spec.lines)
        spec.is_special = bool(meta[s.key].get("special", False))
        planned = [PlannedLine(ln, by_key.get(ln.candidate_key or ""), None) for ln in spec.lines]
        if s.race_type == RaceType.GOVERNOR and lt_enabled:
            planned = _with_running_mates(state, s, spec, planned, holder)
        out.append(
            PlannedRace(
                s.key,
                RaceType(s.race_type),
                meta[s.key]["name"],
                spec,
                planned,
                office_code=meta[s.key]["office"],
                senate_seat_code=meta[s.key].get("seat"),
                holder=holder,
            )
        )
    return out


def get_lt_governors(state: SetupState) -> bool:
    return bool(state.calendar.constitution.lieutenant_governors and state.doc.governors.lieutenant_governors)


def _with_running_mates(
    state: SetupState, slot: RaceSlot, spec: RaceSpec, planned: list[PlannedLine], gov_holder: Holder | None
) -> list[PlannedLine]:
    pv = str(slot.province_code)
    lt_holder = state.holders.get(lt_governor_office_code(pv))
    label = f"Lieutenant Governor of {state.frame.province_names[state.frame.province_index(pv)]}"
    new_lines: list[BallotLine] = []
    out: list[PlannedLine] = []
    for pl in planned:
        ln = pl.line
        mate: CandidateSpec | None = None
        if (
            ln.incumbent
            and gov_holder is not None
            and lt_holder is not None
            and ln.candidate_key == gov_holder.candidate_key
        ):
            mate = _holder_spec(state, lt_holder)
            if mate is not None and mate.party != ln.party_code:
                mate = None
        if mate is None:
            mate = running_mate(
                state.model, state.seed, slot.key, ln, pv, label, state.doc.governors, state.existing_keys
            )
        state.candidate_specs.setdefault(mate.key, mate)
        new = dataclasses.replace(
            ln, running_mate_key=mate.key, running_mate_home_province=mate.home_province or pv
        )
        new_lines.append(new)
        out.append(PlannedLine(new, pl.candidate, mate))
    spec.lines = new_lines
    return out


def party_list_plan(
    state: SetupState,
    race_type: RaceType,
    jurisdictions: Sequence[tuple[str, str, int]],
) -> list[PlannedRace]:
    """Party-list (D'Hondt) races: ``(code, geo code, seats)`` per jurisdiction."""
    frame, model, doc = state.frame, state.model, state.doc
    active = [p.code for p in doc.parties if p.dissolved_year is None or p.dissolved_year > state.year]
    out = []
    for code, geo, seats in jurisdictions:
        if race_type == RaceType.PROVINCIAL_LEGISLATURE:
            units = province_units(frame, geo)
            pv, muni = geo, None
            name = f"Provincial Legislature of {frame.province_names[frame.province_index(geo)]}"
            leg = ("provincial", geo)
        else:
            units = municipality_units(frame, geo)
            m = frame.muni_index(geo)
            pv, muni = frame.province_codes[int(frame.muni_province[m])], geo
            name = f"Municipal Council of {frame.muni_names[m]}"
            leg = ("municipal", geo)
        js = model.jurisdiction_shares(units, active)
        order = sorted(range(len(active)), key=lambda i: (-float(js[i]), active[i]))
        chosen = [active[i] for i in order if float(js[i]) >= LIST_MIN_EXPECTED_SHARE]
        for i in order:
            if len(chosen) >= 2:
                break
            if active[i] not in chosen:
                chosen.append(active[i])
        chosen.sort(key=lambda c: (-float(js[active.index(c)]), c))
        lines = [BallotLine(key=c, party_code=c, label=doc.party(c).name) for c in chosen]
        spec = RaceSpec(
            key=code,
            race_type=race_type,
            unit_index=units,
            lines=lines,
            electoral_system=ElectoralSystem.PROPORTIONAL_DHONDT,
            seats=int(seats),
            province_code=pv,
            municipality_code=muni,
        )
        out.append(
            PlannedRace(
                code, race_type, name, spec, [PlannedLine(ln, None, None) for ln in lines], legislature=leg
            )
        )
    return out


# =========================================================================== persistence
def previous_races(
    session: Session, before: date, exclude_election: int
) -> tuple[dict[int, int], dict[str, int]]:
    """Latest earlier race per office id and per race code."""
    rows = session.execute(
        select(Race.id, Race.code, Race.office_id)
        .join(Election, Election.id == Race.election_id)
        .where(Election.election_date < before, Election.id != exclude_election)
        .order_by(Election.election_date, Election.id, Race.id)
    ).all()
    by_office: dict[int, int] = {}
    by_code: dict[str, int] = {}
    for rid, code, office_id in rows:
        by_code[code] = rid
        if office_id is not None:
            by_office[office_id] = rid
    return by_office, by_code


def store_races(
    session: Session,
    election: Election,
    planned: Sequence[PlannedRace],
    candidate_ids: Mapping[str, int],
    parties: Mapping[str, Party],
    doc: ScenarioDocument,
) -> dict[str, int]:
    """Race rows (+ parent links, previous-race links) and ballot lines with a snapshot of the
    party identity of this election (the scenario's name, abbreviation and colour)."""
    identity = {p.code: (p.name, p.abbreviation, p.color) for p in doc.parties}
    offices = dict(session.execute(select(Office.code, Office.id)).tuples().all())
    prov_ids = dict(session.execute(select(Province.code, Province.id)).tuples().all())
    muni_ids = dict(
        session.execute(
            select(Municipality.cbs_code, Municipality.id).where(
                Municipality.vintage_id == election.vintage_id
            )
        )
        .tuples()
        .all()
    )
    dist_ids = (
        dict(
            session.execute(
                select(HouseDistrict.code, HouseDistrict.id).where(
                    HouseDistrict.plan_id == election.district_plan_id
                )
            )
            .tuples()
            .all()
        )
        if election.district_plan_id
        else {}
    )
    seat_ids = dict(session.execute(select(SenateSeat.code, SenateSeat.id)).tuples().all())
    prev_office, prev_code = previous_races(session, election.election_date, election.id)
    rows: list[dict[str, Any]] = []
    for pr in planned:
        spec = pr.spec
        office_id = offices.get(pr.office_code) if pr.office_code else None
        if pr.office_code and office_id is None:
            raise ElectionError(f"{pr.code}: office {pr.office_code} does not exist (run ensure_offices)")
        holder = pr.holder
        inc_party = holder.seat_party if holder is not None else None
        inc_line = next((pl for pl in pr.lines if pl.line.incumbent), None)
        if inc_party is None and inc_line is not None:
            inc_party = inc_line.line.party_code
        inc_cand = (
            holder.candidate_id
            if holder is not None
            else (candidate_ids.get(inc_line.line.candidate_key or "") if inc_line is not None else None)
        )
        prev = prev_office.get(office_id) if office_id is not None else None
        if prev is None:
            prev = prev_code.get(pr.code)
        rows.append(
            {
                "election_id": election.id,
                "race_type": pr.race_type.value,
                "code": pr.code,
                "name": pr.name[:160],
                "parent_race_id": None,
                "office_id": office_id,
                "province_id": prov_ids.get(spec.province_code) if spec.province_code else None,
                "district_id": dist_ids.get(spec.district_code) if spec.district_code else None,
                "municipality_id": muni_ids.get(spec.municipality_code) if spec.municipality_code else None,
                "senate_seat_id": seat_ids.get(pr.senate_seat_code) if pr.senate_seat_code else None,
                "electoral_votes": spec.electoral_votes,
                "seats": int(spec.seats),
                "electoral_system": ElectoralSystem(spec.electoral_system).value,
                "is_special": bool(spec.is_special),
                "is_open_seat": bool(spec.is_open_seat),
                "incumbent_candidate_id": inc_cand,
                "incumbent_party_id": parties[inc_party].id if inc_party in parties else None,
                "previous_race_id": prev,
                "status": "SCHEDULED",
            }
        )
    bulk_insert(session, Race, rows)
    session.flush()
    race_ids = dict(
        session.execute(select(Race.code, Race.id).where(Race.election_id == election.id)).tuples().all()
    )
    children = [race_ids[pr.code] for pr in planned if pr.parent is not None]
    if children and "PRES" in race_ids:
        session.execute(update(Race).where(Race.id.in_(children)).values(parent_race_id=race_ids["PRES"]))
    ballot_rows: list[dict[str, Any]] = []
    for pr in planned:
        rid = race_ids[pr.code]
        for order, pl in enumerate(pr.lines, start=1):
            ln = pl.line
            party = parties.get(ln.party_code) if ln.party_code else None
            ident = (
                identity.get(party.code, (party.name, party.abbreviation, party.color))
                if party is not None
                else None
            )
            ballot_rows.append(
                {
                    "race_id": rid,
                    "candidate_id": candidate_ids.get(ln.candidate_key) if ln.candidate_key else None,
                    "running_mate_id": candidate_ids.get(ln.running_mate_key)
                    if ln.running_mate_key
                    else None,
                    "party_id": party.id if party is not None else None,
                    "ballot_order": order,
                    "ballot_name": (ln.label or ln.key)[:200],
                    "line_key": ln.key,
                    "quality_snapshot": float(ln.quality) if ln.candidate_key else None,
                    "party_code_snapshot": party.code if party is not None else None,
                    "party_name_snapshot": ident[0] if ident else None,
                    "party_abbr_snapshot": ident[1] if ident else None,
                    "party_color_snapshot": ident[2] if ident else INDEPENDENT_COLOR,
                    "is_incumbent": bool(ln.incumbent),
                    "is_write_in": False,
                    "withdrawn": bool(ln.withdrawn),
                    "withdrawn_reason": "withdrawn (scenario)" if ln.withdrawn else None,
                }
            )
    bulk_insert(session, BallotCandidate, ballot_rows)
    session.flush()
    return race_ids


def legislature_jurisdictions(session: Session, level: str) -> dict[str, int]:
    """Jurisdiction code → seats of the stored legislatures of ``level``."""
    return dict(
        session.execute(
            select(Legislature.jurisdiction_code, Legislature.seats).where(Legislature.level == level)
        )
        .tuples()
        .all()
    )


# =========================================================================== campaigns
def _line_margins(shares: np.ndarray, parties: Sequence[str | None]) -> dict[str, float]:
    """Party → margin (pp) over its strongest opponent from jurisdiction shares."""
    out: dict[str, float] = {}
    for j, p in enumerate(parties):
        if p is None:
            continue
        others = np.delete(shares, j)
        best = float(others.max()) if len(others) else 0.0
        out[p] = 100.0 * (float(shares[j]) - best)
    return out


def campaign_targets(inputs: ElectionInputs) -> dict[str, list[Target]]:
    """Targets per party from the expected competitiveness of its races (no campaign effects yet).

    Presidential tickets target provinces (value = electoral votes), House candidates their
    districts (value 1), Senate candidates their province (value = seats up), governor candidates
    the municipalities of their province (value = the municipality's share of the province's
    eligible voters) — levels as configured in ``campaigns.race_levels``.
    """
    model, ctx, frame = inputs.model, inputs.context, inputs.frame
    cfg = config_from_spec(inputs.scenario.campaigns)
    state = expected_party_state(model, ctx)
    raw: dict[str, dict[tuple[str, str], list[float]]] = {}

    def add(party: str, level: str, code: str, value: float, margin: float, population: float) -> None:
        cur = raw.setdefault(party, {}).get((level, code))
        if cur is None:
            raw[party][(level, code)] = [value, margin, population]
        else:
            cur[0] += value
            if abs(margin) < abs(cur[1]):
                cur[1] = margin

    pop_prov = frame.province_population()
    pop_muni = frame.muni_population
    elig_muni = frame.to_munis(frame.unit_eligible)
    levels = cfg.race_levels
    for spec in inputs.races.values():
        rt = RaceType(spec.race_type)
        if rt not in (RaceType.PRESIDENT_PROVINCE, RaceType.HOUSE, RaceType.SENATE, RaceType.GOVERNOR):
            continue
        plan = prepare_race(model, spec, ctx, state)
        margins = _line_margins(plan.jurisdiction_shares, [ln.party_code for ln in spec.lines])
        pv = spec.province_code
        if rt == RaceType.PRESIDENT_PROVINCE:
            level = levels.get("president", "province")
            for party, m in margins.items():
                if level == "national":
                    add(
                        party,
                        "national",
                        NATIONAL_CODE,
                        float(spec.electoral_votes or 0),
                        m,
                        float(pop_prov.sum()),
                    )
                else:
                    p = frame.province_index(str(pv))
                    add(party, "province", str(pv), float(spec.electoral_votes or 0), m, float(pop_prov[p]))
        elif rt == RaceType.HOUSE:
            pop = float(frame.unit_population[spec.unit_index].sum())
            for party, m in margins.items():
                add(party, "district", str(spec.district_code), 1.0, m, pop)
        elif rt == RaceType.SENATE:
            p = frame.province_index(str(pv))
            for party, m in margins.items():
                add(party, "province", str(pv), 1.0, m, float(pop_prov[p]))
        else:
            level = levels.get("governor", "municipality")
            p = frame.province_index(str(pv))
            if level == "municipality":
                munis = frame.munis_in_province(p)
                tot = float(elig_muni[munis].sum()) or 1.0
                for party, m in margins.items():
                    for mi in munis:
                        add(
                            party,
                            "municipality",
                            frame.muni_codes[mi],
                            float(elig_muni[mi]) / tot,
                            m,
                            float(pop_muni[mi]),
                        )
            else:
                for party, m in margins.items():
                    add(party, "province", str(pv), 1.0, m, float(pop_prov[p]))
    out: dict[str, list[Target]] = {}
    for party, targets in raw.items():
        by_level: dict[str, list[tuple[str, list[float]]]] = {}
        for (level, code), v in sorted(targets.items()):
            by_level.setdefault(level, []).append((code, v))
        lst: list[Target] = []
        for level, items in by_level.items():
            lst += build_targets(
                level,
                [c for c, _ in items],
                [v[0] for _, v in items],
                [v[1] for _, v in items],
                [v[2] for _, v in items],
                config=cfg,
            )
        out[party] = lst
    return out


def store_campaigns(
    session: Session, inputs: ElectionInputs, parties: Mapping[str, Party], seed: int
) -> dict[str, Any]:
    """Plan and realise every budgeted party's campaign; persist ``campaign`` + allocations."""
    spec = inputs.scenario.campaigns
    budgets = {
        p: b for p, b in (spec.budgets or {}).items() if p in parties and p in inputs.model.party_index
    }
    if not spec.enabled or not budgets:
        return {"enabled": False}
    targets = campaign_targets(inputs)
    run = run_campaigns(spec, targets, seed)
    ticket_line = {}
    if "PRES" in inputs.races:
        for ln in inputs.races["PRES"].lines:
            if ln.party_code is not None:
                ticket_line[ln.party_code] = inputs.line_ids["PRES"][ln.key]
    alloc_rows: list[dict[str, Any]] = []
    n_campaigns = 0
    for party in sorted(run.effects):
        if party not in budgets:
            continue
        eff = run.effects[party]
        camp = Campaign(
            election_id=inputs.election_id,
            race_id=inputs.race_ids["PRES"] if party in ticket_line else None,
            ballot_candidate_id=ticket_line.get(party),
            party_id=parties[party].id,
            budget=float(budgets[party]),
            strategy=str(run.strategies.get(party, "balanced"))[:24],
            seed=int(seed),
        )
        session.add(camp)
        session.flush()
        n_campaigns += 1
        for rec in eff.allocations:
            a = rec.allocation
            alloc_rows.append(
                {
                    "campaign_id": camp.id,
                    "target_level": a.level,
                    "target_code": a.code[:12],
                    "action": a.action,
                    "amount": float(a.amount),
                    "units": a.units,
                    "proceeds": float(a.proceeds) if a.proceeds else None,
                    "week": int(a.week),
                    "expected_effect": float(rec.expected_persuasion),
                    "realized_effect": float(rec.realized_persuasion),
                    "turnout_effect": float(rec.realized_turnout),
                }
            )
    bulk_insert(session, CampaignAllocation, alloc_rows)
    session.flush()
    return {"enabled": True, "campaigns": n_campaigns, "allocations": len(alloc_rows), "seed": int(seed)}


# =========================================================================== polls
def poll_truth(inputs: ElectionInputs) -> dict[tuple[str, str], dict[str, float]]:
    """Election-day "true" opinion per poll group = the model's expected shares (with campaigns).

    Keys are party codes (independents: their line key)."""
    model, ctx = inputs.model, inputs.context
    state = expected_party_state(model, ctx)
    truth: dict[tuple[str, str], dict[str, float]] = {}
    senate_by_prov: dict[str, list[str]] = {}
    for code, spec in inputs.races.items():
        if RaceType(spec.race_type) == RaceType.SENATE:
            senate_by_prov.setdefault(str(spec.province_code), []).append(code)
    for code, spec in inputs.races.items():
        rt = RaceType(spec.race_type)
        if rt == RaceType.PRESIDENT:
            group = ("national_president", "NL")
        elif rt == RaceType.PRESIDENT_PROVINCE:
            group = ("province_president", str(spec.province_code))
        elif rt == RaceType.HOUSE:
            group = ("house_district", str(spec.district_code))
        elif rt == RaceType.SENATE:
            pv = str(spec.province_code)
            group = ("senate", pv) if len(senate_by_prov[pv]) == 1 else ("senate", code.removeprefix("SEN-"))
        elif rt == RaceType.GOVERNOR:
            group = ("governor", str(spec.province_code))
        else:
            continue
        plan = prepare_race(model, spec, ctx, state)
        shares: dict[str, float] = {}
        for ln, s in zip(spec.lines, plan.jurisdiction_shares, strict=True):
            if ln.withdrawn:
                continue
            key = ln.party_code or ln.key
            shares[key] = shares.get(key, 0.0) + float(s)
        truth[group] = shares
    if inputs.cycle.house and any(RaceType(r.race_type) == RaceType.HOUSE for r in inputs.races.values()):
        w = model.eligible * state.turnout
        nat = (state.vote_share * w[:, None]).sum(axis=0)
        nat = nat / nat.sum() if nat.sum() > 0 else nat
        truth[("generic_house", "NL")] = {c: float(v) for c, v in zip(model.party_codes, nat, strict=True)}
    return truth


def store_generated_polls(
    session: Session, inputs: ElectionInputs, parties: Mapping[str, Party], seed: int
) -> dict[str, Any]:
    """Generate FICTIONAL polls from the model expectation and persist them."""
    spec = inputs.scenario.polling
    if not spec.generate:
        return {"generated": 0}
    truth = poll_truth(inputs)
    if not truth:
        return {"generated": 0}
    pollsters = resolve_pollsters(list(spec.pollsters))
    polls = generate_polls(truth, pollsters, spec, inputs.election_date, seed)
    prov_ids = {
        c: int(i) for c, i in zip(inputs.frame.province_codes, inputs.frame.province_ids, strict=True)
    }
    stored = store_polls(
        session,
        inputs.election_id,
        list(polls),
        {c: p.id for c, p in parties.items()},
        inputs.race_ids,
        prov_ids,
        pollsters=pollsters,
    )
    return {"generated": len(stored), "seed": int(seed), "groups": len(truth)}


def poll_seed(seed: int) -> int:
    return seed62(derive_seed(seed, "polls"))


def campaign_seed(seed: int) -> int:
    return seed62(derive_seed(seed, "campaigns"))


def senate_seats_up(
    session: Session, doc: ScenarioDocument, cycle: CycleContents
) -> list[tuple[str, int, int, bool]]:
    """(province, seat number, class, is_special) of the Senate seats elected this year."""
    classes = set(doc.senate.classes_up) if doc.senate.classes_up is not None else set(cycle.senate_classes)
    seats = session.execute(
        select(Province.code, SenateSeat.seat_number, SenateSeat.senate_class, SenateSeat.code)
        .join(Province, Province.id == SenateSeat.province_id)
        .order_by(Province.sort_order, SenateSeat.seat_number)
    ).all()
    specials = {s if s.startswith("SEN-") else f"SEN-{s}" for s in doc.senate.special_elections}
    regular = set(cycle.senate_classes)
    out = []
    for pv, n, cls, code in seats:
        if cls in classes:
            # a class the scenario elects outside the calendar's rotation only fills the remainder
            out.append((pv, int(n), int(cls), int(cls) not in regular))
        elif code in specials:
            out.append((pv, int(n), int(cls), True))
    return out


def office_type_of(race_type: RaceType) -> OfficeType | None:
    return {
        RaceType.PRESIDENT: OfficeType.PRESIDENT,
        RaceType.HOUSE: OfficeType.HOUSE,
        RaceType.SENATE: OfficeType.SENATE,
        RaceType.GOVERNOR: OfficeType.GOVERNOR,
        RaceType.MAYOR: OfficeType.MAYOR,
    }.get(race_type)
