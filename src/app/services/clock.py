"""The world clock (docs/CLOCK.md): one *today* for the whole simulated world.

Elections happen on their dates, and the clock moves from one election day to the next:

* :func:`today` — the world's date (``app_meta.world_date``; when unset, the date of the earliest
  election that is not finished yet, else the latest finished one);
* :func:`next_election_day` — the next date with anything on the ballot: an in-between local
  election (every province voting that day, one combined night) or a regular November election.
  Future elections are planned, not stored, until the clock reaches them;
* :func:`advance` — move to the next election day once today's elections are finished, creating
  that day's election (a regular one from :mod:`app.services.continuation` when no built-in
  scenario covers the year) and returning the news since the previous day;
* :func:`count_today` — finish today's election instantly (simulated, then run through an
  instant election night, so every race call is stored); watching instead means simulating it
  (:func:`prepare_today`) and playing its night;
* :func:`skip_to` — count every election day before a date and move the clock there;
* :func:`news` — what happened between two dates: vacancies and recall petitions, the outcomes of
  finished elections, and how your own people (config/people.yaml) did.

The clock only moves forward.  Replaying a night (``reset``) leaves it where it is.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constitution import ElectionStatus, ElectionType, RaceType
from app.core.errors import ElectionError
from app.core.logging import get_logger
from app.elections.calendar import ElectionCalendar
from app.elections.local_calendar import (
    EVENT_COUNCIL_VACANCY,
    EVENT_MAYOR_RECALL,
    EVENT_MAYOR_VACANCY,
)
from app.models import BallotCandidate, Candidate, Election, Race
from app.services._common import REPORTED_STATUSES, get_meta, set_meta

log = get_logger(__name__)

TODAY_KEY = "world_date"
#: How far ahead the clock looks for the next election day.
HORIZON_DAYS = 800


# --------------------------------------------------------------------------- today
def stored_today(session: Session) -> date | None:
    text = get_meta(session, TODAY_KEY)
    return date.fromisoformat(text) if text else None


def set_today(session: Session, on: date) -> None:
    set_meta(session, TODAY_KEY, on.isoformat())
    session.flush()


def today(session: Session) -> date:
    """The world's date: the stored clock, else the earliest unfinished election's date, else the
    latest finished election's date, else the founding election day."""
    stored = stored_today(session)
    if stored is not None:
        return stored
    pending = session.scalar(
        select(Election.election_date)
        .where(Election.status.not_in(list(REPORTED_STATUSES)))
        .order_by(Election.election_date)
        .limit(1)
    )
    if pending is not None:
        return pending
    done = session.scalar(select(Election.election_date).order_by(Election.election_date.desc()).limit(1))
    if done is not None:
        return done
    cal = ElectionCalendar.from_config()
    return cal.election_date(cal.config.founding_year)


# --------------------------------------------------------------------------- election days
@dataclass
class ElectionDay:
    """One date with an election: stored (``election_id``) or planned."""

    date: date
    kind: str  # local | general | midterm
    name: str
    election_id: int | None = None
    status: str | None = None  # None while planned
    races: int | None = None
    provinces: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.status in REPORTED_STATUSES

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        d["planned"] = self.election_id is None
        d["finished"] = self.finished
        return d


def _stored_days(session: Session, start: date, end: date) -> dict[date, ElectionDay]:
    from sqlalchemy import func

    rows = list(
        session.scalars(
            select(Election)
            .where(Election.election_date > start, Election.election_date <= end)
            .order_by(Election.election_date, Election.id)
        )
    )
    counts: dict[int, dict[str, int]] = {}
    if rows:
        for eid, rt, n in session.execute(
            select(Race.election_id, Race.race_type, func.count())
            .where(Race.election_id.in_([e.id for e in rows]))
            .group_by(Race.election_id, Race.race_type)
        ).all():
            counts.setdefault(int(eid), {})[str(rt)] = int(n)
    out: dict[date, ElectionDay] = {}
    for e in rows:
        c = counts.get(int(e.id), {})
        out.setdefault(
            e.election_date,
            ElectionDay(
                date=e.election_date,
                kind=e.election_type,
                name=e.name,
                election_id=int(e.id),
                status=e.status,
                races=sum(c.values()),
                provinces=[p for p in (e.provinces or "").split(",") if p],
                counts=c,
            ),
        )
    return out


