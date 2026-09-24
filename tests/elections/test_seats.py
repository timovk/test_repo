from __future__ import annotations

import numpy as np
import pytest

from app.core.constitution import HOUSE_MAJORITY, HOUSE_SEATS, SENATE_MAJORITY, SENATE_SEATS
from app.core.errors import ElectionError
from app.elections.seats import (
    INDEPENDENT,
    chamber_composition,
    chamber_control,
    council_size,
    dhondt,
    house_composition,
    largest_remainder,
    merge_senate_seats,
    minimal_winning_coalitions,
    municipal_council_size,
    net_change,
    provincial_legislature_size,
    sainte_lague,
    senate_composition,
)

WIKI = {"A": 100_000, "B": 80_000, "C": 30_000, "D": 20_000}


def test_dhondt_known_example() -> None:
    assert dhondt(WIKI, 8) == {"A": 4, "B": 3, "C": 1, "D": 0}
    assert dhondt(WIKI, 0) == {"A": 0, "B": 0, "C": 0, "D": 0}
    assert sum(dhondt(WIKI, 45).values()) == 45


def test_sainte_lague_known_example() -> None:
    assert sainte_lague(WIKI, 8) == {"A": 3, "B": 3, "C": 1, "D": 1}
    assert sainte_lague(WIKI, 8, first_divisor=1.4) == {"A": 3, "B": 3, "C": 1, "D": 1}


def test_dhondt_sequential_equivalence() -> None:
    """Matrix method == seat-by-seat allocation on random inputs (no exact ties)."""
    from app.core.rng import make_rng

    rng = make_rng(3, "dhondt-test")
    for _ in range(50):
        votes = {f"P{i}": int(v) for i, v in enumerate(rng.integers(1, 100_000, size=7))}
        seats = int(rng.integers(1, 60))
        alloc = dict.fromkeys(votes, 0)
        for _ in range(seats):
            best = max(votes, key=lambda k: votes[k] / (alloc[k] + 1))
            alloc[best] += 1
        assert dhondt(votes, seats) == alloc


def test_threshold_excludes_small_parties() -> None:
    assert dhondt(WIKI, 8, threshold=0.10) == {"A": 4, "B": 3, "C": 1, "D": 0}
    res = dhondt(WIKI, 10, threshold=0.10)  # D (8.7 %) excluded
    assert res["D"] == 0 and sum(res.values()) == 10
    with pytest.raises(ElectionError):
        dhondt({"A": 0, "B": 0}, 3)


def test_tie_for_last_seat_decided_by_seeded_lot() -> None:
    votes = {"A": 100, "B": 100}
    a = dhondt(votes, 1, tie_seed=4, lot_key="COUNCIL-GM0101")
    assert a == dhondt(votes, 1, tie_seed=4, lot_key="COUNCIL-GM0101")
    assert sum(a.values()) == 1
    winners = {next(k for k, v in dhondt(votes, 1, tie_seed=s).items() if v) for s in range(30)}
    assert winners == {"A", "B"}
    assert dhondt(votes, 2) == {"A": 1, "B": 1}


def test_largest_remainder_known_example() -> None:
    votes = {"A": 47_000, "B": 16_000, "C": 15_800, "D": 12_000, "E": 6_100, "F": 3_100}
    assert largest_remainder(votes, 10) == {"A": 5, "B": 2, "C": 1, "D": 1, "E": 1, "F": 0}
    assert sum(largest_remainder(votes, 25).values()) == 25


@pytest.mark.parametrize(
    ("population", "seats"),
    [
        (0, 9),
        (3_000, 9),
        (3_001, 11),
        (6_000, 11),
        (10_000, 13),
        (10_001, 15),
        (15_001, 17),
        (20_001, 19),
        (25_001, 21),
        (30_001, 23),
        (35_001, 25),
        (40_001, 27),
        (45_001, 29),
        (50_000, 29),
        (50_001, 31),
        (60_001, 33),
        (70_001, 35),
        (80_001, 37),
        (100_000, 37),
        (100_001, 39),
        (200_000, 39),
        (200_001, 45),
        (935_000, 45),
    ],
)
def test_municipal_council_size_brackets(population: int, seats: int) -> None:
    assert municipal_council_size(population) == seats == council_size(population)


@pytest.mark.parametrize(
    ("population", "seats"),
    [
        (0, 39),
        (390_000, 39),  # Zeeland
        (400_000, 39),
        (400_001, 41),
        (500_000, 41),
        (500_001, 43),  # Drenthe crossed 500,000 → 43 seats in 2023
        (590_000, 43),  # Groningen
        (750_000, 43),
        (750_001, 45),
        (1_000_000, 45),
        (1_000_001, 47),
        (1_250_000, 47),
        (1_250_001, 49),
        (1_370_000, 49),  # Utrecht
        (1_500_001, 51),
        (1_750_001, 53),
        (2_000_000, 53),
        (2_000_001, 55),
        (3_800_000, 55),
    ],
)
def test_provincial_legislature_size_brackets(population: int, seats: int) -> None:
    assert provincial_legislature_size(population) == seats


