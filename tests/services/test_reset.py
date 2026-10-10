"""Election reset ("replay"): the election keeps its id and its hidden result; the night goes
back to polls closing; a reported election has its certification undone exactly (recount
corrections reversed, office terms restored), so a replay certifies the identical election.
Earlier reported elections stay immutable while a later one is reported."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.constitution import HOUSE_SEATS, ElectionStatus, RaceStatus, RaceType
from app.core.errors import ElectionError
from app.elections.recount import RecountConfig, RecountThreshold
from app.models import (
    ContingentElection,
    Election,
    ElectionResult,
    ElectoralVoteAllocation,
    LegislatureSeatResult,
    OfficeHolder,
    Race,
    RaceCall,
    Recount,
    SimulationRun,
    TurnoutResult,
)
from app.services.elections import finalize_election, instant_finalize
from app.services.reset import can_reset, reset_election
from app.services.validation import validate_system

_RACE_COLS = (
    "status",
    "winner_ballot_candidate_id",
    "winner_party_id",
    "total_votes",
    "margin_votes",
    "margin_pct",
    "turnout_pct",
    "flipped",
    "decided_by",
    "called_at",
)


def _snapshot(s, election_id: int) -> dict[str, object]:  # type: ignore[no-untyped-def]
    races = select(Race.id).where(Race.election_id == election_id)
    return {
        "results": sorted(
            s.execute(
                select(
                    ElectionResult.race_id,
                    ElectionResult.ballot_candidate_id,
                    ElectionResult.geo_key,
                    ElectionResult.votes,
                    ElectionResult.share,
                ).where(ElectionResult.race_id.in_(races))
            ).all()
        ),
        "turnout": sorted(
            s.execute(
                select(
                    TurnoutResult.race_id,
                    TurnoutResult.geo_key,
                    TurnoutResult.ballots_cast,
                    TurnoutResult.valid_votes,
                    TurnoutResult.invalid_votes,
                ).where(TurnoutResult.race_id.in_(races))
            ).all()
        ),
        "races": sorted(
            s.execute(
                select(Race.id, *[getattr(Race, c) for c in _RACE_COLS]).where(
                    Race.election_id == election_id
                )
            ).all(),
            key=repr,
        ),
        "holders": sorted(
            s.execute(
                select(
                    OfficeHolder.office_id,
                    OfficeHolder.candidate_id,
                    OfficeHolder.term_start,
                    OfficeHolder.ended_on,
                    OfficeHolder.end_reason,
                )
            ).all(),
            key=repr,
        ),
        "ev": s.scalar(
            select(func.count())
            .select_from(ElectoralVoteAllocation)
            .where(ElectoralVoteAllocation.race_id.in_(races))
        ),
        "seats": s.scalar(
            select(func.count())
            .select_from(LegislatureSeatResult)
            .where(LegislatureSeatResult.race_id.in_(races))
        ),
        "contingent": s.scalar(
            select(func.count()).select_from(ContingentElection).where(ContingentElection.race_id.in_(races))
        ),
        "recounts": s.scalar(select(func.count()).select_from(Recount).where(Recount.race_id.in_(races))),
        "calls": s.scalar(
            select(func.count()).select_from(RaceCall).where(RaceCall.election_id == election_id)
        ),
        "runs": sorted(
            k
            for (k,) in s.execute(select(SimulationRun.kind).where(SimulationRun.election_id == election_id))
        ),
    }


def test_reset_unreported_election_resets_its_night(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    el = s.get(Election, world.second)
    assert el.status == ElectionStatus.SIMULATED.value
    race = s.scalars(select(Race).where(Race.election_id == el.id).limit(1)).first()
    race.status = RaceStatus.CALLED.value
    el.status = ElectionStatus.LIVE.value
    s.flush()
    res = reset_election(s, el.id)
    s.commit()
    assert res.mode == "night" and res.election_id == el.id and res.previous_status == "live"
    s.expire_all()
    assert s.get(Election, el.id).status == ElectionStatus.SIMULATED.value
    assert (
        s.scalar(
            select(func.count())
            .select_from(Race)
            .where(Race.election_id == el.id, Race.status != "SCHEDULED")
        )
        == 0
    )


def test_reset_reported_election_undoes_certification_exactly(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    eid = world.second
    before = _snapshot(s, eid)
    instant_finalize(s, eid)
    s.commit()
    final = _snapshot(s, eid)
    assert final["ev"] and final["holders"] != before["holders"] and final["races"] != before["races"]
    assert s.get(Election, eid).status in (ElectionStatus.FINAL.value, ElectionStatus.CERTIFIED.value)

    # the founding election is followed by a reported election: immutable
    ok, why = can_reset(s, world.founding)
    assert not ok and "most recent reported" in why
    with pytest.raises(ElectionError):
        reset_election(s, world.founding)

    res = reset_election(s, eid)
    s.commit()
    s.expire_all()
    assert res.mode == "uncertified" and res.election_id == eid
    assert s.get(Election, eid).status == ElectionStatus.SIMULATED.value
    assert s.get(Election, eid).finalized_at is None
    after = _snapshot(s, eid)
    for key in before:
        assert after[key] == before[key], key
    report = validate_system(s)
    assert report.ok, report

    # the replay certifies the identical election
    instant_finalize(s, eid)
    s.commit()
    again = _snapshot(s, eid)
    for key in ("results", "turnout", "holders", "ev", "seats", "contingent", "recounts", "runs"):
        assert again[key] == final[key], key
    assert [r[:-1] for r in again["races"]] == [r[:-1] for r in final["races"]]  # called_at aside


def test_reset_reverses_recount_corrections(world) -> None:  # type: ignore[no-untyped-def]
    """Force recounts of every House and province contest: the reset puts every corrected row
    (all levels, turnout included) back to the count before the recounts."""
    s = world.session
    eid = world.second
    before = _snapshot(s, eid)
    cfg = RecountConfig(
        thresholds={
            RaceType.HOUSE: RecountThreshold(margin_pct=100.0),
            RaceType.PRESIDENT_PROVINCE: RecountThreshold(margin_pct=100.0),
        }
    )
    finalize_election(s, eid, recount_config=cfg)
    s.commit()
    corrected = _snapshot(s, eid)
    assert corrected["recounts"] == HOUSE_SEATS + 12
    assert corrected["results"] != before["results"]
    res = reset_election(s, eid)
    s.commit()
    s.expire_all()
    assert res.changed.get("recounts_reversed") == HOUSE_SEATS + 12
    after = _snapshot(s, eid)
    for key in before:
        assert after[key] == before[key], key
    report = validate_system(s)
    assert report.ok, report


def test_scheduled_election_cannot_be_reset(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    el = s.get(Election, world.second)
    el.status = ElectionStatus.SCHEDULED.value
    s.flush()
    ok, why = can_reset(s, el.id)
    assert not ok and "not been simulated" in why
