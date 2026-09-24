from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analytics.results import (
    INDEPENDENT_KEY,
    RESULTS_COLUMNS,
    ResultsFrameError,
    build_results_frame,
    contest_rows,
    geo_totals,
    national_party_totals,
    party_shares,
    race_family,
    seat_contests,
    validate_results_frame,
)


def test_build_results_frame_derives_columns(make_rows) -> None:
    rows = make_rows(1, 2028, "HOUSE-NB-01", "HOUSE", "district", "NB-01", [("a", "A", 30), ("b", "B", 70)])
    for r in rows:
        for c in ("share", "winner"):
            r.pop(c, None)
    df = build_results_frame(rows)
    assert tuple(df.columns) == RESULTS_COLUMNS
    assert df["share"].tolist() == pytest.approx([0.3, 0.7])
    assert df["winner"].tolist() == [False, True]
    assert df["winner"].dtype == bool
    assert validate_results_frame(df) == []


def test_build_results_frame_minimal_input() -> None:
    df = build_results_frame(
        [
            {
                "election_id": 1,
                "year": 2028,
                "race_code": "GOV-UT",
                "race_type": "GOVERNOR",
                "level": "province",
                "geo_code": "UT",
                "line_key": "x",
                "votes": 0,
            },
            {
                "election_id": 1,
                "year": 2028,
                "race_code": "GOV-UT",
                "race_type": "GOVERNOR",
                "level": "province",
                "geo_code": "UT",
                "line_key": "y",
                "votes": 0,
            },
        ]
    )
    assert df["valid_votes"].tolist() == [0, 0]
    assert df["share"].tolist() == [0.0, 0.0]
    assert not df["winner"].any()  # nobody wins without votes
    assert df["province_code"].tolist() == ["UT", "UT"]
    assert df["geo_name"].tolist() == ["UT", "UT"]


def test_build_results_frame_tie_goes_to_lowest_line_key_unless_flagged(make_rows) -> None:
    rows = make_rows(1, 2028, "SEN-ZE-1", "SENATE", "province", "ZE", [("zz", "A", 50), ("aa", "B", 50)])
    for r in rows:
        r.pop("winner", None)
    assert build_results_frame(rows).set_index("line_key")["winner"].to_dict() == {"zz": False, "aa": True}
    flagged = make_rows(
        1, 2028, "SEN-ZE-1", "SENATE", "province", "ZE", [("zz", "A", 50), ("aa", "B", 50)], winner="zz"
    )
    assert build_results_frame(flagged).set_index("line_key")["winner"].to_dict() == {"zz": True, "aa": False}


def test_validate_results_frame_reports_problems(house_prev: pd.DataFrame) -> None:
    bad = house_prev.copy()
    bad.loc[0, "votes"] = -1
    bad.loc[1, "share"] = 1.5
    bad.loc[2, "level"] = "galaxy"
    problems = validate_results_frame(bad)
    assert any("negative" in p for p in problems)
    assert any("share outside" in p for p in problems)
    assert any("unknown levels" in p for p in problems)
    assert any("valid_votes differ" in p for p in problems)
    dup = pd.concat([house_prev, house_prev.iloc[:1]], ignore_index=True)
    assert any("duplicate" in p for p in validate_results_frame(dup))
    assert validate_results_frame(house_prev.drop(columns="winner")) == ["missing columns: ['winner']"]


def test_contest_rows_and_seat_contests(make_pres) -> None:
    f = make_pres(
        1, 2028, {"NB": {"A": 60, "B": 40}, "UT": {"A": 30, "B": 70}}, {"GM0855": ("NB", {"A": 20, "B": 10})}
    )
    cr = contest_rows(f)
    assert set(zip(cr["race_code"], cr["level"], strict=True)) == {
        ("PRES-NB", "province"),
        ("PRES-UT", "province"),
        ("PRES", "national"),
    }
    sc = seat_contests(f)
    assert set(sc["race_code"]) == {"PRES-NB", "PRES-UT"}
    only_munis = f[f["level"] == "municipality"]
    assert set(contest_rows(only_munis)["level"]) == {"municipality"}


def test_race_family() -> None:
    assert race_family("PRESIDENT_PROVINCE") == "PRESIDENT"
    assert race_family("HOUSE") == "HOUSE"
    assert race_family(None) is None


def test_national_party_totals_prefers_national_rows(make_pres, house_curr: pd.DataFrame) -> None:
    f = make_pres(1, 2028, {"NB": {"A": 60, "B": 40}, "UT": {"A": 30, "B": 70}})
    nat = national_party_totals(f).set_index("party")
    assert nat.loc["A", "votes"] == 90 and nat.loc["B", "votes"] == 110
    assert nat.loc["A", "share"] == pytest.approx(0.45)
    # national row deliberately inconsistent with provinces: national rows win for the PRESIDENT family
    g = f.copy()
    g.loc[(g["level"] == "national") & (g["party_code"] == "A"), "votes"] = 1000
    g.loc[g["level"] == "national", "valid_votes"] = 1110
    assert national_party_totals(g).set_index("party").loc["A", "votes"] == 1000
    # House: sum of all district contests; independents pooled
    h = national_party_totals(house_curr).set_index("party")
    assert h.loc["A", "votes"] == 148 and h.loc[INDEPENDENT_KEY, "votes"] == 5
    assert h["share"].sum() == pytest.approx(1.0)


def test_party_shares_family_mode_pools_district_fragments(make_rows) -> None:
    rows = []
    rows += make_rows(
        1,
        2028,
        "HOUSE-NB-01",
        "HOUSE",
        "municipality",
        "GM0855",
        [("a", "A", 30), ("b", "B", 10)],
        province_code="NB",
    )
    rows += make_rows(
        1,
        2028,
        "HOUSE-NB-02",
        "HOUSE",
        "municipality",
        "GM0855",
        [("c", "A", 5), ("d", "B", 55)],
        province_code="NB",
    )
    df = build_results_frame(rows)
    by_race = party_shares(df, by="race")
    assert len(by_race) == 4
    fam = party_shares(df, by="family").set_index("party")
    assert fam.loc["A", "votes"] == 35 and fam.loc["B", "votes"] == 65
    assert fam.loc["A", "valid_votes"] == 100
    assert fam.loc["B", "won"] and not fam.loc["A", "won"]
    assert fam["race_code"].isna().all()
    gt = geo_totals(df, by="family")
    assert gt["valid_votes"].tolist() == [100]


def test_party_shares_race_mode_uses_line_winner_for_split_independents(make_rows) -> None:
    rows = make_rows(
        1,
        2028,
        "MAYOR-GM0855",
        "MAYOR",
        "municipality",
        "GM0855",
        [("i1", None, 30), ("i2", None, 30), ("p", "A", 40)],
    )
    ps = party_shares(build_results_frame(rows), by="race").set_index("party")
    assert ps.loc[INDEPENDENT_KEY, "votes"] == 60
    assert ps.loc["A", "won"] and not ps.loc[INDEPENDENT_KEY, "won"]


def test_party_shares_nan_share_without_valid_votes(make_rows) -> None:
    df = build_results_frame(
        make_rows(1, 2028, "GOV-FL", "GOVERNOR", "province", "FL", [("a", "A", 0), ("b", "B", 0)])
    )
    ps = party_shares(df)
    assert np.isnan(ps["share"]).all() and not ps["won"].any()


def test_unknown_level_raises(house_prev: pd.DataFrame) -> None:
    bad = house_prev.copy()
    bad["level"] = "planet"
    with pytest.raises(ResultsFrameError):
        contest_rows(bad)
