"""In-between local elections (FICTIONAL): planning, creation and chronological finishing.

A local election is one province on one of its local election days
(:class:`app.elections.local_calendar.LocalCalendar`).  Its ballot holds the school boards and
water boards that are due, the generated ballot measures, and the special elections and mayor
recalls that the drawn office events lead to.  This module turns the calendar into stored
elections (``ElectionType.LOCAL``) that the ordinary pipeline then simulates, plays on election
night and certifies (docs/LOCAL_ELECTIONS.md):

* :func:`plan_local_elections` — what the calendar puts on each local day in a date range,
  resolving office events against the office holders in the database;
* :func:`create_local_election` — store one local election (races, ballot lines, candidates);
* :func:`ensure_local_elections` — create every planned local election up to a date;
* :func:`finish_earlier` — elections are certified in strict date order: create, simulate and
  finish every unreported election held before a date, oldest first.

Everything is deterministic: dates, ballots and candidates are keyed by the calendar seed, the
province and the date, never by creation order.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.constitution import (
    NO_LINE,
    YES_LINE,
    ElectionStatus,
    ElectionType,
    ElectoralSystem,
    OfficeType,
    RaceType,
)
from app.core.errors import ElectionError
from app.core.logging import Timer, get_logger, log_ctx
from app.core.rng import derive_seed, make_rng
from app.elections.calendar import ElectionCalendar
from app.elections.local_calendar import (
    EVENT_COUNCIL_VACANCY,
    EVENT_MAYOR_RECALL,
    EVENT_MAYOR_VACANCY,
    BoardCycle,
    LocalCalendar,
    LocalDay,
    MeasurePlan,
    OfficeEvent,
    load_local_config,
)
from app.elections.types import BallotLine, RaceSpec
from app.geography.frame import GeographyFrame
from app.models import (
    Candidate,
    Election,
    LegislatureSeatResult,
    Office,
    Party,
    Province,
    Race,
    Scenario,
)
from app.scenarios.schema import CandidateSpec, Ideology, ScenarioDocument
from app.services import _create as create_ops
from app.services._common import (
    REPORTED_STATUSES,
    RunRecorder,
    active_vintage,
    dumps,
    mayor_office_code,
)
from app.services.runtime import (
    RUN_SETUP,
    get_frame,
    get_model,
    parse_stored_scenario,
    scenario_hash,
)
from app.simulation.candidates import RaceSlot, _make_candidate, generate_down_ballot
from app.simulation.races import races_from_candidates
from app.simulation.structural import IDEOLOGY_DIMS, StructuralModel

log = get_logger(__name__)

#: Election types that are not local (the "big" federal / regular elections).
REGULAR_TYPES: frozenset[str] = frozenset(t.value for t in ElectionType if t != ElectionType.LOCAL)


# ============================================================================ codes
def school_board_race_code(gm: str) -> str:
    return f"SB-{gm}"


def school_board_office_code(gm: str, seat: int) -> str:
    return f"SB-{gm}-{seat}"


def water_board_race_code(ws: str) -> str:
    return f"WB-{ws}"


def water_board_office_code(ws: str, seat: int) -> str:
    return f"WB-{ws}-{seat}"


def council_office_code(gm: str) -> str:
    return f"COUNCIL-{gm}"


def measure_race_code(gm: str, suffix: str) -> str:
    return f"MEASURE-{gm}-{suffix}"


def recall_race_code(gm: str) -> str:
    return f"RECALL-{gm}"


def council_seat_race_code(gm: str, n: int = 1) -> str:
    return f"COUNCILSEAT-{gm}" if n == 1 else f"COUNCILSEAT-{gm}-{n}"


# ============================================================================ calendar access
_calendars: dict[tuple[str, int], LocalCalendar] = {}


def get_local_calendar(session: Session, frame: GeographyFrame | None = None) -> LocalCalendar:
    """The local calendar of the active geography (cached per frame and configuration)."""
    from app.services.runtime import frame_token

    frame = frame or get_frame(session)
    cfg = load_local_config()
    key = (frame_token(frame) + ":" + ",".join(frame.water_board_codes), id(cfg))
    cal = _calendars.get(key)
    if cal is None or cal.frame is not frame:
        cal = LocalCalendar(frame, cfg)
        _calendars.clear()
        _calendars[key] = cal
    return cal


def local_seed(cal: LocalCalendar, day: LocalDay) -> int:
    """Election seed of a local election (stable per province and date)."""
    return int(derive_seed(cal.seed, "local-election", day.province_code, day.date.isoformat()) % (2**62))


# ============================================================================ plan objects
@dataclass
class PlannedContest:
    """One contest of a planned local election."""

    kind: str  # school_board | water_board | measure | mayor_special | council_seat | recall
    code: str
    name: str
    municipality_code: str | None = None
    water_board_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class LocalPlan:
    """What the calendar puts on a province's local day."""

    day: LocalDay
    name: str
    contests: list[PlannedContest]
    municipalities: list[str]

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.contests:
            out[c.kind] = out.get(c.kind, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "province_code": self.day.province_code,
            "date": self.day.date.isoformat(),
            "slot": self.day.slot,
            "name": self.name,
            "municipalities": self.municipalities,
            "races": sum(1 for c in self.contests if c.kind != "recall")
            + sum(2 for c in self.contests if c.kind == "recall"),
            "counts": self.counts,
            "contests": [
                {"kind": c.kind, "code": c.code, "name": c.name, "municipality_code": c.municipality_code}
                for c in self.contests
            ],
        }