def agenda(session: Session, start: date, end: date) -> list[ElectionDay]:
    """Every election day with ``start < date ≤ end``: stored elections, planned local elections
    and regular election days (oldest first)."""
    from app.services import local as local_service

    days = _stored_days(session, start, end)
    cal = ElectionCalendar.from_config()
    for year in range(start.year, end.year + 1):
        cycle = cal.cycle(year)
        on = cal.election_date(year)
        if cycle.has_elections and cycle.election_type is not None and start < on <= end and on not in days:
            kind = cycle.election_type.value
            days[on] = ElectionDay(on, kind, f"{kind.title()} Election {year}")
    for plan in local_service.plan_local_elections(session, start, end):
        if plan.date not in days:
            days[plan.date] = ElectionDay(
                plan.date,
                ElectionType.LOCAL.value,
                plan.name,
                races=plan.races,
                provinces=plan.provinces,
                counts=plan.counts,
            )
    return [days[d] for d in sorted(days)]


def next_election_day(session: Session, after: date) -> ElectionDay | None:
    """The first election day after a date: a stored election, the next regular election day or
    the next local election date, whichever comes first."""
    from app.services import local as local_service

    found: list[date] = []
    stored = session.scalar(
        select(Election.election_date)
        .where(Election.election_date > after)
        .order_by(Election.election_date)
        .limit(1)
    )
    if stored is not None:
        found.append(stored)
    cal = ElectionCalendar.from_config()
    for year in range(after.year, after.year + 3):
        on = cal.election_date(year)
        if on > after and cal.cycle(year).has_elections:
            found.append(on)
            break
    local = local_service.next_local_date(session, after, horizon_days=HORIZON_DAYS)
    if local is not None:
        found.append(local)
    if not found:
        return None
    on = min(found)
    days = agenda(session, on - timedelta(days=1), on)
    return days[0] if days else None


def elections_on(session: Session, on: date) -> list[Election]:
    return list(session.scalars(select(Election).where(Election.election_date == on).order_by(Election.id)))


def unfinished_through(session: Session, on: date) -> list[Election]:
    """Elections held on or before a date that are not finished (oldest first)."""
    return list(
        session.scalars(
            select(Election)
            .where(Election.election_date <= on, Election.status.not_in(list(REPORTED_STATUSES)))
            .order_by(Election.election_date, Election.id)
        )
    )


def ensure_election_on(session: Session, on: date) -> Election:
    """The election of an election day: the stored one, else created (SCHEDULED) — the local
    election of every province voting that day, or the regular election."""
    from app.services import continuation
    from app.services import local as local_service

    found = elections_on(session, on)
    if found:
        return found[0]
    cal = ElectionCalendar.from_config()
    if on == cal.election_date(on.year) and cal.cycle(on.year).has_elections:
        return continuation.ensure_regular_election(session, on.year)
    plan = local_service.plan_for(session, on)
    if not plan.contests:
        raise ElectionError(f"nothing is on the ballot on {on.isoformat()}")
    return local_service.create_local_election(session, on, plan=plan)


# --------------------------------------------------------------------------- moving the clock
def require_today_finished(session: Session) -> None:
    pending = unfinished_through(session, today(session))
    if pending:
        e = pending[0]
        raise ElectionError(
            f"{e.name} ({e.election_date.isoformat()}) is not finished yet: watch its election night "
            "or count it instantly first"
        )


def advance(session: Session) -> dict[str, Any]:
    """Move the clock to the next election day (today's elections must be finished) and create
    that day's election.  Returns ``{previous, today, election, news}``."""
    t = today(session)
    require_today_finished(session)
    nxt = next_election_day(session, t)
    if nxt is None:
        raise ElectionError("no election day ahead")
    el = ensure_election_on(session, nxt.date)
    set_today(session, nxt.date)
    log.info("world clock: %s → %s (%s)", t.isoformat(), nxt.date.isoformat(), el.name)
    return {
        "previous": t.isoformat(),
        "today": nxt.date.isoformat(),
        "election_id": int(el.id),
        "news": news(session, t, nxt.date),
    }


def prepare_today(session: Session) -> Election:
    """Today's election, simulated (hidden result and night timeline) so its night can start."""
    from app.services.elections import simulate_election

    t = today(session)
    found = elections_on(session, t)
    if not found:
        raise ElectionError(f"there is no election today ({t.isoformat()})")
    el = found[0]
    if el.status in REPORTED_STATUSES:
        raise ElectionError(f"{el.name} is already finished")
    if el.status == ElectionStatus.SCHEDULED.value:
        simulate_election(session, el.id)
    return el