def test_sizes_are_odd_and_monotone() -> None:
    pops = np.arange(0, 1_000_000, 997)
    sizes = [municipal_council_size(int(p)) for p in pops]
    assert all(s % 2 == 1 for s in sizes) and sizes == sorted(sizes)
    with pytest.raises(ValueError):
        municipal_council_size(-1)


def test_house_composition_and_control() -> None:
    winners = {f"D{i:03d}": "RLP" for i in range(80)} | {f"D{i:03d}": "GVP" for i in range(80, 140)}
    winners |= {f"D{i:03d}": None for i in range(140, 150)}
    comp = house_composition(winners)
    assert comp == {"RLP": 80, "GVP": 60, INDEPENDENT: 10}
    assert list(comp) == ["RLP", "GVP", INDEPENDENT]
    ctl = chamber_control(comp, HOUSE_SEATS, HOUSE_MAJORITY)
    assert ctl.controlling_party == "RLP" and not ctl.hung and ctl.seats_short == 0
    assert ctl.coalition_hints == [("RLP",)] and ctl.vacant == 0
    assert ctl.label == "RLP control (80/150)"


def test_hung_chamber_and_coalition_hints() -> None:
    comp = {"AAA": 50, "BBB": 40, "CCC": 30, "DDD": 20, "EEE": 10}
    ctl = chamber_control(comp, HOUSE_SEATS, HOUSE_MAJORITY)
    assert ctl.hung and ctl.controlling_party is None
    assert ctl.largest_party == "AAA" and ctl.seats_short == 26
    assert ctl.coalition_hints[0] == ("AAA", "CCC")  # 80 seats: fewest parties, fewest seats
    assert ("AAA", "BBB") in ctl.coalition_hints
    for hint in ctl.coalition_hints:
        seats = sum(comp[p] for p in hint)
        assert seats >= 76 and all(seats - comp[p] < 76 for p in hint)
    assert "No majority" in ctl.label
    tied = chamber_control({"X": 12, "Y": 12}, SENATE_SEATS, SENATE_MAJORITY)
    assert tied.largest_tied and tied.hung and tied.seats_short == 1
    assert minimal_winning_coalitions({"X": 12, "Y": 12, INDEPENDENT: 3}, 13) == [("X", "Y")]
    with pytest.raises(ElectionError):
        chamber_control({"X": 200}, HOUSE_SEATS, HOUSE_MAJORITY)


def test_net_change() -> None:
    assert net_change({"A": 80, "B": 60, "C": 10}, {"A": 70, "B": 75, "D": 5}) == {
        "A": 10,
        "B": -15,
        "C": 10,
        "D": -5,
    }


def test_senate_composition() -> None:
    holdovers = {f"SEN-{p}-{c}": "RLP" for p in ("GR", "FR", "DR", "OV") for c in (1, 2)}
    holdovers |= {f"SEN-{p}-{c}": "GVP" for p in ("FL", "GE", "UT", "NH") for c in (1, 2)}
    results = {
        f"SEN-{p}-{c}": ("GVP" if p != "LI" else None) for p in ("ZH", "ZE", "NB", "LI") for c in (1, 2)
    }
    comp = senate_composition(holdovers, results)
    assert comp == {"GVP": 14, "RLP": 8, INDEPENDENT: 2}
    assert sum(comp.values()) == SENATE_SEATS
    assert chamber_control(comp, SENATE_SEATS, SENATE_MAJORITY).controlling_party == "GVP"
    assert len(merge_senate_seats(holdovers, results)) == 24
    with pytest.raises(ElectionError):
        senate_composition(holdovers, {"SEN-GR-1": "GVP"})
    assert chamber_composition(["A", "A", None]) == {"A": 2, INDEPENDENT: 1}


@pytest.mark.realdata
def test_council_sizes_on_real_municipalities(real_frame) -> None:  # type: ignore[no-untyped-def]
    sizes = [municipal_council_size(int(p)) for p in real_frame.muni_population]
    assert len(sizes) == real_frame.n_munis
    assert min(sizes) >= 9 and max(sizes) == 45
    if "GM0363" in real_frame.muni_codes:  # Amsterdam
        assert sizes[real_frame.muni_index("GM0363")] == 45
    prov = dict(
        zip(
            real_frame.province_codes,
            (provincial_legislature_size(int(p)) for p in real_frame.province_population()),
            strict=True,
        )
    )
    # the statutory brackets reproduce the real Staten sizes of the four provinces above 2 million,
    # and every province is within the statutory range
    assert all(prov[p] == 55 for p in ("GE", "NH", "ZH", "NB"))
    assert all(39 <= s <= 55 and s % 2 == 1 for s in prov.values())
