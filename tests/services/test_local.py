"""In-between local elections on the synthetic sandbox world: the calendar (days, slots, board
cycles, measures, office events), planning, creation, simulation, certification (vote-for-N
winners, Yes/No thresholds, office terms), specials and recalls, strict date order, same-day
independence and reset."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from itertools import pairwise

import numpy as np
import pytest
from sqlalchemy import func, select

from app.core.constitution import ElectionStatus, ElectionType, RaceType
from app.core.errors import ElectionError
from app.elections.local_calendar import LocalCalendar, LocalConfig, load_local_config
from app.models import Election, Office, OfficeHolder, Race
from app.services import local as local_service
from app.services._create import holders_at
from app.services.elections import finalize_election, simulate_election
from app.services.reset import reset_election
from app.services.runtime import election_inputs, get_frame, load_final_race_votes
from app.services.validation import validate_election, validate_system

FOUNDING = date(2024, 11, 6)


def _details(race: Race) -> dict:
    return json.loads(race.details_json) if race.details_json else {}


def _first_plans(session, n: int = 3, until: date = date(2025, 12, 31)):  # type: ignore[no-untyped-def]
    plans = local_service.plan_local_elections(session, FOUNDING, until)
    assert plans, "the calendar holds local elections in 2025"
    return plans[:n]


# ============================================================================ calendar
def test_calendar_days_slots_and_cycles(world) -> None:  # type: ignore[no-untyped-def]
    frame = get_frame(world.session)
    cfg = load_local_config()
    cal = LocalCalendar(frame, cfg)
    for rules in cal.province_slots.values():
        assert len(rules) == cfg.days_per_province
        assert len({r.month for r in rules}) == len(rules) and all(r.month in cfg.months for r in rules)
    # every municipality has one slot; slots are balanced within a province
    assert set(cal.muni_slot) == set(frame.muni_codes)
    for p in range(frame.n_provinces):
        counts = Counter(cal.muni_slot[frame.muni_codes[m]] for m in frame.munis_in_province(p))
        assert max(counts.values()) - min(counts.values()) <= 1
    days = cal.days(FOUNDING, date(2026, 12, 31))
    assert days and all(d.date > FOUNDING and d.date.weekday() == 2 for d in days)  # Wednesdays
    assert [d.date for d in days] == sorted(d.date for d in days)
    assert not any(d.date.month in (10, 11, 12, 1) for d in days)  # never close to November
    # staggered school boards: the first election fills every seat, then halves alternate
    gm = frame.muni_codes[0]
    cycles = [(y, cal.school_board_cycle(gm, y)) for y in range(2025, 2032)]
    held = [(y, c) for y, c in cycles if c is not None]
    assert [y for y, _ in held] == list(range(held[0][0], 2032, 2))
    first = held[0][1]
    assert first.first and sorted(first.seats_up) == list(range(1, first.total_seats + 1))
    assert {c.term_years for c in first.classes} == {4, 2}
    second, third = held[1][1], held[2][1]
    assert set(second.seats_up) | set(third.seats_up) == set(first.seats_up)
    assert not set(second.seats_up) & set(third.seats_up)
    # deterministic: a fresh calendar draws the same ballots and events
    again = LocalCalendar(frame, cfg)
    d0 = days[0]
    munis = cal.municipalities_on(d0)
    assert [m.topic.key for gm_ in munis for m in cal.measures(gm_, d0.date)] == [
        m.topic.key for gm_ in munis for m in again.measures(gm_, d0.date)
    ]
    assert cal.office_events(date(2027, 12, 31)) == again.office_events(date(2027, 12, 31))


def test_water_boards_on_the_calendar(world) -> None:  # type: ignore[no-untyped-def]
    frame = get_frame(world.session)
    if not frame.n_water_boards:
        pytest.skip("geography without water boards")
    cal = LocalCalendar(frame, load_local_config())
    for ws in frame.water_board_codes:
        years = [y for y in range(2025, 2035) if cal.water_board_cycle(ws, y) is not None]
        assert len(years) >= 2 and all(b - a == 4 for a, b in pairwise(years))
    held = [(d, ws) for d in cal.days(FOUNDING, date(2029, 12, 31)) for ws in cal.water_boards_on(d)]
    assert {ws for _, ws in held} == set(frame.water_board_codes)


# ============================================================================ create → certify
def test_local_election_lifecycle(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    plan = _first_plans(s, 1)[0]
    el = local_service.create_local_election(s, plan.date, plan=plan)
    assert el.election_type == ElectionType.LOCAL.value and el.status == ElectionStatus.SCHEDULED.value
    assert el.name.startswith("Local Elections · ") and el.provinces.split(",") == plan.provinces
    races = s.scalars(select(Race).where(Race.election_id == el.id)).all()
    assert len(races) == plan.races
    # every province voting that day is on the one ballot (one combined election night)
    assert {c.province_code for c in plan.contests} == set(plan.provinces)
    with pytest.raises(ElectionError):  # one election per date
        local_service.create_local_election(s, plan.date, plan=plan)
    simulate_election(s, el.id)
    inputs = election_inputs(s, el.id)
    votes = load_final_race_votes(s, el.id, inputs, with_expectation=False)
    for code, rv in votes.items():
        spec = inputs.races[code]
        if RaceType(spec.race_type) in (RaceType.SCHOOL_BOARD, RaceType.WATER_BOARD):
            assert rv.seats == len(_details(next(r for r in races if r.code == code))["seats_up"])
            assert rv.marks_per_ballot == rv.seats
            assert (rv.votes.sum(axis=1) >= rv.valid_ballots).all()
            assert (rv.votes.sum(axis=1) <= rv.valid_ballots * rv.seats).all()
        if RaceType(spec.race_type) == RaceType.BALLOT_MEASURE:
            assert rv.line_keys == ["YES", "NO"] and rv.threshold in (0.5, 0.6, 0.6667)
        # off-cycle turnout is low (the synthetic country votes more than the real one)
        assert 0.1 < rv.ballots_cast.sum() / rv.eligible.sum() < 0.75
    finalize_election(s, el.id)
    s.commit()
    s.expire_all()
    seats_filled = 0
    for r in s.scalars(select(Race).where(Race.election_id == el.id)):
        d = _details(r)
        assert r.status == "FINAL"
        if r.race_type in (RaceType.SCHOOL_BOARD.value, RaceType.WATER_BOARD.value):
            assert len(d["winners"]) == len(d["seats_up"]) == len(d["seat_assignment"])
            seats_filled += len(d["winners"])
        if r.race_type == RaceType.BALLOT_MEASURE.value:
            yes = d["yes_share"]
            t = float(r.threshold)
            assert d["passed"] == (yes > t if abs(t - 0.5) < 1e-9 else yes >= t - 1e-9)
    holders = s.scalar(
        select(func.count())
        .select_from(OfficeHolder)
        .join(Office, Office.id == OfficeHolder.office_id)
        .where(Office.office_type.in_(["SCHOOL_BOARD_MEMBER", "WATER_BOARD_MEMBER"]))
    )
    assert holders == seats_filled
    report = validate_system(s)
    assert report.ok, report

    # reset undoes the certification: the terms the election started are gone
    res = reset_election(s, el.id)
    s.commit()
    assert res.mode == "uncertified"
    assert (
        s.scalar(
            select(func.count())
            .select_from(OfficeHolder)
            .join(Office, Office.id == OfficeHolder.office_id)
            .where(Office.office_type.in_(["SCHOOL_BOARD_MEMBER", "WATER_BOARD_MEMBER"]))
        )
        == 0
    )
    assert all("winners" not in _details(r) for r in s.scalars(select(Race).where(Race.election_id == el.id)))


def test_strict_date_order_and_combined_days(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    plans = local_service.plan_local_elections(s, FOUNDING, date(2025, 12, 31))
    assert [p.date for p in plans] == sorted({p.date for p in plans})  # one election per date
    combined = next(p for p in plans if len(p.provinces) >= 2)
    assert combined.provinces == [
        d.province_code for d in combined.days if d.province_code in combined.provinces
    ]
    first, target = plans[0], plans[min(2, len(plans) - 1)]
    for p in plans:
        if p.date <= target.date:
            local_service.create_local_election(s, p.date, plan=p)
    eid = s.scalars(select(Election.id).where(Election.election_date == target.date)).one()
    # a later election cannot be certified while an earlier one is unfinished
    if target.date > first.date:
        simulate_election(s, eid)
        with pytest.raises(ElectionError, match="must be finished first"):
            finalize_election(s, eid)
    done = local_service.finish_earlier(s, target.date)
    assert all(s.get(Election, i).status == "final" for i in done)
    if s.get(Election, eid).status == ElectionStatus.SCHEDULED.value:
        simulate_election(s, eid)
    finalize_election(s, eid)
    assert not local_service.missing_local_before(s, target.date + timedelta(days=1))
    assert local_service.ensure_local_elections(s, target.date) == []


def test_the_big_election_waits_for_the_local_ones(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    plan = _first_plans(s, 1)[0]
    local_service.create_local_election(s, plan.date, plan=plan)
    with pytest.raises(ElectionError, match="must be finished first"):
        finalize_election(s, world.second)


def test_validation_of_local_elections(world) -> None:  # type: ignore[no-untyped-def]
    """A local election in a House year holds no House races; a moot recall replacement elects
    nobody.  Neither is a validation failure."""
    s = world.session
    plan = _first_plans(s, 1)[0]
    el = local_service.create_local_election(s, plan.date, plan=plan)
    simulate_election(s, el.id)
    finalize_election(s, el.id)
    el.year = 2026  # a midterm year: the calendar's House and presidential checks do not apply
    race = s.scalars(select(Race).where(Race.election_id == el.id)).first()
    race.winner_ballot_candidate_id = None
    s.flush()
    failed = [c.name for c in validate_election(s, el.id).checks if not c.ok]
    assert failed == [f"election {el.id} (2026): every race has a winner"]
    race.details_json = json.dumps({**_details(race), "moot": True}, separators=(",", ":"))
    s.flush()
    assert validate_election(s, el.id).ok


# ============================================================================ specials and recalls
def _with_mayors(session, start: date) -> None:  # type: ignore[no-untyped-def]
    """Give every municipality a sitting mayor (the sandbox holds no midterm)."""
    from app.models import Candidate

    cands = session.scalars(select(Candidate).order_by(Candidate.id).limit(400)).all()
    offices = session.scalars(select(Office).where(Office.office_type == "MAYOR")).all()
    for i, o in enumerate(offices):
        c = cands[i % len(cands)]
        session.add(
            OfficeHolder(
                office_id=o.id,
                candidate_id=c.id,
                party_id=c.party_id,
                term_start=start,
                term_end=date(2027, 1, 1),
                start_reason="appointed",
            )
        )
    session.flush()


def test_specials_and_recalls(world, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    _with_mayors(s, date(2024, 12, 1))
    base = load_local_config()
    busy = LocalConfig.model_validate(
        {
            **base.model_dump(),
            "vacancies": {**base.vacancies.model_dump(), "mayor_rate": 0.6},
            "recalls": {**base.recalls.model_dump(), "mayor_rate": 0.6, "min_days_in_office": 30},
            "regular_election_window_days": 0,
        }
    )
    monkeypatch.setattr(local_service, "load_local_config", lambda: busy)
    local_service._calendars.clear()
    plans = local_service.plan_local_elections(s, FOUNDING, date(2025, 9, 30))
    for p in plans:  # two vacant seats of one council on one day are numbered
        names = [c.name for c in p.contests]
        assert len(names) == len(set(names)), Counter(names).most_common(1)
    kinds = Counter(c.kind for p in plans for c in p.contests)
    assert kinds["mayor_special"] > 0 and kinds["recall"] > 0
    target = next(p for p in plans if any(c.kind in ("recall", "mayor_special") for c in p.contests))
    local_service.finish_earlier(s, target.date)
    el = local_service.create_local_election(s, target.date, plan=target)
    simulate_election(s, el.id)
    finalize_election(s, el.id)
    s.commit()
    s.expire_all()
    for r in s.scalars(select(Race).where(Race.election_id == el.id)):
        d = _details(r)
        if r.race_type == RaceType.RECALL.value:
            child = s.scalars(select(Race).where(Race.parent_race_id == r.id)).one()
            cd = _details(child)
            assert cd["recall_passed"] == d["passed"]
            mayor = holders_at(s, date.fromisoformat(cd["term_start"])).get(d["office_code"])
            if d["passed"]:
                assert mayor is not None and child.winner_ballot_candidate_id is not None
                recalled = s.scalars(select(OfficeHolder).where(OfficeHolder.end_reason == "recalled")).all()
                assert any(h.candidate_id == d["target_candidate_id"] for h in recalled)
            else:
                assert (
                    cd["moot"]
                    and child.winner_ballot_candidate_id is None
                    and child.decided_by == "recall_failed"
                )
        elif r.race_type == RaceType.MAYOR.value and d.get("kind") == "mayor_special":
            ended = s.get(OfficeHolder, d["ended_holder_id"])
            assert ended.ended_on == date.fromisoformat(d["event_date"]) and ended.end_reason == d["reason"]
            new = s.scalars(select(OfficeHolder).where(OfficeHolder.election_race_id == r.id)).one()
            assert new.term_end == date.fromisoformat(d["term_end"])
    report = validate_system(s)
    assert report.ok, report
    # reset puts the vacated / recalled mayors back
    before = {
        (h.office_id, h.candidate_id): (h.ended_on, h.end_reason)
        for h in s.scalars(select(OfficeHolder).where(OfficeHolder.end_reason.is_not(None)))
    }
    reset_election(s, el.id)
    s.commit()
    s.expire_all()
    reopened = [
        k
        for k, v in before.items()
        if v[1] in ("recalled", "resigned", "died", "removed", "appointed_elsewhere")
    ]
    for office_id, cand in reopened:
        h = s.scalars(
            select(OfficeHolder).where(OfficeHolder.office_id == office_id, OfficeHolder.candidate_id == cand)
        ).first()
        assert h is not None and h.ended_on is None, (office_id, cand)
    local_service._calendars.clear()


def test_vote_for_n_mark_draws() -> None:
    from app.simulation.config import CandidateEffectsConfig
    from app.simulation.voting import at_large_marks

    rng = np.random.default_rng(3)
    valid = np.array([0, 1, 50, 1000])
    shares = np.array([[0.7, 0.2, 0.1], [0.5, 0.3, 0.2], [0.6, 0.3, 0.1], [0.9, 0.05, 0.05]])
    marks = at_large_marks(rng, valid, shares, 2, CandidateEffectsConfig())
    assert (marks <= valid[:, None]).all()
    assert (marks.sum(axis=1) >= valid).all() and (marks.sum(axis=1) <= 2 * valid).all()


# ============================================================================ custom people
def test_custom_people_run_where_they_live(world, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from app.models import BallotCandidate, Candidate
    from app.services.read.people import custom_people
    from app.simulation import people as people_mod

    s = world.session
    plan = next(p for p in _first_plans(s, 8) if any(c.kind == "school_board" for c in p.contests))
    sb = next(c for c in plan.contests if c.kind == "school_board")
    frame = get_frame(s)
    name = frame.muni_names[frame.muni_index(sb.municipality_code)]
    path = tmp_path / "people.yaml"
    path.write_text(
        "people:\n"
        f"  - {{name: Sanne de Vries, home: {name}, born: 1990, party: PA, chance: always, offices: [school_board]}}\n"
        f"  - {{name: Tom Jansen, home: {name}, born: 1985, chance: always, offices: [school_board]}}\n"
        f"  - {{name: Not Running, home: {name}, born: 1985, chance: never}}\n"
    )
    monkeypatch.setattr(people_mod, "people_path", lambda: path)
    el = local_service.create_local_election(s, plan.date, plan=plan)
    race = s.scalars(select(Race).where(Race.election_id == el.id, Race.code == sb.code)).one()
    keys = {
        bc.line_key for bc in s.scalars(select(BallotCandidate).where(BallotCandidate.race_id == race.id))
    }
    assert {"person-sanne-de-vries", "person-tom-jansen"} <= keys and "person-not-running" not in keys
    sanne = s.scalars(select(Candidate).where(Candidate.key == "person-sanne-de-vries")).one()
    assert sanne.home_municipality_code == sb.municipality_code and sanne.party_id is not None
    simulate_election(s, el.id)
    finalize_election(s, el.id)
    s.commit()
    data = custom_people(s)
    assert data["error"] is None and data["count"] == 3
    runs = {p["key"]: p["runs"] for p in data["people"]}
    assert runs["person-not-running"] == []
    for key in ("person-sanne-de-vries", "person-tom-jansen"):
        assert (
            len(runs[key]) == 1
            and runs[key][0]["race_code"] == sb.code
            and runs[key][0]["won"] in (True, False)
        )
    winners = [p for p in data["people"] if p["runs"] and p["runs"][0]["won"]]
    assert all(p["holds"] for p in winners)  # a winner takes a board seat
