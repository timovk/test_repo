"""System validation (spec §33): a clean world passes; tampering is detected."""

from __future__ import annotations

from sqlalchemy import delete, select, update

from app.models import (
    DistrictAssignment,
    ElectionResult,
    Race,
    SenateSeat,
    TurnoutResult,
)
from app.services.validation import reconcile_election, validate_election, validate_system


def test_clean_world_passes(world) -> None:  # type: ignore[no-untyped-def]
    rep = validate_system(world.session)
    assert rep.ok, rep.errors
    names = {c.name for c in rep.checks}
    for expected in (
        "province count",
        "house districts",
        "senate seats",
        "senate classes",
        "a province's seats in different classes",
        "electoral votes total",
        "EV = House seats + senators per province",
        "majorities derived",
        "every unit assigned exactly once",
        "no district crosses a province",
        "municipalities in exactly one province",
        "districts in exactly one province",
    ):
        assert expected in names
    assert any("results reconcile" in n for n in names)
    d = rep.to_dict()
    assert d["ok"] and d["errors"] == [] and len(d["checks"]) == len(rep.checks)


def test_tampered_results_are_detected(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    race_id = s.scalar(select(Race.id).where(Race.election_id == world.founding, Race.code == "HOUSE-NB-01"))
    row = s.scalars(
        select(ElectionResult)
        .where(ElectionResult.race_id == race_id, ElectionResult.level == "unit")
        .limit(1)
    ).one()
    row.votes += 1
    s.flush()
    problems = reconcile_election(s, world.founding)
    assert any("unit→municipality votes" in p for p in problems)
    assert not validate_election(s, world.founding).ok


def test_tampered_turnout_is_detected(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    s.execute(
        update(TurnoutResult)
        .where(TurnoutResult.election_id == world.founding, TurnoutResult.level == "national")
        .values(blank_votes=TurnoutResult.blank_votes + 1)
    )
    problems = reconcile_election(s, world.founding)
    assert "valid + blank + invalid != ballots cast" in problems


def test_unassigned_unit_is_detected(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    first = s.scalar(select(DistrictAssignment.id).order_by(DistrictAssignment.id).limit(1))
    s.execute(delete(DistrictAssignment).where(DistrictAssignment.id == first))
    rep = validate_system(s, elections=False)
    assert any(e.startswith("every unit assigned exactly once") for e in rep.errors)


def test_senate_class_violation_is_detected(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    seats = s.scalars(select(SenateSeat).order_by(SenateSeat.province_id, SenateSeat.seat_number)).all()
    seats[1].senate_class = seats[0].senate_class  # both seats of one province in one class
    s.flush()
    rep = validate_system(s, elections=False)
    assert any(e.startswith("a province's seats in different classes") for e in rep.errors)
    assert any(e.startswith("senate classes") for e in rep.errors)
