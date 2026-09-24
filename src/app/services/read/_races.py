"""Race views in the three results modes (``final`` | ``live`` | ``hidden``).

:class:`RaceBook` loads the races of one election with their ballot lines, geography codes,
incumbents and — depending on the election's ``results_source`` — the stored totals (FINAL), the
night service's live counts (LIVE) or nothing (hidden).  :meth:`RaceBook.view` renders one race
in the common JSON shape used by every results page::

    {"code", "name", "type", "status", "province_code", "district_code", "municipality_code",
     "municipality_name", "electoral_votes", "seats", "electoral_system", "is_special",
     "open_seat", "incumbent", "previous_party", "lines": [...], "leader", "winner",
     "margin_pp", "margin_votes", "total_votes", "turnout_pct", "reporting_pct",
     "flip_status", "decided_by", "win_probability", "called_at"}
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constitution import DECIDED_STATUSES, RaceStatus, RaceType
from app.elections.seats import INDEPENDENT
from app.models import (
    Candidate,
    Election,
    LegislatureSeatResult,
    Office,
    OfficeHolder,
    Party,
    Race,
)
from app.services._common import REPORTED_STATUSES
from app.services.read._base import (
    ElectionRef,
    ballot_lines,
    chunks,
    district_maps,
    iso,
    leader_of,
    municipality_maps,
    party_codes_by_id,
    pct,
    province_maps,
    rnd,
    stored_totals,
    stored_turnout,
    with_votes,
)
from app.services.read.live import live_races

DECIDED: frozenset[str] = frozenset(s.value for s in DECIDED_STATUSES)
HIDDEN_STATUS = RaceStatus.SCHEDULED.value

#: Race families (code prefix → race types) used by map / province views.
FAMILIES: dict[str, tuple[RaceType, ...]] = {
    "PRES": (RaceType.PRESIDENT, RaceType.PRESIDENT_PROVINCE),
    "HOUSE": (RaceType.HOUSE,),
    "SEN": (RaceType.SENATE,),
    "GOV": (RaceType.GOVERNOR,),
    "PROVLEG": (RaceType.PROVINCIAL_LEGISLATURE,),
    "MAYOR": (RaceType.MAYOR,),
    "COUNCIL": (RaceType.MUNICIPAL_COUNCIL,),
}
MULTI_SEAT: frozenset[str] = frozenset(
    {RaceType.PROVINCIAL_LEGISLATURE.value, RaceType.MUNICIPAL_COUNCIL.value}
)


def party_key(party: str | None) -> str:
    """Party code, or ``independent`` for lines without a party (as :mod:`app.elections.seats`)."""
    if party is None or party == "IND":
        return INDEPENDENT
    return str(party)


@dataclass
class RaceBook:
    """Races of one election with lines and results in the election's results mode."""

    session: Session
    ref: ElectionRef
    races: list[Race]
    lines: dict[int, list[dict[str, Any]]]
    prov_code: dict[int, str]
    provinces: dict[str, dict[str, Any]]
    munis: dict[int, dict[str, Any]]
    dists: dict[int, dict[str, Any]]
    pcode: dict[int, str]
    incumbents: dict[int, dict[str, Any]]
    previous_party: dict[int, str | None]
    totals: dict[int, dict[int, int]] = field(default_factory=dict)
    turnout: dict[int, dict[str, Any]] = field(default_factory=dict)
    live: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)
    seats_won: dict[int, dict[str, int]] = field(default_factory=dict)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(
        cls,
        session: Session,
        ref: ElectionRef,
        *,
        types: Iterable[RaceType | str] | None = None,
        codes: Iterable[str] | None = None,
        snapshot: Mapping[str, Any] | None = None,
    ) -> RaceBook:
        """Load the races of ``ref`` (optionally only ``types`` / ``codes``).  ``snapshot`` is the
        night snapshot to use for a LIVE election (fetched when omitted)."""
        q = select(Race).where(Race.election_id == ref.id)
        if types is not None:
            q = q.where(Race.race_type.in_([RaceType(t).value for t in types]))
        if codes is not None:
            q = q.where(Race.code.in_(list(codes)))
        races = list(session.scalars(q.order_by(Race.id)))
        ids = [r.id for r in races]
        prov_code, provinces = province_maps(session)
        book = cls(
            session=session,
            ref=ref,
            races=races,
            lines=ballot_lines(session, ids),
            prov_code=prov_code,
            provinces=provinces,
            munis=municipality_maps(session, ref.vintage_id)
            if any(r.municipality_id is not None for r in races)
            else {},
            dists=district_maps(session, ref.district_plan_id)
            if any(r.district_id is not None for r in races)
            else {},
            pcode=party_codes_by_id(session),
            incumbents=_incumbent_people(session, races),
            previous_party={},
        )
        if ref.reported:
            book.totals = stored_totals(session, ref, ids)
            book.turnout = stored_turnout(session, ref, ids)
            book.seats_won = _seats_won(
                session, [r.id for r in races if r.race_type in MULTI_SEAT], book.pcode
            )
        elif ref.live:
            from app.services.read.live import live_snapshot

            snap = dict(snapshot) if snapshot is not None else live_snapshot(session, ref.id)
            book.snapshot = snap
            book.live = live_races(snap)
            _add_live_president(book.live, snap)
        book.previous_party = _previous_parties(session, races, book.pcode)
        return book

    # ------------------------------------------------------------------ lookup
    @property
    def by_code(self) -> dict[str, Race]:
        return {r.code: r for r in self.races}

    def of_type(self, *types: RaceType) -> list[Race]:
        wanted = {t.value for t in types}
        return [r for r in self.races if r.race_type in wanted]

    def line_votes(self, race: Race) -> dict[str, int] | None:
        """Votes per line key (FINAL: stored totals; LIVE: counted so far; hidden: ``None``)."""
        if self.ref.reported:
            t = self.totals.get(race.id, {})
            return {ln["key"]: int(t.get(ln["ballot_candidate_id"], 0)) for ln in self.lines.get(race.id, [])}
        if self.ref.live:
            lr = self.live.get(race.code)
            if lr is None:
                return None
            v = lr.get("votes") or {}
            return {ln["key"]: int(v.get(ln["key"], 0)) for ln in self.lines.get(race.id, [])}
        return None

    def winner_key(self, race: Race) -> str | None:
        if self.ref.reported:
            for ln in self.lines.get(race.id, []):
                if ln["ballot_candidate_id"] == race.winner_ballot_candidate_id:
                    return ln["key"]
            return None
        if self.ref.live:
            lr = self.live.get(race.code) or {}
            return lr.get("called_key") if lr.get("status") in DECIDED else None
        return None

    def line_party(self, race: Race, key: str | None) -> str | None:
        if key is None:
            return None
        for ln in self.lines.get(race.id, []):
            if ln["key"] == key:
                return ln["party"]
        return None

    def line(self, race: Race, key: str | None) -> dict[str, Any] | None:
        if key is None:
            return None
        return next((ln for ln in self.lines.get(race.id, []) if ln["key"] == key), None)

    # ------------------------------------------------------------------ views
    def geo(self, race: Race) -> dict[str, Any]:
        m = self.munis.get(race.municipality_id) if race.municipality_id is not None else None
        d = self.dists.get(race.district_id) if race.district_id is not None else None
        return {
            "province_code": self.prov_code.get(race.province_id) if race.province_id is not None else None,
            "district_code": None if d is None else d["code"],
            "district_name": None if d is None else d["name"],
            "municipality_code": None if m is None else m["code"],
            "municipality_name": None if m is None else m["name"],
        }

    def view(
        self, race: Race, *, lines: bool = True, top: int | None = None, compact: bool = False
    ) -> dict[str, Any]:
        """The race in the common shape (see the module docstring).  ``lines=False`` omits the
        line list; ``top`` keeps only the ``top`` best lines (by votes, else ballot order);
        ``compact`` renders lines with :func:`compact_line` (lists of many races)."""
        votes = self.line_votes(race)
        winner = self.winner_key(race)
        leader, runner_up, margin_calc, margin_votes_calc = leader_of(votes)
        prev = self.previous_party.get(race.id)
        out: dict[str, Any] = {
            "code": race.code,
            "name": race.name,
            "type": race.race_type,
            **self.geo(race),
            "electoral_votes": race.electoral_votes,
            "seats": race.seats,
            "electoral_system": race.electoral_system,
            "is_special": bool(race.is_special),
            "open_seat": bool(race.is_open_seat),
            "incumbent": self._incumbent(race),
            "previous_party": prev,
            "leader": leader,
            "runner_up": runner_up,
            "winner": winner,
            "win_probability": None,
            "decided_by": None,
            "called_at": None,
        }
        if self.ref.reported:
            t = self.turnout.get(race.id, {})
            out.update(
                {
                    "status": race.status or RaceStatus.FINAL.value,
                    "margin_pp": rnd(race.margin_pct, 4) if race.margin_pct is not None else margin_calc,
                    "margin_votes": race.margin_votes if race.margin_votes is not None else margin_votes_calc,
                    "total_votes": race.total_votes if race.total_votes is not None else t.get("valid_votes"),
                    "turnout_pct": rnd(race.turnout_pct, 3)
                    if race.turnout_pct is not None
                    else t.get("turnout_pct"),
                    "ballots_cast": t.get("ballots_cast"),
                    "eligible": t.get("eligible"),
                    "reporting_pct": 100.0,
                    "decided_by": race.decided_by,
                    "called_at": iso(race.called_at),
                    "flip_status": _flip_status(prev, self.line_party(race, winner), winner is not None),
                }
            )
            if race.id in self.seats_won:
                out["seats_won"] = self.seats_won[race.id]
        elif self.ref.live:
            lr = self.live.get(race.code) or {}
            called = winner is not None
            out.update(
                {
                    "status": lr.get("status", RaceStatus.POLLS_CLOSED.value),
                    "leader": lr.get("leader", leader),
                    "margin_pp": rnd(lr.get("margin_pct"), 4),
                    "margin_votes": margin_votes_calc,
                    "total_votes": sum(votes.values()) if votes else 0,
                    "turnout_pct": None,
                    "reporting_pct": rnd(lr.get("reporting_pct"), 3) if lr else 0.0,
                    "win_probability": rnd(lr.get("win_probability"), 4),
                    "lean": lr.get("lean_key"),
                    "is_manual": bool(lr.get("is_manual", False)),
                    "flip_status": _flip_status(prev, self.line_party(race, winner), called),
                }
            )
        else:
            out.update(
                {
                    "status": HIDDEN_STATUS,
                    "leader": None,
                    "runner_up": None,
                    "margin_pp": None,
                    "margin_votes": None,
                    "total_votes": None,
                    "turnout_pct": None,
                    "reporting_pct": None,
                    "flip_status": None,
                }
            )
        wl = self.lines.get(race.id, [])
        for key in ("leader", "winner"):
            ln = self.line(race, out[key])
            out[f"{key}_party"] = None if ln is None else ln["party"]
            out[f"{key}_name"] = None if ln is None else ln["name"]
            out[f"{key}_color"] = None if ln is None else ln["color"]
        if lines:
            lst = with_votes(wl, votes, winner=winner)
            if top is not None:
                if votes is not None:
                    lst = sorted(lst, key=lambda d: (-(d["votes"] or 0), d["order"]))
                lst = lst[:top]
            out["lines"] = [compact_line(x) for x in lst] if compact else lst
        return out

    def _incumbent(self, race: Race) -> dict[str, Any] | None:
        if race.incumbent_candidate_id is None and race.incumbent_party_id is None:
            return None
        person = self.incumbents.get(race.incumbent_candidate_id) if race.incumbent_candidate_id else None
        running = any(ln["incumbent"] for ln in self.lines.get(race.id, []))
        return {
            "candidate_id": race.incumbent_candidate_id,
            "name": None if person is None else person["name"],
            "portrait_key": None if person is None else person["portrait_key"],
            "party": self.pcode.get(race.incumbent_party_id) if race.incumbent_party_id else None,
            "running": running,
        }


