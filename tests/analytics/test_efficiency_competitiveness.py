"""Competitiveness, wasted votes, efficiency gap and the seat–vote relationship."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analytics import metrics as M


def test_competitiveness_index_rating_and_enc(house_curr: pd.DataFrame) -> None:
    comp = M.competitiveness(house_curr).set_index("geo_code")
    assert comp.loc["NB-01", "competitiveness"] == pytest.approx(0.8)
    assert comp.loc["NB-01", "rating"] == "lean"
    assert comp.loc["NB-02", "competitiveness"] == 0.0 and comp.loc["NB-02", "rating"] == "safe"
    assert comp.loc["UT-01", "competitiveness"] == pytest.approx(0.75)
    assert comp.loc["NB-01", "enc"] == pytest.approx(1 / (0.48**2 + 0.52**2))
    assert comp.loc["UT-01", "enc"] == pytest.approx(1 / (0.5**2 + 0.45**2 + 0.05**2))
    assert comp.loc["UT-02", "enc"] == pytest.approx(1.0)
    wide = M.competitiveness(house_curr, threshold_pp=40.0).set_index("geo_code")
    assert wide.loc["NB-02", "competitiveness"] == pytest.approx(0.5)
    with pytest.raises(ValueError):
        M.competitiveness(house_curr, threshold_pp=0)


def test_rating_bands() -> None:
    out = M.rating_for_margin(np.array([0.0, 2.99, 3.0, 7.9, 8.0, 14.9, 15.0, 60.0, np.nan]))
    assert list(out) == ["tossup", "tossup", "lean", "lean", "likely", "likely", "safe", "safe", None]


def test_competitiveness_summary(house_curr: pd.DataFrame) -> None:
    s = M.competitiveness_summary(M.competitiveness(house_curr)).set_index("province_code")
    assert s.loc["NB", "n_contests"] == 2 and s.loc["NB", "n_lean"] == 1 and s.loc["NB", "n_safe"] == 1
    assert s.loc["UT", "mean_competitiveness"] == pytest.approx((0.75 + 0.0) / 2)
    assert s.loc["NB", "n_tossup"] == 0


def test_probability_competitiveness() -> None:
    out = M.probability_competitiveness(np.array([0.5, 0.75, 1.0, 0.0, 0.25]))
    assert out.tolist() == pytest.approx([1.0, 0.5, 0.0, 0.0, 0.5])


def test_wasted_votes_hand_computed(house_prev: pd.DataFrame) -> None:
    wv = M.wasted_votes(house_prev).set_index("line_key")
    assert tuple(M.wasted_votes(house_prev).columns) == M.WASTED_COLUMNS
    assert wv.loc["a1", "wasted_surplus"] == 60 - (40 + 1) and wv.loc["a1", "won"]
    assert wv.loc["b1", "wasted_losing"] == 40
    assert wv.loc["c3", "wasted"] == 40 - 31
    assert wv.loc["a3", "wasted"] == 30 and wv.loc["b3", "wasted"] == 30


def test_wasted_votes_edge_cases(make_house) -> None:
    f = make_house(
        1,
        2028,
        {
            "NB-01": [("u", "A", 50)],  # uncontested: needed one vote
            "NB-02": [("x", "A", 0), ("y", "B", 0)],  # no votes: nobody wins, nothing wasted
            "NB-03": [("p", "A", 40), ("q", "B", 40)],  # exact tie
        },
    )
    wv = M.wasted_votes(f).set_index("line_key")
    assert wv.loc["u", "wasted_surplus"] == 49
    assert not wv.loc["x", "won"] and wv.loc["x", "wasted"] == 0 and wv.loc["y", "wasted"] == 0
    assert wv.loc["p", "won"] and wv.loc["p", "wasted"] == 0 and wv.loc["q", "wasted"] == 40


def test_wasted_votes_exclude_national_presidential_parent(make_pres) -> None:
    f = make_pres(1, 2028, {"NB": {"A": 60, "B": 40}, "UT": {"A": 30, "B": 70}})
    assert set(M.wasted_votes(f)["race_code"]) == {"PRES-NB", "PRES-UT"}


def test_efficiency_gap_sign_and_magnitude_packing(make_house) -> None:
    # A is packed into one district and cracked across four: the map favours B.
    districts = {"NB-01": [("a", "A", 90), ("b", "B", 10)]}
    for i in range(2, 6):
        districts[f"NB-0{i}"] = [("a", "A", 45), ("b", "B", 55)]
    f = make_house(1, 2028, districts)
    eg = M.efficiency_gap(f, "A", "B").iloc[0]
    assert eg["wasted_a"] == (90 - 11) + 4 * 45 and eg["wasted_b"] == 10 + 4 * (55 - 46)
    assert eg["efficiency_gap"] == pytest.approx((46 - 259) / 500)
    assert eg["efficiency_gap"] < 0  # negative: disadvantages party_a
    assert eg["seats_a"] == 1 and eg["seats_b"] == 4
    swapped = M.efficiency_gap(f, "B", "A").iloc[0]
    assert swapped["efficiency_gap"] == pytest.approx(-eg["efficiency_gap"])
    default = M.efficiency_gap(f).iloc[0]
    assert (default["party_a"], default["party_b"]) == ("A", "B")


def test_efficiency_gap_multiparty(house_prev: pd.DataFrame) -> None:
    eg = M.efficiency_gap(house_prev, "A", "B").iloc[0]
    assert eg["wasted_a"] == 94 and eg["wasted_b"] == 79 and eg["total_votes"] == 300
    assert eg["efficiency_gap"] == pytest.approx(-0.05)
    assert eg["efficiency_gap_pair"] == pytest.approx(-15 / 260)
    pe = M.party_efficiency(house_prev).set_index("party")
    W, V = 182.0, 300.0
    assert pe.loc["A", "efficiency_gap"] == pytest.approx((135 * W / V - 94) / V)
    assert pe.loc["C", "efficiency_gap"] == pytest.approx((40 * W / V - 9) / V)
    assert pe["efficiency_gap"].sum() == pytest.approx(0.0, abs=1e-12)
    assert pe.loc["C", "votes_per_seat"] == pytest.approx(40.0)
    assert pe.loc["A", "seat_bonus_pp"] == pytest.approx(100 * (1 / 3 - 0.45))
    assert pe.loc["A", "waste_rate"] == pytest.approx(94 / 135)


def test_party_efficiency_votes_per_seat_nan_without_seats(house_curr: pd.DataFrame) -> None:
    pe = M.party_efficiency(house_curr).set_index("party")
    assert np.isnan(pe.loc["C", "votes_per_seat"])
    assert pe["seats_total"].iloc[0] == 4


def test_seat_vote_table_weighted(make_pres, canonical_ev) -> None:
    provinces = {
        p: ({"A": 55, "B": 45} if p in {"ZH", "NH", "UT", "NB"} else {"A": 40, "B": 60}) for p in canonical_ev
    }
    f = make_pres(1, 2028, provinces)
    sv = M.seat_vote_table(f, seat_weights=canonical_ev).set_index("party")
    assert sv.loc["A", "seats"] == pytest.approx(99.0) and sv.loc["A", "seats_total"] == pytest.approx(174.0)
    assert sv.loc["A", "vote_share"] == pytest.approx(540 / 1200)
    assert tuple(M.seat_vote_table(f).columns) == M.SEAT_VOTE_COLUMNS


def test_seat_vote_fit_linear_and_logit() -> None:
    v = np.array([0.40, 0.45, 0.50, 0.55])
    table = pd.DataFrame(
        {"election_id": [1, 2, 3, 4], "party": "A", "vote_share": v, "seat_share": 0.52 + 2 * (v - 0.5)}
    )
    fit = M.seat_vote_fit(table, reference_share=0.5).iloc[0]
    assert fit["responsiveness"] == pytest.approx(2.0) and fit["bias_pp"] == pytest.approx(2.0)
    assert fit["r_squared"] == pytest.approx(1.0) and fit["n_obs"] == 4
    default_ref = M.seat_vote_fit(table).iloc[0]
    assert default_ref["reference_share"] == pytest.approx(0.475)
    assert default_ref["bias_pp"] == pytest.approx(-0.5)
    logit = lambda x: np.log(x / (1 - x))  # noqa: E731
    s = 1 / (1 + np.exp(-(0.1 + 3.0 * logit(v))))
    lt = M.seat_vote_fit(table.assign(seat_share=s), method="logit", reference_share=0.5).iloc[0]
    assert lt["responsiveness"] == pytest.approx(3.0) and lt["intercept"] == pytest.approx(0.1)
    assert lt["bias_pp"] == pytest.approx(100 * (1 / (1 + np.exp(-0.1)) - 0.5))
    single = M.seat_vote_fit(table.iloc[:1]).iloc[0]
    assert np.isnan(single["responsiveness"])
    with pytest.raises(ValueError):
        M.seat_vote_fit(table, method="cubic")  # type: ignore[arg-type]


def test_seat_vote_from_draws_round_trip() -> None:
    V = np.array([[0.4, 0.6], [0.5, 0.5], [0.6, 0.4]])
    S = np.array([[0.3, 0.7], [0.5, 0.5], [0.7, 0.3]])
    table = M.seat_vote_from_draws(V, S, ["A", "B"], race_family="HOUSE")
    assert len(table) == 6 and set(table.columns) >= {"draw", "party", "vote_share", "seat_share"}
    fit = M.seat_vote_fit(table, sample_col="draw").set_index("party")
    assert fit.loc["A", "responsiveness"] == pytest.approx(2.0) and fit.loc["A", "n_obs"] == 3
    with pytest.raises(ValueError):
        M.seat_vote_from_draws(V, S[:2], ["A", "B"])


def test_partisan_bias_symmetric_and_packed(make_house) -> None:
    sym = make_house(
        1,
        2028,
        {f"NB-0{i}": [("a", "A", a), ("b", "B", 100 - a)] for i, a in enumerate((40, 45, 55, 60), start=1)},
    )
    pb = M.partisan_bias(sym, "A", "B")
    assert pb.bias == pytest.approx(0.0) and pb.vote_share_2p_a == pytest.approx(0.5)
    assert pb.seat_share_a == pytest.approx(0.5)
    wide = M.partisan_bias(sym, "A", "B", h=0.06)
    assert wide.responsiveness == pytest.approx((0.75 - 0.25) / 0.12)
    packed = make_house(
        2,
        2028,
        {f"NB-0{i}": [("a", "A", a), ("b", "B", 100 - a)] for i, a in enumerate((90, 45, 45, 45), start=1)},
    )
    pp = M.partisan_bias(packed, "A", "B")
    assert pp.vote_share_2p_a == pytest.approx(0.5625)
    assert pp.seat_share_a_at_parity == pytest.approx(0.25) and pp.bias == pytest.approx(-0.25)
    with pytest.raises(ValueError):
        M.partisan_bias(sym, "A", "B", h=0.5)


def test_uns_seat_vote_curve_monotone(house_prev: pd.DataFrame) -> None:
    curve = M.uns_seat_vote_curve(house_prev, "A", "B")
    assert len(curve) == 41 and curve["vote_share_2p_a"].iloc[0] == pytest.approx(0.30)
    assert (np.diff(curve["seat_share_a"]) >= -1e-12).all()
    assert (np.diff(curve["seat_share_b"]) <= 1e-12).all()
    assert np.allclose(curve["seat_share_a"] + curve["seat_share_b"] + curve["seat_share_other"], 1.0)
    with pytest.raises(ValueError):
        M.uns_seat_vote_curve(house_prev, "A", "Z")
