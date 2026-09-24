"""Historical comparison: records, divergence, lineage remapping, series."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analytics import history as H
from app.analytics.results import ResultsFrameError, build_results_frame, validate_results_frame

# --------------------------------------------------------------------------- records


def test_closest_races_ordering(house_prev: pd.DataFrame, house_curr: pd.DataFrame) -> None:
    cr = H.closest_races([house_prev, house_curr], n=10)
    assert tuple(cr.columns) == H.RECORD_COLUMNS
    got = list(zip(cr["year"], cr["geo_code"], cr["margin_pp"], strict=True))
    assert got == [
        (2030, "NB-01", pytest.approx(4.0)),
        (2030, "UT-01", pytest.approx(5.0)),
        (2028, "NB-02", pytest.approx(10.0)),  # ties on margin and votes: year, then race code
        (2028, "UT-01", pytest.approx(10.0)),
        (2028, "NB-01", pytest.approx(20.0)),
        (2030, "NB-02", pytest.approx(20.0)),
    ]
    assert cr["rank"].tolist() == [1, 2, 3, 4, 5, 6]
    assert "UT-02" not in set(cr["geo_code"])  # uncontested excluded
    assert len(H.closest_races(pd.concat([house_prev, house_curr]), n=2)) == 2


def test_largest_landslides_and_uncontested(house_prev: pd.DataFrame, house_curr: pd.DataFrame) -> None:
    ls = H.largest_landslides([house_prev, house_curr], n=3)
    assert list(zip(ls["year"], ls["geo_code"], strict=True)) == [
        (2028, "NB-01"),
        (2030, "NB-02"),
        (2028, "NB-02"),
    ]
    with_unc = H.largest_landslides([house_prev, house_curr], n=1, include_uncontested=True)
    assert with_unc["geo_code"].tolist() == ["UT-02"] and with_unc["margin_pp"].iloc[0] == pytest.approx(
        100.0
    )


def test_records_filter_race_types_and_skip_zero_vote_races(make_house, make_pres) -> None:
    h = make_house(
        1, 2028, {"NB-01": [("a", "A", 0), ("b", "B", 0)], "NB-02": [("a", "A", 51), ("b", "B", 49)]}
    )
    p = make_pres(1, 2028, {"NB": {"A": 50, "B": 49}})
    both = pd.concat([h, p], ignore_index=True)
    assert H.closest_races(both, 5)["race_code"].tolist() == ["PRES", "PRES-NB", "HOUSE-NB-02"]
    only_house = H.closest_races(both, 5, race_types="HOUSE")
    assert only_house["race_code"].tolist() == ["HOUSE-NB-02"]
    with pytest.raises(ValueError):
        H.closest_races(both, -1)


# --------------------------------------------------------------------------- divergence


def test_ec_pv_divergence_descriptive() -> None:
    long = pd.DataFrame(
        {
            "election_id": [1, 1, 2, 2, 3, 3, 4, 4, 4],
            "year": [2028, 2028, 2032, 2032, 2036, 2036, 2040, 2040, 2040],
            "key": ["A", "B", "A", "B", "A", "B", "A", "B", "C"],
            "popular_votes": [51, 49, 52, 48, 50, 50, 45, 44, 11],
            "electoral_votes": [100, 74, 80, 94, 90, 84, 86, 86, 2],
        }
    )
    d = H.ec_pv_divergence(long).set_index("year")
    assert tuple(H.ec_pv_divergence(long).columns) == H.DIVERGENCE_COLUMNS
    assert not d.loc[2028, "diverged"] and d.loc[2028, "ev_majority"] and d.loc[2028, "majority"] == 88
    assert d.loc[2032, "diverged"] and d.loc[2032, "ev_leader"] == "B" and d.loc[2032, "pv_leader"] == "A"
    assert d.loc[2032, "ev_leader_pv_share"] == pytest.approx(0.48)
    assert d.loc[2036, "pv_leader"] is None and not d.loc[2036, "diverged"]
    assert "popular vote is tied" in d.loc[2036, "description"]
    assert d.loc[2040, "ev_leader"] is None and not d.loc[2040, "ev_majority"]
    assert "No ticket reached the 88-EV majority" in d.loc[2040, "description"]
    by_year = H.ec_pv_divergence(long.drop(columns="election_id"))
    assert by_year["election_id"].isna().all() and len(by_year) == 4


# --------------------------------------------------------------------------- lineage


def _muni_frame(
    make_rows, eid: int, year: int, munis: dict[str, tuple[int, int]], extra: list[dict] | None = None
) -> pd.DataFrame:
    rows: list[dict] = []
    for gm, (a, b) in munis.items():
        rows += make_rows(
            eid,
            year,
            "PRES-NB",
            "PRESIDENT_PROVINCE",
            "municipality",
            gm,
            [("ta", "A", a), ("tb", "B", b)],
            province_code="NB",
            eligible=2 * (a + b),
        )
    return build_results_frame(rows + (extra or []))


@pytest.fixture()
def lineage() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "from_code": ["GM0001", "GM0002", "GM0004", "GM0004"],
            "to_code": ["GM0100", "GM0100", "GM0005", "GM0006"],
            "population_weight": [1.0, 1.0, 0.6, 0.4],
        }
    )


def test_remap_lineage_merger_and_split(make_rows, lineage: pd.DataFrame) -> None:
    prev = _muni_frame(
        make_rows, 1, 2028, {"GM0001": (60, 40), "GM0002": (20, 30), "GM0003": (10, 10), "GM0004": (100, 50)}
    )
    prev.loc[prev["line_key"] == "tb", "party_code"] = None  # an independent ticket
    out = H.remap_lineage(prev, lineage, names={"GM0100": "Merged"})
    assert out.loc[out["line_key"] == "tb", "party_code"].map(lambda v: v is None).all()
    assert validate_results_frame(out) == []
    g = out.set_index(["geo_code", "line_key"])
    assert g.loc[("GM0100", "ta"), "votes"] == 80 and g.loc[("GM0100", "tb"), "votes"] == 70
    assert g.loc[("GM0100", "ta"), "valid_votes"] == 150 and g.loc[("GM0100", "ta"), "eligible"] == 300
    assert g.loc[("GM0100", "ta"), "winner"] and g.loc[("GM0100", "ta"), "geo_name"] == "Merged"
    assert g.loc[("GM0100", "ta"), "share"] == pytest.approx(80 / 150)
    assert g.loc[("GM0005", "ta"), "votes"] == 60 and g.loc[("GM0006", "tb"), "votes"] == 20
    assert g.loc[("GM0003", "ta"), "votes"] == 10  # unaffected
    assert set(out["geo_code"]) == {"GM0003", "GM0005", "GM0006", "GM0100"}
    assert out["votes"].sum() == prev["votes"].sum()  # clean merger + exact split


def test_remap_lineage_rounding_race_codes_and_validation(make_rows) -> None:
    rows = make_rows(
        1,
        2028,
        "MAYOR-GM0001",
        "MAYOR",
        "municipality",
        "GM0001",
        [("m1", "A", 33), ("m2", "B", 17)],
        province_code="NB",
    )
    prev = build_results_frame(rows)
    split = pd.DataFrame(
        {"from_code": ["GM0001", "GM0001"], "to_code": ["GM0100", "GM0200"], "population_weight": [0.6, 0.4]}
    )
    out = H.remap_lineage(prev, split)
    g = out.set_index(["geo_code", "line_key"])
    assert g.loc[("GM0100", "m1"), "votes"] == 20  # rint(19.8)
    exact = H.remap_lineage(prev, split, round_votes=False).set_index(["geo_code", "line_key"])
    assert exact.loc[("GM0100", "m1"), "votes"] == pytest.approx(19.8)
    assert exact.loc[("GM0200", "m2"), "valid_votes"] == pytest.approx(0.4 * 50)
    assert set(out["race_code"]) == {"MAYOR-GM0100"}  # dominant successor
    assert validate_results_frame(out) == []
    with pytest.raises(ResultsFrameError):
        H.remap_lineage(
            prev,
            pd.DataFrame(
                {"from_code": ["GM0001", "GM0001"], "to_code": ["A", "B"], "population_weight": [0.8, 0.8]}
            ),
        )
    with pytest.raises(ResultsFrameError):
        H.remap_lineage(
            prev, pd.DataFrame({"from_code": ["GM0001"], "to_code": ["A"], "population_weight": [-1.0]})
        )
    with pytest.raises(ResultsFrameError):
        H.remap_lineage(prev, pd.DataFrame({"to_code": ["A"]}))


def test_remap_lineage_identity_keeps_rows(make_rows) -> None:
    prev = _muni_frame(make_rows, 1, 2028, {"GM0001": (50, 50)})
    rows = prev.copy()
    rows.loc[rows["line_key"] == "tb", "winner"] = True
    rows.loc[rows["line_key"] == "ta", "winner"] = False
    out = H.remap_lineage(rows, pd.DataFrame({"from_code": ["GM9999"], "to_code": ["GM8888"]}))
    pd.testing.assert_frame_equal(
        out,
        rows.sort_values(["election_id", "race_code", "level", "geo_code", "line_key"]).reset_index(
            drop=True
        ),
    )


def test_compare_elections_is_lineage_aware(make_rows, lineage: pd.DataFrame) -> None:
    prev = _muni_frame(
        make_rows, 1, 2028, {"GM0001": (60, 40), "GM0002": (20, 30), "GM0003": (10, 10), "GM0004": (100, 50)}
    )
    curr = _muni_frame(
        make_rows, 2, 2032, {"GM0100": (70, 80), "GM0003": (12, 8), "GM0005": (50, 40), "GM0006": (30, 30)}
    )
    naive = H.compare_elections(prev, curr, "municipality")
    assert set(naive.loc[naive["geo_code"] == "GM0100", "status"]) == {"new_geo"}
    assert set(naive.loc[naive["geo_code"] == "GM0001", "status"]) == {"dropped_geo"}
    aware = H.compare_elections(prev, curr, "municipality", lineage=lineage)
    assert tuple(aware.columns) == H.COMPARE_COLUMNS
    assert set(aware["status"]) == {"both"}
    a = aware.set_index(["geo_code", "party"])
    assert a.loc[("GM0100", "A"), "share_prev"] == pytest.approx(80 / 150)
    assert a.loc[("GM0100", "A"), "swing_pp"] == pytest.approx(100 * (70 / 150 - 80 / 150))
    assert a.loc[("GM0100", "A"), "flip_status"] == "flip" and a.loc[("GM0100", "A"), "winner_curr"] == "B"
    assert a.loc[("GM0003", "A"), "flip_status"] == "hold"
    assert a.loc[("GM0100", "A"), "turnout_change_pp"] == pytest.approx(0.0)
    assert set(aware["race_family"]) == {"PRESIDENT"} and aware["race_code"].isna().all()
    flips = H.municipality_flips(prev, curr, lineage=lineage).set_index("geo_code")
    assert flips.loc["GM0100", "status"] == "flip" and flips.loc["GM0005", "status"] == "hold"


def test_compare_elections_requires_rows(house_prev, house_curr) -> None:
    with pytest.raises(ResultsFrameError):
        H.compare_elections(house_prev, house_curr, "municipality")


# --------------------------------------------------------------------------- series


def test_province_trend(make_pres) -> None:
    frames = [
        make_pres(1, 2028, {"NB": {"A": 60, "B": 40}, "UT": {"A": 30, "B": 70}}),
        make_pres(2, 2032, {"NB": {"A": 50, "B": 50}, "UT": {"A": 50, "B": 50}}),
        make_pres(3, 2036, {"NB": {"B": 100}, "UT": {"A": 70, "B": 30}}),
    ]
    tr = H.province_trend(frames, "NB", "A")
    assert tuple(tr.columns) == H.SERIES_COLUMNS
    assert tr["year"].tolist() == [2028, 2032, 2036]
    assert tr["share_pp"].tolist() == pytest.approx([60.0, 50.0, 0.0])  # did not run in NB in 2036
    assert tr["lean_pp"].tolist() == pytest.approx([15.0, 0.0, -35.0])
    assert np.isnan(tr["change_pp"].iloc[0]) and tr["change_pp"].iloc[1] == pytest.approx(-10.0)
    assert tr["won"].tolist() == [True, True, False]  # 2032 tie: the flagged (lot) winner is t-A
    assert H.province_trend(frames, "LI", "A").empty


def test_party_support_series_tilburg_since_2028(make_pres) -> None:
    def year(eid: int, y: int, a: int) -> pd.DataFrame:
        return make_pres(eid, y, {"NB": {"A": 500, "B": 500}}, {"GM0855": ("NB", {"A": a, "B": 100 - a})})

    series = H.party_support_series(
        [year(1, 2024, 40), year(2, 2028, 45), year(3, 2032, 52)], "GM0855", "A", since_year=2028
    )
    assert series["year"].tolist() == [2028, 2032]
    assert series["share"].tolist() == pytest.approx([0.45, 0.52])
    assert series["change_pp"].iloc[1] == pytest.approx(7.0)
    assert series["level"].tolist() == ["municipality", "municipality"]
    assert H.party_support_series([year(1, 2024, 40)], "GM9999", "A").empty


def test_district_history(make_house) -> None:
    frames = [
        make_house(1, 2028, {"NB-01": [("a", "A", 60), ("b", "B", 40)]}),
        make_house(2, 2030, {"NB-01": [("a", "A", 45), ("b", "B", 55)]}),
        make_house(3, 2032, {"NB-01": [("a", "A", 40), ("b", "B", 60)], "NB-02": [("a", "A", 1)]}),
    ]
    dh = H.district_history(frames, "NB-01")
    assert tuple(dh.columns) == H.DISTRICT_HISTORY_COLUMNS
    assert dh["status"].tolist() == ["first", "flip", "hold"]
    assert dh["winner_party"].tolist() == ["A", "B", "B"]
    assert dh["margin_pp"].tolist() == pytest.approx([20.0, 10.0, 20.0])
    assert H.district_history(frames, "ZE-09").empty


def test_seat_totals(house_curr: pd.DataFrame, house_prev: pd.DataFrame) -> None:
    st = H.seat_totals([house_prev, house_curr])
    cur = st[st["election_id"] == 2].set_index("party")
    assert cur.loc["A", "seats"] == 2 and cur.loc["B", "seats"] == 2
    assert (
        cur.loc["A", "seats_total"] == 4 and cur.loc["A", "majority"] == 3 and not cur["has_majority"].any()
    )
    prev = st[st["election_id"] == 1]
    assert prev["seats"].sum() == 3
    assert H.seat_totals(house_curr, "GOVERNOR").empty


# --------------------------------------------------------------------------- lineage on the services layout
@pytest.fixture(scope="module")
def engine_prev(synthetic, make_engine_election) -> pd.DataFrame:
    return make_engine_election(synthetic.frame, 5, 1, 2028)


def test_remap_lineage_merges_races_at_every_level(synthetic, engine_prev: pd.DataFrame) -> None:
    geo = synthetic.frame
    m0, m1 = geo.muni_codes[0], geo.muni_codes[1]
    lineage = pd.DataFrame({"from_code": [m0, m1], "to_code": [m0, m0], "population_weight": [1.0, 1.0]})
    out = H.remap_lineage(engine_prev, lineage)
    assert validate_results_frame(out) == []
    assert f"MAYOR-{m1}" not in set(out["race_code"]) and f"COUNCIL-{m1}" not in set(out["race_code"])
    for level in ("municipality", "province", "national"):
        before = engine_prev[engine_prev["race_code"].isin([f"COUNCIL-{m0}", f"COUNCIL-{m1}"])]
        before = before[before["level"] == level].groupby("line_key")["votes"].sum()
        after = out[(out["race_code"] == f"COUNCIL-{m0}") & (out["level"] == level)]
        assert after["geo_code"].nunique() == 1, level
        assert after.set_index("line_key")["votes"].sort_index().tolist() == before.sort_index().tolist()
        mayor = out[(out["race_code"] == f"MAYOR-{m0}") & (out["level"] == level)]
        assert len(mayor) == 6 and mayor["winner"].sum() <= 1, level  # 3 + 3 candidates pooled
    munis = out[out["level"] == "municipality"]
    assert m1 not in set(munis["geo_code"])
    keep = engine_prev[(engine_prev["level"] == "municipality") & (engine_prev["race_type"] == "GOVERNOR")]
    got = munis[munis["race_type"] == "GOVERNOR"]
    assert got["votes"].sum() == keep["votes"].sum()
    units_before = engine_prev[engine_prev["level"] == "unit"].drop(columns="race_code")
    units_after = out[out["level"] == "unit"].drop(columns="race_code")
    assert len(units_before) == len(units_after) and units_after["votes"].sum() == units_before["votes"].sum()


def test_remap_lineage_with_compute_lineage_output(synthetic, engine_prev: pd.DataFrame) -> None:
    from app.geography.lineage import compute_lineage

    geo = synthetic.frame
    old = synthetic.units.drop(columns="geometry")
    new = old.copy()
    new.loc[new["municipality_code"] == geo.muni_codes[3], "municipality_code"] = geo.muni_codes[2]
    lineage = compute_lineage(old, new)
    assert set(lineage["event"]) >= {"unchanged", "merger"}
    out = H.remap_lineage(engine_prev, lineage)
    assert validate_results_frame(out) == []
    munis = out[out["level"] == "municipality"]
    assert munis["geo_code"].nunique() == geo.n_munis - 1
    fam = lambda f: f[(f["level"] == "municipality") & (f["race_type"] == "PRESIDENT")]["votes"].sum()  # noqa: E731
    assert fam(out) == fam(engine_prev)


def test_lineage_weights_are_validated_and_rounding_is_absorbed(make_rows) -> None:
    prev = _muni_frame(make_rows, 1, 2028, {"GM0001": (300, 600), "GM0002": (10, 20)})
    rounded = pd.DataFrame(
        {
            "from_code": ["GM0001"] * 3 + ["GM0002"],
            "to_code": ["GM0101", "GM0102", "GM0103", "GM0104"],
            "population_weight": [0.3333336, 0.3333336, 0.3333336, 1.0],  # 6-dp rounding: sums to 1.000001
        }
    )
    out = H.remap_lineage(prev, rounded)
    assert validate_results_frame(out) == []
    g = out.set_index(["geo_code", "line_key"])["votes"]
    assert [g[(c, "ta")] for c in ("GM0101", "GM0102", "GM0103")] == [100, 100, 100]
    assert out["votes"].sum() == prev["votes"].sum()
    with pytest.raises(ResultsFrameError, match="more than 1"):
        H.remap_lineage(prev, rounded.assign(population_weight=[0.4, 0.4, 0.4, 1.0]))
    with pytest.raises(ResultsFrameError, match="finite"):
        H.remap_lineage(prev, rounded.assign(population_weight=[np.inf, 0.0, 0.0, 1.0]))
    with pytest.raises(ResultsFrameError, match="unknown level"):
        H.remap_lineage(prev, rounded, level="galaxy")
    partial = H.remap_lineage(prev, rounded.assign(population_weight=[0.25, 0.25, 0.25, 1.0]))
    assert partial.loc[partial["geo_code"] == "GM0101", "votes"].sum() == 225  # a genuinely partial split