def _add_live_president(live: dict[str, dict[str, Any]], snap: Mapping[str, Any]) -> None:
    """The national ``PRES`` race is derived by the night engine (not in ``snapshot.races``):
    rebuild its compact live state from ``snapshot.president`` / ``snapshot.popular_vote``."""
    p = snap.get("president")
    if "PRES" in live or not p:
        return
    pv = snap.get("popular_vote") or {}
    votes = {str(t["key"]): int(t.get("votes") or 0) for t in p.get("tickets") or []}
    lead, _run, margin, _mv = leader_of(votes)
    live["PRES"] = {
        "key": "PRES",
        "status": p.get("status"),
        "votes": votes,
        "leader": lead,
        "called_key": p.get("winner"),
        "reporting_pct": pv.get("reporting_pct"),
        "margin_pct": margin,
        "win_probability": None,
    }


def compact_line(ln: Mapping[str, Any]) -> dict[str, Any]:
    """A ballot line without the nested person objects (for lists of many races)."""
    c = ln.get("candidate") or {}
    m = ln.get("running_mate") or {}
    return {
        "key": ln["key"],
        "name": ln["name"],
        "party": ln["party"],
        "color": ln["color"],
        "candidate_id": c.get("id"),
        "portrait_key": c.get("portrait_key"),
        "running_mate_name": m.get("name"),
        "incumbent": ln["incumbent"],
        "votes": ln.get("votes"),
        "pct": ln.get("pct"),
        "winner": ln.get("winner", False),
    }


