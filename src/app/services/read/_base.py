"""Infrastructure shared by the read models: election resolution, caching, identities, lines.

* :func:`resolve_election` turns an ``{election_id}`` path value (a number, ``latest`` or
  ``demo``) into an :class:`ElectionRef` that also knows where results may come from
  (``results_source``: ``final`` | ``live`` | ``hidden``, the hidden-until-reported rule).
* :func:`cached` memoises immutable payloads per database, keyed by election id **and status**
  (plus its finalisation time), so a status change never serves a stale payload;
  :func:`clear_read_cache` empties it (called after every write action).
* :func:`ballot_lines` returns the ballot lines of races with their FICTIONAL candidates, running
  mates and the party identity printed on the ballot; :func:`stored_totals` /
  :func:`stored_turnout` read stored results — and refuse to do so for unreported elections.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.core.constitution import DataCategory, ElectionStatus
from app.core.errors import ElectionError, NotFoundError
from app.core.logging import get_logger
from app.models import (
    AppMeta,
    BallotCandidate,
    Candidate,
    Election,
    ElectionResult,
    HouseDistrict,
    Municipality,
    Party,
    Province,
    TurnoutResult,
)
from app.services._common import INDEPENDENT_COLOR, REPORTED_STATUSES, active_vintage

log = get_logger(__name__)

T = TypeVar("T")

#: ``results_source`` values.
SOURCE_FINAL = "final"
SOURCE_LIVE = "live"
SOURCE_HIDDEN = "hidden"

#: Path aliases accepted wherever ``{election_id}`` appears.
ELECTION_ALIASES: tuple[str, ...] = ("latest", "demo")
#: ``app_meta`` key naming the demo election (set by the CLI ``demo`` command); optional.
DEMO_ELECTION_KEY = "demo_election_id"

#: Message shown in place of results that are not revealed yet.
HIDDEN_NOTICE = (
    "Results of this election are not revealed yet: they appear live during the election night "
    "or once the election is finalized."
)

SIMULATED = DataCategory.SIMULATED.value
FICTIONAL = DataCategory.FICTIONAL.value
REAL = DataCategory.REAL.value
DERIVED = DataCategory.DERIVED.value

_CHUNK = 400


# =========================================================================== numbers
def rnd(x: Any, nd: int = 4) -> float | None:
    """``x`` rounded to ``nd`` decimals; ``None`` for missing / non-finite values."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, nd)


def pct(part: float | int | None, total: float | int | None, nd: int = 3) -> float | None:
    """``100 · part / total`` rounded (``None`` when undefined)."""
    if part is None or total is None:
        return None
    t = float(total)
    if t <= 0:
        return None
    return round(100.0 * float(part) / t, nd)


