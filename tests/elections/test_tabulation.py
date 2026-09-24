from __future__ import annotations

import numpy as np
import pytest

from app.core.constitution import RaceType
from app.elections.seats import INDEPENDENT, house_composition
from app.elections.tabulation import (
    LEVEL_COLUMNS,
    TabulatedRaceResult,
    aggregate_levels,
    decided_by,
    reconcile,
    results_frame,
    tabulate,
    tabulate_totals,
)
from app.elections.types import BallotLine, RaceSpec, RaceVotes


def _rv(votes: list[list[int]], keys: list[str], extra_blank: int = 1, extra_invalid: int = 1) -> RaceVotes:
    v = np.asarray(votes, dtype=np.int64)
    n = v.shape[0]
    blank = np.full(n, extra_blank, dtype=np.int64)
    invalid = np.full(n, extra_invalid, dtype=np.int64)
    cast = v.sum(axis=1) + blank + invalid
    return RaceVotes("HOUSE-NB-07", keys, np.arange(n), v, cast, blank, invalid, cast + 10)


def test_basic_tabulation() -> None:
    rv = _rv([[10, 20, 5], [30, 10, 5]], ["a", "b", "c"])
    t = tabulate(rv)
    assert isinstance(t, TabulatedRaceResult)
    assert t.totals.tolist() == [40, 30, 10]
    assert t.valid == 80 and t.blank == 2 and t.invalid == 2 and t.ballots_cast == 84 and t.eligible == 104
    assert t.ranking == [0, 1, 2]
    assert t.winner == 0 and t.winner_key == "a" and not t.tied
    assert t.margin_votes == 10
    assert t.margin_pct == pytest.approx(12.5)
    assert t.shares.sum() == pytest.approx(1.0)
    assert t.decided_by == "popular_vote" and decided_by(t) == "popular_vote"
    assert t.turnout_pct == pytest.approx(100 * 84 / 104)


def test_ranking_ties_below_first_keep_ballot_order() -> None:
    t = tabulate_totals("X", ["a", "b", "c", "d"], [50, 10, 30, 10])
    assert t.ranking == [0, 2, 1, 3]


def test_exact_tie_resolved_by_lot_deterministically() -> None:
    rv = _rv([[25, 25, 3]], ["a", "b", "c"])
    t1 = tabulate(rv, tie_seed=11)
    t2 = tabulate(rv, tie_seed=11)
    assert t1.tied and t1.decided_by == "lot" and decided_by(t1) == "lot"
    assert t1.winner == t2.winner and t1.winner in (0, 1)
    assert t1.tied_lines == (0, 1)
    assert t1.margin_votes == 0 and t1.margin_pct == 0.0
    assert t1.ranking[:2] in ([0, 1], [1, 0]) and t1.ranking[2] == 2
    winners = {tabulate(rv, tie_seed=s).winner for s in range(40)}
    assert winners == {0, 1}  # the lot really depends on the seed


def test_unresolved_tie() -> None:
    rv = _rv([[25, 25]], ["a", "b"])
    t = tabulate(rv, resolve_ties=False)
    assert t.tied and t.winner is None and t.decided_by is None and decided_by(t) is None


def test_int_second_argument_is_tie_seed() -> None:
    rv = _rv([[25, 25]], ["a", "b"])
    assert tabulate(rv, 7).winner == tabulate(rv, tie_seed=7).winner
    assert tabulate(rv, 7).race_key == "HOUSE-NB-07"
    assert tabulate(rv, "HOUSE-NB-99").race_key == "HOUSE-NB-99"


def test_single_line_and_zero_votes() -> None:
    t = tabulate_totals("X", ["solo"], [100])
    assert t.winner == 0 and t.margin_votes == 100 and t.margin_pct == 100.0
    z = tabulate_totals("Y", ["a", "b"], [0, 0], tie_seed=3)
    assert z.tied and z.winner in (0, 1) and z.shares.tolist() == [0.0, 0.0]


def test_withdrawn_line_still_counts_and_can_win() -> None:
    lines = [
        BallotLine("jan-de-vries", "AAA", withdrawn=True),
        BallotLine("els-bakker", "BBB"),
    ]
    spec = RaceSpec("HOUSE-UT-03", RaceType.HOUSE, np.arange(2), lines)
    assert spec.active_lines == [1]
    rv = _rv([[60, 40], [50, 45]], spec.line_keys)
    t = tabulate(rv)
    assert t.totals.tolist() == [110, 85]
    assert t.winner_key == "jan-de-vries"  # a withdrawn candidate's printed ballots still count


def test_party_disappearance_line_without_party() -> None:
    lines = [BallotLine("ind-1", None), BallotLine("p-1", "PPP")]
    rv = _rv([[70, 30]], [ln.key for ln in lines])
    t = tabulate(rv)
    party = {ln.key: ln.party_code for ln in lines}
    comp = house_composition({"UT-03": party[t.winner_key], "UT-04": "PPP"})
    assert comp == {INDEPENDENT: 1, "PPP": 1}


def test_negative_votes_rejected() -> None:
    rv = _rv([[1, 2]], ["a", "b"])
    rv.votes[0, 0] = -1
    with pytest.raises(Exception, match="negative"):
        tabulate(rv)


# --------------------------------------------------------------------------- aggregation
def _districts_for(frame) -> tuple[np.ndarray, list[str]]:  # type: ignore[no-untyped-def]
    """Fake districts nested in provinces: province code + (municipality index mod 3)."""
    labels = np.array(
        [
            f"{frame.province_codes[p]}-{(m % 3) + 1:02d}"
            for p, m in zip(frame.unit_province, frame.unit_muni, strict=True)
        ],
        dtype=object,
    )
    return labels, sorted(set(labels))


