"""Endless regular elections (docs/CLOCK.md): the next November election, generated from the
elections before it.

The built-in scenarios cover 2024, 2026 and 2028.  For every later regular election day the
world clock creates the election from a *continuation scenario*:

* the latest stored scenario of the same type (general or midterm) is moved to the new year
  (date, Senate class, polling window);
* the parties carry over; the national mood (``calibration.national``) is the previous House
  popular vote pulled back towards the parties' long-run ``base_share``, plus a random drift;
* the year-specific narrative is dropped: party swings, named events and explicit candidates are
  replaced by a few generic, randomly drawn events (a slowing economy hurting the president's
  party, a scandal, a turnout surge);
* office holders run again as incumbents (``create_election`` reads them from the database).  In
  a general election the sitting President runs again with the Vice-President unless they have
  served two terms; the other tickets are led by the party's most prominent office holders
  (governors, then senators, then House members) or by new FICTIONAL people.

Everything is seeded by the template's seed and the year, so a rebuilt world generates the same
elections.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constitution import ElectionType, RaceType
from app.core.errors import ElectionError
from app.core.logging import get_logger
from app.core.rng import derive_seed, make_rng
from app.elections.calendar import ElectionCalendar
from app.models import (
    BallotCandidate,
    Candidate,
    Election,
    ElectionResult,
    Office,
    OfficeHolder,
    Race,
    Scenario,
)
from app.scenarios.loader import list_scenarios, load_scenario
from app.scenarios.schema import CandidateSpec, ScenarioDocument
from app.services import _create as create_ops
from app.services._common import PRESIDENT_OFFICE, REPORTED_STATUSES, VICE_PRESIDENT_OFFICE
from app.services.runtime import (
    candidate_spec_from_row,
    geography_source,
    get_frame,
    parse_stored_scenario,
)
from app.simulation.candidates import fictional_name, slugify

log = get_logger(__name__)

REGULAR_TYPES = (ElectionType.GENERAL.value, ElectionType.MIDTERM.value)
#: Weight of the previous result in the next national mood (the rest is the long-run base).
CARRY_OVER = 0.55
MOOD_DRIFT_SD = 0.015
#: Parties at or above this national share field a presidential ticket even without a template one.
TICKET_THRESHOLD = 0.10
TERM_LIMIT = 2  # a President who has served two terms does not run again (a tradition)


# --------------------------------------------------------------------------- lookups
def regular_election(session: Session, year: int) -> Election | None:
    return session.scalars(
        select(Election).where(Election.year == int(year), Election.election_type.in_(REGULAR_TYPES)).limit(1)
    ).first()


def _builtin(year: int) -> ScenarioDocument | None:
    for info in list_scenarios():
        if info.year == year and info.valid:
            try:
                return load_scenario(info.path)
            except Exception:
                log.warning("scenario %s for %d does not load; generating a continuation", info.slug, year)
    return None


def _template(session: Session, election_type: str) -> ScenarioDocument:
    """The scenario of the latest regular election of this type (else the built-in one)."""
    row = session.execute(
        select(Scenario)
        .join(Election, Election.scenario_id == Scenario.id)
        .where(Election.election_type == election_type)
        .order_by(Election.election_date.desc(), Election.id.desc())
        .limit(1)
    ).first()
    if row is not None:
        return parse_stored_scenario(row[0].document)
    for info in sorted(list_scenarios(), key=lambda i: -i.year):
        if info.election_type == election_type and info.valid:
            return load_scenario(info.path)
    raise ElectionError(f"no {election_type} scenario to continue from")


def previous_house_shares(session: Session, before: date) -> dict[str, float]:
    """National House popular vote shares by party of the latest reported regular election
    before a date (empty when there is none)."""
    eid = session.scalar(
        select(Election.id)
        .where(
            Election.election_type.in_(REGULAR_TYPES),
            Election.status.in_(list(REPORTED_STATUSES)),
            Election.election_date < before,
        )
        .order_by(Election.election_date.desc())
        .limit(1)
    )
    if eid is None:
        return {}
    rows = session.execute(
        select(BallotCandidate.party_code_snapshot, func.sum(ElectionResult.votes))
        .join(Race, Race.id == ElectionResult.race_id)
        .join(BallotCandidate, BallotCandidate.id == ElectionResult.ballot_candidate_id)
        .where(
            Race.election_id == eid,
            Race.race_type == RaceType.HOUSE.value,
            ElectionResult.level == "national",
        )
        .group_by(BallotCandidate.party_code_snapshot)
    ).all()
    votes = {str(p): float(v or 0) for p, v in rows if p}
    total = sum(votes.values())
    return {p: v / total for p, v in votes.items()} if total > 0 else {}


# --------------------------------------------------------------------------- continuation
def scenario_for_year(session: Session, year: int) -> ScenarioDocument:
    """The scenario of a regular election year: the built-in one when it exists, else a
    continuation of the previous elections."""
    cal = ElectionCalendar.from_config()
    cycle = cal.cycle(year)
    if not cycle.has_elections or cycle.election_type is None:
        raise ElectionError(f"no regular election is held in {year}")
    builtin = _builtin(year)
    if builtin is not None:
        return builtin
    return continue_scenario(session, _template(session, cycle.election_type.value), year)


def continue_scenario(session: Session, template: ScenarioDocument, year: int) -> ScenarioDocument:
    """``template`` moved to ``year`` with the national mood, events and tickets regenerated."""
    from app.services.elections import _shift_year

    cal = ElectionCalendar.from_config()
    cycle = cal.cycle(year)
    edate = cal.election_date(year)
    general = bool(cycle.president)
    data = _shift_year(template, year).model_dump(mode="json")
    seed = int(derive_seed(template.scenario.seed, "continuation", year) % (2**31))
    rng = make_rng(seed, "continuation")
    kind = "General" if general else "Midterm"
    data["scenario"].update(
        slug=f"auto-{year}",
        name=f"{kind} Election {year}",
        description=(
            "Generated by the world clock: the parties, the office holders and the national mood carry "
            "over from the previous elections (FICTIONAL)."
        ),
        seed=seed,
        election_type=cycle.election_type.value if cycle.election_type else template.scenario.election_type,
        election_date=None,
    )
    data["party_events"] = []
    active = [p for p in template.parties if p.dissolved_year is None or p.dissolved_year > year]
    codes = [p.code for p in active]

    # --- national mood: last result pulled towards the long-run base, plus drift
    base = np.array([p.base_share for p in active], dtype=float)
    base = base / base.sum()
    last = previous_house_shares(session, edate)
    prev = np.array([last.get(c, b) for c, b in zip(codes, base, strict=True)], dtype=float)
    prev = prev / prev.sum() if prev.sum() > 0 else base
    mood = CARRY_OVER * prev + (1 - CARRY_OVER) * base + rng.normal(0.0, MOOD_DRIFT_SD, size=len(codes))
    mood = np.clip(mood, 0.005, None)
    mood = mood / mood.sum()
    data["calibration"]["national"] = {c: round(float(v), 4) for c, v in zip(codes, mood, strict=True)}

    holders = create_ops.holders_at(session, edate)
    pres = holders.get(PRESIDENT_OFFICE)
    pres_party = (pres.current_party or pres.seat_party) if pres is not None else None
    env = data["environment"]
    env["national"] = {}
    env["provinces"] = {}
    env["events"] = _random_events(rng, codes, pres_party)
    env["president_party"] = pres_party if pres_party in codes else None
    for section in ("house", "senate", "governors", "municipal"):
        if isinstance(data.get(section), dict) and "explicit_candidates" in data[section]:
            data[section]["explicit_candidates"] = {}
    if isinstance(data.get("senate"), dict):
        data["senate"]["special_elections"] = []
    if general:
        tickets, people = _tickets(
            session, template, year, holders, codes, dict(zip(codes, mood, strict=True)), rng
        )
    else:
        tickets, people = [], []
    data["president"]["tickets"] = tickets
    data["candidates"] = [c.model_dump(mode="json", exclude_none=True) for c in people]
    doc = ScenarioDocument.model_validate(data)
    log.info("continuation scenario %s (%s, %d tickets)", doc.scenario.slug, kind.lower(), len(tickets))
    return doc


def _random_events(
    rng: np.random.Generator, codes: list[str], pres_party: str | None
) -> list[dict[str, Any]]:
    """A few generic, FICTIONAL events (each happens only in some draws)."""
    out: list[dict[str, Any]] = []
    if pres_party in codes:
        out.append(
            {
                "name": "economic-mood",
                "description": "Fictional — the state of the economy reflects on the President's party.",
                "national": {pres_party: round(float(rng.normal(-0.01, 0.03)), 3)},
                "probability": 0.6,
            }
        )
    if codes and rng.random() < 0.6:
        hit = codes[int(rng.integers(len(codes)))]
        out.append(
            {
                "name": "party-scandal",
                "description": f"Fictional — a scandal damages the {hit}.",
                "national": {hit: round(float(-rng.uniform(0.02, 0.06)), 3)},
                "probability": 0.35,
            }
        )
    if rng.random() < 0.4:
        out.append(
            {
                "name": "turnout-surge",
                "description": "Fictional — an unusually energised electorate.",
                "turnout": round(float(rng.uniform(0.02, 0.06)), 3),
                "probability": 0.3,
            }
        )
    return out


# --------------------------------------------------------------------------- tickets
def _terms_served(session: Session, candidate_id: int) -> int:
    office = session.scalar(select(Office.id).where(Office.code == PRESIDENT_OFFICE))
    if office is None:
        return 0
    return int(
        session.scalar(
            select(func.count())
            .select_from(OfficeHolder)
            .where(OfficeHolder.office_id == office, OfficeHolder.candidate_id == candidate_id)
        )
        or 0
    )


def _spec_of(session: Session, candidate_id: int, party: str | None) -> CandidateSpec | None:
    row = session.get(Candidate, candidate_id)
    if row is None:
        return None
    frame = get_frame(session)
    m = frame.muni_index_or_none(row.home_municipality_code) if row.home_municipality_code else None
    pv = frame.province_codes[int(frame.muni_province[m])] if m is not None else None
    return candidate_spec_from_row(row, party, pv)


_PROMINENCE = (("GOV-", 3.0), ("SEN-", 2.0), ("HOUSE-", 1.0))


def _prominent(
    session: Session,
    holders: dict[str, create_ops.Holder],
    party: str,
    exclude: set[str],
    rng: np.random.Generator,
    avoid_province: str | None = None,
) -> CandidateSpec | None:
    """One of the party's prominent office holders (governors, senators, House members), the
    stronger and more senior the likelier."""
    pool: list[tuple[float, create_ops.Holder]] = []
    for code, h in holders.items():
        if (h.current_party or h.seat_party) != party or h.candidate_key in exclude:
            continue
        weight = next(
            (w for prefix, w in _PROMINENCE if code.startswith(prefix) and not code.startswith("LTGOV")), 0.0
        )
        if weight <= 0:
            continue
        pool.append((weight, h))
    if not pool:
        return None
    quality = {
        int(i): float(q or 0.0)
        for i, q in session.execute(
            select(Candidate.id, Candidate.quality).where(Candidate.id.in_([h.candidate_id for _, h in pool]))
        ).all()
    }
    specs, w = [], []
    for weight, h in pool:
        spec = _spec_of(session, h.candidate_id, party)
        if spec is None or (avoid_province and spec.home_province == avoid_province):
            continue
        specs.append(spec)
        w.append(weight * float(np.exp(quality.get(h.candidate_id, 0.0))))
    if not specs:
        return None
    p = np.asarray(w) / np.sum(w)
    return specs[int(rng.choice(len(specs), p=p))]


def _new_person(
    session: Session, party: str, year: int, role: str, rng: np.random.Generator, avoid_province: str | None
) -> CandidateSpec:
    """A new FICTIONAL national politician of a party."""
    frame = get_frame(session)
    w = frame.muni_population.astype(float)
    if avoid_province is not None:
        w = w * np.array(
            [
                frame.province_codes[int(frame.muni_province[m])] != avoid_province
                for m in range(frame.n_munis)
            ]
        )
    m = int(rng.choice(frame.n_munis, p=w / w.sum()))
    pv = frame.province_codes[int(frame.muni_province[m])]
    gender = "F" if rng.random() < 0.46 else "M"
    first, last = fictional_name(rng, gender, province=pv, heritage_prob=0.08)
    age = int(rng.integers(44, 66))
    return CandidateSpec(
        key=f"{slugify(f'{first} {last}')}-{year}-{party.lower()}-{role}",
        first_name=first,
        last_name=last,
        gender=gender,
        birth_date=date(year - age, int(rng.integers(1, 13)), int(rng.integers(1, 29))),
        party=party,
        home_municipality=frame.muni_codes[m],
        home_province=pv,
        quality=float(np.clip(rng.normal(0.4, 0.3), -1.0, 1.5)),
        campaign_strength=float(np.clip(rng.normal(0.2, 0.4), -1.5, 1.5)),
        fundraising=float(np.round(rng.lognormal(0.2, 0.3), 3)),
        bio=f"Fictional {party} {'presidential' if role == 'p' else 'vice-presidential'} nominee ({year}).",
    )


def _tickets(
    session: Session,
    template: ScenarioDocument,
    year: int,
    holders: dict[str, create_ops.Holder],
    codes: list[str],
    mood: dict[str, float],
    rng: np.random.Generator,
) -> tuple[list[dict[str, Any]], list[CandidateSpec]]:
    pres = holders.get(PRESIDENT_OFFICE)
    vp = holders.get(VICE_PRESIDENT_OFFICE)
    pres_party = (pres.current_party or pres.seat_party) if pres is not None else None
    parties = [t.party for t in template.president.tickets if t.party in codes]
    parties += [
        c for c in sorted(codes, key=lambda c: -mood[c]) if mood[c] >= TICKET_THRESHOLD and c not in parties
    ]
    if pres_party in codes and pres_party not in parties:
        parties.insert(0, pres_party)
    used: set[str] = set()
    tickets: list[dict[str, Any]] = []
    people: dict[str, CandidateSpec] = {}
    for party in dict.fromkeys(parties):
        incumbent = False
        top: CandidateSpec | None = None
        mate: CandidateSpec | None = None
        if (
            pres is not None
            and pres_party == party
            and _terms_served(session, pres.candidate_id) < TERM_LIMIT
        ):
            top = _spec_of(session, pres.candidate_id, party)
            incumbent = top is not None
            if vp is not None and (vp.current_party or vp.seat_party) == party:
                mate = _spec_of(session, vp.candidate_id, party)
        if top is None:
            top = _prominent(session, holders, party, used, rng) or _new_person(
                session, party, year, "p", rng, None
            )
        used.add(top.key)
        if mate is None or mate.key in used:
            mate = _prominent(
                session, holders, party, used, rng, avoid_province=top.home_province
            ) or _new_person(session, party, year, "vp", rng, top.home_province)
        used.add(mate.key)
        people[top.key], people[mate.key] = top, mate
        tickets.append(
            {"party": party, "president": top.key, "vice_president": mate.key, "incumbent": incumbent}
        )
    return tickets, list(people.values())


# --------------------------------------------------------------------------- entry point
def ensure_regular_election(session: Session, year: int) -> Election:
    """The regular election of ``year``: the stored one, else created from
    :func:`scenario_for_year` (SCHEDULED)."""
    from app.services.elections import create_election

    found = regular_election(session, year)
    if found is not None:
        return found
    doc = scenario_for_year(session, year)
    # the REAL scenarios name real places that the synthetic test country lacks
    strict = geography_source(session).kind != "synthetic"
    el = create_election(session, doc, strict=strict)
    log.info("created regular election %d from %s", year, doc.scenario.slug)
    return el


def describe(doc: ScenarioDocument) -> dict[str, Any]:
    """A short summary of a continuation scenario (for logs and the API)."""
    return {
        "slug": doc.scenario.slug,
        "year": doc.scenario.year,
        "national": dict(doc.calibration.national),
        "tickets": [t.model_dump(mode="json") for t in doc.president.tickets],
        "events": json.loads(json.dumps(doc.environment.events)),
    }


__all__ = [
    "continue_scenario",
    "describe",
    "ensure_regular_election",
    "previous_house_shares",
    "regular_election",
    "scenario_for_year",
]
