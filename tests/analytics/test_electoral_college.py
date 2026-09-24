"""Electoral College efficiency, tipping point and EC bias (implemented locally in analytics)."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from app.analytics import metrics as M
from app.analytics.history import ec_pv_divergence
from app.analytics.results import ResultsFrameError
from app.core.constitution import PRESIDENTIAL_MAJORITY

WIN_A = {"ZH", "NH", "UT", "NB"}


@pytest.fixture()
def pres_2028(make_pres, canonical_ev) -> pd.DataFrame:
    provinces = {p: ({"A": 55, "B": 45} if p in WIN_A else {"A": 40, "B": 60}) for p in canonical_ev}
    return make_pres(1, 2028, provinces)


def test_ec_efficiency() -> None:
    out = M.ec_efficiency({"A": 520, "B": 480}, {"A": 100, "B": 74}).set_index("key")
    assert out.loc["A", "efficiency_pp"] == pytest.approx(100 * (100 / 174 - 0.52))
    assert out.loc["B", "efficiency_pp"] == pytest.approx(100 * (74 / 174 - 0.48))
    assert out["efficiency_pp"].sum() == pytest.approx(0.0)
    third = M.ec_efficiency({"A": 50, "B": 40, "C": 10}, {"A": 174}).set_index("key")
    assert third.loc["C", "electoral_votes"] == 0 and third.loc["C", "efficiency_pp"] == pytest.approx(-10.0)


def test_tipping_point_canonical_map(canonical_ev) -> None:
    margins = {p: -5.0 for p in canonical_ev} | {"ZH": 10.0, "NH": 8.0, "UT": 6.0, "NB": 2.0, "GE": -1.0}
    tp = M.tipping_point(margins, canonical_ev)
    assert tp.majority == PRESIDENTIAL_MAJORITY
    assert tp.order[:5] == ("ZH", "NH", "UT", "NB", "GE")
    assert tp.province == "NB" and tp.margin_pp == 2.0 and tp.cumulative_ev == 34 + 27 + 14 + 24
    assert tp.cumulative[-1] == sum(canonical_ev.values())
    lower = M.tipping_point(margins, canonical_ev, majority=60)
    assert lower.province == "NH"


def test_tipping_point_ties_keep_map_order_and_nan_last(canonical_ev) -> None:
    margins = {p: -5.0 for p in canonical_ev} | {"ZH": 10.0, "NH": 8.0, "UT": 6.0, "NB": 6.0}
    tp = M.tipping_point(margins, canonical_ev)
    assert tp.order[2:4] == ("UT", "NB") and tp.province == "NB"
    with_nan = margins | {"GR": math.nan}
    assert M.tipping_point(with_nan, canonical_ev).order[-1] == "GR"


def test_tipping_point_validation(canonical_ev) -> None:
    with pytest.raises(ValueError):
        M.tipping_point({"ZH": 1.0}, canonical_ev)
    with pytest.raises(ValueError):
        M.tipping_point(dict.fromkeys(canonical_ev, 1.0), canonical_ev, majority=1000)


def test_ec_bias(canonical_ev) -> None:
    margins = {p: -5.0 for p in canonical_ev} | {"ZH": 10.0, "NH": 8.0, "UT": 6.0, "NB": 2.0}
    b = M.ec_bias(margins, canonical_ev, national_margin_pp=3.0, key="A")
    assert b.tipping_province == "NB" and b.tipping_margin_pp == 2.0
    assert b.bias_pp == pytest.approx(-1.0)


def test_electoral_college_tally_from_frame(pres_2028: pd.DataFrame, canonical_ev) -> None:
    tally = M.electoral_college_tally(pres_2028, canonical_ev).set_index("key")
    assert tally.loc["A", "electoral_votes"] == 99 and tally.loc["B", "electoral_votes"] == 75
    assert tally.loc["A", "provinces_won"] == 4
    assert tally.loc["A", "pv_share"] == pytest.approx(540 / 1200)
    assert tally.loc["A", "efficiency_pp"] == pytest.approx(100 * (99 / 174 - 0.45))
    by_line = M.electoral_college_tally(pres_2028, canonical_ev, by="line")
    assert set(by_line["key"]) == {"t-A", "t-B"}
    with pytest.raises(ResultsFrameError):
        M.electoral_college_tally(pres_2028[pres_2028["geo_code"] != "ZE"], canonical_ev)
    with pytest.raises(ValueError):
        M.electoral_college_tally(pres_2028, canonical_ev, by="candidate")  # type: ignore[arg-type]


def test_electoral_college_tally_uses_lot_winner(make_pres, canonical_ev) -> None:
    provinces = {p: ({"A": 55, "B": 45} if p in WIN_A else {"A": 40, "B": 60}) for p in canonical_ev}
    provinces["ZE"] = {"A": 50, "B": 50}
    f = make_pres(1, 2028, provinces, winners={"ZE": "A"})
    tally = M.electoral_college_tally(f, canonical_ev).set_index("key")
    assert tally.loc["A", "electoral_votes"] == 99 + canonical_ev["ZE"]


def test_tipping_point_from_frame_and_bias(pres_2028: pd.DataFrame, canonical_ev) -> None:
    b = M.tipping_point_from_frame(pres_2028, canonical_ev)
    assert b.key == "A"
    assert b.tipping_province == "NB" and b.tipping_margin_pp == pytest.approx(10.0)
    assert b.national_margin_pp == pytest.approx(-10.0)
    assert b.bias_pp == pytest.approx(20.0)  # the EC favours A by 20 pp relative to the popular vote
    loser = M.tipping_point_from_frame(pres_2028, canonical_ev, key="B")
    assert loser.tipping_margin_pp == pytest.approx(-10.0) and loser.bias_pp == pytest.approx(-20.0)
    with pytest.raises(ResultsFrameError, match="provinces mismatch"):
        M.tipping_point_from_frame(pres_2028[pres_2028["geo_code"] != "ZE"], canonical_ev, key="A")


def test_ec_pv_divergence_from_tally(pres_2028: pd.DataFrame, canonical_ev) -> None:
    tally = M.electoral_college_tally(pres_2028, canonical_ev)
    div = ec_pv_divergence(tally).iloc[0]
    assert div["diverged"] and div["ev_leader"] == "A" and div["pv_leader"] == "B"
    assert div["pv_margin_pp"] == pytest.approx(10.0) and div["ev_majority"]
    assert "A leads the electoral vote with 99 EV while B leads the popular vote" in div["description"]


def test_tipping_point_agrees_with_the_elections_engine_on_random_canonical_maps(
    make_pres, canonical_ev
) -> None:
    """Cross-check against :func:`app.elections.electoral_college.tipping_point` (same ordering,
    tie rule, majority and margin arithmetic) for every ticket on random canonical maps —
    including small vote counts that produce exactly tied margins."""
    from app.core.rng import make_rng
    from app.elections import electoral_college as EC
    from app.elections.tabulation import tabulate_totals

    rng = make_rng(2032, "analytics-test", "tipping-cross-check")
    checked = ties = 0
    for trial in range(120):
        parties = ["A", "B", "C", "D", "E"][: int(rng.integers(2, 6))]
        high = 12 if trial % 3 == 0 else 5_000_000  # small counts → exact margin ties
        provinces = {
            pv: {
                p: int(v) for p, v in zip(parties, rng.integers(0, high, size=len(parties)) + 1, strict=True)
            }
            for pv in canonical_ev
        }
        frame = make_pres(1, 2028, provinces)
        tabs = {
            pv: tabulate_totals(f"PRES-{pv}", [f"t-{p}" for p in parties], list(votes.values()))
            for pv, votes in provinces.items()
        }
        majority = None if trial % 4 else int(rng.integers(1, sum(canonical_ev.values()) + 1))
        for p in parties:
            key = f"t-{p}"
            engine = EC.tipping_point(tabs, canonical_ev, key, majority=majority)
            ours = M.tipping_point_from_frame(frame, canonical_ev, key=key, by="line", majority=majority)
            assert (ours.tipping_province, ours.tipping_margin_pp) == engine, (trial, key)
            margins = EC.province_margins(tabs, key)
            assert M.tipping_point(margins, canonical_ev, majority).province == engine[0]
            ties += len(set(margins.values())) < len(margins)
            checked += 1
    assert checked > 300 and ties > 25


def test_tipping_point_zero_vote_province_sorts_last(canonical_ev) -> None:
    """Documented difference from the elections engine: a province without valid votes has an
    undefined (NaN) margin and sorts last (the engine treats it as a 0 pp margin)."""
    margins = {p: -5.0 for p in canonical_ev} | {"ZH": 10.0, "NH": 8.0, "UT": 6.0, "NB": 2.0, "GR": math.nan}
    tp = M.tipping_point(margins, canonical_ev)
    assert tp.order[-1] == "GR" and tp.province == "NB"
