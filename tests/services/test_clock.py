"""The world clock (app.services.clock): one *today*, rolled from election day to election day —
watch or count each day, skip ahead, read the news."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.constitution import ElectionType
from app.core.errors import ElectionError
from app.models import Election
from app.services import clock
from app.services.runtime import get_frame


def test_rolling_through_the_calendar(world, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from app.simulation import people as people_mod

    s = world.session
    frame = get_frame(s)
    path = tmp_path / "people.yaml"
    path.write_text(
        f"people:\n  - {{name: Sanne de Vries, home: {frame.muni_names[0]}, born: 1980, chance: always, "
        "offices: [school_board, water_board]}\n"
    )
    monkeypatch.setattr(people_mod, "people_path", lambda: path)

    second = s.get(Election, world.second)
    clock.set_today(s, second.election_date)
    assert clock.today(s) == second.election_date
    assert [e.id for e in clock.elections_on(s, clock.today(s))] == [second.id]
    with pytest.raises(ElectionError, match="not finished yet"):
        clock.advance(s)
    assert clock.count_today(s) == [second.id]
    assert s.get(Election, second.id).status == "final"

    # the next election day is a combined local election of every province voting that day
    nxt = clock.next_election_day(s, clock.today(s))
    assert nxt is not None and nxt.kind == "local" and nxt.election_id is None and nxt.provinces
    arrived = clock.advance(s)
    assert arrived["today"] == nxt.date.isoformat() and clock.today(s) == nxt.date
    local = s.get(Election, arrived["election_id"])
    assert local.election_type == ElectionType.LOCAL.value and local.provinces.split(",") == nxt.provinces
    assert local.status == "scheduled"
    clock.prepare_today(s)  # watch: simulated, ready for its night
    assert s.get(Election, local.id).status == "simulated"
    assert clock.count_today(s) == [local.id]  # … or count it instantly

    # skip ahead: every election day before the target is counted, the 2030 midterm generated
    target = date(2030, 12, 1)
    out = clock.skip_to(s, target)
    s.commit()
    assert clock.today(s) == target and out["counted"]
    held = list(
        s.scalars(select(Election).where(Election.election_date > nxt.date, Election.election_date < target))
    )
    assert held and all(e.status == "final" for e in held)
    mid = next(e for e in held if e.year == 2030 and e.election_type == ElectionType.MIDTERM.value)
    assert mid.id in out["counted"]
    with pytest.raises(ElectionError, match="only moves forward"):
        clock.skip_to(s, target - timedelta(days=1))

    news = clock.news(s, second.election_date, target)
    kinds = {n["kind"] for n in news}
    assert "result" in kinds and [n["date"] for n in news] == sorted(n["date"] for n in news)
    assert any(n["kind"] == "person" and "Sanne de Vries" in n["text"] for n in news)
    assert all(n["text"].endswith(".") for n in news)

    # the agenda: what is coming in the next year (planned, not stored yet)
    ahead = clock.agenda(s, target, target + timedelta(days=365))
    assert ahead and all(d.election_id is None for d in ahead if d.kind == "local")
    assert any(
        d.kind == "general" and d.date.year == 2032 for d in clock.agenda(s, target, date(2032, 12, 31))
    )
