"""House apportionment: all methods, constitutional totals, minimum seats, ties, paradoxes."""

from __future__ import annotations

import math

import pytest

from app.core.constitution import (
    ELECTORAL_VOTES,
    HOUSE_SEATS,
    PROVINCE_COUNT,
    SENATE_SEATS,
    ConstitutionConfig,
)
from app.core.errors import ApportionmentError
from app.districts.apportionment import METHODS, apportion, canonical_method, compare_methods

#: CBS 2025 neighbourhood population totals per province (rounded; REAL order of magnitude).
REAL_2025 = {
    "GR": 602_850, "FR": 664_280, "DR": 506_515, "OV": 1_195_810, "FL": 456_375, "GE": 2_161_200,
    "UT": 1_409_195, "NH": 2_992_105, "ZH": 3_863_380, "ZE": 392_950, "NB": 2_664_165, "LI": 1_135_295,
}  # fmt: skip


@pytest.mark.parametrize("method", [*METHODS, "sainte_lague", "dhondt"])
def test_totals_for_every_method(method: str) -> None:
    r = apportion(REAL_2025, HOUSE_SEATS, method)
    assert len(r.seats) == PROVINCE_COUNT
    assert sum(r.seats.values()) == HOUSE_SEATS
    assert r.total_electoral_votes == ELECTORAL_VOTES == HOUSE_SEATS + SENATE_SEATS
    assert all(r.electoral_votes[c] == r.seats[c] + 2 for c in REAL_2025)
    assert all(s >= 1 for s in r.seats.values())
    assert math.isclose(sum(r.quotas.values()), HOUSE_SEATS)
    assert len(r.priority_order) == HOUSE_SEATS - PROVINCE_COUNT or method == "hamilton"
    assert len(r.first_out) == 5 if method != "hamilton" else len(r.first_out) <= 5
    df = r.to_frame()
    assert list(df["province_code"]) == list(REAL_2025)
    assert r.to_dict()["total_electoral_votes"] == ELECTORAL_VOTES


def test_real_2025_huntington_hill() -> None:
    r = apportion(REAL_2025, HOUSE_SEATS)
    assert r.method == "huntington_hill"
    assert r.seats == {
        "GR": 5, "FR": 6, "DR": 4, "OV": 10, "FL": 4, "GE": 18,
        "UT": 12, "NH": 25, "ZH": 32, "ZE": 3, "NB": 22, "LI": 9,
    }  # fmt: skip
    # the next seat would go to Limburg (the "first province out")
    assert r.first_out[0].province == "LI"
    assert r.first_out[0].rank == HOUSE_SEATS + 1
    # quotas are exact proportional shares
    assert math.isclose(r.quotas["ZH"], REAL_2025["ZH"] * 150 / sum(REAL_2025.values()))
    assert math.isclose(r.persons_per_seat["ZE"], REAL_2025["ZE"] / 3)


def test_huntington_hill_hand_computed_example() -> None:
    # A=100, B=80, C=30, 7 seats.  After one seat each, priorities P/sqrt(n(n+1)):
    #   seat 4: A 100/√2=70.71 · seat 5: B 80/√2=56.57 · seat 6: A 100/√6=40.82 · seat 7: B 80/√6=32.66
    #   next: A 100/√12=28.87 (first out)
    r = apportion({"A": 100, "B": 80, "C": 30}, 7, "huntington_hill")
    assert r.seats == {"A": 3, "B": 3, "C": 1}
    assert [s.province for s in r.priority_order] == ["A", "B", "A", "B"]
    assert [s.rank for s in r.priority_order] == [4, 5, 6, 7]
    expected = [100 / math.sqrt(2), 80 / math.sqrt(2), 100 / math.sqrt(6), 80 / math.sqrt(6)]
    assert all(math.isclose(s.priority, e) for s, e in zip(r.priority_order, expected, strict=True))
    assert r.first_out[0].province == "A"
    assert math.isclose(r.first_out[0].priority, 100 / math.sqrt(12))
    assert r.first_out[0].province_seat == 4


