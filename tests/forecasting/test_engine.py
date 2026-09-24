"""Monte Carlo engine: determinism, constitutional invariants per draw, output consistency."""

from __future__ import annotations

import json
from collections.abc import Callable

import numpy as np
import pytest

from app.core.constitution import DataCategory, RaceType
from app.core.errors import ElectionError
from app.elections.types import RaceSpec
from app.forecasting import ForecastResult, prepare_forecast, run_forecast, run_plan
from app.forecasting.engine import resolve_workers
from app.forecasting.montecarlo import run_forecast as alias_run_forecast

from .conftest import HOLDOVER


# --------------------------------------------------------------------------- determinism
def test_same_seed_same_result_and_different_seed_differs(run, pres_races) -> None:
    a = run(pres_races, 1000, 21)
    b = run(pres_races, 1000, 21)
    c = run(pres_races, 1000, 22)
    assert a.fingerprint() == b.fingerprint()
    assert np.array_equal(a.draws.ev, b.draws.ev)
    assert a.fingerprint() != c.fingerprint()


def test_identical_for_any_chunking_and_worker_count(run, full_races, test_config) -> None:
    """Block-seeded randomness: chunk size and worker processes never change a number."""
    base = run(full_races, 1500, 33, holdover_senate=HOLDOVER, workers=1)
    rechunked = run(
        full_races,
        1500,
        33,
        holdover_senate=HOLDOVER,
        workers=1,
        config=test_config.model_copy(update={"chunk_size": 250}),
    )
    parallel = run(full_races, 1500, 33, holdover_senate=HOLDOVER, workers=2)
    assert parallel.runtime["workers"] == 2 and parallel.runtime["chunks"] > 1
    assert base.fingerprint() == rechunked.fingerprint() == parallel.fingerprint()
    for name in ("ev", "pv_share", "province_winner", "house", "senate", "governors", "turnout"):
        assert np.array_equal(getattr(base.draws, name), getattr(parallel.draws, name)), name


def test_block_size_is_part_of_the_configuration(run, pres_races, test_config) -> None:
    a = run(pres_races, 600, 5)
    b = run(pres_races, 600, 5, config=test_config.model_copy(update={"block_size": 200}))
    assert a.metadata["config_hash"] != b.metadata["config_hash"]
    assert a.fingerprint() != b.fingerprint()


# --------------------------------------------------------------------------- invariants
def test_electoral_college_invariants(full_forecast: ForecastResult) -> None:
    pres = full_forecast.president
    d = full_forecast.draws
    N = full_forecast.n_simulations
    assert pres is not None and d is not None and d.ev is not None
    assert pres.total_ev == 174 and pres.majority == 88
    assert np.all(d.ev.sum(axis=1) == 174)  # every draw allocates all 174 electoral votes
    for t in pres.tickets:
        assert len(t.ev_histogram) == 175
        assert sum(t.ev_histogram) == pytest.approx(1.0)
        assert t.ev.p05 <= t.ev.p25 <= t.ev.median <= t.ev.p75 <= t.ev.p95
        assert t.pv.p05 <= t.pv.median <= t.pv.p95
        assert sum(p for _, p in t.pv_histogram) == pytest.approx(1.0)
        assert t.prob_majority <= t.prob_plurality + 1e-12
    no_majority = (d.ev.max(axis=1) < 88).mean()
    assert pres.prob_contingent == pytest.approx(no_majority)
    assert pres.prob_contingent == pytest.approx((d.winner < 0).mean())
    assert sum(t.prob_majority for t in pres.tickets) + pres.prob_contingent == pytest.approx(1.0)
    top2 = np.sort(d.ev, axis=1)[:, -2:]
    assert pres.prob_ev_tie == pytest.approx(((top2[:, 0] == 87) & (top2[:, 1] == 87)).mean())
    assert sum(pres.tipping_point.values()) == pytest.approx(1.0)
    for pv in pres.provinces.values():
        assert sum(pv.win.values()) == pytest.approx(1.0)
    assert np.all(d.pv_share.sum(axis=1) == pytest.approx(1.0, abs=1e-5))
    # the most common maps: frequencies ordered, EV per map sums to 174
    freqs = [c.frequency for c in pres.combinations]
    assert freqs == sorted(freqs, reverse=True) and 0 < sum(freqs) <= 1.0 + 1e-9
    for c in pres.combinations:
        assert sum(c.ev.values()) == pytest.approx(174)
        assert set(c.winners) == set(pres.provinces)
    top = pres.combinations[0]
    key = np.array([pres.tickets.index(pres.ticket(top.winners[p])) for p in d.provinces])
    assert (d.province_winner == key).all(axis=1).mean() == pytest.approx(top.frequency)
    assert N == 2000


