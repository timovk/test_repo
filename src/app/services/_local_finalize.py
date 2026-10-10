"""Certification steps specific to the in-between local elections (docs/LOCAL_ELECTIONS.md).

Called by :class:`app.services._finalize.Finalizer` for ``ElectionType.LOCAL`` elections:

* :func:`record_outcomes` — after the race summaries: the winners of every vote-for-N race, whether
  each Yes/No question passed, and the fate of a recall's replacement race (moot when the recall
  failed) go into ``race.details_json``;
* :func:`collect_installs` — the office terms the election starts: school board and water board
  seats (re-elected incumbents keep their seat; other winners fill the remaining seats by rank,
  longest terms first), the winner of a mayoral special election or of a successful recall's
  replacement race (for the rest of the term), and the winner of a council-seat special election.
  The holder whose office became vacant is ended on the vacancy date first;
* :func:`after_install` — a recalled mayor's term ends with ``end_reason = "recalled"``.

Every change is undone by :func:`app.services.reset.reset_election` (``ended_holder_id`` in the
race details names the holder ended at a vacancy; terms started here carry the race id).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.constitution import ElectoralSystem, RaceType
from app.models import OfficeHolder, Race
from app.services._common import bulk_insert, dumps, loads

if TYPE_CHECKING:  # pragma: no cover
    from app.services._finalize import Finalizer

#: ``details_json`` keys written by certification (removed again by a reset).
CERTIFICATION_KEYS: tuple[str, ...] = (
    "winners",
    "passed",
    "yes_share",
    "moot",
    "recall_passed",
    "ended_holder_id",
    "seat_assignment",
)


def _details(row: Race) -> dict[str, Any]:
    return loads(row.details_json) if row.details_json else {}


def _plus_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


def record_outcomes(fin: Finalizer, race_rows: dict[str, Race]) -> None:
    """Winners of vote-for-N races, Yes/No outcomes and moot recall replacements."""
    recall_passed: dict[str, bool] = {}
    for code, spec in fin.inputs.races.items():
        row = race_rows[code]
        tab = fin.tabs[code]
        d = _details(row)
        if ElectoralSystem(spec.electoral_system) == ElectoralSystem.PLURALITY_AT_LARGE:
            d["winners"] = [tab.line_keys[i] for i in tab.winners]
        if spec.threshold is not None:
            d["passed"] = bool(tab.passed)
            yes = tab.line_keys.index("YES") if "YES" in tab.line_keys else None
            if yes is not None:
                d["yes_share"] = round(float(tab.shares[yes]), 6)
            if RaceType(spec.race_type) == RaceType.RECALL:
                recall_passed[code] = bool(tab.passed)
        row.details_json = dumps(d) if d else None
    for code in fin.inputs.races:
        row = race_rows[code]
        d = _details(row)
        parent = d.get("parent")
        if parent and parent in recall_passed:
            d["recall_passed"] = recall_passed[parent]
            if not recall_passed[parent]:
                # the mayor stays: the replacement race is counted but elects nobody
                d["moot"] = True
                row.winner_ballot_candidate_id = None
                row.winner_party_id = None
                row.flipped = None
                row.decided_by = "recall_failed"
            row.details_json = dumps(d)
    fin.session.flush()


def collect_installs(fin: Finalizer, race_rows: dict[str, Race], offices: dict[str, Any], add: Any) -> None:
    """Queue (via ``add``) the board seats and special-election winners; end vacated holders and
    insert council-seat winners directly (a council has many members, so its office is shared)."""
    s = fin.session
    office_code_of = {o.id: c for c, o in offices.items()}
    council_rows: list[dict[str, Any]] = []
    for code, spec in fin.inputs.races.items():
        rt = RaceType(spec.race_type)
        row = race_rows[code]
        d = _details(row)
        tab = fin.tabs[code]
        if rt in (RaceType.SCHOOL_BOARD, RaceType.WATER_BOARD):
            start = date.fromisoformat(d["term_start"])
            seat_terms = {int(k): int(v) for k, v in d.get("seat_terms", {}).items()}
            office_codes = dict(
                zip((int(x) for x in d.get("seats_up", [])), d.get("office_codes", []), strict=False)
            )
            winners = [tab.line_keys[i] for i in tab.winners]
            incumbent_seat = _incumbent_seats(fin, code, office_codes, start)
            free = sorted(seat_terms, key=lambda x: (-seat_terms[x], x))
            assignment: dict[str, int] = {}
            for key in winners:  # re-elected incumbents keep their seat
                seat = incumbent_seat.get(key)
                if seat is not None and seat in free:
                    assignment[key] = seat
                    free.remove(seat)
            for key in winners:
                if key not in assignment and free:
                    assignment[key] = free.pop(0)
            for key, seat in assignment.items():
                ln = fin.line(code, key)
                if ln is None or seat not in office_codes:
                    continue
                add(
                    office_codes[seat],
                    ln.candidate_id,
                    None,
                    (start, _plus_years(start, seat_terms[seat])),
                    "elected",
                    code,
                )
            d["seat_assignment"] = {k: v for k, v in assignment.items()}
            row.details_json = dumps(d)
        elif rt == RaceType.MAYOR and spec.is_special:
            if d.get("moot"):
                continue
            office = office_code_of.get(row.office_id) if row.office_id is not None else d.get("office_code")
            winner = fin.winners.get(code)
            ln = fin.line(code, winner)
            if office is None or ln is None:
                continue
            start, end = date.fromisoformat(d["term_start"]), date.fromisoformat(d["term_end"])
            if d.get("kind") == "mayor_special" and d.get("event_date"):
                ended = _end_vacated(
                    fin, offices.get(office), date.fromisoformat(d["event_date"]), d.get("reason")
                )
                if ended is not None:
                    d["ended_holder_id"] = ended
                    row.details_json = dumps(d)
            add(office, ln.candidate_id, ln.party_id, (start, end), "elected", code)
        elif rt == RaceType.COUNCIL_SEAT:
            winner = fin.winners.get(code)
            ln = fin.line(code, winner)
            office = offices.get(d.get("office_code"))
            if ln is None or office is None:
                continue
            council_rows.append(
                {
                    "office_id": office.id,
                    "candidate_id": ln.candidate_id,
                    "party_id": ln.party_id,
                    "term_start": date.fromisoformat(d["term_start"]),
                    "term_end": date.fromisoformat(d["term_end"]),
                    "ended_on": None,
                    "start_reason": "elected",
                    "end_reason": None,
                    "election_race_id": row.id,
                }
            )
    if council_rows:
        bulk_insert(s, OfficeHolder, council_rows)
        fin.result.office_holders += len(council_rows)
    s.flush()


def _incumbent_seats(fin: Finalizer, code: str, office_codes: dict[int, str], on: date) -> dict[str, int]:
    """Line key → seat number for the incumbents (serving holders of the seats up) on the ballot."""
    from app.services._create import holders_at

    holders = holders_at(fin.session, on)
    by_candidate = {}
    for seat, oc in office_codes.items():
        h = holders.get(oc)
        if h is not None:
            by_candidate[h.candidate_id] = seat
    out: dict[str, int] = {}
    for ln in fin.inputs.races[code].lines:
        line = fin.line(code, ln.key)
        if line is not None and line.candidate_id in by_candidate:
            out[ln.key] = by_candidate[line.candidate_id]
    return out


def _end_vacated(fin: Finalizer, office: Any, on: date, reason: str | None) -> int | None:
    """End the term of whoever held ``office`` on the vacancy date (returns the holder id)."""
    if office is None:
        return None
    h = fin.session.scalars(
        select(OfficeHolder)
        .where(
            OfficeHolder.office_id == office.id,
            OfficeHolder.term_start <= on,
            (OfficeHolder.ended_on.is_(None)) | (OfficeHolder.ended_on > on),
        )
        .order_by(OfficeHolder.term_start.desc())
        .limit(1)
    ).first()
    if h is None:
        return None
    h.ended_on = on
    h.end_reason = (reason or "resigned")[:24]
    return int(h.id)


def after_install(fin: Finalizer, race_rows: dict[str, Race]) -> None:
    """A successful recall ends the mayor's term with ``end_reason = 'recalled'``."""
    s = fin.session
    for code, spec in fin.inputs.races.items():
        if RaceType(spec.race_type) != RaceType.MAYOR or not spec.is_special:
            continue
        row = race_rows[code]
        d = _details(row)
        if not d.get("recall_passed") or row.office_id is None:
            continue
        start = date.fromisoformat(d["term_start"])
        target = d.get("vacated_candidate_id")
        for h in s.scalars(
            select(OfficeHolder).where(
                OfficeHolder.office_id == row.office_id, OfficeHolder.ended_on == start
            )
        ):
            if target is None or h.candidate_id == target:
                h.end_reason = "recalled"
    s.flush()