# ============================================================================ office events
def _regular_municipal_date(calendar: ElectionCalendar, after: date) -> date:
    """The first regular municipal (midterm) election day after a date."""
    for year in range(after.year, after.year + 12):
        if calendar.cycle(year).municipal:
            d = calendar.election_date(year)
            if d > after:
                return d
    raise ElectionError("no regular municipal election found")  # pragma: no cover


def _council_seat_parties(session: Session, gm: str, before: date) -> dict[str, int]:
    """Seats per party of a municipality's council as elected at the latest certified council
    election before a date (empty when none)."""
    row = session.execute(
        select(Race.id)
        .join(Election, Election.id == Race.election_id)
        .where(
            Race.race_type == RaceType.MUNICIPAL_COUNCIL.value,
            Race.code == f"COUNCIL-{gm}",
            Election.election_date < before,
            Election.status.in_(list(REPORTED_STATUSES)),
        )
        .order_by(Election.election_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return {}
    rows = session.execute(
        select(Party.code, LegislatureSeatResult.seats)
        .join(Party, Party.id == LegislatureSeatResult.party_id)
        .where(LegislatureSeatResult.race_id == row[0], LegislatureSeatResult.seats > 0)
    ).all()
    return {str(c): int(s) for c, s in rows}


def resolve_office_events(session: Session, cal: LocalCalendar, until: date) -> list[OfficeEvent]:
    """The drawn office events that reach a ballot by ``until`` (election date): the office must
    be held at the event date (a mayor in office for long enough to be recalled; a council that
    has been elected), the regular midterm must not come soon after, and an office can have only
    one pending special election or recall at a time.  ``details`` carries the target holder /
    the vacated council seat's party."""
    calendar = cal.calendar
    window = cal.config.regular_election_window_days
    min_days_in_office = cal.config.recalls.min_days_in_office
    raw = cal.office_events(until)
    pending: dict[str, date] = {}
    out: list[OfficeEvent] = []
    holder_cache: dict[date, dict[str, create_ops.Holder]] = {}
    for ev in raw:
        if ev.election_date > until:
            continue
        regular = _regular_municipal_date(calendar, ev.event_date)
        if ev.election_date >= regular - timedelta(days=window):
            continue
        office = mayor_office_code(ev.municipality_code)
        key = (
            office
            if ev.kind != EVENT_COUNCIL_VACANCY
            else f"{council_office_code(ev.municipality_code)}#{ev.seq}"
        )
        if ev.kind != EVENT_COUNCIL_VACANCY:
            busy = pending.get(office)
            if busy is not None and ev.event_date <= busy:
                continue
        details: dict[str, Any] = {}
        if ev.kind in (EVENT_MAYOR_VACANCY, EVENT_MAYOR_RECALL):
            # the holder serving the day before the event (a finished special election ends that
            # term on the vacancy date, so re-planning later still finds the same person)
            day_before = ev.event_date - timedelta(days=1)
            holders = holder_cache.get(day_before)
            if holders is None:
                holders = holder_cache[day_before] = create_ops.holders_at(session, day_before)
            h = holders.get(office)
            if h is None:
                continue
            if ev.kind == EVENT_MAYOR_RECALL and (ev.event_date - h.term_start).days < min_days_in_office:
                continue
            details = {
                "holder_id": h.holder_id,
                "candidate_id": h.candidate_id,
                "candidate_key": h.candidate_key,
                "party": h.seat_party,
                "term_end": h.term_end.isoformat() if h.term_end else None,
            }
        else:
            seats = _council_seat_parties(session, ev.municipality_code, ev.event_date)
            if not seats:
                continue
            parties = sorted(seats)
            w = np.array([seats[p] for p in parties], dtype=float)
            rng = make_rng(cal.seed, "council-vacancy-party", ev.municipality_code, ev.event_date.isoformat())
            details = {"vacated_party": parties[int(rng.choice(len(parties), p=w / w.sum()))]}
        pending[key] = ev.election_date
        out.append(
            OfficeEvent(
                ev.kind, ev.municipality_code, ev.event_date, ev.election_date, ev.reason, ev.seq, details
            )
        )
    return out


# ============================================================================ planning
def _plan_day(
    cal: LocalCalendar, day: LocalDay, events: Sequence[OfficeEvent], frame: GeographyFrame
) -> LocalPlan:
    contests: list[PlannedContest] = []
    munis = cal.municipalities_on(day)
    for gm in munis:
        mname = frame.muni_names[frame.muni_index(gm)]
        cyc = cal.school_board_cycle(gm, day.date.year)
        if cyc is not None:
            contests.append(
                PlannedContest(
                    "school_board",
                    school_board_race_code(gm),
                    f"{mname} School Board",
                    municipality_code=gm,
                    details={
                        "cycle": cyc.number,
                        "seats_total": cyc.total_seats,
                        "seats_up": list(cyc.seats_up),
                    },
                )
            )
        for m in cal.measures(gm, day.date):
            contests.append(
                PlannedContest(
                    "measure",
                    measure_race_code(gm, m.suffix),
                    f"{mname} {m.label} – {m.topic.title}",
                    municipality_code=gm,
                    details={"label": m.label, "topic": m.topic.key},
                )
            )
    seat_n: dict[str, int] = {}
    for ev in events:
        gm = ev.municipality_code
        mname = frame.muni_names[frame.muni_index(gm)]
        if ev.kind == EVENT_MAYOR_VACANCY:
            contests.append(
                PlannedContest(
                    "mayor_special",
                    mayor_office_code(gm),
                    f"Mayor of {mname} (special election)",
                    municipality_code=gm,
                    details={"event_date": ev.event_date.isoformat(), "reason": ev.reason, **ev.details},
                )
            )
        elif ev.kind == EVENT_MAYOR_RECALL:
            contests.append(
                PlannedContest(
                    "recall",
                    recall_race_code(gm),
                    f"Recall of the Mayor of {mname}",
                    municipality_code=gm,
                    details={"event_date": ev.event_date.isoformat(), "reason": ev.reason, **ev.details},
                )
            )
        else:
            seat_n[gm] = seat_n.get(gm, 0) + 1
            contests.append(
                PlannedContest(
                    "council_seat",
                    council_seat_race_code(gm, seat_n[gm]),
                    f"{mname} Municipal Council – vacant seat (special election)",
                    municipality_code=gm,
                    details={"event_date": ev.event_date.isoformat(), "reason": ev.reason, **ev.details},
                )
            )
    for ws in cal.water_boards_on(day):
        w = frame.water_board_index(ws)
        cyc = cal.water_board_cycle(ws, day.date.year)
        assert cyc is not None
        contests.append(
            PlannedContest(
                "water_board",
                water_board_race_code(ws),
                f"{frame.water_board_names[w]} – Water Board",
                water_board_code=ws,
                details={"cycle": cyc.number, "seats_total": cyc.total_seats, "seats_up": list(cyc.seats_up)},
            )
        )
    return LocalPlan(day, cal.election_name(day), contests, munis)


def plan_local_elections(
    session: Session, start: date, end: date, *, include_empty: bool = False
) -> list[LocalPlan]:
    """Planned local elections with ``start < date ≤ end`` (oldest first)."""
    frame = get_frame(session)
    cal = get_local_calendar(session, frame)
    events = resolve_office_events(session, cal, end)
    by_day: dict[tuple[str, date], list[OfficeEvent]] = {}
    for ev in events:
        p = frame.province_codes[int(frame.muni_province[frame.muni_index(ev.municipality_code)])]
        by_day.setdefault((p, ev.election_date), []).append(ev)
    out = []
    for day in cal.days(start, end):
        plan = _plan_day(cal, day, by_day.get((day.province_code, day.date), []), frame)
        if plan.contests or include_empty:
            out.append(plan)
    return out


def plan_for(session: Session, province_code: str, on: date) -> LocalPlan:
    """The plan of one province's local day (raises when the province holds none that day)."""
    plans = plan_local_elections(session, on - timedelta(days=1), on, include_empty=True)
    for p in plans:
        if p.day.province_code == province_code:
            return p
    raise ElectionError(f"{province_code} holds no local election on {on.isoformat()}")


# ============================================================================ base scenario
def base_scenario(session: Session, on: date) -> tuple[Scenario, ScenarioDocument]:
    """The scenario of the latest regular election before ``on`` (its parties and environment
    carry over to the local elections that follow); else the latest stored scenario."""
    row = session.execute(
        select(Scenario)
        .join(Election, Election.scenario_id == Scenario.id)
        .where(Election.election_type.in_(list(REGULAR_TYPES)), Election.election_date < on)
        .order_by(Election.election_date.desc(), Election.id.desc())
        .limit(1)
    ).first()
    scen = (
        row[0]
        if row is not None
        else session.scalars(select(Scenario).order_by(Scenario.id.desc()).limit(1)).first()
    )
    if scen is None:
        raise ElectionError("no scenario stored yet: create a regular election first (python -m app demo)")
    return scen, parse_stored_scenario(scen.document)


# ============================================================================ candidates
def _ideology(spec: CandidateSpec, model: StructuralModel) -> tuple[float, ...]:
    if spec.ideology is not None:
        return tuple(float(getattr(spec.ideology, d)) for d in IDEOLOGY_DIMS)
    if spec.party is not None and spec.party in model.party_index:
        return tuple(float(x) for x in model.ideology[model.party_index[spec.party]])
    return tuple(0.0 for _ in IDEOLOGY_DIMS)


def _expected_party_shares(model: StructuralModel, units: np.ndarray) -> np.ndarray:
    """Eligible-weighted expected party vote shares in a jurisdiction (P,)."""
    from app.simulation.voting import expected_party_state

    state = expected_party_state(model, units=units)
    w = model.eligible[units] * state.turnout
    tot = w.sum()
    if tot <= 0:
        return np.full(model.n_parties, 1.0 / model.n_parties)
    return (state.vote_share * w[:, None]).sum(axis=0) / tot


def _board_candidates(
    model: StructuralModel,
    cal: LocalCalendar,
    seed: int,
    race_key: str,
    race_type: RaceType,
    units: np.ndarray,
    cyc: BoardCycle,
    incumbents: Sequence[CandidateSpec],
    office_label: str,
    existing: set[str],
    candidate_cfg: Any,
) -> list[tuple[BallotLine, CandidateSpec]]:
    """Nonpartisan candidates of a board race: running incumbents plus newcomers whose position
    is drawn around a party supported in the jurisdiction (not shown on the ballot)."""
    rng = make_rng(seed, "board-candidates", race_key)
    seats = len(cyc.seats_up)
    lo, hi = candidate_cfg.candidates_per_seat
    n_total = max(seats + 1, round(seats * rng.uniform(lo, hi)))
    shares = _expected_party_shares(model, units)
    out: list[tuple[BallotLine, CandidateSpec]] = []
    for inc in incumbents:
        if rng.random() < candidate_cfg.incumbent_runs_again:
            ideo = _ideology(inc, model)
            out.append(
                (
                    BallotLine(
                        key=inc.key,
                        party_code=None,
                        candidate_key=inc.key,
                        label=f"{inc.first_name} {inc.last_name}",
                        quality=inc.quality,
                        incumbent=True,
                        home_province=inc.home_province,
                        home_municipality=inc.home_municipality,
                        ideology=ideo,
                    ),
                    inc,
                )
            )
    while len(out) < n_total:
        p = int(rng.choice(model.n_parties, p=shares / shares.sum()))
        spec = _make_candidate(
            model,
            rng,
            seed=seed,
            race_key=race_key,
            race_type=race_type,
            party=None,
            units=units,
            quality_sd=0.8,
            quality_mean=0.0,
            office_label=office_label,
            existing=existing,
        )
        ideo = np.clip(model.ideology[p] + rng.normal(0.0, 0.22, size=len(IDEOLOGY_DIMS)), -1.0, 1.0)
        ideo_t = tuple(round(float(x), 3) for x in ideo)
        spec = spec.model_copy(
            update={
                "bio": spec.bio.replace("independent candidate", "nonpartisan candidate")
                if spec.bio
                else spec.bio,
                "ideology": Ideology(economic=ideo_t[0], social=ideo_t[1], europe=ideo_t[2]),
            }
        )
        out.append(
            (
                BallotLine(
                    key=spec.key,
                    party_code=None,
                    candidate_key=spec.key,
                    label=f"{spec.first_name} {spec.last_name}",
                    quality=spec.quality,
                    incumbent=False,
                    home_province=spec.home_province,
                    home_municipality=spec.home_municipality,
                    ideology=ideo_t,
                ),
                spec,
            )
        )
    order = rng.permutation(len(out))
    return [out[i] for i in order.tolist()]


def _measure_lines(m: MeasurePlan, base_appeal: float = 0.0) -> list[BallotLine]:
    yes = m.topic.lean.vector()
    no = tuple(-x for x in yes)
    appeal = float(m.topic.appeal) + float(base_appeal)
    return [
        BallotLine(key=YES_LINE, party_code=None, label="Yes", quality=appeal, ideology=yes),
        BallotLine(key=NO_LINE, party_code=None, label="No", quality=0.0, ideology=no),
    ]


# ============================================================================ creation
def _ensure_offices(session: Session, wanted: Sequence[dict[str, Any]]) -> dict[str, int]:
    existing = dict(session.execute(select(Office.code, Office.id)).tuples().all())
    new = list({o["code"]: o for o in wanted if o["code"] not in existing}.values())
    for o in new:
        session.add(Office(**o))
    if new:
        session.flush()
        existing = dict(session.execute(select(Office.code, Office.id)).tuples().all())
    return existing


def require_local_creatable(session: Session, on: date, province_code: str) -> None:
    """A local election can only be created while no later election is reported, and only once
    per province and date."""
    dup = session.execute(
        select(Election.id)
        .join(Province, Province.id == Election.province_id)
        .where(
            Election.election_type == ElectionType.LOCAL.value,
            Election.election_date == on,
            Province.code == province_code,
        )
    ).first()
    if dup is not None:
        raise ElectionError(
            f"the {province_code} local election of {on.isoformat()} already exists (id {dup[0]})"
        )
    later = session.scalars(
        select(Election.id)
        .where(Election.status.in_(list(REPORTED_STATUSES)), Election.election_date > on)
        .limit(1)
    ).first()
    if later is not None:
        raise ElectionError(
            f"election {later} after {on.isoformat()} is already final; elections are certified in date order"
        )


def create_local_election(
    session: Session,
    province_code: str,
    on: date,
    *,
    plan: LocalPlan | None = None,
    seed: int | None = None,
) -> Election:
    """Create (SCHEDULED) the local election of a province on one of its local days."""
    frame = get_frame(session)
    cal = get_local_calendar(session, frame)
    require_local_creatable(session, on, province_code)
    plan = plan or plan_for(session, province_code, on)
    if not plan.contests:
        raise ElectionError(f"nothing is on the {province_code} ballot of {on.isoformat()}")
    vintage = active_vintage(session)
    if vintage is None:
        raise ElectionError("no geography loaded (run the setup first)")
    scen, doc = base_scenario(session, on)
    model = get_model(frame, doc)
    run_seed = int(seed if seed is not None else local_seed(cal, plan.day))
    prov = session.scalars(select(Province).where(Province.code == province_code)).one()
    prev = session.scalars(
        select(Election)
        .where(Election.election_date < on)
        .order_by(Election.election_date.desc(), Election.id.desc())
        .limit(1)
    ).first()
    el = Election(
        year=on.year,
        election_date=on,
        name=plan.name[:160],
        election_type=ElectionType.LOCAL.value,
        status=ElectionStatus.SCHEDULED.value,
        scenario_id=scen.id,
        seed=run_seed,
        vintage_id=vintage.id,
        apportionment_id=None,
        district_plan_id=None,
        previous_election_id=prev.id if prev is not None else None,
        province_id=prov.id,
        polls_close_local=cal.config.polls_close,
        timezone=cal.calendar.config.timezone,
        is_fictional=True,
        notes="FICTIONAL local election in a FICTIONAL federal system over REAL Dutch geography",
    )
    session.add(el)
    session.flush()
    from app.services._common import set_meta

    if not local_elections_enabled(session):
        set_meta(session, LOCAL_ENABLED_KEY, "1")
    with (
        Timer(log, f"create local election {province_code} {on.isoformat()}"),
        RunRecorder(
            session,
            RUN_SETUP,
            run_seed,
            election_id=el.id,
            scenario_id=scen.id,
            config_hash=scenario_hash(doc),
        ) as rec,
    ):
        planned, specs, offices_wanted, details = _plan_races(session, cal, frame, model, doc, plan, run_seed)
        office_ids = _ensure_offices(session, offices_wanted)
        parties = {p.code: p for p in session.scalars(select(Party))}
        party_ids = {c: p.id for c, p in parties.items()}
        prov_ids = dict(session.execute(select(Province.code, Province.id)).tuples().all())
        ideology = {p.code: (p.ideology.economic, p.ideology.social) for p in doc.parties}
        existing_ids = (
            dict(
                session.execute(select(Candidate.key, Candidate.id).where(Candidate.key.in_(list(specs))))
                .tuples()
                .all()
            )
            if specs
            else {}
        )
        new_specs = [s for k, s in specs.items() if k not in existing_ids]
        cand_ids = dict(existing_ids)
        cand_ids.update(
            create_ops.upsert_candidates(
                session, new_specs, on.year, party_ids, prov_ids, frame, ideology, update_existing=False
            )
        )
        race_ids = create_ops.store_races(session, el, planned, cand_ids, parties, doc)
        _store_local_details(session, race_ids, planned, details, office_ids)
        president = session.execute(
            select(Party.code)
            .join(Candidate, Candidate.party_id == Party.id)
            .where(Candidate.id == _president_candidate_id(session, on))
        ).first()
        snapshot = {"president_party": president[0] if president else None, "holdover_senate": {}}
        rec.summary.update(snapshot)
        counts: dict[str, int] = {}
        for pr in planned:
            counts[pr.race_type.value] = counts.get(pr.race_type.value, 0) + 1
        rec.summary.update(
            {
                "local": True,
                "province": province_code,
                "date": on.isoformat(),
                "races": counts,
                "ballot_lines": sum(len(pr.lines) for pr in planned),
                "new_candidates": len(new_specs),
                "municipalities": plan.municipalities,
            }
        )
    log.info(
        "local election created",
        extra=log_ctx(
            election_id=el.id, province=province_code, date=on.isoformat(), races=sum(counts.values())
        ),
    )
    return el


def _president_candidate_id(session: Session, on: date) -> int | None:
    from app.services._common import PRESIDENT_OFFICE

    h = create_ops.holders_at(session, on).get(PRESIDENT_OFFICE)
    return h.candidate_id if h is not None else -1


def _plan_races(
    session: Session,
    cal: LocalCalendar,
    frame: GeographyFrame,
    model: StructuralModel,
    doc: ScenarioDocument,
    plan: LocalPlan,
    seed: int,
) -> tuple[
    list[create_ops.PlannedRace], dict[str, CandidateSpec], list[dict[str, Any]], dict[str, dict[str, Any]]
]:
    """Planned races (with ballot lines), every candidate spec, office rows to ensure and the
    ``details_json`` of every race."""
    day = plan.day
    holders = create_ops.holders_at(session, day.date)
    existing = set(session.scalars(select(Candidate.key)).all())
    prov_id = dict(session.execute(select(Province.code, Province.id)).tuples().all())
    specs: dict[str, CandidateSpec] = {}
    planned: list[create_ops.PlannedRace] = []
    offices: list[dict[str, Any]] = []
    details: dict[str, dict[str, Any]] = {}
    term_start = cal.term_start(day.date)
    special_slots: list[tuple[RaceSlot, PlannedContest]] = []
    cand_rows_cache: dict[int, CandidateSpec] = {}

    def holder_spec(h: create_ops.Holder) -> CandidateSpec | None:
        if h.candidate_id in cand_rows_cache:
            return cand_rows_cache[h.candidate_id]
        row = session.get(Candidate, h.candidate_id)
        if row is None:
            return None
        from app.services.runtime import candidate_spec_from_row

        spec = candidate_spec_from_row(row, None, None)
        if row.ideology_economic is not None:
            spec = spec.model_copy(
                update={
                    "ideology": Ideology(
                        economic=float(np.clip(row.ideology_economic or 0.0, -1, 1)),
                        social=float(np.clip(row.ideology_social or 0.0, -1, 1)),
                        europe=0.0,
                    )
                }
            )
        cand_rows_cache[h.candidate_id] = spec
        return spec

    for c in plan.contests:
        gm = c.municipality_code
        if c.kind in ("school_board", "water_board"):
            if c.kind == "school_board":
                assert gm is not None
                m = frame.muni_index(gm)
                units = frame.units_in_muni(m)
                cyc = cal.school_board_cycle(gm, day.date.year)
                rt, otype = RaceType.SCHOOL_BOARD, OfficeType.SCHOOL_BOARD_MEMBER
                ccfg = cal.config.school_boards
                pv = frame.province_codes[int(frame.muni_province[m])]
                body = f"School Board of {frame.muni_names[m]}"
                office_code = lambda s, gm=gm: school_board_office_code(gm, s)  # noqa: E731
            else:
                ws = str(c.water_board_code)
                w = frame.water_board_index(ws)
                units = frame.units_in_water_board(w)
                cyc = cal.water_board_cycle(ws, day.date.year)
                rt, otype = RaceType.WATER_BOARD, OfficeType.WATER_BOARD_MEMBER
                ccfg = cal.config.water_boards
                pv = frame.province_codes[int(frame.water_board_province[w])]
                body = f"Board of {frame.water_board_names[w]}"
                office_code = lambda s, ws=ws: water_board_office_code(ws, s)  # noqa: E731
            assert cyc is not None
            seat_terms: dict[str, int] = {}
            for cls in cyc.classes:
                for s in cls.seats:
                    seat_terms[str(s)] = cls.term_years
                    offices.append(
                        {
                            "office_type": otype.value,
                            "code": office_code(s),
                            "name": f"{body}, seat {s}",
                            "province_id": prov_id.get(pv),
                            "municipality_code": gm,
                            "term_years": ccfg.term_years,
                            "is_active": True,
                        }
                    )
            incumbents = []
            for s in cyc.seats_up:
                h = holders.get(office_code(s))
                if h is not None:
                    sp = holder_spec(h)
                    if sp is not None:
                        incumbents.append(sp)
            lines = _board_candidates(
                model, cal, seed, c.code, rt, units, cyc, incumbents, body, existing, ccfg
            )
            for _ln, sp in lines:
                specs.setdefault(sp.key, sp)
            seats = len(cyc.seats_up)
            spec = RaceSpec(
                key=c.code,
                race_type=rt,
                unit_index=units,
                lines=[ln for ln, _ in lines],
                electoral_system=ElectoralSystem.PLURALITY_AT_LARGE,
                seats=seats,
                province_code=pv,
                municipality_code=gm,
                is_open_seat=not any(ln.incumbent for ln, _ in lines),
                details={},
            )
            details[c.code] = {
                "kind": c.kind,
                "body": body,
                "water_board": c.water_board_code,
                "cycle": cyc.number,
                "seats_total": cyc.total_seats,
                "seats_up": list(cyc.seats_up),
                "seat_terms": seat_terms,
                "office_codes": [office_code(s) for s in cyc.seats_up],
                "term_start": term_start.isoformat(),
                "nonpartisan": True,
                "lines": {ln.key: {"ideology": list(ln.ideology or ())} for ln, _ in lines},
            }
            planned.append(
                create_ops.PlannedRace(
                    c.code, rt, c.name, spec, [create_ops.PlannedLine(ln, sp, None) for ln, sp in lines]
                )
            )
        elif c.kind == "measure":
            assert gm is not None
            m = frame.muni_index(gm)
            mp = next(x for x in cal.measures(gm, day.date) if measure_race_code(gm, x.suffix) == c.code)
            lines = _measure_lines(mp, cal.config.measures.base_appeal)
            pv = frame.province_codes[int(frame.muni_province[m])]
            spec = RaceSpec(
                key=c.code,
                race_type=RaceType.BALLOT_MEASURE,
                unit_index=frame.units_in_muni(m),
                lines=lines,
                electoral_system=ElectoralSystem.QUESTION,
                province_code=pv,
                municipality_code=gm,
                threshold=mp.topic.threshold,
            )
            details[c.code] = {
                "kind": "measure",
                "label": mp.label,
                "topic": mp.topic.key,
                "measure_kind": mp.topic.kind,
                "title": mp.topic.title,
                "summary": mp.topic.summary,
                "threshold": mp.topic.threshold,
                "lines": {ln.key: {"ideology": list(ln.ideology or ())} for ln in lines},
            }
            planned.append(
                create_ops.PlannedRace(
                    c.code,
                    RaceType.BALLOT_MEASURE,
                    c.name,
                    spec,
                    [create_ops.PlannedLine(ln, None, None) for ln in lines],
                )
            )
        elif c.kind == "recall":
            assert gm is not None
            m = frame.muni_index(gm)
            pv = frame.province_codes[int(frame.muni_province[m])]
            target_party = c.details.get("party")
            if target_party is not None and target_party in model.party_index:
                pos = tuple(float(x) for x in model.ideology[model.party_index[target_party]])
            else:
                pos = tuple(0.0 for _ in IDEOLOGY_DIMS)
            rcfg = cal.config.recalls
            appeal = rcfg.base_appeal + (rcfg.scandal_appeal if c.details.get("reason") == "scandal" else 0.0)
            yes = tuple(round(-0.8 * x, 3) for x in pos)
            lines = [
                BallotLine(key=YES_LINE, party_code=None, label="Yes", quality=float(appeal), ideology=yes),
                BallotLine(key=NO_LINE, party_code=None, label="No", quality=0.0, ideology=pos),
            ]
            spec = RaceSpec(
                key=c.code,
                race_type=RaceType.RECALL,
                unit_index=frame.units_in_muni(m),
                lines=lines,
                electoral_system=ElectoralSystem.QUESTION,
                province_code=pv,
                municipality_code=gm,
                incumbent_party=target_party,
                threshold=0.5,
            )
            target = (
                session.get(Candidate, c.details.get("candidate_id"))
                if c.details.get("candidate_id")
                else None
            )
            details[c.code] = {
                "kind": "recall",
                "office_code": mayor_office_code(gm),
                "reason": c.details.get("reason"),
                "event_date": c.details.get("event_date"),
                "target_candidate_id": c.details.get("candidate_id"),
                "target_name": target.full_name if target is not None else None,
                "target_party": target_party,
                "replacement_race": mayor_office_code(gm),
                "threshold": 0.5,
                "lines": {ln.key: {"ideology": list(ln.ideology or ())} for ln in lines},
            }
            planned.append(
                create_ops.PlannedRace(
                    c.code,
                    RaceType.RECALL,
                    c.name,
                    spec,
                    [create_ops.PlannedLine(ln, None, None) for ln in lines],
                )
            )
            slot = RaceSlot(
                key=mayor_office_code(gm),
                race_type=RaceType.MAYOR,
                unit_index=frame.units_in_muni(m),
                province_code=pv,
                office_label=f"Mayor of {frame.muni_names[m]}",
            )
            special_slots.append(
                (
                    slot,
                    PlannedContest(
                        "recall_replacement",
                        mayor_office_code(gm),
                        f"Mayor of {frame.muni_names[m]} (replacement if recalled)",
                        municipality_code=gm,
                        details={"parent": c.code, **c.details},
                    ),
                )
            )
        elif c.kind in ("mayor_special", "council_seat"):
            assert gm is not None
            m = frame.muni_index(gm)
            pv = frame.province_codes[int(frame.muni_province[m])]
            if c.kind == "council_seat":
                offices.append(
                    {
                        "office_type": OfficeType.COUNCIL_MEMBER.value,
                        "code": council_office_code(gm),
                        "name": f"Municipal Council of {frame.muni_names[m]}",
                        "province_id": prov_id.get(pv),
                        "municipality_code": gm,
                        "term_years": 4,
                        "is_active": True,
                    }
                )
            slot = RaceSlot(
                key=c.code,
                race_type=RaceType.MAYOR if c.kind == "mayor_special" else RaceType.COUNCIL_SEAT,
                unit_index=frame.units_in_muni(m),
                province_code=pv,
                office_label=f"Mayor of {frame.muni_names[m]}"
                if c.kind == "mayor_special"
                else f"the Municipal Council of {frame.muni_names[m]}",
            )
            special_slots.append((slot, c))
    if special_slots:
        fielded = generate_down_ballot(
            model,
            [s for s, _ in special_slots],
            seed,
            rules={RaceType.MAYOR: doc.municipal, RaceType.COUNCIL_SEAT: doc.municipal},
            existing_keys=existing,
        )
        race_specs = races_from_candidates([s for s, _ in special_slots], fielded)
        regular = _regular_municipal_date(cal.calendar, day.date)
        reg_start = cal.calendar.term_bounds(OfficeType.MAYOR, regular.year)[0]
        for (slot, c), spec in zip(special_slots, race_specs, strict=True):
            rc = fielded[slot.key]
            by_key = {x.key: x for x in rc.candidates}
            for x in rc.candidates:
                specs.setdefault(x.key, x)
            spec.is_special = True
            spec.is_open_seat = True
            gm = str(c.municipality_code)
            spec.municipality_code = gm
            if c.kind == "council_seat":
                spec.incumbent_party = c.details.get("vacated_party")
                office = council_office_code(gm)
            else:
                spec.incumbent_party = c.details.get("party")
                office = mayor_office_code(gm)
            details[slot.key] = {
                "kind": c.kind,
                "office_code": office,
                "reason": c.details.get("reason"),
                "event_date": c.details.get("event_date"),
                "vacated_party": c.details.get("vacated_party") or c.details.get("party"),
                "vacated_candidate_id": c.details.get("candidate_id"),
                "vacated_holder_id": c.details.get("holder_id"),
                "parent": c.details.get("parent"),
                "term_start": term_start.isoformat(),
                "term_end": reg_start.isoformat(),
            }
            planned.append(
                create_ops.PlannedRace(
                    slot.key,
                    RaceType(slot.race_type),
                    c.name,
                    spec,
                    [
                        create_ops.PlannedLine(ln, by_key.get(ln.candidate_key or ""), None)
                        for ln in spec.lines
                    ],
                    office_code=office
                    if c.kind == "mayor_special" or c.kind == "recall_replacement"
                    else None,
                    parent=c.details.get("parent"),
                )
            )
    return planned, specs, offices, details


def _store_local_details(
    session: Session,
    race_ids: dict[str, int],
    planned: Sequence[create_ops.PlannedRace],
    details: dict[str, dict[str, Any]],
    office_ids: dict[str, int],
) -> None:
    for pr in planned:
        row = session.get(Race, race_ids[pr.code])
        if row is None:  # pragma: no cover
            continue
        d = details.get(pr.code, {})
        row.details_json = dumps(d) if d else None
        row.threshold = pr.spec.threshold
        parent = d.get("parent")
        if parent and parent in race_ids:
            row.parent_race_id = race_ids[parent]
    session.flush()


# ============================================================================ bulk helpers
def existing_local_days(session: Session) -> set[tuple[str, date]]:
    rows = session.execute(
        select(Province.code, Election.election_date)
        .join(Province, Province.id == Election.province_id)
        .where(Election.election_type == ElectionType.LOCAL.value)
    ).all()
    return {(str(p), d) for p, d in rows}


def latest_reported_date(session: Session) -> date | None:
    return session.scalar(
        select(func.max(Election.election_date)).where(Election.status.in_(list(REPORTED_STATUSES)))
    )


def open_after(session: Session) -> date:
    """Local days *after* this date may still have to be created or held: the day before the
    latest reported election, so the same-day local elections of other provinces count too
    (an interrupted run may have certified only some of a day's provinces)."""
    lo = latest_reported_date(session)
    return lo - timedelta(days=1) if lo is not None else date(1900, 1, 1)


def ensure_local_elections(
    session: Session, until: date, *, after: date | None = None, progress: Callable[[str], None] | None = None
) -> list[int]:
    """Create (SCHEDULED) every planned local election with ``after < date ≤ until`` that does
    not exist yet (default ``after``: :func:`open_after`).  Returns the new ids."""
    lo = after or open_after(session)
    have = existing_local_days(session)
    created: list[int] = []
    for plan in plan_local_elections(session, lo, until):
        key = (plan.day.province_code, plan.day.date)
        if key in have:
            continue
        el = create_local_election(session, plan.day.province_code, plan.day.date, plan=plan)
        created.append(el.id)
        if progress is not None:
            progress(plan.name)
    return created


def unreported_before(session: Session, on: date, *, exclude_id: int | None = None) -> list[Election]:
    """Elections held before a date that are not reported yet (oldest first)."""
    q = select(Election).where(Election.election_date < on, Election.status.not_in(list(REPORTED_STATUSES)))
    if exclude_id is not None:
        q = q.where(Election.id != exclude_id)
    return list(session.scalars(q.order_by(Election.election_date, Election.id)))


def missing_local_before(session: Session, on: date) -> list[LocalPlan]:
    """Planned local elections before a date that have not been created yet (they must be held
    before an election of that date can be certified).  Cheap when nothing is missing: only local
    days without a stored election are planned in full."""
    lo = open_after(session)
    if lo >= on:
        return []
    frame = get_frame(session)
    cal = get_local_calendar(session, frame)
    have = existing_local_days(session)
    todo = [d for d in cal.days(lo, on - timedelta(days=1)) if (d.province_code, d.date) not in have]
    if not todo:
        return []
    out: list[LocalPlan] = []
    events: list[OfficeEvent] | None = None
    for day in todo:
        plan = _plan_day(cal, day, [], frame)
        if not plan.contests:  # only specials or recalls could still put it on the calendar
            if events is None:
                events = resolve_office_events(session, cal, on - timedelta(days=1))
            mine = [
                ev
                for ev in events
                if ev.election_date == day.date
                and frame.province_codes[int(frame.muni_province[frame.muni_index(ev.municipality_code)])]
                == day.province_code
            ]
            plan = _plan_day(cal, day, mine, frame)
        if plan.contests:
            out.append(plan)
    return out


def finish_earlier(
    session: Session,
    on: date,
    *,
    finish: Callable[[Session, int], None] | None = None,
    progress: Callable[[str], None] | None = None,
    commit: bool = False,
) -> list[int]:
    """Create, simulate and finish every election held before ``on`` that is not reported yet,
    oldest first (elections are certified in strict date order).  ``finish(session, id)``
    certifies one simulated election (default: :func:`app.services.elections.instant_finalize`);
    with ``commit`` each election is committed as soon as it is finished.  Returns the ids."""
    from app.services import elections as election_service

    done: list[int] = []
    ensure_local_elections(session, on - timedelta(days=1))
    for el in unreported_before(session, on):
        eid = el.id
        if el.status == ElectionStatus.SCHEDULED.value:
            election_service.simulate_election(session, eid)
        if finish is not None:
            finish(session, eid)
        else:
            election_service.finalize_election(session, eid)
        done.append(eid)
        if commit:
            session.commit()
        if progress is not None:
            progress(el.name)
    return done


LOCAL_ENABLED_KEY = "local_elections_enabled"


def local_elections_enabled(session: Session) -> bool:
    """Whether this database holds in-between local elections (set when the first one is
    created); only then do planned-but-not-created local elections take part in the date order."""
    from app.services._common import get_meta

    return get_meta(session, LOCAL_ENABLED_KEY) == "1"


def is_local(election: Election) -> bool:
    return election.election_type == ElectionType.LOCAL.value


def local_elections_query() -> Any:
    return select(Election).where(Election.election_type == ElectionType.LOCAL.value)


__all__ = [
    "LocalPlan",
    "PlannedContest",
    "base_scenario",
    "create_local_election",
    "ensure_local_elections",
    "finish_earlier",
    "get_local_calendar",
    "missing_local_before",
    "open_after",
    "plan_for",
    "plan_local_elections",
    "resolve_office_events",
    "unreported_before",
]

_ = or_  # re-exported helper kept for callers building their own filters