def test_chamber_invariants(full_forecast: ForecastResult, full_races: list[RaceSpec]) -> None:
    d = full_forecast.draws
    house, senate, gov = full_forecast.house, full_forecast.senate, full_forecast.governors
    assert house is not None and senate is not None and gov is not None
    assert np.all(d.house.sum(axis=1) == 150)
    assert house.seats_total == 150 and house.majority == 76
    no_maj = (d.house.max(axis=1) < 76).mean()
    assert house.prob_no_majority == pytest.approx(no_maj)
    for k, sf in house.parties.items():
        j = d.house_keys.index(k)
        assert sf.prob_majority == pytest.approx((d.house[:, j] >= 76).mean())
        assert sum(sf.histogram) == pytest.approx(1.0) and len(sf.histogram) == 151
    # Senate totals include the 16 holdovers in every draw
    assert np.all(d.senate.sum(axis=1) == 24)
    assert senate.seats_up == 8 and senate.seats_total == 24 and senate.majority == 13
    for party, n in HOLDOVER.items():
        j = d.senate_keys.index(party)
        assert d.senate[:, j].min() >= n
        assert senate.parties[party].holdover == n
    assert sum(c["frequency"] for c in senate.compositions) <= 1.0 + 1e-9
    assert all(sum(c["seats"].values()) == 24 for c in senate.compositions)
    assert np.all(d.governors.sum(axis=1) == 12)
    n_house = sum(1 for r in full_races if r.race_type == RaceType.HOUSE)
    assert len(house.race_win) == n_house
    for probs in list(house.race_win.values()) + list(senate.race_win.values()) + list(gov.race_win.values()):
        assert sum(probs.values()) == pytest.approx(1.0)


def test_race_summaries(full_forecast: ForecastResult, full_races: list[RaceSpec]) -> None:
    assert next(iter(full_forecast.races)) == "PRES"
    assert set(full_forecast.races) == {r.key for r in full_races}
    for key, rf in full_forecast.races.items():
        wins = sum(ln.win_probability for ln in rf.lines)
        if rf.derived:  # national race: outright Electoral College majority; else contingent
            assert wins + full_forecast.president.prob_contingent == pytest.approx(1.0)
        else:
            assert wins == pytest.approx(1.0), key
        assert sum(ln.share_mean for ln in rf.lines) == pytest.approx(1.0, abs=1e-5), key
        for ln in rf.lines:
            assert 0.0 <= ln.share_p05 <= ln.share_p50 <= ln.share_p95 <= 1.0
            assert ln.share_p05 - 0.005 <= ln.share_mean <= ln.share_p95 + 0.005
            assert ln.mean_votes >= 0
    pres = full_forecast.races["PRES"]
    assert pres.derived
    total = sum(ln.mean_votes for ln in pres.lines)
    provinces = sum(
        ln.mean_votes for k, r in full_forecast.races.items() if k.startswith("PRES-") for ln in r.lines
    )
    assert total == pytest.approx(provinces, rel=1e-6)


def test_municipality_distributions(full_forecast: ForecastResult, frame) -> None:
    munis = full_forecast.president.municipalities
    assert set(munis) == set(frame.muni_codes)
    for by_ticket in munis.values():
        assert sum(v["mean"] for v in by_ticket.values()) == pytest.approx(1.0, abs=1e-5)
        assert sum(v["lead"] for v in by_ticket.values()) == pytest.approx(1.0)
        for v in by_ticket.values():
            assert v["p05"] <= v["p95"] and v["p05"] - 0.005 <= v["mean"] <= v["p95"] + 0.005


