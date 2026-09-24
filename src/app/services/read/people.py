"""FICTIONAL political actors: parties (identity, lineage, electoral record) and candidates
(search, profiles and careers).  Results in careers and party records are SIMULATED and only
shown for reported elections."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.core.errors import NotFoundError, ValidationError
from app.models import (
    BallotCandidate,
    Candidate,
    CandidateAffiliation,
    Election,
    Office,
    OfficeHolder,
    Party,
    PartyEvent,
    Province,
    Race,
)
from app.services import elections as election_service
from app.services._common import REPORTED_STATUSES
from app.services.read._base import (
    FICTIONAL,
    SIMULATED,
    cached,
    chunks,
    current_holders,
    iso,
    pct,
    person,
    ref_of,
    reported_refs,
    rnd,
)

MAX_LIMIT = 500


# =========================================================================== parties
def _party_record(session: Session) -> dict[str, list[dict[str, Any]]]:
    """Per party code: its record in every reported election (President %, EV, House / Senate
    seats, governorships)."""
    from app.services.read.elections import president

    out: dict[str, list[dict[str, Any]]] = {}
    for ref in reported_refs(session):
        res = election_service.election_summary(session, ref.id).get("results") or {}
        pres_by_party: dict[str, dict[str, Any]] = {}
        if res.get("president"):
            for t in president(session, ref)["tickets"]:
                if t["party"]:
                    pres_by_party[t["party"]] = {"pct": t["pct"], "electoral_votes": t["electoral_votes"]}
        house = (res.get("house") or {}).get("composition") or {}
        senate = (res.get("senate") or {}).get("composition") or {}
        govs: dict[str, int] = {}
        for p in (res.get("governors") or {}).values():
            if p:
                govs[p] = govs.get(p, 0) + 1
        parties = set(pres_by_party) | set(house) | set(senate) | set(govs)
        for p in parties:
            out.setdefault(p, []).append(
                {
                    "election_id": ref.id,
                    "year": ref.year,
                    "president_pct": (pres_by_party.get(p) or {}).get("pct"),
                    "electoral_votes": (pres_by_party.get(p) or {}).get("electoral_votes"),
                    "house_seats": house.get(p) if house else None,
                    "senate_seats": senate.get(p) if senate else None,
                    "governors": govs.get(p, 0) if res.get("governors") else None,
                }
            )
    return out


def parties(session: Session) -> dict[str, Any]:
    """``GET /api/parties`` — every FICTIONAL party: identity, colours, ideology, lineage events
    and its record in the reported elections."""
    state = tuple(r.cache_key() for r in reported_refs(session))

    def build() -> dict[str, Any]:
        rows = session.scalars(select(Party).order_by(Party.id)).all()
        code_of = {p.id: p.code for p in rows}
        events: dict[int, list[dict[str, Any]]] = {}
        for e in session.scalars(select(PartyEvent).order_by(PartyEvent.year, PartyEvent.id)):
            events.setdefault(e.party_id, []).append(
                {
                    "year": e.year,
                    "event_type": e.event_type,
                    "old_value": e.old_value,
                    "new_value": e.new_value,
                    "related_party": code_of.get(e.related_party_id) if e.related_party_id else None,
                    "notes": e.notes,
                }
            )
        record = _party_record(session)
        counts = dict(
            session.execute(select(Candidate.party_id, func.count()).group_by(Candidate.party_id))
            .tuples()
            .all()
        )
        return {
            "data_category": FICTIONAL,
            "provenance": {"identity": FICTIONAL, "record": SIMULATED},
            "parties": [
                {
                    "code": p.code,
                    "name": p.name,
                    "abbreviation": p.abbreviation,
                    "color": p.color,
                    "color_secondary": p.color_secondary,
                    "family": p.family,
                    "description": p.description,
                    "ideology": {
                        "economic": rnd(p.ideology_economic, 3),
                        "social": rnd(p.ideology_social, 3),
                        "europe": rnd(p.ideology_europe, 3),
                    },
                    "founded_year": p.founded_year,
                    "dissolved_year": p.dissolved_year,
                    "successor": code_of.get(p.successor_party_id) if p.successor_party_id else None,
                    "is_active": bool(p.is_active),
                    "candidates": int(counts.get(p.id, 0)),
                    "events": events.get(p.id, []),
                    "record": sorted(record.get(p.code, []), key=lambda r: r["year"]),
                }
                for p in rows
            ],
        }

    return cached(session, "parties", state, build)


# =========================================================================== candidates
def _offices_by_candidate(session: Session) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for code, h in current_holders(session).items():
        out.setdefault(int(h["candidate_id"]), []).append(code)
    return out


def candidates(
    session: Session,
    *,
    search: str | None = None,
    party: str | None = None,
    office: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """``GET /api/candidates?search=&party=&office=&limit=&offset=`` — FICTIONAL people matching
    a name / key search, current party and serving office prefix (``PRES``, ``HOUSE``, ``SEN``,
    ``GOV``, ``MAYOR``)."""
    if limit < 1 or limit > MAX_LIMIT:
        raise ValidationError(f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise ValidationError("offset must be ≥ 0")
    holders = _offices_by_candidate(session)
    q = select(Candidate, Party.code, Party.color).outerjoin(Party, Party.id == Candidate.party_id)
    cq = select(func.count()).select_from(Candidate).outerjoin(Party, Party.id == Candidate.party_id)
    if search:
        pat = f"%{search.strip()}%"
        cond = or_(Candidate.full_name.ilike(pat), Candidate.key.ilike(pat))
        q, cq = q.where(cond), cq.where(cond)
    if party:
        q, cq = q.where(Party.code == party.upper()), cq.where(Party.code == party.upper())
    if office:
        pref = office.upper().rstrip("-")
        ids = sorted(
            cid for cid, codes in holders.items() if any(c == pref or c.startswith(pref + "-") for c in codes)
        )
        q, cq = q.where(Candidate.id.in_(ids)), cq.where(Candidate.id.in_(ids))
    total = int(session.scalar(cq) or 0)
    rows = session.execute(
        q.order_by(Candidate.last_name, Candidate.first_name, Candidate.id).offset(offset).limit(limit)
    ).all()
    ids = [c.id for c, _p, _col in rows]
    races: dict[int, int] = {}
    for part in chunks(ids):
        for cid, n in session.execute(
            select(BallotCandidate.candidate_id, func.count())
            .where(BallotCandidate.candidate_id.in_(part))
            .group_by(BallotCandidate.candidate_id)
        ).all():
            races[int(cid)] = int(n)
    prov = dict(session.execute(select(Province.id, Province.code)).tuples().all())
    return {
        "data_category": FICTIONAL,
        "total": total,
        "limit": limit,
        "offset": offset,
        "candidates": [
            {
                **(person(c) or {}),
                "party": pcode,
                "color": color,
                "birth_date": iso(c.birth_date),
                "home_province": prov.get(c.home_province_id) if c.home_province_id else None,
                "offices": holders.get(c.id, []),
                "races": races.get(c.id, 0),
            }
            for c, pcode, color in rows
        ],
    }


def candidate_profile(session: Session, candidate_id: int) -> dict[str, Any]:
    """``GET /api/candidates/{id}`` — profile, party history, offices held and every candidacy
    (results only for reported elections)."""
    c = session.get(Candidate, candidate_id)
    if c is None:
        raise NotFoundError(f"candidate {candidate_id} not found")
    code_of = dict(session.execute(select(Party.id, Party.code)).tuples().all())
    prov = dict(session.execute(select(Province.id, Province.code)).tuples().all())
    affiliations = [
        {
            "party": code_of.get(a.party_id) if a.party_id else None,
            "from_year": a.from_year,
            "to_year": a.to_year,
        }
        for a in session.scalars(
            select(CandidateAffiliation)
            .where(CandidateAffiliation.candidate_id == c.id)
            .order_by(CandidateAffiliation.from_year)
        )
    ]
    offices = []
    for oh, o, race_code in session.execute(
        select(OfficeHolder, Office, Race.code)
        .join(Office, Office.id == OfficeHolder.office_id)
        .outerjoin(Race, Race.id == OfficeHolder.election_race_id)
        .where(OfficeHolder.candidate_id == c.id)
        .order_by(OfficeHolder.term_start)
    ).all():
        offices.append(
            {
                "office": o.code,
                "office_name": o.name,
                "office_type": o.office_type,
                "party": code_of.get(oh.party_id) if oh.party_id else None,
                "term_start": iso(oh.term_start),
                "term_end": iso(oh.term_end),
                "ended_on": iso(oh.ended_on),
                "serving": oh.ended_on is None,
                "start_reason": oh.start_reason,
                "end_reason": oh.end_reason,
                "elected_in": race_code,
            }
        )
    mate = aliased(Candidate)
    lead = aliased(Candidate)
    ballots = session.execute(
        select(BallotCandidate, Race, Election, lead, mate)
        .join(Race, Race.id == BallotCandidate.race_id)
        .join(Election, Election.id == Race.election_id)
        .outerjoin(lead, lead.id == BallotCandidate.candidate_id)
        .outerjoin(mate, mate.id == BallotCandidate.running_mate_id)
        .where(or_(BallotCandidate.candidate_id == c.id, BallotCandidate.running_mate_id == c.id))
        .order_by(Election.election_date, Race.id)
    ).all()
    from app.models import ElectionResult

    candidacies = []
    wins = losses = 0
    for b, r, el, ld, mt in ballots:
        reported = el.status in REPORTED_STATUSES
        result = None
        if reported:
            votes = session.execute(
                select(ElectionResult.ballot_candidate_id, ElectionResult.votes).where(
                    ElectionResult.race_id == r.id, ElectionResult.level == "national"
                )
            ).all()
            total = sum(int(v) for _b, v in votes)
            mine = sum(int(v) for bid, v in votes if bid == b.id)
            won = r.winner_ballot_candidate_id == b.id
            wins += int(won)
            losses += int(not won)
            result = {"votes": mine, "pct": pct(mine, total), "won": won, "margin_pp": rnd(r.margin_pct, 3)}
        candidacies.append(
            {
                "election": ref_of(el).brief(),
                "race_code": r.code,
                "race_name": r.name,
                "race_type": r.race_type,
                "role": "running_mate"
                if b.running_mate_id == c.id and b.candidate_id != c.id
                else "candidate",
                "ballot_name": b.ballot_name,
                "line_key": b.line_key,
                "party": b.party_code_snapshot,
                "color": b.party_color_snapshot,
                "incumbent": bool(b.is_incumbent),
                "running_mate": person(mt) if b.candidate_id == c.id else None,
                "ticket_leader": person(ld) if b.running_mate_id == c.id else None,
                "result": result,
            }
        )
    return {
        "data_category": FICTIONAL,
        "provenance": {"profile": FICTIONAL, "results": SIMULATED},
        **(person(c) or {}),
        "birth_date": iso(c.birth_date),
        "party": code_of.get(c.party_id) if c.party_id else None,
        "home_province": prov.get(c.home_province_id) if c.home_province_id else None,
        "ideology": {"economic": rnd(c.ideology_economic, 3), "social": rnd(c.ideology_social, 3)},
        "quality": rnd(c.quality, 3),
        "campaign_strength": rnd(c.campaign_strength, 3),
        "fundraising": rnd(c.fundraising, 3),
        "favorability": rnd(c.favorability, 2),
        "approval": rnd(c.approval, 2),
        "bio": c.bio,
        "affiliations": affiliations,
        "offices": offices,
        "candidacies": candidacies,
        "summary": {
            "candidacies": len(candidacies),
            "wins": wins,
            "losses": losses,
            "offices_held": len(offices),
            "serving": [o["office"] for o in offices if o["serving"]],
        },
    }
