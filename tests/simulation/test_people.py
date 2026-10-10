"""Custom people of config/people.yaml (app.simulation.people): parsing, municipality lookup and
who runs where."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from app.core.constitution import RaceType
from app.core.errors import ScenarioError
from app.geography.synthetic import SyntheticGeography
from app.simulation.people import (
    PeopleSlot,
    assign_people,
    load_people,
    parse_people,
    resolve_municipality,
)

PARTIES = {"PA": (-0.6, -0.7, 0.6), "VLP": (0.7, -0.1, 0.2)}


def _home(synthetic: SyntheticGeography, i: int = 0) -> tuple[str, str]:
    f = synthetic.frame
    return f.muni_codes[i], f.muni_names[i]


def test_parse_and_resolve(synthetic: SyntheticGeography) -> None:
    code, name = _home(synthetic, 3)
    text = f"""
people:
  - name: Sanne de Vries
    home: {name.upper()}
    born: 1990
    party: pa
    chance: often
  - first_name: Riet
    last_name: van den Berg
    home: {code.lower()}
    born: 1952-03-08
    chance: 0.2
    offices: [school_board, council]
"""
    cfg = parse_people(text, synthetic.frame)
    a, b = cfg.people
    assert (a.first_name, a.last_name, a.key) == ("Sanne", "de Vries", "person-sanne-de-vries")
    assert a.home == code and a.party == "PA" and a.chance == 0.65 and a.birth_date == date(1990, 7, 1)
    assert b.home == code and b.offices == frozenset({RaceType.SCHOOL_BOARD, RaceType.COUNCIL_SEAT})
    assert a.age_on(date(2028, 11, 8)) == 38 and b.age_on(date(2028, 3, 7)) == 75
    spec = a.spec(PARTIES)
    assert spec.party == "PA" and spec.home_municipality == code and spec.ideology is not None
    assert resolve_municipality(synthetic.frame, name) == code


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("people:\n  - name: Solo\n    home: X\n", "unknown municipality|first and a last name"),
        ("people:\n  - name: A B\n    home: Nowhere-at-all\n", "unknown municipality"),
        ("people:\n  - name: A B\n    home: GM9999\n", "no municipality with code"),
        ("people:\n  - name: A B\n    home: {h}\n    chance: 2\n", "chance"),
        ("people:\n  - name: A B\n    home: {h}\n    offices: [king]\n", "unknown office kind"),
        ("people:\n  - name: A B\n    home: {h}\n  - name: A B\n    home: {h}\n", "share the key"),
        ("people:\n  - home: {h}\n", "give a name"),
    ],
)
def test_bad_files_are_explained(synthetic: SyntheticGeography, text: str, match: str) -> None:
    with pytest.raises(ScenarioError, match=match):
        parse_people(text.format(h=_home(synthetic)[1]), synthetic.frame)


def test_missing_file_means_nobody(synthetic: SyntheticGeography, tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert not load_people(synthetic.frame, tmp_path / "people.yaml")


def test_assignment_rules(synthetic: SyntheticGeography) -> None:
    f = synthetic.frame
    home, name = _home(synthetic)
    other = next(c for c in f.muni_codes if c != home)
    text = f"""
people:
  - {{name: Always Here, home: {name}, born: 1980, party: PA, chance: always}}
  - {{name: Never Here, home: {name}, born: 1980, chance: never}}
  - {{name: Too Young, home: {name}, born: 2015, chance: always}}
  - {{name: Board Only, home: {name}, born: 1970, chance: always, offices: [school_board]}}
  - {{name: Lone Wolf, home: {name}, born: 1975, chance: always, offices: [mayor]}}
  - {{name: Wrong Party, home: {name}, born: 1975, party: XX, chance: always, offices: [house]}}
"""
    cfg = parse_people(text, f)
    slots = [
        PeopleSlot("SB-" + home, RaceType.SCHOOL_BOARD, frozenset({home})),
        PeopleSlot("MAYOR-" + home, RaceType.MAYOR, frozenset({home})),
        PeopleSlot("HOUSE-X", RaceType.HOUSE, frozenset({home, other})),
        PeopleSlot("SB-" + other, RaceType.SCHOOL_BOARD, frozenset({other})),
    ]
    on = date(2029, 2, 7)
    a = assign_people(cfg, slots, seed=11, on=on, parties=PARTIES)
    by_person = {c.key: race for race, cs in a.races.items() for c in cs}
    assert "person-never-here" not in by_person and "person-too-young" not in by_person
    assert "person-wrong-party" not in by_person  # its party is not in this election, House only
    assert by_person["person-board-only"] == "SB-" + home
    assert by_person["person-lone-wolf"] == "MAYOR-" + home
    lone = next(c for c in a.races["MAYOR-" + home] if c.key == "person-lone-wolf")
    assert lone.party is None  # no party: runs as an independent
    assert by_person["person-always-here"] in {"SB-" + home, "MAYOR-" + home, "HOUSE-X"}
    assert all(race != "SB-" + other for race in by_person.values())  # only where they live
    assert assign_people(cfg, slots, seed=11, on=on, parties=PARTIES).races == a.races  # deterministic
    excluded = assign_people(cfg, slots, seed=11, on=on, parties=PARTIES, exclude={"person-always-here"})
    assert "person-always-here" not in excluded.keys
    # over many elections an "often" person runs in about 65 % of them
    often = parse_people(f"people:\n  - {{name: Often Runner, home: {name}, chance: often}}\n", f)
    ran = [len(assign_people(often, slots, seed=s, on=on, parties=PARTIES)) for s in range(400)]
    assert 0.55 < np.mean(ran) < 0.75