def _flip_status(previous: str | None, party: str | None, decided: bool) -> str | None:
    if not decided:
        return None
    if previous is None:
        return "new"
    return "hold" if party_key(party) == party_key(previous) else "flip"


def _incumbent_people(session: Session, races: Sequence[Race]) -> dict[int, dict[str, Any]]:
    ids = sorted({r.incumbent_candidate_id for r in races if r.incumbent_candidate_id is not None})
    out: dict[int, dict[str, Any]] = {}
    for part in chunks(ids):
        for c in session.scalars(select(Candidate).where(Candidate.id.in_(part))):
            out[c.id] = {"name": c.full_name, "portrait_key": c.portrait_key}
    return out


def _previous_parties(
    session: Session, races: Sequence[Race], pcode: Mapping[int, str]
) -> dict[int, str | None]:
    """Party that held each race's seat before the election: the winner of the previous race for
    the same office when that election is reported, else the incumbent's party."""
    prev_ids = sorted({r.previous_race_id for r in races if r.previous_race_id is not None})
    prev_winner: dict[int, int | None] = {}
    for part in chunks(prev_ids):
        for rid, wp in session.execute(
            select(Race.id, Race.winner_party_id)
            .join(Election, Election.id == Race.election_id)
            .where(Race.id.in_(part), Election.status.in_(list(REPORTED_STATUSES)))
        ).all():
            prev_winner[int(rid)] = wp
    out: dict[int, str | None] = {}
    for r in races:
        base = prev_winner.get(r.previous_race_id) if r.previous_race_id is not None else None
        if base is None:
            base = r.incumbent_party_id
        out[r.id] = pcode.get(base) if base is not None else None
    return out


