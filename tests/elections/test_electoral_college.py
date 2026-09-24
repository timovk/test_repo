from __future__ import annotations

import pytest

from app.core.constitution import (
    ELECTORAL_VOTES,
    PRESIDENTIAL_MAJORITY,
    ConstitutionConfig,
    EVAllocationMethod,
)
from app.core.errors import ElectionError
from app.elections.electoral_college import (
    allocate,
    closest_province,
    ev_margin,
    ev_pv_divergence,
    largest_victory,
    national_popular_votes,
    popular_vote_winner,
    pv_margin,
    tipping_point,
)
from app.elections.tabulation import TabulatedRaceResult, tabulate_totals


def tab(
    key: str, votes: dict[str, int], tie_seed: int | None = None, resolve: bool = True
) -> TabulatedRaceResult:
    return tabulate_totals(key, list(votes), list(votes.values()), tie_seed=tie_seed, resolve_ties=resolve)


def _all(winner_by_province: dict[str, str], ev_map: dict[str, int], lines=("A", "B", "C")) -> dict:
    """Province tabs where the named winner leads 50/30/20."""
    out = {}
    for p in ev_map:
        w = winner_by_province[p]
        others = [ln for ln in lines if ln != w]
        out[p] = tab(f"PRES-{p}", {w: 5000, others[0]: 3000, others[1]: 2000})
    return out


def test_canonical_ev_map_arithmetic(ev_map) -> None:  # type: ignore[no-untyped-def]
    assert sum(ev_map.values()) == ELECTORAL_VOTES == 174
    assert PRESIDENTIAL_MAJORITY == 88


def test_wta_spec_example_noord_brabant() -> None:
    nb = tab("PRES-NB", {"A": 358_100, "B": 357_900, "C": 284_000})
    assert nb.shares[0] * 100 == pytest.approx(35.81)
    assert nb.shares[1] * 100 == pytest.approx(35.79)
    out = allocate({"NB": nb}, {"NB": 24}, constitution=ConstitutionConfig())
    assert out.ev_by_line == {"A": 24, "B": 0, "C": 0}
    assert out.allocations == [("NB", "A", 24)]
    assert out.province_winners == {"NB": "A"}
    assert out.decided_by == {"NB": "popular_vote"}


def test_clear_winner(ev_map) -> None:  # type: ignore[no-untyped-def]
    winners = dict.fromkeys(ev_map, "A")
    winners.update({"GR": "B", "FR": "B", "LI": "C"})
    out = allocate(_all(winners, ev_map), ev_map)
    assert out.total_ev == 174 and out.majority == 88
    assert out.ev_by_line == {"A": 174 - 7 - 8 - 11, "B": 15, "C": 11}
    assert out.total_allocated == 174
    assert out.winner == "A" and not out.needs_contingent
    assert out.ranking == ["A", "B", "C"]
    assert ev_margin(out) == out.ev_margin == 148 - 15


def test_exact_87_87_tie_needs_contingent(ev_map) -> None:  # type: ignore[no-untyped-def]
    a_prov = {"ZH", "NH", "UT", "OV"}  # 34 + 27 + 14 + 12 = 87
    winners = {p: ("A" if p in a_prov else "B") for p in ev_map}
    out = allocate(_all(winners, ev_map), ev_map)
    assert out.ev_by_line["A"] == 87 and out.ev_by_line["B"] == 87
    assert out.winner is None and out.needs_contingent
    assert out.leader is None and ev_margin(out) == 0


def test_three_way_race_without_majority(ev_map) -> None:  # type: ignore[no-untyped-def]
    winners = {}
    for p in ev_map:
        winners[p] = "A" if p in {"ZH", "UT", "GE", "GR"} else "B" if p in {"NH", "NB", "FL"} else "C"
    out = allocate(_all(winners, ev_map), ev_map)
    assert out.ev_by_line == {"A": 75, "B": 57, "C": 42}
    assert out.winner is None and out.needs_contingent and out.leader == "A"
    assert all(v < 88 for v in out.ev_by_line.values())