def test_divisor_methods_differ_as_expected() -> None:
    # Jefferson/D'Hondt favours large provinces, Adams small ones.
    pops = {"BIG": 1_000_000, "MID": 180_000, "SMALL": 60_000}
    j = apportion(pops, 10, "jefferson", min_seats=0)
    a = apportion(pops, 10, "adams", min_seats=1)
    w = apportion(pops, 10, "webster", min_seats=0)
    assert j.seats["BIG"] >= w.seats["BIG"] >= a.seats["BIG"]
    assert a.seats["SMALL"] >= w.seats["SMALL"] >= j.seats["SMALL"]
    assert sum(j.seats.values()) == sum(a.seats.values()) == 10


def test_minimum_seat_guarantee() -> None:
    pops = {"A": 10_000_000, "B": 1_000, "C": 500}
    for method in METHODS:
        r = apportion(pops, 20, method, min_seats=1)
        assert r.seats["B"] >= 1 and r.seats["C"] >= 1, method
        assert sum(r.seats.values()) == 20
    r2 = apportion(pops, 20, "huntington_hill", min_seats=2)
    assert min(r2.seats.values()) == 2
    h = apportion(pops, 20, "hamilton", min_seats=2)
    assert h.seats == {"A": 16, "B": 2, "C": 2}


def test_deterministic_ties() -> None:
    # Equal populations: the tie is broken by code (ascending).
    r = apportion({"B": 100, "A": 100, "C": 100}, 4, "huntington_hill")
    assert r.seats == {"B": 1, "A": 2, "C": 1}
    # Equal priority but different populations cannot occur for a single divisor step, so check
    # that repeated calls are identical and independent of dict order.
    r2 = apportion({"C": 100, "A": 100, "B": 100}, 4, "huntington_hill")
    assert r2.seats == {"C": 1, "A": 2, "B": 1}
    h = apportion({"B": 50, "A": 50}, 3, "hamilton", min_seats=0)
    assert h.seats == {"B": 1, "A": 2}


def test_hamilton_alabama_paradox() -> None:
    # The classic demonstration: adding a seat to the House costs C a seat under Hamilton.
    pops = {"A": 6, "B": 6, "C": 2}
    r10 = apportion(pops, 10, "hamilton", min_seats=0)
    r11 = apportion(pops, 11, "hamilton", min_seats=0)
    assert r10.seats == {"A": 4, "B": 4, "C": 2}
    assert r11.seats == {"A": 5, "B": 5, "C": 1}
    # Divisor methods are house-monotone: Huntington-Hill never loses a seat when the House grows.
    for s in range(3, 30):
        a, b = apportion(pops, s, "huntington_hill", 0), apportion(pops, s + 1, "huntington_hill", 0)
        assert all(b.seats[c] >= a.seats[c] for c in pops)


def test_electoral_votes_follow_constitution() -> None:
    cons = ConstitutionConfig(senators_per_province=3, senate_classes=3)
    r = apportion(REAL_2025, 150, constitution=cons)
    assert all(r.electoral_votes[c] == r.seats[c] + 3 for c in REAL_2025)
    assert r.total_electoral_votes == 150 + 36


def test_invalid_inputs() -> None:
    with pytest.raises(ApportionmentError):
        apportion({"A": 10, "B": 10}, 1, min_seats=1)  # infeasible minimum
    with pytest.raises(ApportionmentError):
        apportion({"A": -1, "B": 10}, 5)
    with pytest.raises(ApportionmentError):
        apportion({"A": 0, "B": 0}, 5)
    with pytest.raises(ApportionmentError):
        apportion({"A": 10, "B": 10}, 5, "adams", min_seats=0)
    with pytest.raises(ApportionmentError):
        apportion({}, 5)
    with pytest.raises(ApportionmentError):
        canonical_method("borda")
    assert canonical_method("Sainte-Lague") == "webster"
    assert canonical_method("DHONDT") == "jefferson"


def test_zero_population_province_gets_minimum_only() -> None:
    r = apportion({"A": 1000, "B": 0, "C": 500}, 9, "huntington_hill", min_seats=1)
    assert r.seats["B"] == 1
    assert sum(r.seats.values()) == 9


def test_compare_methods_table() -> None:
    df = compare_methods(REAL_2025, 150)
    assert list(df.columns) == ["population", *METHODS]
    assert (df[list(METHODS)].sum() == 150).all()