def _seats_won(
    session: Session, race_ids: Sequence[int], pcode: Mapping[int, str]
) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    for part in chunks(list(race_ids)):
        for rid, pid, seats in session.execute(
            select(
                LegislatureSeatResult.race_id, LegislatureSeatResult.party_id, LegislatureSeatResult.seats
            ).where(LegislatureSeatResult.race_id.in_(part))
        ).all():
            if int(seats) > 0:
                out.setdefault(int(rid), {})[pcode.get(pid, str(pid))] = int(seats)
    return {k: dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))) for k, v in out.items()}


def holders_on(session: Session, office_codes: Iterable[str], day: date) -> dict[str, dict[str, Any]]:
    """Holder of each office serving on ``day`` (term started on or before, not ended before)."""
    codes = list(office_codes)
    if not codes:
        return {}
    out: dict[str, dict[str, Any]] = {}
    rows = session.execute(
        select(Office.code, OfficeHolder, Candidate, Party.code)
        .join(OfficeHolder, OfficeHolder.office_id == Office.id)
        .join(Candidate, Candidate.id == OfficeHolder.candidate_id)
        .outerjoin(Party, Party.id == OfficeHolder.party_id)
        .where(Office.code.in_(codes), OfficeHolder.term_start <= day)
        .order_by(Office.code, OfficeHolder.term_start)
    ).all()
    for code, oh, c, party in rows:
        if oh.ended_on is not None and oh.ended_on <= day:
            continue
        out[code] = {
            "candidate_id": c.id,
            "name": c.full_name,
            "portrait_key": c.portrait_key,
            "party": party,
            "term_start": iso(oh.term_start),
            "term_end": iso(oh.term_end),
        }
    return out


def share_map(votes: Mapping[str, int] | None) -> dict[str, float] | None:
    """``{key: pct}`` of a vote mapping (``None`` when hidden)."""
    if votes is None:
        return None
    total = sum(int(v) for v in votes.values())
    return {k: (pct(v, total) if total else 0.0) for k, v in votes.items()}
