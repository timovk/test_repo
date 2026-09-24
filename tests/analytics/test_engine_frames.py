"""Analytics and export on results frames laid out like the services store them: every race at
every level (unit → national, presidential and House races also by district), built by the engine
(:func:`app.analytics.results.results_frame_from_draw`).  All data SIMULATED."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analytics import history as H
from app.analytics import metrics as M
from app.analytics.results import (
    JURISDICTION_LEVELS,
    PROPORTIONAL_RACE_TYPES,
    ResultsFrameError,
    contest_rows,
    dedupe_family_rows,
    geo_totals,
    national_party_totals,
    party_shares,
    results_frame_from_draw,
    seat_contests,
    validate_results_frame,
)
from app.core.constitution import HOUSE_MAJORITY, HOUSE_SEATS, PRESIDENTIAL_MAJORITY
from app.elections import electoral_college as EC
from app.elections.tabulation import tabulate_totals
from app.elections.types import ElectionDraw, UnitTurnout
from app.export import builders as B
from app.export import schemas as S
from app.export import writers as W


@pytest.fixture(scope="module")
def pair(synthetic, make_engine_election) -> tuple[pd.DataFrame, pd.DataFrame]:
    geo = synthetic.frame
    return make_engine_election(geo, 21, 1, 2028), make_engine_election(geo, 21, 2, 2032, tilt={"B": 0.25})


def _pres_unit_votes(frame: pd.DataFrame, level: str) -> pd.Series:
    rows = frame[(frame["race_type"] == "PRESIDENT_PROVINCE") & (frame["level"] == level)]
    return rows.groupby("party_code")["votes"].sum()


# --------------------------------------------------------------------------- layout & contest rows
def test_engine_frame_is_a_valid_services_layout(pair) -> None:
    prev, _ = pair
    assert validate_results_frame(prev) == []
    # every race at every level, like app.services.results.results_frame
    per_race = prev.groupby("race_code")["level"].agg(set)
    assert per_race["HOUSE-NB-01"] == {"unit", "municipality", "district", "province", "national"}
    assert per_race["GOV-NB"] == {"unit", "municipality", "province", "national"}


def test_contest_rows_follow_the_jurisdiction_not_the_coarsest_level(pair) -> None:
    prev, _ = pair
    cr = contest_rows(prev)
    levels = cr.groupby("race_type")["level"].agg(set).to_dict()
    assert levels == {rt: {lvl} for rt, lvl in JURISDICTION_LEVELS.items()}
    assert (cr.groupby("race_code")["geo_code"].nunique() == 1).all()
    house = cr[cr["race_type"] == "HOUSE"]
    assert (house["race_code"].str.removeprefix("HOUSE-") == house["geo_code"]).all()
    assert house["race_code"].nunique() == HOUSE_SEATS
    mayor = cr[cr["race_type"] == "MAYOR"]
    assert (mayor["race_code"].str.removeprefix("MAYOR-") == mayor["geo_code"]).all()


def test_contest_rows_fallbacks(pair, make_rows) -> None:
    prev, _ = pair
    no_district = prev[prev["level"] != "district"]
    cr = contest_rows(no_district)
    house = cr[cr["race_type"] == "HOUSE"]
    assert set(house["level"]) == {"province"}  # the finest level holding the whole race
    by_district = contest_rows(prev)
    totals = lambda f: f[f["race_type"] == "HOUSE"].groupby(["race_code", "line_key"])["votes"].sum()  # noqa: E731
    pd.testing.assert_series_equal(totals(house), totals(by_district))
    only_national = contest_rows(prev[prev["level"] == "national"])
    assert set(only_national["level"]) == {"national"}
    only_munis = contest_rows(prev[prev["level"] == "municipality"])
    assert set(only_munis["level"]) == {"municipality"}
    assert only_munis[only_munis["race_type"] == "PRESIDENT_PROVINCE"]["geo_code"].nunique() > 12
    from app.analytics.results import build_results_frame

    custom = build_results_frame(
        make_rows(1, 2028, "REF-1", "REFERENDUM", "municipality", "GM0001", [("y", None, 3), ("n", None, 2)])
        + make_rows(1, 2028, "REF-1", "REFERENDUM", "province", "NB", [("y", None, 3), ("n", None, 2)])
    )
    assert set(contest_rows(custom)["level"]) == {"province"}  # unknown type: coarsest level


# --------------------------------------------------------------------------- no double counting
def test_national_totals_count_every_ballot_once(pair) -> None:
    prev, _ = pair
    nat = national_party_totals(prev).set_index(["race_family", "party"])["votes"]
    units = _pres_unit_votes(prev, "unit")
    assert nat.loc["PRESIDENT"].sort_index().tolist() == units.sort_index().tolist()
    pres = prev[(prev["race_code"] == "PRES") & (prev["level"] == "national")].set_index("party_code")
    assert nat.loc["PRESIDENT"].sort_index().tolist() == pres["votes"].sort_index().tolist()
    house = prev[(prev["race_type"] == "HOUSE") & (prev["level"] == "district")]
    assert nat.loc["HOUSE"].sum() == house["votes"].sum()
    gov = prev[(prev["race_type"] == "GOVERNOR") & (prev["level"] == "province")]
    assert nat.loc["GOVERNOR"].sum() == gov["votes"].sum()
    # the same holds for a frame fetched at a single level
    for level in ("unit", "province", "national"):
        one = national_party_totals(prev[prev["level"] == level]).set_index(["race_family", "party"])["votes"]
        assert one.loc["PRESIDENT"].sort_index().tolist() == units.sort_index().tolist(), level
        assert one.loc["HOUSE"].sum() == house["votes"].sum(), level


def test_family_pooling_counts_every_ballot_once(pair) -> None:
    prev, curr = pair
    fam = party_shares(prev, level="municipality", by="family")
    pres = fam[fam["race_family"] == "PRESIDENT"].groupby("party")["votes"].sum()
    assert pres.sort_index().tolist() == _pres_unit_votes(prev, "unit").sort_index().tolist()
    gt = geo_totals(prev, level="province", by="family").set_index(["race_family", "geo_code"])
    nb = prev[(prev["race_code"] == "PRES-NB") & (prev["level"] == "province")].iloc[0]
    assert gt.loc[("PRESIDENT", "NB"), "valid_votes"] == nb["valid_votes"]
    assert gt.loc[("PRESIDENT", "NB"), "eligible"] == nb["eligible"]
    dropped = dedupe_family_rows(prev)
    assert set(dropped.loc[dropped["race_type"] == "PRESIDENT_PROVINCE", "level"]) == set()
    cmp = H.compare_elections(prev, curr, "municipality", race_type="PRESIDENT")
    muni_curr = curr[(curr["race_type"] == "PRESIDENT_PROVINCE") & (curr["level"] == "municipality")]
    assert cmp["votes_curr"].sum() == muni_curr["votes"].sum()
    assert set(cmp["status"]) == {"both"}
    t = M.turnout(curr, level="national", by="family").set_index("race_family")
    pres_nat = curr[(curr["race_code"] == "PRES") & (curr["level"] == "national")].iloc[0]
    assert t.loc["PRESIDENT", "eligible"] == pres_nat["eligible"]


# --------------------------------------------------------------------------- seats, records, summaries
def test_seat_analytics_on_services_layout(pair) -> None:
    prev, curr = pair
    sc = seat_contests(prev)
    assert not set(sc["race_type"]) & (PROPORTIONAL_RACE_TYPES | {"PRESIDENT"})
    st = H.seat_totals([prev, curr])
    assert (st.groupby("election_id")["seats"].sum() == HOUSE_SEATS).all()
    assert set(st["seats_total"]) == {HOUSE_SEATS} and set(st["majority"]) == {HOUSE_MAJORITY}
    with pytest.raises(ValueError, match="proportional"):
        H.seat_totals(prev, "MUNICIPAL_COUNCIL")
    wv = M.wasted_votes(prev)
    assert wv.groupby("race_type")["race_code"].nunique().to_dict() == {
        "GOVERNOR": 12,
        "HOUSE": HOUSE_SEATS,
        "MAYOR": 3,
        "PRESIDENT_PROVINCE": 12,
        "SENATE": 4,
    }
    pe = M.party_efficiency(prev)
    assert np.allclose(pe.groupby("race_family")["efficiency_gap"].sum(), 0.0)
    records = H.closest_races([prev, curr], n=10_000)
    assert not set(records["race_type"]) & PROPORTIONAL_RACE_TYPES
    councils = H.closest_races([prev, curr], n=10_000, race_types="MUNICIPAL_COUNCIL")
    assert len(councils) == 6
    rs = M.race_summaries(curr, prev=prev)
    assert rs["race_code"].is_unique and len(rs) == curr["race_code"].nunique()
    assert set(rs["flip_status"]) <= {"hold", "flip", "undecided"}


def test_electoral_college_on_services_layout(pair, canonical_ev) -> None:
    _, curr = pair
    tally = M.electoral_college_tally(curr, canonical_ev, by="line")
    assert tally["electoral_votes"].sum() == sum(canonical_ev.values())
    prov = curr[(curr["race_type"] == "PRESIDENT_PROVINCE") & (curr["level"] == "province")]
    tabs = {
        g["geo_code"].iloc[0]: tabulate_totals(race, g["line_key"].tolist(), g["votes"].to_numpy())
        for race, g in prov.groupby("race_code", sort=False)
    }
    for key in tally["key"]:
        engine = EC.tipping_point(tabs, canonical_ev, key)
        ours = M.tipping_point_from_frame(curr, canonical_ev, key=key, by="line")
        assert (ours.tipping_province, ours.tipping_margin_pp) == engine
    nat = curr[(curr["race_code"] == "PRES") & (curr["level"] == "national")]
    assert M.tipping_point_from_frame(curr, canonical_ev).tipping_point.majority == PRESIDENTIAL_MAJORITY
    assert tally["popular_votes"].sum() == nat["votes"].sum()


def test_uns_on_services_layout_groups_seats_by_family(pair) -> None:
    prev, curr = pair
    proj = M.uniform_swing_projection(prev, {})
    assert not proj.contests["flipped"].any()
    per_family = proj.seats.groupby("race_family")["seats_prev"].sum().to_dict()
    assert per_family == {"GOVERNOR": 12, "HOUSE": HOUSE_SEATS, "MAYOR": 3, "PRESIDENT": 12, "SENATE": 4}
    house = prev[prev["race_type"] == "HOUSE"]
    sw = M.national_swing(prev[prev["race_type"] == "HOUSE"], curr[curr["race_type"] == "HOUSE"])
    moved = M.uniform_swing_projection(house, sw)
    assert moved.seats["seats_projected"].sum() == HOUSE_SEATS
    assert set(moved.seats["race_family"]) == {"HOUSE"}


# --------------------------------------------------------------------------- export
def test_export_builders_on_services_layout(tmp_path, pair, canonical_ev) -> None:
    prev, curr = pair
    house = B.house_results_export(curr, prev=prev)
    assert S.validate(house, "house_results") == [] and len(house) == HOUSE_SEATS
    assert (house["race_code"].str.removeprefix("HOUSE-") == house["district_code"]).all()
    assert house["district_name"].notna().all()
    # without district rows the district code comes from the race code (keys stay unique)
    bare = B.house_results_export(curr[curr["level"] != "district"])
    assert S.validate(bare, "house_results") == [] and bare["district_name"].isna().all()
    assert bare["district_code"].tolist() == house["district_code"].tolist()
    sen = B.senate_results_export(curr[curr["level"] != "province"])
    assert S.validate(sen, "senate_results") == [] and set(sen["province_code"]) == {"GR", "OV", "UT", "ZE"}
    gov = B.governor_results_export(curr, prev=prev)
    assert S.validate(gov, "governor_results") == [] and len(gov) == 12
    ev = B.electoral_votes_export(curr, canonical_ev)
    assert ev["electoral_votes"].sum() == sum(canonical_ev.values())
    frames = B.build_all_results(curr) | {
        "house": house,
        "governors": gov,
        "electoral_votes": ev,
        "swing": B.swing_export(prev, curr, "municipality"),
    }
    paths = W.export_bundle(frames, tmp_path, generated_at="2032-11-04T00:00:00+00:00")
    back = W.read_csv(tmp_path / "unit_results.csv", "unit_results")
    assert len(back) == int((curr["level"] == "unit").sum())
    assert len(paths) == 2 * len(frames) + 1


# --------------------------------------------------------------------------- the builder itself
def test_results_frame_from_draw_arguments(synthetic) -> None:
    geo = synthetic.frame
    empty = ElectionDraw(1, UnitTurnout(geo.unit_eligible, geo.unit_eligible), races={})
    out = results_frame_from_draw(empty, {}, geo, election_id=1, year=2028)
    assert out.empty and list(out.columns) == list(validate_results_frame.__globals__["RESULTS_COLUMNS"])
    with pytest.raises(ResultsFrameError):
        results_frame_from_draw(empty, {}, geo, election_id=1, year=2028, levels=["galaxy"])


def test_results_frame_from_draw_requires_specs(synthetic, make_engine_election) -> None:
    from app.core.constitution import RaceType
    from app.elections.types import BallotLine, RaceSpec, RaceVotes

    geo = synthetic.frame
    units = geo.units_in_muni(0)
    n = len(units)
    rv = RaceVotes(
        "MAYOR-X",
        ["a", "b"],
        units,
        np.tile([[3, 1]], (n, 1)),
        np.full(n, 4),
        np.zeros(n, dtype=np.int64),
        np.zeros(n, dtype=np.int64),
        np.full(n, 9),
    )
    draw = ElectionDraw(1, UnitTurnout(geo.unit_eligible, geo.unit_eligible), races={"MAYOR-X": rv})
    with pytest.raises(ResultsFrameError, match="no race spec"):
        results_frame_from_draw(draw, {}, geo, election_id=1, year=2028)
    spec = RaceSpec(
        "MAYOR-X", RaceType.MAYOR, units, [BallotLine("a", "PA", label="Ann"), BallotLine("b", None)]
    )
    out = results_frame_from_draw(draw, [spec], geo, election_id=7, year=2030, levels=("municipality",))
    assert out["candidate"].tolist() == ["Ann", "b"] and out["party_code"].tolist() == ["PA", None]
    assert out["winner"].tolist() == [True, False] and validate_results_frame(out) == []


def test_outputs_do_not_depend_on_input_row_order(pair, synthetic, canonical_ev) -> None:
    from app.core.rng import make_rng

    prev, curr = pair
    rng = make_rng(9, "analytics-test", "shuffle")
    sp = prev.iloc[rng.permutation(len(prev))].reset_index(drop=True)
    sc = curr.iloc[rng.permutation(len(curr))].reset_index(drop=True)
    m0, m1 = synthetic.frame.muni_codes[0], synthetic.frame.muni_codes[1]
    lineage = pd.DataFrame({"from_code": [m0, m1], "to_code": [m0, m0]})
    checks = {
        "margin_table": lambda p, c: M.margin_table(c),
        "party_shares": lambda p, c: party_shares(c, level="municipality", by="family"),
        "geo_totals": lambda p, c: geo_totals(c, by="family"),
        "national_party_totals": lambda p, c: national_party_totals(c),
        "swing": lambda p, c: M.swing(p, c, level="province"),
        "flips": lambda p, c: M.flips(p, c, level="municipality", by="family"),
        "race_summaries": lambda p, c: M.race_summaries(c, prev=p),
        "wasted_votes": lambda p, c: M.wasted_votes(c),
        "party_efficiency": lambda p, c: M.party_efficiency(c),
        "uns": lambda p, c: M.uniform_swing_projection(p, {"A": 2.0}).contests,
        "uns_seats": lambda p, c: M.uniform_swing_projection(p, {"A": 2.0}).seats,
        "tally": lambda p, c: M.electoral_college_tally(c, canonical_ev),
        "remap": lambda p, c: H.remap_lineage(p, lineage),
        "compare": lambda p, c: H.compare_elections(p, c, "municipality"),
        "competitiveness": lambda p, c: M.competitiveness(c),
    }
    for name, fn in checks.items():
        pd.testing.assert_frame_equal(fn(sp, sc), fn(prev, curr), obj=name)


def test_uns_at_a_level_counts_each_presidential_geo_once(pair) -> None:
    prev, _ = pair
    proj = M.uniform_swing_projection(
        prev[prev["race_type"].str.startswith("PRESIDENT")], {}, level="province"
    )
    assert len(proj.contests) == 12 and proj.seats["seats_prev"].sum() == 12


def test_contest_margins_and_ev_agree_with_the_elections_engine(pair, canonical_ev) -> None:
    _, curr = pair
    cr = contest_rows(curr)
    mt = M.margin_table(cr).set_index("race_code")
    for race, g in cr.groupby("race_code"):
        tab = tabulate_totals(race, g["line_key"].tolist(), g["votes"].to_numpy())
        row = mt.loc[race]
        assert row["winner_line_key"] == tab.line_keys[tab.winner], race
        assert (row["margin_votes"], row["margin_pp"]) == (tab.margin_votes, pytest.approx(tab.margin_pct)), (
            race
        )
    prov = curr[(curr["race_type"] == "PRESIDENT_PROVINCE") & (curr["level"] == "province")]
    tabs = {
        g["geo_code"].iloc[0]: tabulate_totals(race, g["line_key"].tolist(), g["votes"].to_numpy())
        for race, g in prov.groupby("race_code", sort=False)
    }
    outcome = EC.allocate(tabs, canonical_ev)
    tally = M.electoral_college_tally(curr, canonical_ev, by="line").set_index("key")["electoral_votes"]
    assert {k: v for k, v in outcome.ev_by_line.items() if v} == {k: v for k, v in tally.items() if v}
