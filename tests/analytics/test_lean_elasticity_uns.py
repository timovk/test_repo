"""Partisan lean, elasticity and uniform-national-swing projection."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analytics import metrics as M
from app.analytics.results import INDEPENDENT_KEY, build_results_frame
from app.core.rng import make_rng

NAT_A = [0.30, 0.35, 0.40, 0.33, 0.38]


@pytest.fixture()
def elasticity_frame(make_rows) -> pd.DataFrame:
    """Five presidential elections; municipality shares are exact linear functions of the
    national share: GM0001 = 0.1 + 1.5·nat, GM0002 = 0.5 + 0.5·nat (for party A)."""
    rows: list[dict] = []
    for t, nat in enumerate(NAT_A):
        eid, year = t + 1, 2028 + 4 * t
        a = round(nat * 1_000_000)
        rows += make_rows(
            eid, year, "PRES", "PRESIDENT", "national", "NL", [("ta", "A", a), ("tb", "B", 1_000_000 - a)]
        )
        for gm, (c0, c1) in {"GM0001": (0.1, 1.5), "GM0002": (0.5, 0.5)}.items():
            va = round((c0 + c1 * nat) * 100_000)
            rows += make_rows(
                eid,
                year,
                "PRES-NB",
                "PRESIDENT_PROVINCE",
                "municipality",
                gm,
                [("ta", "A", va), ("tb", "B", 100_000 - va)],
                province_code="NB",
            )
    return build_results_frame(rows)


def test_partisan_lean_hand_computed(make_pres) -> None:
    f = make_pres(1, 2028, {"NB": {"A": 60, "B": 40}, "UT": {"A": 30, "B": 70}})
    lean = M.partisan_lean(f, level="province").set_index(["geo_code", "party"])
    assert lean.loc[("NB", "A"), "national_share"] == pytest.approx(0.45)
    assert lean.loc[("NB", "A"), "lean_pp"] == pytest.approx(15.0)
    assert lean.loc[("UT", "A"), "lean_pp"] == pytest.approx(-15.0)
    assert lean.loc[("UT", "B"), "lean_pp"] == pytest.approx(15.0)
    nat_lean = M.partisan_lean(f, level="national")
    assert np.allclose(nat_lean["lean_pp"], 0.0)
    assert set(M.partisan_lean(f, level="province", parties=["B"])["party"]) == {"B"}


def test_margin_lean(make_pres) -> None:
    f = make_pres(1, 2028, {"NB": {"A": 60, "B": 40}, "UT": {"A": 30, "B": 70}})
    ml = M.margin_lean(f, "A", "B", level="province").set_index("geo_code")
    assert ml.loc["NB", "national_margin_pp"] == pytest.approx(-10.0)
    assert ml.loc["NB", "lean_pp"] == pytest.approx(30.0) and ml.loc["NB", "leans"] == "A"
    assert ml.loc["UT", "lean_pp"] == pytest.approx(-30.0) and ml.loc["UT", "leans"] == "B"
    raw = M.margin_lean(f, "A", "B", level="province", two_party=False).set_index("geo_code")
    assert raw.loc["NB", "lean_pp"] == pytest.approx(30.0)  # only two parties: identical


def test_elasticity_ols_recovers_known_slopes(elasticity_frame: pd.DataFrame) -> None:
    el = M.elasticity(elasticity_frame, level="municipality")
    assert tuple(el.columns) == M.ELASTICITY_COLUMNS
    e = el.set_index(["geo_code", "party"])
    assert e.loc[("GM0001", "A"), "elasticity"] == pytest.approx(1.5, abs=1e-4)
    assert e.loc[("GM0002", "A"), "elasticity"] == pytest.approx(0.5, abs=1e-4)
    assert e.loc[("GM0001", "B"), "elasticity"] == pytest.approx(1.5, abs=1e-4)
    assert e.loc[("GM0001", "A"), "intercept"] == pytest.approx(0.1, abs=1e-4)
    assert e.loc[("GM0001", "A"), "r_squared"] == pytest.approx(1.0, abs=1e-6)
    assert set(e["method"]) == {"ols"} and set(e["n_obs"]) == {5}


def test_elasticity_two_elections_is_swing_ratio(elasticity_frame: pd.DataFrame) -> None:
    two = elasticity_frame[elasticity_frame["election_id"].isin([1, 2])]
    e = M.elasticity(two, level="municipality").set_index(["geo_code", "party"])
    assert e.loc[("GM0001", "A"), "method"] == "swing_ratio"
    assert e.loc[("GM0001", "A"), "elasticity"] == pytest.approx(0.075 / 0.05, abs=1e-4)
    assert np.isnan(e.loc[("GM0001", "A"), "r_squared"])


def test_elasticity_nan_without_national_variation(make_rows) -> None:
    rows: list[dict] = []
    for eid in (1, 2, 3):
        rows += make_rows(
            eid, 2026 + 2 * eid, "PRES", "PRESIDENT", "national", "NL", [("ta", "A", 500), ("tb", "B", 500)]
        )
        rows += make_rows(
            eid,
            2026 + 2 * eid,
            "PRES-UT",
            "PRESIDENT_PROVINCE",
            "municipality",
            "GM0344",
            [("ta", "A", 40 + eid), ("tb", "B", 60 - eid)],
            province_code="UT",
        )
    e = M.elasticity(build_results_frame(rows), level="municipality")
    assert e["elasticity"].isna().all()


def test_elasticity_from_draws_known_slope() -> None:
    rng = make_rng(42, "test", "elasticity-draws")
    nat = 0.3 + 0.05 * rng.standard_normal(4000)
    slopes = np.array([0.4, 1.0, 1.8])
    geo = 0.05 + nat[:, None] * slopes[None, :] + 0.002 * rng.standard_normal((4000, 3))
    est = M.elasticity_from_draws(geo, nat)
    assert est == pytest.approx(slopes, abs=0.01)
    assert np.isnan(M.elasticity_from_draws(geo, np.full(4000, 0.3))).all()
    with pytest.raises(ValueError):
        M.elasticity_from_draws(geo, nat[:10])


def test_uns_projection_flips_and_seats(house_prev: pd.DataFrame) -> None:
    proj = M.uniform_swing_projection(house_prev, {"A": 6.0, "B": -6.0})
    c = proj.contests.set_index("geo_code")
    assert c.loc["NB-02", "winner_prev"] == "B" and c.loc["NB-02", "winner_projected"] == "A"
    assert c.loc["NB-02", "flipped"] and c.loc["NB-02", "margin_projected_pp"] == pytest.approx(2.0)
    assert c.loc["UT-01", "winner_projected"] == "C" and not c.loc["UT-01", "flipped"]
    seats = proj.seats.set_index("party")
    assert seats.loc["A", "seats_projected"] == 2 and seats.loc["B", "seats_projected"] == 0
    assert seats.loc["B", "change"] == -1 and seats["seats_projected"].sum() == 3
    sh = proj.shares.set_index(["geo_code", "party"])
    assert sh.loc[("UT-01", "A"), "share_projected"] == pytest.approx(0.36)
    assert sh.loc[("UT-01", "C"), "swing_pp"] == 0.0


def test_uns_zero_swing_reproduces_previous_winners(house_prev: pd.DataFrame) -> None:
    proj = M.uniform_swing_projection(house_prev, {})
    assert not proj.contests["flipped"].any()
    assert (proj.seats["change"] == 0).all()


def test_uns_clipping_renormalisation_and_contested_only(house_prev: pd.DataFrame) -> None:
    proj = M.uniform_swing_projection(house_prev, {"B": -50.0, "C": 10.0})
    sh = proj.shares.set_index(["geo_code", "party"])
    assert sh.loc[("NB-01", "B"), "share_projected"] == 0.0
    assert sh.loc[("NB-01", "A"), "share_projected"] == pytest.approx(1.0)  # renormalised
    assert ("NB-01", "C") not in sh.index  # C did not run in NB-01: no swing there
    sums = proj.shares.groupby("geo_code")["share_projected"].sum()
    assert np.allclose(sums, 1.0)
    raw = M.uniform_swing_projection(house_prev, {"B": -50.0}, renormalize=False).shares.set_index(
        ["geo_code", "party"]
    )
    assert raw.loc[("NB-01", "A"), "share_projected"] == pytest.approx(0.6)
    everywhere = M.uniform_swing_projection(house_prev, {"C": 10.0}, contested_only=False).shares
    assert ("NB-01", "C") in set(zip(everywhere["geo_code"], everywhere["party"], strict=True))


def test_uns_seat_weights(house_prev: pd.DataFrame) -> None:
    w = {"NB-01": 3, "NB-02": 2, "UT-01": 1}
    seats = M.uniform_swing_projection(house_prev, {"A": 6.0, "B": -6.0}, seat_weights=w).seats.set_index(
        "party"
    )
    assert seats.loc["A", "seats_prev"] == 3 and seats.loc["A", "seats_projected"] == 5
    assert seats.loc["C", "seats_projected"] == 1
    with pytest.raises(ValueError):
        M.uniform_swing_projection(house_prev, {"A": 1.0}, seat_weights={"NB-01": 1})


def test_uns_from_national_swing_between_elections(
    house_prev: pd.DataFrame, house_curr: pd.DataFrame
) -> None:
    sw = M.national_swing(house_prev, house_curr)
    proj = M.uniform_swing_projection(house_prev, sw)
    assert set(proj.contests["geo_code"]) == {"NB-01", "NB-02", "UT-01"}
    assert proj.seats["seats_projected"].sum() == 3


def test_uns_zero_swing_keeps_lot_winners_and_does_not_pool_independents(make_rows) -> None:
    rows = make_rows(
        1,
        2028,
        "HOUSE-NB-01",
        "HOUSE",
        "district",
        "NB-01",
        [("a", "A", 50), ("b", "B", 50)],
        province_code="NB",
        winner="b",  # exact tie decided by lot for B
    )
    rows += make_rows(
        1,
        2028,
        "HOUSE-NB-02",
        "HOUSE",
        "district",
        "NB-02",
        [("i1", None, 30), ("i2", None, 25), ("a", "A", 45)],  # independents: 55 % pooled, 30 % best
        province_code="NB",
    )
    prev = build_results_frame(rows)
    proj = M.uniform_swing_projection(prev, {})
    c = proj.contests.set_index("geo_code")
    assert c.loc["NB-01", "winner_projected"] == "B" and c.loc["NB-02", "winner_projected"] == "A"
    assert not proj.contests["flipped"].any() and (proj.seats["change"] == 0).all()
    assert c.loc["NB-02", "margin_projected_pp"] == pytest.approx(15.0)  # A 45 vs the best independent
    sh = proj.shares.set_index(["geo_code", "party"])
    assert sh.loc[("NB-02", INDEPENDENT_KEY), "share_projected"] == pytest.approx(0.55)  # shares stay pooled
    # a pooled swing is shared by the independents in proportion to their votes
    moved = M.uniform_swing_projection(prev, {INDEPENDENT_KEY: 40.0}).contests.set_index("geo_code")
    assert moved.loc["NB-02", "winner_projected"] == INDEPENDENT_KEY and moved.loc["NB-02", "flipped"]
    assert moved.loc["NB-02", "margin_projected_pp"] == pytest.approx(100 / 1.4 * (0.30 * 0.95 / 0.55 - 0.45))
    small = M.uniform_swing_projection(prev, {INDEPENDENT_KEY: 20.0}).contests.set_index("geo_code")
    assert small.loc["NB-02", "winner_projected"] == "A"  # pooled 62.5 % but no single independent leads


def test_uns_party_projected_where_it_did_not_run(house_prev: pd.DataFrame) -> None:
    proj = M.uniform_swing_projection(house_prev, {"C": 70.0}, contested_only=False, renormalize=False)
    c = proj.contests.set_index("geo_code")
    assert c.loc["NB-01", "winner_projected"] == "C" and c.loc["NB-01", "flipped"]
    assert c.loc["NB-01", "margin_projected_pp"] == pytest.approx(10.0)  # C 70 % vs A 60 %
    tie = M.uniform_swing_projection(house_prev, {"C": 60.0}, contested_only=False, renormalize=False)
    assert tie.contests.set_index("geo_code").loc["NB-01", "winner_projected"] == "A"  # flagged winner first


def test_partisan_bias_does_not_pool_independents(make_rows) -> None:
    rows = make_rows(
        1,
        2028,
        "HOUSE-NB-01",
        "HOUSE",
        "district",
        "NB-01",
        [("i1", None, 30), ("i2", None, 30), ("a", "A", 40)],
        province_code="NB",
    )
    rows += make_rows(
        1,
        2028,
        "HOUSE-NB-02",
        "HOUSE",
        "district",
        "NB-02",
        [("b", "B", 60), ("a", "A", 40)],
        province_code="NB",
    )
    pb = M.partisan_bias(build_results_frame(rows), "A", "B")
    assert pb.seat_share_a == pytest.approx(0.5) and pb.seat_share_b == pytest.approx(0.5)