def test_tipping_point_hand_built_map() -> None:
    ev = {"P1": 10, "P2": 20, "P3": 30, "P4": 15, "P5": 25}  # total 100, majority 51
    tabs = {
        "P1": tab("P1", {"W": 700, "L": 300}),  # W +40
        "P2": tab("P2", {"W": 550, "L": 450}),  # W +10
        "P3": tab("P3", {"W": 520, "L": 480}),  # W +4
        "P4": tab("P4", {"W": 400, "L": 600}),  # W −20
        "P5": tab("P5", {"W": 510, "L": 490}),  # W +2
    }
    # order by W margin: P1 (10) → 10, P2 (20) → 30, P3 (30) → 60 ≥ 51
    p, margin = tipping_point(tabs, ev, "W")
    assert p == "P3" and margin == pytest.approx(4.0)
    # the loser's tipping point: order P4 (+20) 15, P5 (−2) 40, P3 (−4) 70 → P3
    p2, m2 = tipping_point(tabs, ev, "L")
    assert p2 == "P3" and m2 == pytest.approx(-4.0)
    # explicit majority
    assert tipping_point(tabs, ev, "W", majority=30)[0] == "P2"


def test_tipping_point_three_way_margin_over_strongest_opponent() -> None:
    ev = {"X": 3, "Y": 3, "Z": 3}
    tabs = {
        "X": tab("X", {"A": 40, "B": 35, "C": 25}),  # A +5 over B
        "Y": tab("Y", {"A": 30, "B": 20, "C": 50}),  # A −20 vs C
        "Z": tab("Z", {"A": 45, "B": 44, "C": 11}),  # A +1
    }
    p, m = tipping_point(tabs, ev, "A")  # majority 5: X (3) then Z (6)
    assert p == "Z" and m == pytest.approx(1.0)


def test_ev_pv_divergence() -> None:
    ev = {"BIG": 10, "S1": 6, "S2": 6}  # majority 12
    tabs = {
        "BIG": tab("BIG", {"A": 900, "B": 100}),
        "S1": tab("S1", {"A": 190, "B": 210}),
        "S2": tab("S2", {"A": 190, "B": 210}),
    }
    out = allocate(tabs, ev)
    assert out.winner == "B" and out.ev_by_line == {"A": 10, "B": 12}
    pv = national_popular_votes(tabs)
    assert pv == {"A": 1280, "B": 520}
    assert popular_vote_winner(tabs) == "A" == popular_vote_winner(pv)
    div = ev_pv_divergence(out, tabs)
    assert div.diverged and bool(div)
    assert div.popular_vote_winner == "A" and div.ev_winner == "B"
    assert div.pv_margin_votes == 760
    same = allocate({"BIG": tabs["BIG"]}, {"BIG": 10})
    assert not ev_pv_divergence(same, {"BIG": tabs["BIG"]})


def test_district_method_sums_to_province_ev() -> None:
    ev = {"NB": 5, "ZE": 3}
    ptabs = {"NB": tab("NB", {"A": 60, "B": 40}), "ZE": tab("ZE", {"A": 45, "B": 55})}
    dtabs = {
        "NB-01": tab("NB-01", {"A": 30, "B": 10}),
        "NB-02": tab("NB-02", {"A": 10, "B": 20}),
        "NB-03": tab("NB-03", {"A": 20, "B": 10}),
        "ZE-01": tab("ZE-01", {"A": 45, "B": 55}),
    }
    dprov = {d: d[:2] for d in dtabs}
    out = allocate(ptabs, ev, EVAllocationMethod.DISTRICT, dtabs, dprov)
    assert out.provinces["NB"].awards == {"A": 4, "B": 1}
    assert out.provinces["ZE"].awards == {"B": 3}
    for p, n in ev.items():
        assert sum(out.provinces[p].awards.values()) == n
    assert out.total_allocated == 8
    bases = {(a.province_code, a.basis) for a in out.awards}
    assert ("NB", "statewide") in bases and ("NB", "district:NB-02") in bases
    with pytest.raises(ElectionError):
        allocate(ptabs, {"NB": 6, "ZE": 3}, EVAllocationMethod.DISTRICT, dtabs, dprov)
    with pytest.raises(ElectionError):
        allocate(ptabs, ev, "district")


