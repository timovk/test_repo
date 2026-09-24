"""Forecasts on the REAL CBS geography (skipped when the processed store is not built).

All parties, candidates and numbers are FICTIONAL / SIMULATED; only the geography is real.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from app.core.constitution import HOUSE_SEATS, SENATORS_PER_PROVINCE
from app.forecasting import ForecastResult, load_forecast_config, run_forecast
from app.scenarios.loader import load_scenario
from app.simulation.candidates import generate_down_ballot
from app.simulation.races import house_slots, presidential_races, races_from_candidates, ticket_lines
from app.simulation.structural import StructuralModel
from app.simulation.voting import ElectionContext, simulate_election

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def real_setup(real_frame):  # type: ignore[no-untyped-def]
    """demo-2028 on the REAL country with a generated House plan (President + 150 House races)."""
    from app.districts.apportionment import apportion
    from app.districts.config import load_district_config
    from app.districts.generator import generate_plan
    from app.geography.store import load_municipalities_gdf, load_unit_adjacency, load_units_attrs

    units = load_units_attrs()
    munis = load_municipalities_gdf(columns=["code", "name"])
    seats = apportion(
        units.groupby("province_code")["population"].sum().astype(int).to_dict(), HOUSE_SEATS
    ).seats
    plan = generate_plan(
        units,
        load_unit_adjacency(),
        seats,
        load_district_config(),
        seed=2028,
        municipality_names=dict(zip(munis["code"], munis["name"], strict=True)),
        workers=0,
    )
    codes = dict(zip(plan.unit_codes, plan.unit_district_codes(), strict=True))
    unit_district = np.array([codes[c] for c in real_frame.unit_codes], dtype=object)
    doc = load_scenario("demo-2028")
    model = StructuralModel.build(real_frame, doc, strict=True)
    ev = {pv: seats[pv] + SENATORS_PER_PROVINCE for pv in real_frame.province_codes}
    slots = house_slots(real_frame, unit_district)
    fielded = generate_down_ballot(model, slots, seed=doc.scenario.seed)
    pres = presidential_races(real_frame, ticket_lines(doc, real_frame), ev)
    return model, pres, races_from_candidates(slots, fielded), ev, ElectionContext.from_scenario(doc)


def test_100k_simulations_real_country(real_setup) -> None:  # type: ignore[no-untyped-def]
    model, pres, house, ev, ctx = real_setup
    t0 = time.perf_counter()
    res = run_forecast(model, pres + house, ctx, 100_000, 2028, ev_by_province=ev, workers=0)
    elapsed = time.perf_counter() - t0
    print(
        f"\n100,000 draws on the REAL country (President + House): {elapsed:.1f}s, "
        f"workers={res.runtime['workers']}, timing={res.runtime['timing']}, cells={res.metadata['cells']}"
    )
    p = res.president
    for t in p.tickets:
        print(
            f"  {t.party_code}: EV {t.ev.mean:.1f} (p05 {t.ev.p05:.0f}–p95 {t.ev.p95:.0f}), outright {t.prob_majority:.3f}"
        )
    print(f"  contingent {p.prob_contingent:.3f}, House no majority {res.house.prob_no_majority:.3f}")
    assert np.all(res.draws.ev.sum(axis=1) == 174)
    assert np.all(res.draws.house.sum(axis=1) == 150)
    assert len(p.municipalities) == model.frame.n_munis
    assert elapsed < 60.0, f"{elapsed:.1f}s"


def test_real_presidential_forecast_matches_simulations(real_setup) -> None:  # type: ignore[no-untyped-def]
    model, pres, _, ev, ctx = real_setup
    n = 300
    totals: dict[str, list[np.ndarray]] = {}
    for s in range(n):
        d = simulate_election(model, pres, 90_000 + s, ctx)
        for k, rv in d.races.items():
            totals.setdefault(k, []).append(rv.votes.sum(axis=0))
    cfg = load_forecast_config()
    res: ForecastResult = run_forecast(model, pres, ctx, 20_000, 7, ev_by_province=ev, workers=1, config=cfg)
    for key, rows in totals.items():
        v = np.array(rows, dtype=float)
        sh = v / v.sum(axis=1, keepdims=True)
        se = sh.std(axis=0) / np.sqrt(n)
        fc = np.array([ln.share_mean for ln in res.races[key].lines])
        assert np.all(np.abs(fc - sh.mean(axis=0)) <= 4 * se + 0.003), key
        if key != "PRES":
            p_sim = np.bincount(v.argmax(axis=1), minlength=v.shape[1]) / n
            p_fc = np.array([ln.win_probability for ln in res.races[key].lines])
            assert np.all(
                np.abs(p_fc - p_sim) <= 4 * np.sqrt(np.maximum(p_sim * (1 - p_sim), 0.01) / n) + 0.03
            ), key
