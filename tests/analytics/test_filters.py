from __future__ import annotations

import pandas as pd
import pytest

from app.analytics import filters as F
from app.analytics.results import build_results_frame


@pytest.fixture()
def mixed(make_rows, make_pres, house_prev, house_curr) -> pd.DataFrame:
    rows: list[dict] = []
    rows += make_rows(
        3,
        2032,
        "MAYOR-GM0855",
        "MAYOR",
        "municipality",
        "GM0855",
        [("m1", "PA", 10), ("m2", None, 12)],
        province_code="NB",
        geo_name="Tilburg",
    )
    rows += make_rows(
        3,
        2032,
        "MAYOR-GM0855",
        "MAYOR",
        "unit",
        "BU08550101",
        [("m1", "PA", 4), ("m2", None, 3)],
        province_code="NB",
    )
    rows += make_rows(
        4,
        2034,
        "GOV-UT",
        "GOVERNOR",
        "province",
        "UT",
        [("g1", "A", 40), ("g2", "B", 41)],
        province_code="UT",
    )
    pres = make_pres(5, 2036, {"NB": {"A": 60, "B": 40}}, {"GM0855": ("NB", {"A": 30, "B": 20})})
    return pd.concat([house_prev, house_curr, build_results_frame(rows), pres], ignore_index=True)


def test_years_and_elections(mixed: pd.DataFrame) -> None:
    assert set(F.years(2028)(mixed)["year"]) == {2028}
    assert set(F.years(since=2032)(mixed)["year"]) == {2032, 2034, 2036}
    assert set(F.years(since=2030, until=2032)(mixed)["year"]) == {2030, 2032}
    assert set(F.years([2028, 2036])(mixed)["year"]) == {2028, 2036}
    assert set(F.elections(1, 2)(mixed)["election_id"]) == {1, 2}


def test_parties_and_candidates(mixed: pd.DataFrame) -> None:
    assert set(F.parties("A")(mixed)["party_code"]) == {"A"}
    ind = F.parties("_IND")(mixed)
    assert ind["party_code"].isna().all() and len(ind) == 3
    assert set(F.candidates("cand m1")(mixed)["line_key"]) == {"m1"}  # case-insensitive by default
    assert F.candidates("cand m1", case_sensitive=True)(mixed).empty
    assert set(F.candidates("t-", contains=True)(mixed)["line_key"]) == {"t-A", "t-B"}


def test_geography_filters(mixed: pd.DataFrame) -> None:
    nb = F.provinces("NB")(mixed)
    assert set(nb["province_code"]) == {"NB"} and "NL" not in set(nb["geo_code"])
    ut = F.provinces("UT")(mixed)
    assert set(ut["race_code"]) == {"HOUSE-UT-01", "HOUSE-UT-02", "GOV-UT"}
    t = F.municipalities("GM0855")(mixed)
    assert set(t["level"]) == {"municipality"} and set(t["geo_code"]) == {"GM0855"}
    tu = F.municipalities("GM0855", include_units=True)(mixed)
    assert "BU08550101" in set(tu["geo_code"])
    tr = F.municipalities("GM0855", include_races=True)(mixed)
    assert set(tr.loc[tr["race_type"] == "MAYOR", "level"]) == {"municipality", "unit"}
    d = F.districts("NB-01")(mixed)
    assert set(d["race_code"]) == {"HOUSE-NB-01"} and len(d) == 4
    assert set(F.geos("NL")(mixed)["level"]) == {"national"}


def test_race_and_level_filters(mixed: pd.DataFrame) -> None:
    assert set(F.race_types("PRESIDENT")(mixed)["race_code"]) == {"PRES"}
    fam = F.race_types("PRESIDENT", family=True)(mixed)
    assert set(fam["race_code"]) == {"PRES", "PRES-NB"}
    assert set(F.race_codes("HOUSE-NB", prefix=True)(mixed)["race_code"]) == {"HOUSE-NB-01", "HOUSE-NB-02"}
    assert set(F.levels("unit", "national")(mixed)["level"]) == {"unit", "national"}
    with pytest.raises(ValueError):
        F.levels("galaxy")
    with pytest.raises(ValueError):
        F.race_types("KING")
    assert F.winners_only()(mixed)["winner"].all()


def test_election_types_inferred_and_explicit(mixed: pd.DataFrame) -> None:
    types = F.infer_election_types(mixed)
    by_eid = dict(zip(mixed["election_id"], types, strict=True))
    assert by_eid == {1: "midterm", 2: "midterm", 3: "municipal", 4: "provincial", 5: "general"}
    assert set(F.election_types("general")(mixed)["election_id"]) == {5}
    explicit = mixed.assign(election_type="special")
    assert len(F.election_types("special")(explicit)) == len(mixed)
    with pytest.raises(ValueError):
        F.election_types("coronation")


def test_composition(mixed: pd.DataFrame) -> None:
    f = F.years(since=2030) & F.parties("A") & ~F.levels("national")
    out = f(mixed)
    assert (
        set(out["party_code"]) == {"A"}
        and (out["year"] >= 2030).all()
        and "national" not in set(out["level"])
    )
    either = F.race_types("GOVERNOR") | F.race_types("MAYOR")
    assert set(either(mixed)["race_type"]) == {"GOVERNOR", "MAYOR"}
    assert "&" in f.label and repr(f).startswith("Filter(")
    assert len(F.all_of()(mixed)) == len(mixed)
    assert F.any_of()(mixed).empty
    idx = F.parties("B").mask(mixed)
    assert idx.dtype == bool and idx.shape == (len(mixed),)


def test_filter_results_keywords(mixed: pd.DataFrame) -> None:
    out = F.filter_results(mixed, year=2036, level="municipality", party="A", municipality="GM0855")
    assert len(out) == 1 and out["votes"].iloc[0] == 30
    out2 = F.filter_results(mixed, F.provinces("NB"), race_type="PRESIDENT", family=True, winners=True)
    assert set(out2["line_key"]) == {"t-A"}
    assert len(F.filter_results(mixed)) == len(mixed)
    out3 = F.filter_results(mixed, since_year=2034, election_type="provincial", candidate="Cand g2")
    assert out3["line_key"].tolist() == ["g2"]
    out4 = F.filter_results(
        mixed, district="UT-01", election_id=[2], race_code="HOUSE-UT-01", geo_code="UT-01"
    )
    assert set(out4["election_id"]) == {2} and len(out4) == 3