def test_proportional_method_sums_to_province_ev(ev_map) -> None:  # type: ignore[no-untyped-def]
    tabs = {p: tab(p, {"A": 4700 + i, "B": 3100, "C": 1600, "D": 600}) for i, p in enumerate(ev_map)}
    out = allocate(tabs, ev_map, EVAllocationMethod.PROPORTIONAL)
    for p, n in ev_map.items():
        assert sum(out.provinces[p].awards.values()) == n
    assert out.total_allocated == 174
    zh = out.provinces["ZH"].awards  # 34 EV: quotas 16.0, 10.5, 5.4, 2.0 (approximately)
    assert sum(zh.values()) == 34 and zh["A"] >= zh["B"] >= zh["C"] >= zh.get("D", 0)
    assert allocate(
        {"X": tab("X", {"A": 470, "B": 160, "C": 158, "D": 120, "E": 61, "F": 31})}, {"X": 10}, "proportional"
    ).ev_by_line == {
        "A": 5,
        "B": 2,
        "C": 1,
        "D": 1,
        "E": 1,
        "F": 0,
    }


def test_province_tie_by_lot_is_deterministic() -> None:
    tabs = {"UT": tab("UT", {"A": 500, "B": 500}, resolve=False)}
    first = allocate(tabs, {"UT": 14}, tie_seed=5)
    again = allocate(tabs, {"UT": 14}, tie_seed=5)
    assert first.province_winners == again.province_winners
    assert first.decided_by == {"UT": "lot"} and first.ties == ["UT"]
    assert first.total_allocated == 14
    winners = {allocate(tabs, {"UT": 14}, tie_seed=s).province_winners["UT"] for s in range(30)}
    assert winners == {"A", "B"}
    # a lot already drawn at tabulation is honoured
    drawn = {"UT": tab("UT", {"A": 500, "B": 500}, tie_seed=9)}
    out = allocate(drawn, {"UT": 14}, tie_seed=123)
    assert out.province_winners["UT"] == drawn["UT"].winner_key


def test_province_tie_rule_contingent_withholds(ev_map) -> None:  # type: ignore[no-untyped-def]
    cfg = ConstitutionConfig(province_tie_rule="contingent")
    winners = dict.fromkeys(ev_map, "A")
    tabs = _all(winners, ev_map)
    tabs["NB"] = tab("PRES-NB", {"A": 1000, "B": 1000, "C": 10})
    tabs["ZH"] = tab("PRES-ZH", {"A": 1000, "B": 1000, "C": 10})
    tabs["NH"] = tab("PRES-NH", {"A": 1000, "B": 1000, "C": 10})
    out = allocate(tabs, ev_map, constitution=cfg)
    assert out.withheld == {"ZH": 34, "NH": 27, "NB": 24}
    assert out.ev_by_line["A"] == 174 - 85 and out.winner == "A"
    assert out.decided_by["NB"] == "withheld" and out.province_winners["NB"] is None
    assert out.total_allocated + sum(out.withheld.values()) == 174
    more = dict(tabs)
    more["GE"] = tab("PRES-GE", {"A": 7, "B": 7})
    out2 = allocate(more, ev_map, constitution=cfg)
    assert out2.ev_by_line["A"] == 69 and out2.needs_contingent  # withheld EV still count toward 88


def test_line_missing_from_some_provinces() -> None:
    tabs = {"GR": tab("GR", {"A": 10, "B": 5}), "FR": tab("FR", {"A": 3, "C": 9})}
    out = allocate(tabs, {"GR": 7, "FR": 8})
    assert out.line_keys == ["A", "B", "C"]
    assert out.ev_by_line == {"A": 7, "B": 0, "C": 8}


def test_margins_and_extremes() -> None:
    tabs = {
        "GR": tab("GR", {"A": 510, "B": 490}),
        "FR": tab("FR", {"A": 800, "B": 200}),
        "DR": tab("DR", {"A": 450, "B": 550}),
    }
    assert closest_province(tabs) == ("GR", pytest.approx(2.0))
    assert largest_victory(tabs) == ("FR", pytest.approx(60.0))
    votes, pct = pv_margin(tabs)
    assert votes == 1760 - 1240 and pct == pytest.approx(100 * 520 / 3000)
    assert popular_vote_winner({"A": 5, "B": 5}) is None


def test_missing_province_result_raises(ev_map) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ElectionError):
        allocate({"GR": tab("GR", {"A": 1})}, ev_map)


def test_outcome_serialises(ev_map) -> None:  # type: ignore[no-untyped-def]
    out = allocate(_all(dict.fromkeys(ev_map, "B"), ev_map), ev_map)
    d = out.to_dict()
    assert d["winner"] == "B" and d["method"] == "winner_take_all"
    assert sum(ev for _, _, ev in d["allocations"]) == 174
