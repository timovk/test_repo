"""FICTIONAL candidate generation."""

from __future__ import annotations

import unicodedata

import numpy as np
import pytest

from app.core.constitution import RaceType
from app.core.rng import make_rng
from app.scenarios.schema import CandidateSpec, ContestRule, DownBallotSpec
from app.simulation import names as N
from app.simulation.candidates import (
    fictional_name,
    generate_down_ballot,
    generate_race_candidates,
    slugify,
)
from app.simulation.races import governor_slots, house_slots, mayor_slots


def _plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch)).replace("ı", "i")


def test_fictional_names_are_deterministic_and_unblocked():
    a = [fictional_name(make_rng(1, "n", i)) for i in range(300)]
    b = [fictional_name(make_rng(1, "n", i)) for i in range(300)]
    assert a == b
    assert len(set(a)) > 200
    assert all(_plain(f"{f} {last}") not in N.BLOCKED_FULL_NAMES for f, last in a)
    assert any(
        last.split()[0] in {"van", "de", "ter", "van der", "van den", "van 't"}
        or last.startswith(("van ", "de ", "ter "))
        for _, last in a
    )
    females = [fictional_name(make_rng(2, "f", i), "F")[0] for i in range(50)]
    assert set(females) <= set(N.GIVEN_NAMES_FEMALE) | set(N.GIVEN_NAMES_FEMALE_HERITAGE)
    north = [fictional_name(make_rng(3, "p", i), "M", province="FR")[1] for i in range(200)]
    assert sum(n in N.SURNAMES_NORTH for n in north) > 40


def test_slugify():
    assert slugify("Fleur van 't Hof") == "fleur-van-t-hof"
    assert slugify("Elif Yılmaz") == "elif-yilmaz"
    assert slugify("Çelik Öztürk") == "celik-ozturk"


def test_generate_race_candidates_rules(small_model, frame):
    units = frame.units_in_province(frame.province_index("NB"))
    rules = DownBallotSpec(
        contest_rules={"LEFT": ContestRule(always=True), "MINOR": ContestRule(min_expected_share=0.5)},
        default_rule=ContestRule(min_expected_share=0.05),
        max_candidates=4,
        independents_prob=0.0,
    )
    rc = generate_race_candidates(
        small_model, "GOV-NB", RaceType.GOVERNOR, units, rules, seed=9, province_code="NB"
    )
    parties = [c.party for c in rc.candidates]
    assert "LEFT" in parties and "MINOR" not in parties
    assert 2 <= len(rc.lines) <= 4
    assert [ln.key for ln in rc.lines] == [c.key for c in rc.candidates]
    for c in rc.candidates:
        assert c.home_municipality is not None
        m = frame.muni_index(c.home_municipality)
        assert frame.muni_province[m] == frame.province_index("NB")
        assert c.home_province == "NB"
        age = 2028 - c.birth_date.year
        assert 36 <= age <= 72
        assert "Fictional" in c.bio
    again = generate_race_candidates(
        small_model, "GOV-NB", RaceType.GOVERNOR, units, rules, seed=9, province_code="NB"
    )
    assert [c.model_dump() for c in again.candidates] == [c.model_dump() for c in rc.candidates]
    other = generate_race_candidates(
        small_model, "GOV-NB", RaceType.GOVERNOR, units, rules, seed=10, province_code="NB"
    )
    assert [c.key for c in other.candidates] != [c.key for c in rc.candidates]


