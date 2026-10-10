"""Endless regular elections (app.services.continuation): after the built-in scenarios run out,
every next November election is generated from the elections before it — and custom people of
config/people.yaml run in it where they live."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.constitution import ElectionType, RaceType
from app.models import BallotCandidate, Election, Race, Scenario
from app.services.continuation import ensure_regular_election, previous_house_shares
from app.services.elections import instant_finalize
from app.services.runtime import get_frame, parse_stored_scenario


def _doc(session, el: Election):  # type: ignore[no-untyped-def]
    return parse_stored_scenario(session.get(Scenario, el.scenario_id).document)


def test_the_next_elections_are_generated(world, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from app.simulation import people as people_mod

    s = world.session
    frame = get_frame(s)
    home = frame.muni_names[0]
    path = tmp_path / "people.yaml"
    path.write_text(
        f"people:\n  - {{name: Sanne de Vries, home: {home}, born: 1980, party: PA, chance: always, offices: [house]}}\n"
    )
    monkeypatch.setattr(people_mod, "people_path", lambda: path)

    second = s.get(Election, world.second)
    assert second.year == 2028
    instant_finalize(s, world.second)
    shares = previous_house_shares(s, second.election_date.replace(year=2031))
    assert shares and abs(sum(shares.values()) - 1) < 1e-9

    mid = ensure_regular_election(s, 2030)
    assert (mid.year, mid.election_type, mid.status) == (2030, ElectionType.MIDTERM.value, "scheduled")
    assert ensure_regular_election(s, 2030).id == mid.id  # created once
    doc = _doc(s, mid)
    assert doc.scenario.slug == "auto-2030" and doc.scenario.year == 2030
    nat = doc.calibration.national
    assert set(nat) == {p.code for p in doc.parties} and abs(sum(nat.values()) - 1) < 1e-3
    assert not doc.president.tickets and not doc.party_events
    types = {r.race_type for r in s.scalars(select(Race).where(Race.election_id == mid.id))}
    assert RaceType.HOUSE.value in types and RaceType.PRESIDENT.value not in types
    # the custom person runs for the House where she lives
    lines = s.execute(
        select(BallotCandidate.line_key, BallotCandidate.party_code_snapshot, Race.code)
        .join(Race, Race.id == BallotCandidate.race_id)
        .where(Race.election_id == mid.id, BallotCandidate.line_key == "person-sanne-de-vries")
    ).all()
    assert len(lines) == 1 and lines[0][1] == "PA" and lines[0][2].startswith("HOUSE-")

    instant_finalize(s, mid.id)
    gen = ensure_regular_election(s, 2032)
    assert (gen.year, gen.election_type) == (2032, ElectionType.GENERAL.value)
    doc = _doc(s, gen)
    tickets = doc.president.tickets
    assert len(tickets) >= 2 and len({t.party for t in tickets}) == len(tickets)
    people = {c.key for c in doc.candidates}
    assert all(t.president in people and t.vice_president in people for t in tickets)
    assert sum(t.incumbent for t in tickets) <= 1
    pres = s.scalars(
        select(Race).where(Race.election_id == gen.id, Race.race_type == RaceType.PRESIDENT.value)
    ).one()
    n_lines = len(s.scalars(select(BallotCandidate).where(BallotCandidate.race_id == pres.id)).all())
    assert n_lines == len(tickets)
    events = doc.environment.events
    assert all(e.get("description", "").startswith("Fictional") for e in events)
    assert json.loads(json.dumps(events)) == events
