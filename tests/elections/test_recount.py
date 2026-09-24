from __future__ import annotations

import numpy as np
import pytest

from app.core.constitution import RaceType
from app.elections.recount import (
    INVALID_PILE,
    RecountConfig,
    RecountProcedure,
    RecountThreshold,
    load_recount_config,
    needs_recount,
    perform_recount,
)
from app.elections.tabulation import tabulate, tabulate_totals
from app.elections.types import RaceVotes

CFG = RecountConfig()


def t(votes: dict[str, int]):  # type: ignore[no-untyped-def]
    return tabulate_totals("X", list(votes), list(votes.values()))


def test_config_file_matches_defaults() -> None:
    loaded = load_recount_config()
    assert loaded.thresholds[RaceType.PRESIDENT_PROVINCE].margin_pct == 0.25
    assert loaded.thresholds[RaceType.HOUSE].margin_pct == 0.5
    assert loaded.thresholds[RaceType.MAYOR].margin_pct == 0.5
    assert loaded.procedure.max_delta == CFG.procedure.max_delta


@pytest.mark.parametrize(
    ("race_type", "a", "b", "expected"),
    [
        (RaceType.PRESIDENT_PROVINCE, 50_100, 49_900, True),  # 200 of 100,000 = 0.2 pp ≤ 0.25
        (RaceType.PRESIDENT_PROVINCE, 50_130, 49_870, False),  # 0.26 pp
        (RaceType.HOUSE, 50_200, 49_800, True),  # 0.4 pp ≤ 0.5
        (RaceType.HOUSE, 50_300, 49_700, False),  # 0.6 pp
        (RaceType.SENATE, 50_120, 49_880, True),  # 0.24 pp ≤ 0.25
        (RaceType.GOVERNOR, 50_150, 49_850, False),  # 0.3 pp
        (RaceType.MAYOR, 5_020, 4_980, True),  # 0.4 pp
    ],
)
def test_needs_recount_thresholds(race_type: RaceType, a: int, b: int, expected: bool) -> None:
    tab = t({"A": a, "B": b})
    hit, reason = needs_recount(tab, race_type, CFG)
    margin_pp = 100 * (a - b) / (a + b)
    assert hit == (margin_pp <= CFG.thresholds[race_type].margin_pct)
    assert hit is expected
    assert ("within" in reason) == hit


def test_needs_recount_special_cases() -> None:
    tie = tabulate_totals("X", ["A", "B"], [500, 500], tie_seed=1)
    assert needs_recount(tie, RaceType.HOUSE, CFG) == (True, "exact tie")  # even after a lot at tabulation
    hit, reason = needs_recount(t({"A": 10, "B": 9}), RaceType.PRESIDENT, CFG)
    assert not hit and "no automatic recount" in reason
    assert not needs_recount(t({"A": 10, "B": 9}), RaceType.MUNICIPAL_COUNCIL, CFG)[0]
    assert needs_recount(t({"A": 10}), RaceType.HOUSE, CFG) == (False, "uncontested race")
    votes_rule = RecountConfig(thresholds={RaceType.HOUSE: RecountThreshold(margin_votes=50)})
    assert needs_recount(t({"A": 1_000_050, "B": 1_000_000}), RaceType.HOUSE, votes_rule)[0]
    assert not needs_recount(t({"A": 1_000_051, "B": 1_000_000}), RaceType.HOUSE, votes_rule)[0]
    both = RecountConfig(
        thresholds={RaceType.HOUSE: RecountThreshold(margin_pct=0.5, margin_votes=10, combine="and")}
    )
    assert not needs_recount(t({"A": 5_020, "B": 4_980}), RaceType.HOUSE, both)[0]  # 0.4 pp but 40 votes
    assert needs_recount(t({"A": 1_005, "B": 1_000}), RaceType.HOUSE, both)[0]
    no_tie = RecountConfig(thresholds={RaceType.HOUSE: RecountThreshold(exact_tie=False)})
    assert not needs_recount(tie, RaceType.HOUSE, no_tie)[0]


def _close_race(race_votes_factory, synthetic):  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    idx = frame.units_in_province(frame.province_index("UT"))
    return race_votes_factory("PRES-UT", [0.4, 0.4, 0.2], ["A", "B", "C"], unit_index=idx, seed=3)


