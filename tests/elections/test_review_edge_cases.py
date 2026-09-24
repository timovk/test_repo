"""Adversarial / regression tests from the review of the electoral-mechanics engines.

Every test here either pins down a bug that was fixed (duplicate line keys corrupting the
ranking, silent truncation of fractional counts, inconsistent PROPORTIONAL tie reporting, the
calendar silently overriding constitutional terms, …) or stresses an invariant with randomised
inputs (recount audit trail, ranking permutations, tipping-point arithmetic).
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import time
from datetime import date

import numpy as np
import pytest

from app.core.constitution import (
    ConstitutionConfig,
    ContingentElectionConfig,
    ContingentElectionMode,
    EVAllocationMethod,
    RaceType,
)
from app.core.errors import ConfigError, ElectionError
from app.core.rng import make_rng
from app.elections.calendar import CalendarConfig, ElectionCalendar
from app.elections.contingent import run_contingent_election
from app.elections.electoral_college import allocate, province_margins, tipping_point
from app.elections.recount import (
    ADJUSTMENT_KINDS,
    INVALID_PILE,
    RecountConfig,
    RecountProcedure,
    needs_recount,
    perform_recount,
)
from app.elections.seats import largest_remainder, sainte_lague
from app.elections.tabulation import aggregate_levels, reconcile, tabulate, tabulate_totals
from app.elections.types import RaceVotes

from .conftest import CANONICAL_EV


def _rv(votes, keys, key: str = "HOUSE-UT-01", unit_index=None, eligible_extra: int = 5) -> RaceVotes:  # type: ignore[no-untyped-def]
    v = np.asarray(votes)
    n = v.shape[0]
    zeros = np.zeros(n, dtype=np.int64)
    cast = v.sum(axis=1)
    idx = np.arange(n) if unit_index is None else np.asarray(unit_index)
    return RaceVotes(key, list(keys), idx, v, cast, zeros, zeros.copy(), cast + eligible_extra)


# --------------------------------------------------------------------------- tabulation
def test_duplicate_line_keys_are_rejected(synthetic) -> None:  # type: ignore[no-untyped-def]
    # used to return ranking [1, 1, 0, 2] (a duplicated index, 4 entries for 3 lines)
    rv = _rv(np.array([[5, 5, 1]], dtype=np.int64), ["a", "a", "b"])
    with pytest.raises(ElectionError, match="duplicate"):
        tabulate(rv)
    with pytest.raises(ElectionError, match="duplicate"):
        aggregate_levels(rv, synthetic.frame)


def test_fractional_counts_are_rejected_not_truncated() -> None:
    frac = _rv(np.array([[5.7, 5.2]]), ["a", "b"])
    with pytest.raises(ElectionError, match="whole ballot counts"):
        tabulate(frac)
    whole = _rv(np.array([[5.0, 3.0]]), ["a", "b"])
    assert tabulate(whole).totals.tolist() == [5, 3]
    nan = _rv(np.array([[np.nan, 3.0]]), ["a", "b"])
    with pytest.raises(ElectionError):
        tabulate(nan)


def test_numpy_integer_positional_tie_seed() -> None:
    rv = _rv(np.array([[7, 7]], dtype=np.int64), ["a", "b"])
    t = tabulate(rv, np.int64(9))
    assert t.race_key == "HOUSE-UT-01" and t.lot_seed == 9
    assert t.winner == tabulate(rv, tie_seed=9).winner


def test_ranking_is_always_a_permutation() -> None:
    rng = make_rng(11, "ranking-permutation")
    for trial in range(300):
        L = int(rng.integers(1, 7))
        totals = rng.integers(0, 4, size=L)  # many exact ties, including at zero
        t = tabulate_totals(f"R{trial}", [f"l{i}" for i in range(L)], totals, tie_seed=trial)
        assert sorted(t.ranking) == list(range(L))
        ranked = totals[t.ranking]
        assert (np.diff(ranked) <= 0).all()  # votes never increase down the ranking
        assert t.winner == t.ranking[0]
        assert t.tied == (L > 1 and int((totals == totals.max()).sum()) > 1)
        if t.tied:
            assert t.decided_by == "lot" and t.winner in t.tied_lines and t.margin_votes == 0


def test_three_way_first_place_tie_lot_covers_all_tied_lines() -> None:
    keys = ["a", "b", "c", "d"]
    winners = set()
    for s in range(60):
        t = tabulate_totals("GOV-ZE", keys, [100, 100, 40, 100], tie_seed=s)
        assert t.tied_lines == (0, 1, 3) and t.ranking[3] == 2
        winners.add(t.winner_key)
        assert t.winner_key == tabulate_totals("GOV-ZE", keys, [100, 100, 40, 100], tie_seed=s).winner_key
    assert winners == {"a", "b", "d"}


def test_duplicate_unit_index_rejected(synthetic) -> None:  # type: ignore[no-untyped-def]
    rv = _rv(np.array([[3, 2], [3, 2]], dtype=np.int64), ["a", "b"], unit_index=[0, 0])
    with pytest.raises(ElectionError, match="more than once"):
        aggregate_levels(rv, synthetic.frame)


def test_inconsistent_race_votes_raise_election_error(synthetic) -> None:  # type: ignore[no-untyped-def]
    rv = _rv(np.array([[3, 2]], dtype=np.int64), ["a", "b"])
    rv.ballots_cast = rv.ballots_cast + 1  # valid + blank + invalid != ballots cast
    with pytest.raises(ElectionError, match="inconsistent"):
        aggregate_levels(rv, synthetic.frame)


def test_empty_race_aggregates_and_reconciles(synthetic) -> None:  # type: ignore[no-untyped-def]
    empty = np.zeros(0, dtype=np.int64)
    rv = RaceVotes("MAYOR-GM0101", ["a", "b"], empty, np.zeros((0, 2), np.int64), empty, empty, empty, empty)
    levels = aggregate_levels(rv, synthetic.frame)
    assert len(levels["unit"]) == 0 and len(levels["national"]) == 2
    assert levels["national"]["votes"].tolist() == [0, 0]
    assert reconcile(levels) == []  # used to report a false 'geography/line sets differ'
    bad = {k: v.copy() for k, v in levels.items()}
    bad["national"].loc[0, "votes"] = 3
    assert reconcile(bad)


def test_zero_eligible_units_aggregate_cleanly(synthetic) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    idx = frame.units_in_muni(0)[:4]
    votes = np.array([[0, 0], [0, 0], [10, 4], [0, 0]], dtype=np.int64)
    cast = votes.sum(axis=1)
    zeros = np.zeros(4, dtype=np.int64)
    eligible = np.array([0, 0, 20, 0], dtype=np.int64)
    rv = RaceVotes("MAYOR-X", ["a", "b"], idx, votes, cast, zeros, zeros.copy(), eligible)
    levels = aggregate_levels(rv, frame)
    unit = levels["unit"]
    assert np.isfinite(unit["share"]).all()
    assert unit.loc[unit["valid_votes"] == 0, "share"].eq(0).all()
    assert int(unit["winner"].sum()) == 1
    assert reconcile(levels) == []
    assert tabulate(rv).turnout_pct == pytest.approx(100 * 14 / 20)


def test_integral_float_district_indices(synthetic, race_votes_factory) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    idx = frame.units_in_province(frame.province_index("GR"))
    rv = race_votes_factory("PRES-GR", [0.5, 0.3, 0.2], ["A", "B", "C"], unit_index=idx)
    as_int = (frame.unit_muni[idx] % 3).astype(np.int64)
    codes = ["GR-01", "GR-02", "GR-03"]
    a = aggregate_levels(rv, frame, unit_district=as_int, district_codes=codes)
    b = aggregate_levels(rv, frame, unit_district=as_int.astype(float), district_codes=codes)
    assert a["district"].equals(b["district"]) and reconcile(b) == []


# --------------------------------------------------------------------------- Electoral College
def _t(key: str, votes: dict[str, int], **kw):  # type: ignore[no-untyped-def]
    return tabulate_totals(key, list(votes), list(votes.values()), **kw)


def test_proportional_never_withholds_under_contingent_tie_rule() -> None:
    cfg = ConstitutionConfig(province_tie_rule="contingent")
    tabs = {"P": _t("P", {"A": 100, "B": 100, "C": 50})}
    out = allocate(tabs, {"P": 10}, EVAllocationMethod.PROPORTIONAL, constitution=cfg)
    assert out.withheld == {} and out.total_allocated == 10
    assert out.ev_by_line == {"A": 4, "B": 4, "C": 2}
    # used to report decided_by 'withheld' although every EV was allocated
    assert out.decided_by == {"P": "popular_vote"}
    assert out.ties == ["P"]  # the province-wide tie is still reported as a fact
    assert out.provinces["P"].withheld == 0


def test_proportional_equal_remainder_for_last_ev_is_a_recorded_lot() -> None:
    tabs = {"P": _t("P", {"A": 100, "B": 100, "C": 100})}
    winners = set()
    for s in range(30):
        out = allocate(tabs, {"P": 2}, "proportional", tie_seed=s, constitution=ConstitutionConfig())
        assert sum(out.ev_by_line.values()) == 2 and max(out.ev_by_line.values()) == 1
        assert out.decided_by == {"P": "lot"}
        assert all(a.decided_by == "lot" for a in out.awards)
        winners.add(tuple(k for k, v in out.ev_by_line.items() if v == 0))
    assert len(winners) == 3  # the lot really varies with the seed
    clear = allocate({"P": _t("P", {"A": 70, "B": 30})}, {"P": 5}, "proportional")
    assert clear.decided_by == {"P": "popular_vote"}


def test_district_method_withholds_a_tied_district_under_contingent_rule() -> None:
    cfg = ConstitutionConfig(province_tie_rule="contingent")
    ptabs = {"ZE": _t("ZE", {"A": 60, "B": 40})}
    dtabs = {"ZE-01": _t("ZE-01", {"A": 30, "B": 30}), "ZE-02": _t("ZE-02", {"A": 30, "B": 10})}
    out = allocate(ptabs, {"ZE": 4}, "district", dtabs, {d: "ZE" for d in dtabs}, constitution=cfg)
    assert out.ev_by_line == {"A": 3, "B": 0}
    assert out.withheld == {"ZE": 1} and out.ties == ["ZE-01"]
    assert out.total_allocated + sum(out.withheld.values()) == 4


def test_partial_map_needs_explicit_constitutional_majority(caplog) -> None:  # type: ignore[no-untyped-def]
    cfg = ConstitutionConfig()
    decided = {p: _t(p, {"A": 60, "B": 40}) for p in ("ZH", "NH", "UT")}  # 75 EV so far
    partial = {p: CANONICAL_EV[p] for p in decided}
    with caplog.at_level(logging.WARNING, logger="app.elections.electoral_college"):
        naive = allocate(decided, partial, constitution=cfg)
    assert naive.winner == "A" and naive.majority == 38  # majority of the subset …
    assert any("differs from the constitution" in r.message for r in caplog.records)  # … loudly
    proper = allocate(decided, partial, constitution=cfg, majority=cfg.presidential_majority)
    assert proper.majority == 88 and proper.winner is None and proper.ev_by_line["A"] == 75
    with pytest.raises(ElectionError):
        allocate(decided, partial, constitution=cfg, majority=37)  # ≤ half of 75: two could win


def test_three_way_exact_province_tie_by_lot() -> None:
    tabs = {"FL": _t("FL", {"A": 300, "B": 300, "C": 300}, resolve_ties=False)}
    seen = {allocate(tabs, {"FL": 6}, tie_seed=s).province_winners["FL"] for s in range(60)}
    assert seen == {"A", "B", "C"}
    first = allocate(tabs, {"FL": 6}, tie_seed=4)
    assert first.province_winners == allocate(tabs, {"FL": 6}, tie_seed=4).province_winners
    assert first.decided_by == {"FL": "lot"} and first.ties == ["FL"]


def test_allocation_outcome_is_json_serialisable(ev_map) -> None:  # type: ignore[no-untyped-def]
    tabs = {p: _t(p, {"A": 50 + i, "B": 50, "C": 10}) for i, p in enumerate(ev_map)}
    for method in EVAllocationMethod:
        if method is EVAllocationMethod.DISTRICT:
            continue
        d = allocate(tabs, ev_map, method).to_dict()
        assert json.loads(json.dumps(d))["total_ev"] == 174


def test_tipping_point_property_on_random_canonical_maps() -> None:
    """The tipping point is the province whose EV first push the sorted running total to 88."""
    rng = make_rng(5, "tipping-property")
    provinces = list(CANONICAL_EV)
    for _ in range(100):
        tabs = {
            p: _t(p, {k: int(v) for k, v in zip("ABC", rng.integers(1_000, 100_000, size=3), strict=True)})
            for p in provinces
        }
        out = allocate(tabs, CANONICAL_EV, constitution=ConstitutionConfig())
        who = out.winner or out.leader or "A"
        p, margin = tipping_point(tabs, CANONICAL_EV, who)
        margins = province_margins(tabs, who)
        assert margin == margins[p]
        ahead = [q for q in provinces if margins[q] > margin]
        level = [q for q in provinces if margins[q] == margin]
        before_p = ahead + level[: level.index(p)]
        assert (
            sum(CANONICAL_EV[q] for q in before_p)
            < 88
            <= sum(CANONICAL_EV[q] for q in before_p) + CANONICAL_EV[p]
        )
        if out.winner is not None:
            assert margin > 0  # a winner-take-all majority is always built from won provinces


# --------------------------------------------------------------------------- contingent election
PROVS = list(CANONICAL_EV)
LINE_PARTY = {"A": "PA", "B": "PB", "C": "PC"}
VP = {"A": "vp-a", "B": "vp-b", "C": "vp-c"}


def _three_way():  # type: ignore[no-untyped-def]
    winners = {
        p: "A" if p in {"ZH", "UT", "GE", "GR"} else "B" if p in {"NH", "NB", "FL"} else "C" for p in PROVS
    }
    tabs = {p: _t(p, {k: (400 if k == winners[p] else 300) for k in "ABC"}) for p in PROVS}
    out = allocate(tabs, CANONICAL_EV, constitution=ConstitutionConfig())
    assert out.needs_contingent
    return out


def _house(parties: list[str]) -> dict[str, list[str]]:
    return {p: [parties[i % len(parties)]] * 3 for i, p in enumerate(PROVS)}


def _run(outcome, house, cfg, ideology, pv=None, runoff_fn=None, seed=1):  # type: ignore[no-untyped-def]
    pv = pv or {"A": 30, "B": 20, "C": 10}
    senate = ["PA"] * 12 + ["PB"] * 12
    return run_contingent_election(
        outcome, pv, None, house, senate, LINE_PARTY, ideology, VP, cfg, seed, runoff_fn
    )


def test_nan_ideology_is_treated_as_unknown() -> None:
    nan = float("nan")
    ideology = {"PA": (0.0, 0.0), "PB": (nan, 0.0), "PC": (1.0, 1.0), "PX": (0.9, 0.9), "PY": (nan, nan)}
    res = _run(_three_way(), _house(["PX", "PY"]), ContingentElectionConfig(max_ballots=1), ideology)
    r1 = res.rounds[0]
    # PX: closest known finalist is C (PB's distance is unknown → ranked last);
    # PY: unknown ideology → national popular vote order (A first)
    assert r1.delegations["GR"] == "C" and r1.delegations["FR"] == "A"
    assert r1.tallies == {"A": 6, "B": 0, "C": 6}


def test_unusable_runoff_result_falls_back_and_is_noted(caplog) -> None:  # type: ignore[no-untyped-def]
    cfg = ContingentElectionConfig(mode=ContingentElectionMode.NATIONAL_RUNOFF)
    ideology = {"PA": (0.0,), "PB": (1.0,), "PC": (0.5,)}
    with caplog.at_level(logging.WARNING, logger="app.elections.contingent"):
        res = _run(_three_way(), _house(["PA"]), cfg, ideology, runoff_fn=lambda f: "not-a-finalist")
    assert res.winner == "A" and res.decided_by == "national_popular_vote"
    assert "unusable" in res.rounds[0].note
    assert any("runoff_fn" in r.message for r in caplog.records)
    zero = _run(_three_way(), _house(["PA"]), cfg, ideology, runoff_fn=lambda f: dict.fromkeys(f, 0))
    assert zero.winner == "A" and zero.decided_by == "national_popular_vote"


@pytest.mark.parametrize("mode", list(ContingentElectionMode))
def test_every_mode_is_deterministic_and_serialisable(mode: ContingentElectionMode) -> None:
    cfg = ContingentElectionConfig(mode=mode)
    ideology = {"PA": (-0.5, 0.2), "PB": (0.5, 0.1), "PC": (0.0, 0.9), "PD": (0.1, 0.5)}
    runoff = lambda f: {f[0]: 10, f[1]: 10}  # noqa: E731 - exact runoff tie → seeded lot
    a = _run(_three_way(), _house(["PA", "PB", "PC", "PD"]), cfg, ideology, runoff_fn=runoff, seed=3)
    b = _run(_three_way(), _house(["PA", "PB", "PC", "PD"]), cfg, ideology, runoff_fn=runoff, seed=3)
    assert a.to_dict() == b.to_dict()
    assert json.loads(json.dumps(a.to_dict()))["mode"] == mode.value
    assert a.winner in a.finalists and a.vice_president.winner_line in {"A", "B", "C"}
    if mode is ContingentElectionMode.NATIONAL_RUNOFF:
        assert a.decided_by == "lot"
        winners = {
            _run(_three_way(), _house(["PA"]), cfg, ideology, runoff_fn=runoff, seed=s).winner
            for s in range(30)
        }
        assert winners == {"A", "B"}


def test_finalists_filled_from_popular_vote_when_every_ev_is_withheld() -> None:
    cfg = ConstitutionConfig(province_tie_rule="contingent")
    tabs = {p: _t(p, {"A": 10, "B": 10, "C": 3}) for p in PROVS}
    out = allocate(tabs, CANONICAL_EV, constitution=cfg)
    assert out.total_allocated == 0 and out.needs_contingent and sum(out.withheld.values()) == 174
    res = _run(
        out,
        _house(["PC"]),
        ContingentElectionConfig(),
        {"PA": (0.0,), "PB": (1.0,), "PC": (0.1,)},
        pv={"A": 5, "B": 9, "C": 1},
    )
    assert res.finalists == ["B", "A"]  # nobody has EV → the two popular-vote leaders
    assert res.winner == "A"  # PC members are ideologically closest to A: 12 delegations


def test_house_members_mode_needs_76_of_150() -> None:
    members = ["PA"] * 75 + ["PB"] * 60 + ["PC"] * 15
    house = {"all": members}
    ideology = {"PA": (0.0,), "PB": (1.0,), "PC": (0.9,)}
    res = _run(_three_way(), house, ContingentElectionConfig(mode="house_members"), ideology)
    r1, r2 = res.rounds[:2]
    assert r1.required == 76 and r1.tallies == {"A": 75, "B": 60, "C": 15} and r1.winner is None
    assert r2.tallies == {"A": 75, "B": 75} and r2.winner is None  # PC → B (closer): 75–75 deadlock
    assert res.outcome == "fallback_popular_vote" and res.winner == "A" and res.ballots == 10


# --------------------------------------------------------------------------- calendar
def test_calendar_yaml_follows_the_constitution() -> None:
    k = ConstitutionConfig(house_term_years=3, senate_term_years=9, presidential_term_years=6)
    cal = ElectionCalendar.from_config(constitution=k)
    assert cal.house.every == 3 and cal.senate_term == 9 and cal.president.every == 6
    assert cal.senate_initial_terms == {1: 3, 2: 6, 3: 9}
    assert [cal.senate_class_up(y) for y in (2027, 2030, 2033, 2036)] == [1, 2, 3, 1]
    assert cal.cycle(2030).president and not cal.cycle(2027).president


@pytest.mark.parametrize(
    "override",
    [
        {"president": {"every_years": 5, "term_years": 5}},  # contradicts presidential_term_years
        {"house": {"every_years": 2, "term_years": 3}},  # gaps/overlaps and ≠ constitution
        {"municipal": {"first_year_offset": 2, "every_years": 4, "term_years": 5}},
        {"provincial_legislatures": {"every_years": 4, "term_years": 2}},
        {"senate": {"term_years": 8}},
    ],
)
def test_calendar_rejects_terms_contradicting_the_constitution(override: dict) -> None:
    with pytest.raises(ConfigError):
        ElectionCalendar(CalendarConfig.model_validate(override), ConstitutionConfig())


def test_term_bounds_rejects_a_senate_class_that_is_not_up() -> None:
    cal = ElectionCalendar(CalendarConfig(), ConstitutionConfig())
    assert cal.term_bounds("SENATE", 2028, senate_class=2) == (date(2029, 1, 15), date(2035, 1, 15))
    with pytest.raises(ElectionError, match="not up"):
        cal.term_bounds("SENATE", 2028, senate_class=1)
    with pytest.raises(ElectionError, match="unknown"):
        cal.term_bounds("SENATE", 2024, senate_class=4)


def test_every_regular_election_after_founding_has_exactly_one_senate_class() -> None:
    cal = ElectionCalendar.from_config(constitution=ConstitutionConfig())
    elections = cal.upcoming(30, 2025)
    assert all(len(c.senate_classes) == 1 and c.house for c in elections)
    for cls in (1, 2, 3):
        years = [c.year for c in elections if cls in c.senate_classes]
        assert {b - a for a, b in itertools.pairwise(years)} == {6}


# --------------------------------------------------------------------------- recount
def _random_race(rng: np.random.Generator, key: str) -> RaceVotes:
    n = int(rng.integers(0, 25))
    L = int(rng.integers(1, 5))
    eligible = rng.integers(0, 60, size=n)
    cast = rng.integers(0, eligible + 1)
    blank = rng.integers(0, cast + 1)
    invalid = rng.integers(0, cast - blank + 1)
    valid = cast - blank - invalid
    votes = np.zeros((n, L), dtype=np.int64)
    for i, v in enumerate(valid):
        votes[i] = rng.multinomial(int(v), np.full(L, 1.0 / L))
    idx = np.sort(rng.choice(5_000, size=n, replace=False))
    rv = RaceVotes(key, [f"l{i}" for i in range(L)], idx, votes, cast, blank, invalid, eligible)
    rv.check()
    return rv


def test_recount_invariants_on_random_races() -> None:
    rng = make_rng(99, "recount-property")
    for trial in range(200):
        rv = _random_race(rng, f"MAYOR-{trial}")
        proc = RecountProcedure(
            confirm_probability=float(rng.random()), min_units=1, max_delta=int(rng.integers(1, 6))
        )
        out = perform_recount(rv, seed=trial, config=RecountConfig(procedure=proc))
        rc = out.recounted
        rc.check()
        assert (rc.invalid >= 0).all() and np.array_equal(rc.blank, rv.blank)
        assert np.array_equal(rc.eligible, rv.eligible) and np.array_equal(rc.unit_index, rv.unit_index)
        votes, invalid = rv.votes.copy(), rv.invalid.copy()
        per_unit: dict[int, int] = {}
        for unit, line, before, after, delta, reason in out.adjustments:  # 6-tuple unpacking
            assert 1 <= abs(delta) <= proc.max_delta and after == before + delta and after >= 0
            assert reason in ADJUSTMENT_KINDS
            row = int(np.flatnonzero(rv.unit_index == unit)[0])
            per_unit[row] = per_unit.get(row, 0) + 1
            if line == INVALID_PILE:
                assert invalid[row] == before
                invalid[row] = after
            else:
                assert votes[row, line] == before
                votes[row, line] = after
        assert all(n <= 2 for n in per_unit.values())  # one correction (≤ 2 entries) per unit
        assert np.array_equal(votes, rc.votes) and np.array_equal(invalid, rc.invalid)
        assert out.decided_by == ("lot" if out.after.tied else "recount")


def test_recount_uses_the_official_lot_seed() -> None:
    votes = np.array([[40, 40], [10, 10]], dtype=np.int64)
    rv = _rv(votes, ["a", "b"], key="SEN-DR-2")
    confirm_all = RecountConfig(procedure=RecountProcedure(confirm_probability=1.0))
    for official_seed in range(20):
        official = tabulate(rv, tie_seed=official_seed)
        out = perform_recount(rv, seed=1234, config=confirm_all, tie_seed=official_seed)
        assert out.winner_before == official.winner_key == out.winner_after
        assert not out.outcome_changed and out.decided_by == "lot"


def test_recount_of_a_race_without_ballots() -> None:
    zeros = np.zeros(3, dtype=np.int64)
    rv = RaceVotes(
        "MAYOR-GM9999", ["a", "b"], np.arange(3), np.zeros((3, 2), np.int64), zeros, zeros, zeros, zeros
    )
    assert needs_recount(tabulate(rv), RaceType.MAYOR, RecountConfig()) == (True, "exact tie")
    out = perform_recount(rv, seed=3, config=RecountConfig())
    assert out.adjustments == [] and out.units_examined == [] and out.decided_by == "lot"


# --------------------------------------------------------------------------- seats
def test_sainte_lague_matches_sequential_allocation() -> None:
    rng = make_rng(8, "sainte-lague-test")
    for _ in range(50):
        votes = {f"P{i}": int(v) for i, v in enumerate(rng.integers(1, 100_000, size=6))}
        seats = int(rng.integers(1, 50))
        alloc = dict.fromkeys(votes, 0)
        for _ in range(seats):
            best = max(votes, key=lambda k: votes[k] / (2 * alloc[k] + 1))
            alloc[best] += 1
        assert sainte_lague(votes, seats) == alloc


def test_largest_remainder_satisfies_quota() -> None:
    rng = make_rng(9, "hamilton-quota")
    for _ in range(200):
        votes = {f"P{i}": int(v) for i, v in enumerate(rng.integers(0, 10_000, size=5))}
        if not sum(votes.values()):
            continue
        seats = int(rng.integers(0, 40))
        alloc = largest_remainder(votes, seats, tie_seed=1, lot_key="q")
        assert sum(alloc.values()) == seats
        total = sum(votes.values())
        for k, v in votes.items():
            quota = v * seats / total
            assert math.floor(quota) <= alloc[k] <= math.ceil(quota)


# --------------------------------------------------------------------------- real data
@pytest.mark.realdata
def test_presidential_pipeline_on_real_cbs_frame(real_frame) -> None:  # type: ignore[no-untyped-def]
    """12 province EV contests on the REAL frame: tabulate, aggregate, reconcile, allocate, recount."""
    frame = real_frame
    rng = make_rng(2028, "real-presidential-pipeline")
    shares = np.array([0.34, 0.33, 0.21, 0.12])
    t0 = time.perf_counter()
    tabs, races = {}, {}
    for p, code in enumerate(frame.province_codes):
        idx = frame.units_in_province(p)
        eligible = frame.unit_eligible[idx].astype(np.int64)
        cast = rng.binomial(eligible, 0.8).astype(np.int64)
        invalid = rng.binomial(cast, 0.004).astype(np.int64)
        valid = cast - invalid
        local = rng.dirichlet(shares * 300, size=len(idx))
        votes = np.stack([rng.multinomial(int(v), local[i]) for i, v in enumerate(valid)]).astype(np.int64)
        rv = RaceVotes(f"PRES-{code}", list("ABCD"), idx, votes, cast, np.zeros_like(cast), invalid, eligible)
        races[code] = rv
        tabs[code] = tabulate(rv)
        assert reconcile(aggregate_levels(rv, frame)) == []
    out = allocate(tabs, CANONICAL_EV, constitution=ConstitutionConfig())
    assert out.total_allocated + sum(out.withheld.values()) == 174
    assert sum(t.valid for t in tabs.values()) == sum(int(r.votes.sum()) for r in races.values())
    closest = min(tabs, key=lambda c: tabs[c].margin_pct)
    rec = perform_recount(races[closest], seed=7, config=RecountConfig())
    rec.recounted.check()
    assert abs(rec.margin_votes_after - rec.margin_votes_before) <= 2 * 3 * 250
    assert time.perf_counter() - t0 < 30