def iso(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def chunks(ids: Sequence[int], size: int = _CHUNK) -> Iterable[list[int]]:
    """Chunks of ``ids`` for ``IN (…)`` queries."""
    for i in range(0, len(ids), size):
        yield list(ids[i : i + size])


# =========================================================================== cache
class _LRU:
    """Tiny thread-safe LRU mapping (values are built outside the lock)."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._data: OrderedDict[tuple, Any] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: tuple) -> tuple[bool, Any]:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return True, self._data[key]
        return False, None

    def put(self, key: tuple, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


_cache = _LRU(512)
#: Separate, smaller store for large objects (results frames).
_big_cache = _LRU(32)


def db_token(session: Session) -> tuple[int, str]:
    """Identity of the database behind ``session`` (engine object + URL)."""
    bind = session.get_bind()
    return id(bind), str(getattr(bind, "url", ""))


def cached(session: Session, name: str, key: tuple, build: Callable[[], T], *, big: bool = False) -> T:
    """Memoise ``build()`` under ``(database, name, *key)``.

    Callers put everything the payload depends on into ``key`` — for election payloads the
    election id, its status and finalisation time (see :meth:`ElectionRef.cache_key`).  Cached
    payloads are shared between requests and must never be mutated.  ``big`` keeps the value in a
    smaller store (results frames).
    """
    store = _big_cache if big else _cache
    k = (db_token(session), name, *key)
    hit, value = store.get(k)
    if hit:
        return value
    value = build()
    store.put(k, value)
    return value


def clear_read_cache() -> None:
    """Forget every cached read payload (after writes, and in tests)."""
    _cache.clear()
    _big_cache.clear()


# =========================================================================== elections
@dataclass(frozen=True)
class ElectionRef:
    """An election as seen by the read layer, with the source its results may come from."""

    id: int
    year: int
    name: str
    status: str
    election_type: str
    election_date: date
    seed: int
    previous_election_id: int | None
    scenario_id: int | None
    vintage_id: int
    apportionment_id: int | None
    district_plan_id: int | None
    simulated_at: datetime | None
    finalized_at: datetime | None
    results_source: str

    @property
    def reported(self) -> bool:
        return self.results_source == SOURCE_FINAL

    @property
    def live(self) -> bool:
        return self.results_source == SOURCE_LIVE

    @property
    def hidden(self) -> bool:
        return self.results_source == SOURCE_HIDDEN

    def cache_key(self) -> tuple:
        """Cache key component: changes whenever the election's status or result changes."""
        return (self.id, self.status, iso(self.finalized_at), iso(self.simulated_at), self.results_source)

    def brief(self) -> dict[str, Any]:
        """The ``election`` block embedded in every election-scoped payload."""
        return {
            "id": self.id,
            "year": self.year,
            "name": self.name,
            "election_type": self.election_type,
            "status": self.status,
            "election_date": self.election_date.isoformat(),
            "previous_election_id": self.previous_election_id,
            "reported": self.reported,
            "live": self.live,
            "results_source": self.results_source,
        }

    def envelope(self, **payload: Any) -> dict[str, Any]:
        """Standard election payload: ``election``, ``data_category``, ``results_source`` (+ a
        ``notice`` when results are hidden) followed by ``payload``."""
        out: dict[str, Any] = {
            "election": self.brief(),
            "data_category": SIMULATED,
            "results_source": self.results_source,
        }
        if self.hidden:
            out["notice"] = HIDDEN_NOTICE
        out.update(payload)
        return out


def _source(el: Election) -> str:
    """``final`` for reported elections, ``live`` during an election night (status LIVE, set by
    the night service), otherwise ``hidden``."""
    if el.status in REPORTED_STATUSES:
        return SOURCE_FINAL
    if el.status == ElectionStatus.LIVE.value:
        return SOURCE_LIVE
    return SOURCE_HIDDEN


def ref_of(el: Election) -> ElectionRef:
    """:class:`ElectionRef` of an ``election`` row."""
    return ElectionRef(
        id=int(el.id),
        year=int(el.year),
        name=el.name,
        status=el.status,
        election_type=el.election_type,
        election_date=el.election_date,
        seed=int(el.seed),
        previous_election_id=el.previous_election_id,
        scenario_id=el.scenario_id,
        vintage_id=int(el.vintage_id),
        apportionment_id=el.apportionment_id,
        district_plan_id=el.district_plan_id,
        simulated_at=el.simulated_at,
        finalized_at=el.finalized_at,
        results_source=_source(el),
    )


def _latest_id(session: Session) -> int | None:
    return session.scalar(
        select(Election.id).order_by(Election.election_date.desc(), Election.id.desc()).limit(1)
    )


def demo_election_id(session: Session) -> int | None:
    """The demo election: ``app_meta.demo_election_id`` when set and valid, else the most recent
    election that is not reported yet (ready for an election night), else the latest one."""
    row = session.get(AppMeta, DEMO_ELECTION_KEY)
    if row is not None:
        try:
            eid = int(row.value)
        except ValueError:
            eid = None
        if eid is not None and session.get(Election, eid) is not None:
            return eid
    pending = session.scalar(
        select(Election.id)
        .where(Election.status.not_in(list(REPORTED_STATUSES)))
        .order_by(Election.election_date.desc(), Election.id.desc())
        .limit(1)
    )
    return int(pending) if pending is not None else _latest_id(session)


def latest_election_id(session: Session) -> int | None:
    """The most recent election (by date, then id), whatever its status."""
    eid = _latest_id(session)
    return int(eid) if eid is not None else None


def resolve_election_id(session: Session, ref: str | int) -> int:
    """Election id of a path value: a positive integer, ``latest`` or ``demo``."""
    text = str(ref).strip().lower()
    if text == "latest":
        eid = latest_election_id(session)
    elif text == "demo":
        eid = demo_election_id(session)
    else:
        try:
            eid = int(text)
        except ValueError:
            raise NotFoundError(
                f"election {ref!r} not found (use a numeric id, 'latest' or 'demo')"
            ) from None
    if eid is None:
        raise NotFoundError("no elections exist yet")
    return int(eid)


def resolve_election(session: Session, ref: str | int) -> ElectionRef:
    """:class:`ElectionRef` for a path value (``NotFoundError`` when unknown)."""
    eid = resolve_election_id(session, ref)
    el = session.get(Election, eid)
    if el is None:
        raise NotFoundError(f"election {eid} not found")
    return ref_of(el)


def all_election_refs(session: Session) -> list[ElectionRef]:
    """Every election, chronologically."""
    return [
        ref_of(e) for e in session.scalars(select(Election).order_by(Election.election_date, Election.id))
    ]


def reported_refs(session: Session) -> list[ElectionRef]:
    """Reported (FINAL / CERTIFIED) elections, chronologically."""
    return [r for r in all_election_refs(session) if r.reported]


def require_reported(ref: ElectionRef) -> None:
    """Guard of every stored-result read: raises :class:`ElectionError` for unreported elections."""
    if not ref.reported:
        raise ElectionError(f"election {ref.id} is {ref.status}: its results are not reported yet")


# =========================================================================== identities
def party_index(session: Session) -> dict[str, dict[str, Any]]:
    """Current party identities by code (FICTIONAL)."""
    return {
        p.code: {
            "code": p.code,
            "name": p.name,
            "abbreviation": p.abbreviation,
            "color": p.color,
            "color_secondary": p.color_secondary,
            "family": p.family,
            "is_active": bool(p.is_active),
        }
        for p in session.scalars(select(Party).order_by(Party.id))
    }


def party_codes_by_id(session: Session) -> dict[int, str]:
    return dict(session.execute(select(Party.id, Party.code)).tuples().all())


def province_maps(session: Session) -> tuple[dict[int, str], dict[str, dict[str, Any]]]:
    """``(id → code, code → {code, name, name_en, cbs_code, sort_order})``."""
    rows = session.scalars(select(Province).order_by(Province.sort_order, Province.code)).all()
    return (
        {p.id: p.code for p in rows},
        {
            p.code: {
                "id": p.id,
                "code": p.code,
                "name": p.name,
                "name_en": p.name_en,
                "cbs_code": p.cbs_code,
                "capital": p.capital,
                "sort_order": p.sort_order,
            }
            for p in rows
        },
    )


def municipality_maps(session: Session, vintage_id: int | None = None) -> dict[int, dict[str, Any]]:
    """``municipality.id → {code, name, province_id}`` of a vintage (default: the active one)."""
    if vintage_id is None:
        v = active_vintage(session)
        if v is None:
            return {}
        vintage_id = v.id
    return {
        int(i): {"code": c, "name": n, "province_id": int(p)}
        for i, c, n, p in session.execute(
            select(Municipality.id, Municipality.cbs_code, Municipality.name, Municipality.province_id).where(
                Municipality.vintage_id == vintage_id
            )
        ).all()
    }


def district_maps(session: Session, plan_id: int | None) -> dict[int, dict[str, Any]]:
    """``house_district.id → {code, name, province_id, number}`` of a plan."""
    if plan_id is None:
        return {}
    return {
        int(i): {"code": c, "name": n, "province_id": int(p), "number": int(k)}
        for i, c, n, p, k in session.execute(
            select(
                HouseDistrict.id,
                HouseDistrict.code,
                HouseDistrict.name,
                HouseDistrict.province_id,
                HouseDistrict.number,
            ).where(HouseDistrict.plan_id == plan_id)
        ).all()
    }


def person(c: Candidate | None) -> dict[str, Any] | None:
    """Public identity of a FICTIONAL person."""
    if c is None:
        return None
    return {
        "id": c.id,
        "key": c.key,
        "name": c.full_name,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "gender": c.gender,
        "portrait_key": c.portrait_key,
        "home_municipality": c.home_municipality_code,
    }


# =========================================================================== ballot lines
def ballot_lines(session: Session, race_ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
    """Ballot lines per race id (ballot order) with candidate, running mate and the party
    identity printed on the ballot (all FICTIONAL)."""
    ids = [int(i) for i in race_ids]
    if not ids:
        return {}
    cand = aliased(Candidate)
    mate = aliased(Candidate)
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    for part in chunks(ids):
        rows = session.execute(
            select(BallotCandidate, cand, mate)
            .outerjoin(cand, cand.id == BallotCandidate.candidate_id)
            .outerjoin(mate, mate.id == BallotCandidate.running_mate_id)
            .where(BallotCandidate.race_id.in_(part))
            .order_by(BallotCandidate.race_id, BallotCandidate.ballot_order)
        ).all()
        for b, c, m in rows:
            key = b.line_key or (c.key if c is not None else b.party_code_snapshot) or str(b.id)
            out[int(b.race_id)].append(
                {
                    "key": key,
                    "ballot_candidate_id": b.id,
                    "order": b.ballot_order,
                    "name": b.ballot_name,
                    "candidate": person(c),
                    "running_mate": person(m),
                    "party": b.party_code_snapshot,
                    "party_name": b.party_name_snapshot,
                    "party_abbr": b.party_abbr_snapshot,
                    "color": b.party_color_snapshot or INDEPENDENT_COLOR,
                    "incumbent": bool(b.is_incumbent),
                    "withdrawn": bool(b.withdrawn),
                    "write_in": bool(b.is_write_in),
                }
            )
    return out


# =========================================================================== stored results
def stored_totals(
    session: Session, ref: ElectionRef, race_ids: Sequence[int], level: str = "national"
) -> dict[int, dict[int, int]]:
    """Stored votes per race id → ballot_candidate id at ``level`` (the national row of any race
    is its total).  Only for reported elections (hidden-until-reported rule)."""
    require_reported(ref)
    out: dict[int, dict[int, int]] = {int(i): {} for i in race_ids}
    for part in chunks([int(i) for i in race_ids]):
        for rid, bid, v in session.execute(
            select(ElectionResult.race_id, ElectionResult.ballot_candidate_id, ElectionResult.votes).where(
                ElectionResult.race_id.in_(part), ElectionResult.level == level
            )
        ).all():
            out[int(rid)][int(bid)] = out[int(rid)].get(int(bid), 0) + int(v)
    return out


def stored_turnout(
    session: Session, ref: ElectionRef, race_ids: Sequence[int], level: str = "national"
) -> dict[int, dict[str, Any]]:
    """Stored turnout per race id at ``level`` (national = the race's total).  Reported only."""
    require_reported(ref)
    out: dict[int, dict[str, Any]] = {}
    for part in chunks([int(i) for i in race_ids]):
        for rid, el, cast, valid, blank, invalid in session.execute(
            select(
                TurnoutResult.race_id,
                TurnoutResult.eligible_voters,
                TurnoutResult.ballots_cast,
                TurnoutResult.valid_votes,
                TurnoutResult.blank_votes,
                TurnoutResult.invalid_votes,
            ).where(TurnoutResult.race_id.in_(part), TurnoutResult.level == level)
        ).all():
            d = out.setdefault(
                int(rid),
                {"eligible": 0, "ballots_cast": 0, "valid_votes": 0, "blank_votes": 0, "invalid_votes": 0},
            )
            d["eligible"] += int(el)
            d["ballots_cast"] += int(cast)
            d["valid_votes"] += int(valid)
            d["blank_votes"] += int(blank or 0)
            d["invalid_votes"] += int(invalid or 0)
    for d in out.values():
        d["turnout_pct"] = pct(d["ballots_cast"], d["eligible"])
    return out


def with_votes(
    lines: Sequence[Mapping[str, Any]],
    votes: Mapping[str, int] | None,
    *,
    winner: str | None = None,
) -> list[dict[str, Any]]:
    """Copies of ``lines`` with ``votes``, ``pct`` (of the lines' total) and ``winner`` flags
    (``votes=None``: results hidden → ``None`` values)."""
    total = sum(int(v) for v in votes.values()) if votes else 0
    out = []
    for ln in lines:
        d = dict(ln)
        if votes is None:
            d["votes"] = None
            d["pct"] = None
        else:
            v = int(votes.get(ln["key"], 0))
            d["votes"] = v
            d["pct"] = pct(v, total) if total else 0.0
        d["winner"] = winner is not None and ln["key"] == winner
        out.append(d)
    return out


def leader_of(votes: Mapping[str, int] | None) -> tuple[str | None, str | None, float | None, int | None]:
    """``(leader, runner_up, margin_pp, margin_votes)`` of a vote mapping (ties → first key)."""
    if not votes:
        return None, None, None, None
    items = sorted(votes.items(), key=lambda kv: (-int(kv[1]), kv[0]))
    total = sum(int(v) for _, v in items)
    if total <= 0:
        return None, None, None, None
    lead, lv = items[0]
    run, rv = (items[1][0], int(items[1][1])) if len(items) > 1 else (None, 0)
    mv = int(lv) - rv
    return lead, run, round(100.0 * mv / total, 4), mv


# =========================================================================== office holders
def current_holders(session: Session, *, prefix: str | None = None) -> dict[str, dict[str, Any]]:
    """The serving (most recent, not ended) holder of every office, by office code; ``prefix``
    restricts the offices (e.g. ``"HOUSE-"``).  FICTIONAL people; SIMULATED outcomes."""
    from app.models import Office, OfficeHolder

    q = (
        select(Office.code, OfficeHolder, Candidate, Party.code)
        .join(OfficeHolder, OfficeHolder.office_id == Office.id)
        .join(Candidate, Candidate.id == OfficeHolder.candidate_id)
        .outerjoin(Party, Party.id == OfficeHolder.party_id)
        .where(OfficeHolder.ended_on.is_(None))
        .order_by(Office.code, OfficeHolder.term_start)
    )
    if prefix:
        q = q.where(Office.code.like(f"{prefix}%"))
    out: dict[str, dict[str, Any]] = {}
    for code, oh, c, pcode in session.execute(q).all():
        out[code] = {
            "office": code,
            "candidate_id": c.id,
            "name": c.full_name,
            "portrait_key": c.portrait_key,
            "party": pcode,
            "term_start": iso(oh.term_start),
            "term_end": iso(oh.term_end),
            "start_reason": oh.start_reason,
        }
    return out