def test_recount_is_small_auditable_and_consistent(race_votes_factory, synthetic) -> None:  # type: ignore[no-untyped-def]
    rv = _close_race(race_votes_factory, synthetic)
    original = rv.votes.copy()
    out = perform_recount(rv, seed=99, config=CFG)
    rc = out.recounted
    rc.check()
    assert np.array_equal(rv.votes, original)  # input untouched
    assert (rc.votes >= 0).all() and (rc.invalid >= 0).all() and (rc.ballots_cast <= rc.eligible).all()
    assert np.array_equal(rc.eligible, rv.eligible) and np.array_equal(rc.blank, rv.blank)
    assert len(out.units_examined) == max(
        CFG.procedure.min_units, round(0.1 * int((rv.ballots_cast > 0).sum()))
    )
    assert out.adjustments, "some corrections expected with 60% confirmation over 10 units"
    # every adjustment is small and within the examined sample
    examined = set(out.units_examined)
    for a in out.adjustments:
        assert 1 <= abs(a.delta) <= CFG.procedure.max_delta
        assert a.unit_index in examined
        assert a.votes_after == a.votes_before + a.delta and a.votes_after >= 0
        assert a.reason in {
            "misread tally",
            "uncounted ballot found",
            "ballot ruled invalid",
            "invalid ballot ruled valid",
        }
        assert len(a.as_tuple()) == 6
    # replaying the audit trail on the original counts reproduces the recount exactly
    votes = rv.votes.copy()
    invalid = rv.invalid.copy()
    for a in out.adjustments:
        if a.line_index == INVALID_PILE:
            assert invalid[a.row] == a.votes_before
            invalid[a.row] = a.votes_after
        else:
            assert votes[a.row, a.line_index] == a.votes_before
            votes[a.row, a.line_index] = a.votes_after
    assert np.array_equal(votes, rc.votes) and np.array_equal(invalid, rc.invalid)
    # never a different election: untouched units identical, total movement tiny
    changed_rows = {a.row for a in out.adjustments}
    untouched = np.array([r for r in range(len(rv.unit_index)) if r not in changed_rows])
    assert np.array_equal(rc.votes[untouched], rv.votes[untouched])
    moved = int(np.abs(rc.votes - rv.votes).sum())
    assert moved <= 2 * CFG.procedure.max_delta * len(out.units_examined)
    assert moved < 0.001 * rv.votes.sum()
    assert out.units_changed == len(changed_rows)
    assert sum(v for k, v in out.net_change.items() if k != "<invalid>") == int(
        rc.votes.sum() - rv.votes.sum()
    )
    assert out.ballots_cast_change == int(rc.ballots_cast.sum() - rv.ballots_cast.sum())
    assert out.margin_votes_after == tabulate(rc).margin_votes
    assert out.decided_by == "recount"
    assert len(out.audit_rows()) == len(out.adjustments)


def test_recount_deterministic_and_seeded(race_votes_factory, synthetic) -> None:  # type: ignore[no-untyped-def]
    rv = _close_race(race_votes_factory, synthetic)
    a = perform_recount(rv, seed=5, config=CFG)
    b = perform_recount(rv, seed=5, config=CFG)
    assert [x.as_tuple() for x in a.adjustments] == [x.as_tuple() for x in b.adjustments]
    assert np.array_equal(a.recounted.votes, b.recounted.votes)
    c = perform_recount(rv, seed=6, config=CFG)
    assert [x.as_tuple() for x in a.adjustments] != [x.as_tuple() for x in c.adjustments]


def _tied_house_race() -> RaceVotes:
    votes = np.array([[400, 380, 20], [300, 320, 10], [150, 150, 5]], dtype=np.int64)
    blank = np.array([2, 1, 0], dtype=np.int64)
    invalid = np.array([1, 3, 0], dtype=np.int64)
    cast = votes.sum(axis=1) + blank + invalid
    return RaceVotes(
        "HOUSE-GE-04", ["a", "b", "c"], np.array([10, 11, 12]), votes, cast, blank, invalid, cast + 100
    )


def test_tied_house_race_recount_then_lot() -> None:
    rv = _tied_house_race()
    tab = tabulate(rv, resolve_ties=False)
    assert tab.tied and tab.winner is None and tab.totals[0] == tab.totals[1] == 850
    assert needs_recount(tab, RaceType.HOUSE, CFG) == (True, "exact tie")
    confirm_all = RecountConfig(procedure=RecountProcedure(confirm_probability=1.0))
    out = perform_recount(rv, seed=21, config=confirm_all)
    assert out.adjustments == [] and np.array_equal(out.recounted.votes, rv.votes)
    assert out.decided_by == "lot" and out.after.decided_by == "lot"
    assert out.winner_after in {"a", "b"}
    again = perform_recount(rv, seed=21, config=confirm_all)
    assert again.winner_after == out.winner_after
    winners = {perform_recount(rv, seed=s, config=confirm_all).winner_after for s in range(30)}
    assert winners == {"a", "b"}
    assert not out.outcome_changed  # the provisional lot and the certified lot use the same seed


def test_recount_can_change_a_razor_thin_outcome() -> None:
    votes = np.array([[500, 499]] * 40, dtype=np.int64)  # 'a' leads by 40 votes (0.1 pp)
    zeros = np.zeros(40, dtype=np.int64)
    cast = votes.sum(axis=1)
    rv = RaceVotes("MAYOR-GM0101", ["a", "b"], np.arange(40), votes, cast, zeros, zeros.copy(), cast + 50)
    assert int(votes[:, 0].sum() - votes[:, 1].sum()) == 40
    heavy = RecountConfig(
        procedure=RecountProcedure(
            unit_sample_fraction=1.0, max_units=40, confirm_probability=0.0, max_delta=3
        )
    )
    results = [perform_recount(rv, seed=s, config=heavy) for s in range(25)]
    for r in results:
        r.recounted.check()
        assert abs(r.margin_votes_after - r.margin_votes_before) <= 2 * 3 * 40
    assert {r.winner_before for r in results} == {"a"}
    # outcome_changed is reported consistently
    for r in results:
        assert r.outcome_changed == (r.winner_after != "a")
    assert 0 < sum(r.outcome_changed for r in results) < len(results)  # a 40-vote lead can flip


def test_recount_single_unit_without_headroom() -> None:
    votes = np.array([[3, 0]], dtype=np.int64)
    zeros = np.zeros(1, dtype=np.int64)
    rv = RaceVotes(
        "SEN-ZE-1", ["a", "b"], np.array([0]), votes, np.array([3]), zeros, zeros.copy(), np.array([3])
    )
    cfg = RecountConfig(procedure=RecountProcedure(confirm_probability=0.0, min_units=1))
    for s in range(20):
        out = perform_recount(rv, seed=s, config=cfg)
        out.recounted.check()
        assert out.recounted.ballots_cast[0] == 3  # no headroom for found ballots
