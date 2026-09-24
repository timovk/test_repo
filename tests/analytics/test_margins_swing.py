"""Margins, two-party share, swing, flips and turnout on hand-computed frames."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analytics import metrics as M
from app.analytics.results import INDEPENDENT_KEY, ResultsFrameError, build_results_frame


def test_margin_table_multiparty(house_prev: pd.DataFrame) -> None:
    mt = M.margin_table(house_prev).set_index("geo_code")
    assert tuple(M.margin_table(house_prev).columns) == M.MARGIN_COLUMNS
    assert mt.loc["NB-01", "winner_party"] == "A" and mt.loc["NB-01", "margin_pp"] == pytest.approx(20.0)
    ut = mt.loc["UT-01"]
    assert ut["winner_party"] == "C" and ut["runner_up_votes"] == 30
    assert ut["margin_votes"] == 10 and ut["margin_pp"] == pytest.approx(10.0)
    assert ut["n_lines"] == 3 and not ut["tied"]
    # runner-up among the tied A/B pair: lowest line key
    assert ut["runner_up_line_key"] == "a3"


def test_margin_table_ties_zero_votes_and_uncontested(make_rows) -> None:
    rows = []
    rows += make_rows(
        1,
        2028,
        "SEN-ZE-1",
        "SENATE",
        "province",
        "ZE",
        [("x", "A", 50), ("y", "B", 50)],
        province_code="ZE",
        winner="y",
    )
    rows += make_rows(
        1, 2028, "GOV-FL", "GOVERNOR", "province", "FL", [("p", "A", 0), ("q", "B", 0)], province_code="FL"
    )
    rows += make_rows(
        1, 2028, "HOUSE-DR-01", "HOUSE", "district", "DR-01", [("s", "C", 77)], province_code="DR"
    )
    mt = M.margin_table(build_results_frame(rows)).set_index("race_code")
    tie = mt.loc["SEN-ZE-1"]
    assert tie["tied"] and tie["margin_pp"] == 0.0 and tie["winner_line_key"] == "y"  # lot winner ranks first
    zero = mt.loc["GOV-FL"]
    assert np.isnan(zero["margin_pp"]) and zero["winner_party"] is None and zero["tied"]
    unc = mt.loc["HOUSE-DR-01"]
    assert unc["margin_pp"] == pytest.approx(100.0) and not unc["contested"] and unc["runner_up_votes"] == 0


def test_top_two_margin_vectorised() -> None:
    assert M.top_two_margin([50, 30, 20]) == pytest.approx(20.0)
    out = M.top_two_margin(np.array([[1, 1, 2], [0, 0, 0], [5, 0, 0]]))
    assert out[0] == pytest.approx(25.0) and np.isnan(out[1]) and out[2] == pytest.approx(100.0)
    assert M.top_two_margin([7]) == pytest.approx(100.0)


def test_two_party_share_explicit_and_default_pair(house_curr: pd.DataFrame) -> None:
    tp = M.two_party_share(house_curr, "A", "B").set_index("geo_code")
    assert tp.loc["NB-01", "two_party_share_a"] == pytest.approx(0.48)
    assert tp.loc["UT-01", "two_party_share_a"] == pytest.approx(1.0)  # B did not run
    assert tp.loc["UT-01", "margin_pp"] == pytest.approx(50.0)
    assert tp.loc["UT-01", "two_party_margin_pp"] == pytest.approx(100.0)
    assert M.national_top_two(house_curr) == ("A", "B")
    default = M.two_party_share(house_curr)
    assert set(default["party_a"]) == {"A"} and set(default["party_b"]) == {"B"}
    with pytest.raises(ValueError):
        M.two_party_share(house_curr, "A", None)
    with pytest.raises(ValueError):
        M.two_party_share(house_curr, "A", "A")


def test_two_party_share_nan_when_neither_ran(make_rows) -> None:
    df = build_results_frame(
        make_rows(1, 2028, "HOUSE-LI-01", "HOUSE", "district", "LI-01", [("c", "C", 10)], province_code="LI")
    )
    tp = M.two_party_share(df, "A", "B")
    assert np.isnan(tp["two_party_share_a"].iloc[0])


def test_national_top_two_requires_single_group(house_prev, house_curr) -> None:
    with pytest.raises(ResultsFrameError):
        M.national_top_two(pd.concat([house_prev, house_curr]))


def test_swing_statuses_and_values(house_prev: pd.DataFrame, house_curr: pd.DataFrame) -> None:
    sw = M.swing(house_prev, house_curr)
    assert tuple(sw.columns) == M.SWING_COLUMNS
    s = sw.set_index(["geo_code", "party"])
    assert s.loc[("NB-01", "A"), "swing_pp"] == pytest.approx(-12.0)
    assert s.loc[("NB-01", "B"), "swing_pp"] == pytest.approx(12.0)
    assert s.loc[("NB-01", "A"), "vote_change"] == -12
    assert s.loc[("NB-02", "B"), "vote_change_pct"] == pytest.approx(100.0 * 5 / 55)
    assert s.loc[("UT-01", "B"), "status"] == "dropped_party"
    assert s.loc[("UT-01", "B"), "share_curr"] == 0.0 and s.loc[("UT-01", "B"), "swing_pp"] == pytest.approx(
        -30.0
    )
    assert s.loc[("UT-01", INDEPENDENT_KEY), "status"] == "new_party"
    assert s.loc[("UT-01", INDEPENDENT_KEY), "swing_pp"] == pytest.approx(5.0)
    new = s.loc[("UT-02", "A")]
    assert new["status"] == "new_geo" and np.isnan(new["share_prev"]) and np.isnan(new["swing_pp"])
    assert pd.isna(new["votes_prev"]) and new["votes_curr"] == 10
    assert sw["votes_prev"].dtype == "Int64"
    assert set(sw["year_prev"]) == {2028} and set(sw["year_curr"]) == {2030}
    only_a = M.swing(house_prev, house_curr, parties=["A"])
    assert set(only_a["party"]) == {"A"}


def test_swing_dropped_geo(house_prev: pd.DataFrame, house_curr: pd.DataFrame) -> None:
    sw = M.swing(house_curr, house_prev)  # reversed: UT-02 disappears
    assert set(sw.loc[sw["geo_code"] == "UT-02", "status"]) == {"dropped_geo"}


def test_swing_requires_single_elections(house_prev, house_curr) -> None:
    with pytest.raises(ResultsFrameError):
        M.swing(pd.concat([house_prev, house_curr]), house_curr)
    with pytest.raises(ResultsFrameError):
        M.swing(house_prev.iloc[0:0], house_curr)  # missing previous election


def test_two_party_swing_and_national_swing(house_prev: pd.DataFrame, house_curr: pd.DataFrame) -> None:
    tps = M.two_party_swing(house_prev, house_curr, "A", "B").set_index("geo_code")
    assert tps.loc["NB-01", "two_party_swing_pp"] == pytest.approx(-12.0)
    assert tps.loc["UT-01", "two_party_swing_pp"] == pytest.approx(50.0)  # 50/50 → 100 % of the pair
    assert np.isnan(tps.loc["UT-02", "two_party_swing_pp"])
    ns = M.national_swing(house_prev, house_curr)
    assert ns["A"] == pytest.approx(100 * (148 / 310 - 135 / 300))
    assert ns["C"] == pytest.approx(100 * (45 / 310 - 40 / 300))
    assert ns[INDEPENDENT_KEY] == pytest.approx(100 * 5 / 310)
    assert M.national_shares(house_curr).sum() == pytest.approx(1.0)


def test_swing_ratio() -> None:
    out = M.swing_ratio(np.array([2.0, -1.0, 3.0]), 2.0)
    assert out.tolist() == pytest.approx([1.0, -0.5, 1.5])
    assert np.isnan(M.swing_ratio(np.array([1.0]), 0.0)).all()


def test_flips_and_summary(house_prev: pd.DataFrame, house_curr: pd.DataFrame) -> None:
    fl = M.flips(house_prev, house_curr)
    assert tuple(fl.columns) == M.FLIP_COLUMNS
    st = fl.set_index("geo_code")
    assert (
        st.loc["NB-01", "status"] == "flip"
        and st.loc["NB-01", "gained_by"] == "B"
        and st.loc["NB-01", "lost_by"] == "A"
    )
    assert st.loc["NB-02", "status"] == "hold" and not st.loc["NB-02", "flipped"]
    assert st.loc["UT-01", "status"] == "flip" and st.loc["UT-01", "gained_by"] == "A"
    assert st.loc["UT-02", "status"] == "new"
    summary = M.flip_summary(fl).set_index("party")
    assert summary.loc["A"].to_dict() == {
        "wins_prev": 1,
        "wins_curr": 2,
        "gains": 1,
        "losses": 1,
        "holds": 0,
        "net": 1,
    }
    assert summary.loc["B"].to_dict() == {
        "wins_prev": 1,
        "wins_curr": 2,
        "gains": 1,
        "losses": 0,
        "holds": 1,
        "net": 1,
    }
    assert summary.loc["C"].to_dict() == {
        "wins_prev": 1,
        "wins_curr": 0,
        "gains": 0,
        "losses": 1,
        "holds": 0,
        "net": -1,
    }
    weighted = M.flip_summary(fl, weights={"NB-01": 5, "NB-02": 3, "UT-01": 2, "UT-02": 1}).set_index("party")
    assert weighted.loc["B", "wins_curr"] == pytest.approx(8.0) and weighted.loc[
        "A", "gains"
    ] == pytest.approx(2.0)
    with pytest.raises(ValueError):
        M.flip_summary(fl, weights={"NB-01": 1})


def test_flips_undecided_and_dropped(make_house) -> None:
    prev = make_house(1, 2028, {"NB-01": [("a", "A", 0), ("b", "B", 0)], "NB-02": [("a", "A", 5)]})
    curr = make_house(2, 2030, {"NB-01": [("a", "A", 10), ("b", "B", 20)]})
    st = M.flips(prev, curr).set_index("geo_code")["status"].to_dict()
    assert st == {"NB-01": "undecided", "NB-02": "dropped"}


def test_flips_family_mode(make_pres) -> None:
    prev = make_pres(1, 2028, {"NB": {"A": 60, "B": 40}}, {"GM0855": ("NB", {"A": 20, "B": 10})})
    curr = make_pres(2, 2032, {"NB": {"A": 40, "B": 60}}, {"GM0855": ("NB", {"A": 5, "B": 10})})
    fl = M.flips(prev, curr, level="municipality", by="family")
    assert fl["status"].tolist() == ["flip"] and fl["race_code"].isna().all()
    assert fl["margin_prev_pp"].iloc[0] == pytest.approx(100 * (20 - 10) / 30)


def test_turnout_and_change(make_house) -> None:
    prev = make_house(
        1, 2028, {"NB-01": [("a", "A", 60), ("b", "B", 40)], "LI-01": [("a", "A", 0)]}, eligible=200
    )
    curr = make_house(2, 2030, {"NB-01": [("a", "A", 70), ("b", "B", 50)]}, eligible=200, extra_ballots=10)
    t = M.turnout(curr).set_index("geo_code")
    assert t.loc["NB-01", "turnout"] == pytest.approx(130 / 200)
    assert t.loc["NB-01", "blank_or_invalid"] == 10
    tc = M.turnout_change(prev, curr).set_index("geo_code")
    assert tc.loc["NB-01", "turnout_change_pp"] == pytest.approx(100 * (130 - 100) / 200)
    assert tc.loc["NB-01", "ballots_change"] == 30
    assert tc.loc["LI-01", "status"] == "dropped_geo"
    zero = make_house(3, 2032, {"FL-01": [("a", "A", 0)]}, eligible=0)
    assert np.isnan(M.turnout(zero)["turnout"].iloc[0])


def test_race_summaries_with_incumbents(house_prev: pd.DataFrame, house_curr: pd.DataFrame) -> None:
    inc = pd.DataFrame(
        {
            "race_code": ["HOUSE-NB-01", "HOUSE-NB-02", "HOUSE-UT-02"],
            "incumbent_candidate": ["Cand a1", "Cand b2", None],
            "incumbent_party": ["A", "B", "C"],
            "is_open_seat": [False, False, True],
        }
    )
    rs = M.race_summaries(house_curr, prev=house_prev, incumbents=inc).set_index("race_code")
    assert tuple(M.race_summaries(house_curr).columns) == M.RACE_SUMMARY_COLUMNS
    assert (
        rs.loc["HOUSE-NB-01", "flip_status"] == "flip"
        and bool(rs.loc["HOUSE-NB-01", "incumbent_won"]) is False
    )
    assert (
        rs.loc["HOUSE-NB-02", "flip_status"] == "hold"
        and bool(rs.loc["HOUSE-NB-02", "incumbent_won"]) is True
    )
    # no race in prev → incumbent party used as previous holder
    assert (
        rs.loc["HOUSE-UT-02", "previous_winner_party"] == "C"
        and rs.loc["HOUSE-UT-02", "flip_status"] == "flip"
    )
    assert pd.isna(rs.loc["HOUSE-UT-02", "incumbent_won"])
    assert rs.loc["HOUSE-NB-02", "turnout"] == pytest.approx(1.0)
    no_info = M.race_summaries(house_curr)
    assert set(no_info["flip_status"]) == {"new"}