def count_election(session: Session, election_id: int, *, manager: Any = None) -> None:
    """Simulate (when needed) and finish one election through an instant election night."""
    from app.services.elections import simulate_election
    from app.services.night import run_instant_night

    lock = manager.election_lock(election_id) if manager is not None else None
    if lock is not None:
        lock.acquire()
    try:
        el = session.get(Election, election_id)
        if el is None or el.status in REPORTED_STATUSES:
            return
        if el.status == ElectionStatus.SCHEDULED.value:
            simulate_election(session, election_id)
        run_instant_night(session, election_id)
        if manager is not None:
            manager.forget(election_id)
    finally:
        if lock is not None:
            lock.release()


def count_today(
    session: Session,
    *,
    manager: Any = None,
    commit: Callable[[], None] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> list[int]:
    """Finish every unfinished election up to today instantly (oldest first).  Returns the ids."""
    pending = unfinished_through(session, today(session))
    done: list[int] = []
    for i, el in enumerate(pending):
        if progress is not None:
            progress(el.name, i, len(pending))
        count_election(session, el.id, manager=manager)
        if commit is not None:
            commit()
        done.append(int(el.id))
    return done


def skip_to(
    session: Session,
    target: date,
    *,
    manager: Any = None,
    commit: Callable[[], None] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Count every election day before ``target`` instantly and move the clock there.  When
    ``target`` is itself an election day, its election is created and waits for you."""
    start = today(session)
    if target <= start:
        raise ElectionError(f"the clock only moves forward (today is {start.isoformat()})")
    planned = [d for d in agenda(session, start, target - timedelta(days=1))]
    total = len(planned) + len(unfinished_through(session, start))
    counted: list[int] = []

    def step(name: str, _i: int, _n: int) -> None:
        if progress is not None:
            progress(name, len(counted), total)

    counted += count_today(session, manager=manager, commit=commit, progress=step)
    for day in planned:
        el = ensure_election_on(session, day.date)
        set_today(session, day.date)
        if commit is not None:
            commit()
        step(el.name, 0, 0)
        count_election(session, el.id, manager=manager)
        if commit is not None:
            commit()
        counted.append(int(el.id))
    election_id = None
    nxt = agenda(session, target - timedelta(days=1), target)
    if nxt:
        election_id = int(ensure_election_on(session, target).id)
    set_today(session, target)
    if commit is not None:
        commit()
    return {
        "previous": start.isoformat(),
        "today": target.isoformat(),
        "counted": counted,
        "election_id": election_id,
        "news": news(session, start, target),
    }


# --------------------------------------------------------------------------- news
def _event_items(session: Session, start: date, end: date) -> list[dict[str, Any]]:
    from app.services import local as local_service
    from app.services.runtime import get_frame

    frame = get_frame(session)
    cal = local_service.get_local_calendar(session, frame)
    out = []
    for ev in local_service.resolve_office_events(session, cal, end + timedelta(days=400)):
        if not start < ev.event_date <= end:
            continue
        m = frame.muni_index(ev.municipality_code)
        place = frame.muni_names[m]
        when = cal.format_date(ev.election_date)
        who = ""
        if ev.details.get("candidate_id"):
            c = session.get(Candidate, ev.details["candidate_id"])
            if c is not None:
                party = ev.details.get("party")
                who = f"{c.full_name}{f' ({party})' if party else ''}"
        reason = {
            "resigned": "resigned",
            "died": "died in office",
            "appointed_elsewhere": "was appointed elsewhere",
            "removed": "was removed from office",
        }.get(ev.reason, ev.reason)
        if ev.kind == EVENT_MAYOR_VACANCY:
            text = f"The Mayor of {place}{f', {who},' if who else ''} {reason}. Special election on {when}."
            kind = "vacancy"
        elif ev.kind == EVENT_MAYOR_RECALL:
            why = " after a scandal" if ev.reason == "scandal" else ""
            text = f"A recall petition against the Mayor of {place}{f', {who},' if who else ''} qualified{why}. Recall election on {when}."
            kind = "recall"
        elif ev.kind == EVENT_COUNCIL_VACANCY:
            party = ev.details.get("vacated_party")
            text = f"A{f' {party}' if party else ''} seat on the {place} Municipal Council fell vacant ({reason}). Special election on {when}."
            kind = "vacancy"
        else:  # pragma: no cover - future event kinds
            continue
        out.append(
            {
                "date": ev.event_date.isoformat(),
                "kind": kind,
                "text": text,
                "municipality_code": ev.municipality_code,
                "election_date": ev.election_date.isoformat(),
            }
        )
    return out


def _result_items(session: Session, start: date, end: date) -> list[dict[str, Any]]:
    from app.services.elections import _final_results

    out = []
    for el in session.scalars(
        select(Election)
        .where(
            Election.election_date > start,
            Election.election_date <= end,
            Election.status.in_(list(REPORTED_STATUSES)),
        )
        .order_by(Election.election_date)
    ):
        if el.election_type == ElectionType.LOCAL.value:
            races = session.scalars(select(Race).where(Race.election_id == el.id)).all()
            measures = [r for r in races if r.race_type == RaceType.BALLOT_MEASURE.value]
            passed = sum(1 for r in measures if (json.loads(r.details_json or "{}")).get("passed"))
            recalls = [r for r in races if r.race_type == RaceType.RECALL.value]
            recalled = sum(1 for r in recalls if (json.loads(r.details_json or "{}")).get("passed"))
            parts = [f"{len(races)} races decided"]
            if measures:
                parts.append(f"{passed} of {len(measures)} ballot measures passed")
            if recalls:
                parts.append(f"{recalled} of {len(recalls)} recalls succeeded")
            text = f"{el.name}: " + ", ".join(parts) + "."
        else:
            res = _final_results(session, el)
            parts = []
            pres = res.get("president") or {}
            if pres.get("winner_name"):
                party = pres.get("winner_party")
                parts.append(f"{pres['winner_name']}{f' ({party})' if party else ''} elected President")
            for chamber in ("house", "senate"):
                ctl = (res.get(chamber) or {}).get("controlling_party")
                if ctl:
                    parts.append(f"the {chamber.title()} is controlled by the {ctl}")
            text = f"{el.name}: " + ("; ".join(parts) if parts else "final") + "."
        out.append(
            {"date": el.election_date.isoformat(), "kind": "result", "text": text, "election_id": int(el.id)}
        )
    return out


def _people_items(session: Session, start: date, end: date) -> list[dict[str, Any]]:
    from app.core.errors import ScenarioError
    from app.services.runtime import get_frame
    from app.simulation.people import load_people

    try:
        config = load_people(get_frame(session))
    except ScenarioError:
        return []
    keys = [p.key for p in config.people]
    if not keys:
        return []
    out = []
    q = (
        select(BallotCandidate, Race, Election, Candidate)
        .join(Race, Race.id == BallotCandidate.race_id)
        .join(Election, Election.id == Race.election_id)
        .join(Candidate, Candidate.id == BallotCandidate.candidate_id)
        .where(
            Candidate.key.in_(keys),
            Election.election_date > start,
            Election.election_date <= end,
            Election.status.in_(list(REPORTED_STATUSES)),
        )
        .order_by(Election.election_date)
    )
    for bc, race, el, cand in session.execute(q).all():
        d = json.loads(race.details_json) if race.details_json else {}
        if d.get("winners") is not None:
            won = bc.line_key in d["winners"]
        else:
            won = race.winner_ballot_candidate_id == bc.id and not d.get("moot")
        party = f" ({bc.party_code_snapshot})" if bc.party_code_snapshot else ""
        verb = "won" if won else "lost"
        out.append(
            {
                "date": el.election_date.isoformat(),
                "kind": "person",
                "text": f"{cand.full_name}{party} {verb}: {race.name}.",
                "candidate_id": int(cand.id),
                "election_id": int(el.id),
                "won": won,
            }
        )
    return out


def news(session: Session, start: date, end: date) -> list[dict[str, Any]]:
    """What happened with ``start < date ≤ end`` (oldest first): office events, results and your
    own people's races."""
    items = (
        _event_items(session, start, end)
        + _result_items(session, start, end)
        + _people_items(session, start, end)
    )
    order = {"result": 0, "person": 1, "vacancy": 2, "recall": 2}
    items.sort(key=lambda x: (x["date"], order.get(x["kind"], 3)))
    return items


__all__ = [
    "ElectionDay",
    "advance",
    "agenda",
    "count_today",
    "elections_on",
    "ensure_election_on",
    "news",
    "next_election_day",
    "prepare_today",
    "set_today",
    "skip_to",
    "stored_today",
    "today",
]