def test_result_is_labelled_and_json_serialisable(full_forecast: ForecastResult) -> None:
    d = json.loads(full_forecast.to_json())
    assert d["data_category"] == DataCategory.SIMULATED.value == "SIMULATED"
    assert "not a prediction" in d["disclaimer"].lower()
    meta = d["metadata"]
    for k in ("seed", "n_simulations", "model_fingerprint", "config_hash", "input_hash", "errors", "cells"):
        assert k in meta
    assert meta["seed"] == 11 and meta["n_simulations"] == 2000
    rt = d["runtime"]
    assert rt["workers"] == 1 and rt["timing"]["total_s"] > 0 and rt["blocks"] == 8
    json.dumps(full_forecast.to_dict(), allow_nan=False)  # strictly JSON: no numpy scalars, no NaN
    assert "runtime" not in full_forecast.to_dict(include_runtime=False)
    assert "municipalities" not in full_forecast.to_dict(include_municipalities=False)["president"]


# --------------------------------------------------------------------------- options
@pytest.mark.parametrize("method", ["proportional", "district"])
def test_alternative_ev_allocation(run, pres_races, down_ballot, method: str) -> None:
    res = run(pres_races + down_ballot["HOUSE"], 500, 4, ev_method=method)
    assert res.president.method == method
    assert np.all(res.draws.ev.sum(axis=1) == 174)
    if method == "proportional":  # proportional splits make outright wins rarer, never impossible
        assert res.president.ticket("jasper-de-lange").ev.mean > 0


def test_progress_and_alias(run, pres_races, demo_model, context, ev_by_province, test_config) -> None:
    calls: list[tuple[int, int]] = []
    run(pres_races, 700, 1, progress=lambda done, total: calls.append((done, total)))
    assert calls[-1] == (700, 700) and all(t == 700 for _, t in calls)
    assert [c[0] for c in calls] == sorted(c[0] for c in calls)
    r = alias_run_forecast(
        demo_model, pres_races, context, 300, 1, ev_by_province=ev_by_province, config=test_config
    )
    assert r.n_simulations == 300


def test_prepared_plan_can_be_rerun(
    demo_model, context, pres_races, ev_by_province, test_config, run
) -> None:
    plan = prepare_forecast(
        demo_model, pres_races, context, config=test_config, ev_by_province=ev_by_province
    )
    a = run_plan(plan, 700, 8, config=test_config, workers=1)
    assert a.fingerprint() == run(pres_races, 700, 8).fingerprint()
    assert run_plan(plan, 700, 9, config=test_config).fingerprint() != a.fingerprint()
    other = test_config.model_copy(update={"block_size": 100})
    with pytest.raises(ElectionError, match="configuration"):
        run_plan(plan, 700, 8, config=other)


def test_house_only_forecast_without_president(run, down_ballot) -> None:
    res = run(down_ballot["HOUSE"], 400, 2)
    assert res.president is None and res.senate is None and res.governors is None
    assert np.all(res.draws.house.sum(axis=1) == 150)


def test_argument_validation(demo_model, pres_races, context, ev_by_province, test_config) -> None:
    with pytest.raises(ElectionError, match="n_sims"):
        run_forecast(demo_model, pres_races, context, 0, 1, ev_by_province=ev_by_province, config=test_config)
    with pytest.raises(ElectionError, match="seed"):
        run_forecast(
            demo_model, pres_races, context, 10, -1, ev_by_province=ev_by_province, config=test_config
        )
    with pytest.raises(ElectionError, match="workers"):
        resolve_workers(-1, 10, 1, test_config)
    assert resolve_workers(0, 100, 5, test_config.model_copy(update={"parallel_min_simulations": 1000})) == 1
    assert resolve_workers(3, 100, 2, test_config) == 2


def test_uncertainty_scale_widens_the_distribution(run, pres_races, test_config) -> None:
    narrow = run(
        pres_races,
        800,
        3,
        config=test_config.model_copy(
            update={"errors": test_config.errors.model_copy(update={"scale": 0.5})}
        ),
    )
    wide = run(pres_races, 800, 3)
    t = "lotte-van-der-ploeg"
    n_spread = narrow.president.ticket(t).pv.p95 - narrow.president.ticket(t).pv.p05
    w_spread = wide.president.ticket(t).pv.p95 - wide.president.ticket(t).pv.p05
    assert n_spread < 0.8 * w_spread


def test_forecast_callable_fixture_types(run: Callable[..., ForecastResult]) -> None:
    assert callable(run)
