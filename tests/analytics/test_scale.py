"""Invariants and performance on the synthetic geography and (optionally) the REAL CBS frame."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from app.analytics import history as H
from app.analytics import metrics as M
from app.analytics.results import national_party_totals, validate_results_frame
from app.core.config import get_constitution


@pytest.fixture(scope="module")
def synth_pair(synthetic, make_synthetic_pres) -> tuple[pd.DataFrame, pd.DataFrame]:
    geo = synthetic.frame
    prev = make_synthetic_pres(geo, seed=11, election_id=1, year=2028)
    curr = make_synthetic_pres(geo, seed=11, election_id=2, year=2032, tilt={"B": 0.15})
    return prev, curr


def _ev_map(province_codes: list[str], populations: np.ndarray) -> dict[str, int]:
    """Toy EV map (largest remainder of the constitution's House seats + senators)."""
    cfg = get_constitution()
    quota = populations / populations.sum() * cfg.house_seats
    seats = np.maximum(np.floor(quota).astype(int), cfg.min_house_seats_per_province)
    order = np.argsort(-(quota - np.floor(quota)), kind="stable")
    i = 0
    while seats.sum() < cfg.house_seats:
        seats[order[i % len(order)]] += 1
        i += 1
    return {p: int(s) + cfg.senators_per_province for p, s in zip(province_codes, seats, strict=True)}


def test_synthetic_frame_is_valid_and_reconciles(synthetic, synth_pair) -> None:
    prev, _ = synth_pair
    assert validate_results_frame(prev) == []
    nat = national_party_totals(prev).set_index("party")["votes"]
    units = prev[prev["level"] == "unit"].groupby("party_code")["votes"].sum()
    assert nat.sort_index().tolist() == units.sort_index().tolist()
    mt = M.margin_table(prev, level="unit")
    assert len(mt) == synthetic.frame.n_units


def test_lean_weighted_mean_is_zero(synth_pair) -> None:
    prev, _ = synth_pair
    lean = M.partisan_lean(prev, level="municipality")
    for _, g in lean.groupby("party"):
        w = g["votes"].to_numpy(dtype=float) / g["share"].to_numpy(dtype=float)  # valid votes
        assert np.average(g["lean_pp"], weights=w) == pytest.approx(0.0, abs=1e-9)


def test_metrics_on_synthetic_country(synthetic, synth_pair) -> None:
    prev, curr = synth_pair
    geo = synthetic.frame
    ev = _ev_map(geo.province_codes, geo.province_population().astype(float))
    cfg = get_constitution()
    assert sum(ev.values()) == cfg.electoral_votes
    t0 = time.perf_counter()
    tally = M.electoral_college_tally(curr, ev)
    assert tally.groupby("election_id")["electoral_votes"].sum().iloc[0] == cfg.electoral_votes
    bias = M.tipping_point_from_frame(curr, ev)
    assert bias.tipping_point.majority == cfg.presidential_majority
    assert bias.tipping_point.cumulative_ev >= cfg.presidential_majority
    sw = M.swing(prev, curr, level="unit")
    assert len(sw) == geo.n_units * 4 and set(sw["status"]) == {"both"}
    proj = M.uniform_swing_projection(prev, {}, level="municipality")
    assert not proj.contests["flipped"].any()
    comp = M.competitiveness(curr, level="municipality")
    assert comp["competitiveness"].between(0, 1).all()
    el = M.elasticity(pd.concat([prev, curr]), level="municipality")
    assert el["n_obs"].eq(2).all()
    cmp = H.compare_elections(prev, curr, "unit")
    assert len(cmp) == geo.n_units * 4
    assert time.perf_counter() - t0 < 30.0


def test_lineage_merger_preserves_totals(synthetic, synth_pair) -> None:
    prev, _ = synth_pair
    codes = synthetic.frame.muni_codes
    lineage = pd.DataFrame(
        {"from_code": codes[1::2], "to_code": codes[0::2][: len(codes[1::2])], "population_weight": 1.0}
    )
    remapped = H.remap_lineage(prev, lineage)
    assert validate_results_frame(remapped) == []
    before = prev[prev["level"] == "municipality"].groupby("party_code")["votes"].sum()
    after = remapped[remapped["level"] == "municipality"].groupby("party_code")["votes"].sum()
    assert before.equals(after)
    assert remapped.loc[remapped["level"] == "municipality", "geo_code"].nunique() == len(codes[0::2])


@pytest.mark.realdata
def test_real_frame_scale(real_frame, make_synthetic_pres) -> None:
    prev = make_synthetic_pres(real_frame, seed=3, election_id=1, year=2028)
    curr = make_synthetic_pres(real_frame, seed=3, election_id=2, year=2032, tilt={"A": 0.1})
    assert validate_results_frame(curr) == []
    t0 = time.perf_counter()
    mt = M.margin_table(curr, level="unit")
    assert len(mt) == real_frame.n_units
    cmp = H.compare_elections(prev, curr, "municipality")
    assert cmp["geo_code"].nunique() == real_frame.n_munis
    lean = M.partisan_lean(curr, level="unit")
    assert lean["geo_code"].nunique() == real_frame.n_units
    fl = H.municipality_flips(prev, curr)
    assert len(fl) == real_frame.n_munis
    assert time.perf_counter() - t0 < 60.0


@pytest.mark.realdata
@pytest.mark.slow
def test_million_row_history_on_the_real_frame(real_frame, make_engine_election, tmp_path) -> None:
    """Four full general elections stored like the services store them (every race at every level
    down to the 14,729 CBS buurten): ~1M rows through the history, seat and export paths."""
    from app.core.constitution import HOUSE_SEATS
    from app.elections import electoral_college as EC
    from app.elections.tabulation import tabulate_totals
    from app.export import builders as B
    from app.export import writers as W

    t_build = time.perf_counter()
    elections = [
        make_engine_election(real_frame, 17, i + 1, 2024 + 4 * i, tilt={"A": 0.08 * i}, local=False)
        for i in range(4)
    ]
    frame = pd.concat(elections, ignore_index=True)
    build_s = time.perf_counter() - t_build
    assert len(frame) > 1_000_000
    t0 = time.perf_counter()
    assert validate_results_frame(frame) == []
    units = frame[(frame["level"] == "unit") & (frame["race_type"] == "PRESIDENT_PROVINCE")]
    nat = national_party_totals(frame)
    pres = nat[nat["race_family"] == "PRESIDENT"].set_index(["election_id", "party"])["votes"]
    assert pres.sort_index().tolist() == units.groupby(["election_id", "party_code"])["votes"].sum().tolist()
    st = H.seat_totals(frame)
    assert (st.groupby("election_id")["seats"].sum() == HOUSE_SEATS).all()
    assert len(H.closest_races(frame, 25)) == 25
    el = M.elasticity(frame, level="municipality", by="family", parties=["A", "B"])
    ok = el[el["race_family"] == "PRESIDENT"]
    assert set(ok["method"]) == {"ols"} and ok["geo_code"].nunique() == real_frame.n_munis
    rs = M.race_summaries(elections[3], prev=frame)
    assert set(rs["flip_status"]) <= {"hold", "flip", "undecided"} and rs["race_code"].is_unique
    cmp = H.compare_elections(elections[2], elections[3], "unit", race_type="PRESIDENT")
    assert cmp["geo_code"].nunique() == real_frame.n_units and set(cmp["status"]) == {"both"}
    mt = M.margin_table(frame, level="unit")
    assert len(mt) == int(
        frame.loc[frame["level"] == "unit", ["election_id", "race_code", "geo_code"]]
        .drop_duplicates()
        .shape[0]
    )
    ev = {
        p: e
        for p, e in zip(real_frame.province_codes, (7, 8, 6, 12, 6, 20, 14, 27, 34, 5, 24, 11), strict=True)
    }
    last = elections[3]
    tally = M.electoral_college_tally(frame, ev, by="line")
    assert (tally.groupby("election_id")["electoral_votes"].sum() == 174).all()
    prov = last[(last["race_type"] == "PRESIDENT_PROVINCE") & (last["level"] == "province")]
    tabs = {
        g["geo_code"].iloc[0]: tabulate_totals(r, g["line_key"].tolist(), g["votes"].to_numpy())
        for r, g in prov.groupby("race_code", sort=False)
    }
    for key in ("t-A", "t-B"):
        ours = M.tipping_point_from_frame(last, ev, key=key, by="line")
        assert (ours.tipping_province, ours.tipping_margin_pp) == EC.tipping_point(tabs, ev, key)
    house = [e[e["race_type"] == "HOUSE"] for e in elections[2:]]
    proj = M.uniform_swing_projection(house[0], M.national_swing(house[0], house[1]))
    assert proj.seats["seats_projected"].sum() == HOUSE_SEATS
    comp = M.competitiveness(frame)
    assert comp["race_code"].nunique() == frame["race_code"].nunique()
    unit_export = B.results_export(last, "unit")
    W.write_csv(unit_export, "unit_results", tmp_path / "units.csv")
    with (tmp_path / "units.csv").open(encoding="utf-8") as fh:
        assert sum(1 for _ in fh) == len(unit_export) + 1
    assert len(B.house_results_export(last, prev=frame)) == HOUSE_SEATS
    elapsed = time.perf_counter() - t0
    assert elapsed < 240.0, f"analytics on {len(frame):,} rows took {elapsed:.1f}s (build {build_s:.1f}s)"