def test_aggregate_levels_reconcile_national_race(synthetic, race_votes_factory) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    rv = race_votes_factory("PRES", [0.3, 0.25, 0.2, 0.15, 0.1], ["A", "B", "C", "D", "E"])
    labels, codes = _districts_for(frame)
    levels = aggregate_levels(rv, frame, unit_district=labels)
    assert list(levels) == ["unit", "municipality", "district", "province", "national"]
    for df in levels.values():
        assert tuple(df.columns) == LEVEL_COLUMNS
    assert reconcile(levels) == []
    nat = levels["national"]
    assert nat["votes"].tolist() == rv.totals().tolist()
    assert int(nat["valid_votes"].iloc[0]) == int(rv.valid.sum())
    assert int(nat["ballots_cast"].iloc[0]) == int(rv.ballots_cast.sum())
    assert len(levels["unit"]) == frame.n_units * 5
    assert len(levels["municipality"]) == frame.n_munis * 5
    assert len(levels["province"]) == 12 * 5
    assert len(levels["district"]) == len(codes) * 5
    # national winner flag == tabulated winner
    t = tabulate(rv)
    assert nat.loc[nat["winner"], "line_key"].tolist() == [t.winner_key]
    # exact integer dtype
    assert levels["province"]["votes"].dtype == np.int64
    shares = levels["province"].groupby("geo_code")["share"].sum()
    assert np.allclose(shares.to_numpy(), 1.0)
    full = results_frame(levels)
    assert set(full["level"]) == {"unit", "municipality", "district", "province", "national"}


def test_aggregate_province_race_and_district_alignment(synthetic, race_votes_factory) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    p = frame.province_index("NB")
    idx = frame.units_in_province(p)
    rv = race_votes_factory("PRES-NB", [0.36, 0.35, 0.29], ["A", "B", "C"], unit_index=idx)
    labels, _ = _districts_for(frame)
    by_frame = aggregate_levels(rv, frame, unit_district=labels)
    by_race = aggregate_levels(rv, frame, unit_district=labels[idx])
    assert by_frame["district"].equals(by_race["district"])
    assert reconcile(by_frame) == []
    assert by_frame["province"]["geo_code"].unique().tolist() == ["NB"]
    assert set(by_frame["municipality"]["province_code"]) == {"NB"}
    # integer district indices with explicit codes give the same result
    codes = sorted(set(labels[idx]))
    as_int = np.array([codes.index(x) for x in labels[idx]])
    by_int = aggregate_levels(rv, frame, unit_district=as_int, district_codes=codes)
    assert by_int["district"]["votes"].tolist() == by_race["district"]["votes"].tolist()


def test_reconcile_detects_inconsistency(synthetic, race_votes_factory) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    rv = race_votes_factory("PRES", [0.5, 0.5], ["A", "B"])
    levels = aggregate_levels(rv, frame)
    assert reconcile(levels) == []
    bad = {k: v.copy() for k, v in levels.items()}
    bad["municipality"].loc[0, "votes"] += 1
    problems = reconcile(bad)
    assert any("unit→municipality" in p for p in problems)
    bad2 = {k: v.copy() for k, v in levels.items()}
    bad2["national"].loc[0, "ballots_cast"] += 5
    assert reconcile(bad2)
    assert reconcile({"unit": levels["unit"]}) != []


def test_district_spanning_provinces_rejected(synthetic, race_votes_factory) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    rv = race_votes_factory("PRES", [0.5, 0.5], ["A", "B"])
    with pytest.raises(Exception, match="spans"):
        aggregate_levels(rv, frame, unit_district=np.zeros(frame.n_units, dtype=int), district_codes=["X-01"])


def test_unit_level_ties_are_not_flagged(synthetic) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    idx = np.arange(3)
    v = np.array([[5, 5], [0, 0], [7, 1]], dtype=np.int64)
    rv = RaceVotes(
        "MAYOR-GM0101",
        ["A", "B"],
        idx,
        v,
        v.sum(1),
        np.zeros(3, np.int64),
        np.zeros(3, np.int64),
        frame.unit_eligible[idx],
    )
    levels = aggregate_levels(rv, frame)
    unit = levels["unit"]
    assert unit.groupby("geo_code", sort=False)["winner"].sum().tolist() == [0, 0, 1]


@pytest.mark.realdata
def test_aggregate_levels_reconcile_on_real_cbs_frame(real_frame) -> None:  # type: ignore[no-untyped-def]
    from app.core.rng import make_rng

    frame = real_frame
    rng = make_rng(2028, "test-real-tabulation")
    idx = np.arange(frame.n_units)
    eligible = frame.unit_eligible.astype(np.int64)
    cast = rng.binomial(eligible, 0.78).astype(np.int64)
    invalid = rng.binomial(cast, 0.005).astype(np.int64)
    valid = cast - invalid
    votes = np.stack([rng.multinomial(int(v), [0.3, 0.25, 0.2, 0.15, 0.1]) for v in valid]).astype(np.int64)
    rv = RaceVotes("PRES", list("ABCDE"), idx, votes, cast, np.zeros_like(cast), invalid, eligible)
    labels = np.array(
        [
            f"{frame.province_codes[p]}-{m % 4:02d}"
            for p, m in zip(frame.unit_province, frame.unit_muni, strict=True)
        ],
        dtype=object,
    )
    levels = aggregate_levels(rv, frame, unit_district=labels)
    assert reconcile(levels) == []
    assert levels["municipality"]["geo_code"].nunique() == frame.n_munis
    assert levels["province"]["geo_code"].nunique() == frame.n_provinces == 12
    assert levels["national"]["votes"].tolist() == votes.sum(axis=0).tolist()