def test_region_and_province_rules(small_model, frame, make_doc, synth_regions):
    from app.simulation.structural import StructuralModel

    m = StructuralModel.build(frame, make_doc(), regions=synth_regions)
    rules = DownBallotSpec(
        contest_rules={
            "MINOR": ContestRule(min_expected_share=0.0, regions=["south"]),
            "RURAL": ContestRule(min_expected_share=0.0, provinces=["DR"]),
        },
        default_rule=ContestRule(always=True),
        max_candidates=6,
        independents_prob=0.0,
    )
    south = generate_race_candidates(
        m, "GOV-LI", RaceType.GOVERNOR, frame.units_in_province(11), rules, seed=1, province_code="LI"
    )
    north = generate_race_candidates(
        m, "GOV-GR", RaceType.GOVERNOR, frame.units_in_province(0), rules, seed=1, province_code="GR"
    )
    drenthe = generate_race_candidates(
        m, "GOV-DR", RaceType.GOVERNOR, frame.units_in_province(2), rules, seed=1, province_code="DR"
    )
    assert "MINOR" in south.contested_parties and "MINOR" not in north.contested_parties
    assert "RURAL" in drenthe.contested_parties and "RURAL" not in south.contested_parties


def test_incumbents_explicit_candidates_and_independents(small_model, frame):
    units = frame.units_in_province(frame.province_index("UT"))
    inc = CandidateSpec(
        key="sitting-governor",
        first_name="Ina",
        last_name="Zittend",
        party="RIGHT",
        home_municipality="GM0701",
        quality=0.9,
    )
    always = DownBallotSpec(incumbent_runs_again_prob=1.0, independents_prob=1.0, max_candidates=5)
    rc = generate_race_candidates(
        small_model, "GOV-UT", RaceType.GOVERNOR, units, always, seed=3, incumbent=inc
    )
    assert rc.incumbent_running
    assert any(ln.incumbent and ln.key == "sitting-governor" for ln in rc.lines)
    assert sum(1 for c in rc.candidates if c.party is None) == 1
    assert sum(1 for c in rc.candidates if c.party == "RIGHT") == 1
    never = DownBallotSpec(incumbent_runs_again_prob=0.0, independents_prob=0.0)
    rc2 = generate_race_candidates(
        small_model, "GOV-UT", RaceType.GOVERNOR, units, never, seed=3, incumbent=inc
    )
    assert not rc2.incumbent_running and "sitting-governor" not in [c.key for c in rc2.candidates]
    explicit = DownBallotSpec(explicit_candidates={"GOV-UT": ["left-pres", "ind-cand"]})
    lookup = {c.key: c for c in small_model.scenario.candidates}
    rc3 = generate_race_candidates(
        small_model, "GOV-UT", RaceType.GOVERNOR, units, explicit, seed=3, scenario_candidates=lookup
    )
    assert [c.key for c in rc3.candidates] == ["left-pres", "ind-cand"]
    from app.core.errors import ScenarioError

    with pytest.raises(ScenarioError):
        generate_race_candidates(
            small_model,
            "GOV-UT",
            RaceType.GOVERNOR,
            units,
            DownBallotSpec(explicit_candidates={"GOV-UT": ["ghost"]}),
            seed=3,
        )


def test_batch_generation_unique_and_reproducible(demo_model, frame):
    ud = np.array(
        [f"{frame.province_codes[p]}-{(i % 3) + 1:02d}" for i, p in enumerate(frame.unit_province)],
        dtype=object,
    )
    slots = house_slots(frame, ud) + governor_slots(frame) + mayor_slots(frame, frame.muni_codes[:20])
    a = generate_down_ballot(demo_model, slots, seed=77)
    b = generate_down_ballot(demo_model, slots, seed=77)
    keys = [c.key for rc in a.values() for c in rc.candidates]
    assert len(keys) == len(set(keys))
    assert keys == [c.key for rc in b.values() for c in rc.candidates]
    assert all(len(rc.lines) >= 2 for rc in a.values())
    # parties marked `always` in the demo scenario contest every House race
    house = [rc for k, rc in a.items() if k.startswith("HOUSE-")]
    for code in ("PA", "VLP", "NVB", "CVU"):
        assert all(code in rc.contested_parties for rc in house)
    # the scenario's own candidates never collide with generated keys
    assert not set(keys) & {c.key for c in demo_model.scenario.candidates}
