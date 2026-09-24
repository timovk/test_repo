"""The forecast approximates the distribution of full ``simulate_election`` draws.

The engine evaluates cells (fragments / municipalities) instead of neighbourhoods and replaces the
per-neighbourhood noise and the multinomial count noise by their aggregate, so it is an
approximation.  Documented tolerances (docs/FORECASTING.md §6): mean shares within Monte Carlo
error of the simulation sample plus 0.3 percentage points, standard deviations within ±25 %
(sample-size limited), win probabilities within Monte Carlo error plus 3 points.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from app.core.constitution import RaceType
from app.elections.types import RaceSpec
from app.forecasting import ForecastResult
from app.simulation.structural import StructuralModel
from app.simulation.voting import ElectionContext, simulate_election

from .conftest import HOLDOVER

MEAN_TOL = 0.003  # share points on top of 4 standard errors of the simulation mean
PROB_TOL = 0.03  # probability points on top of 4 standard errors


def _simulate(
    model: StructuralModel, races: list[RaceSpec], ctx: ElectionContext, n: int, seed0: int
) -> dict:
    """Race totals of ``n`` full simulated elections: {race: (n, L) votes}."""
    out: dict[str, list[np.ndarray]] = {}
    for s in range(n):
        draw = simulate_election(model, races, seed0 + s, ctx)
        for key, rv in draw.races.items():
            out.setdefault(key, []).append(rv.votes.sum(axis=0))
    return {k: np.array(v, dtype=float) for k, v in out.items()}


def _check_means(res: ForecastResult, sims: dict[str, np.ndarray], keys: list[str]) -> float:
    worst = 0.0
    for key in keys:
        v = sims[key]
        shares = v / v.sum(axis=1, keepdims=True)
        se = shares.std(axis=0) / np.sqrt(len(shares))
        fc = np.array([ln.share_mean for ln in res.races[key].lines])
        err = np.abs(fc - shares.mean(axis=0))
        assert np.all(err <= 4 * se + MEAN_TOL), (key, err, se)
        worst = max(worst, float(err.max()))
    return worst


def _check_wins(res: ForecastResult, sims: dict[str, np.ndarray], keys: list[str]) -> None:
    for key in keys:
        v = sims[key]
        n, L = v.shape
        p_sim = np.bincount(v.argmax(axis=1), minlength=L) / n
        p_fc = np.array([ln.win_probability for ln in res.races[key].lines])
        tol = 4 * np.sqrt(np.maximum(p_sim * (1 - p_sim), 0.01) / n) + PROB_TOL
        assert np.all(np.abs(p_fc - p_sim) <= tol), (key, p_fc, p_sim)


@pytest.fixture(scope="module")
def pres_sims(demo_model, pres_races, context) -> dict[str, np.ndarray]:
    return _simulate(demo_model, pres_races, context, 400, 70_000)


def test_presidential_forecast_matches_simulated_elections(
    run: Callable[..., ForecastResult],
    pres_races: list[RaceSpec],
    pres_sims: dict[str, np.ndarray],
    ev_by_province,
) -> None:
    res = run(pres_races, 6000, 17)
    keys = [r.key for r in pres_races]
    _check_means(res, pres_sims, keys)
    _check_wins(res, pres_sims, [k for k in keys if k != "PRES"])
    # spread of the national popular vote
    nat = pres_sims["PRES"] / pres_sims["PRES"].sum(axis=1, keepdims=True)
    sd_fc = res.draws.pv_share.std(axis=0)
    sd_sim = nat.std(axis=0)
    big = nat.mean(axis=0) > 0.05
    ratio = sd_fc[big] / sd_sim[big]
    assert np.all((ratio > 0.75) & (ratio < 1.33)), ratio
    # expected electoral votes (winner-take-all from the simulated province totals)
    provinces = list(ev_by_province)
    T = len(res.president.tickets)
    ev = np.zeros((len(nat), T))
    for pv in provinces:
        w = pres_sims[f"PRES-{pv}"].argmax(axis=1)
        ev[np.arange(len(w)), w] += ev_by_province[pv]
    se = ev.std(axis=0) / np.sqrt(len(ev))
    fc_ev = np.array([t.ev.mean for t in res.president.tickets])
    assert np.all(np.abs(fc_ev - ev.mean(axis=0)) <= 4 * se + 1.5), (fc_ev, ev.mean(axis=0))


@pytest.mark.slow
def test_full_election_forecast_matches_simulated_elections(
    run: Callable[..., ForecastResult],
    demo_model: StructuralModel,
    full_races: list[RaceSpec],
    context: ElectionContext,
) -> None:
    sims = _simulate(demo_model, full_races, context, 200, 80_000)
    res = run(full_races, 4000, 19, holdover_senate=HOLDOVER)
    keys = [r.key for r in full_races]
    worst = _check_means(res, sims, keys)
    assert worst < 0.02
    _check_wins(res, sims, [r.key for r in full_races if r.race_type == RaceType.GOVERNOR])
    # House seats per party: winners of the simulated districts vs the forecast mean
    house = [r for r in full_races if r.race_type == RaceType.HOUSE]
    parties = sorted({ln.party_code or "independent" for r in house for ln in r.lines})
    seats = np.zeros((200, len(parties)))
    for r in house:
        w = sims[r.key].argmax(axis=1)
        for i, j in enumerate(w):
            seats[i, parties.index(r.lines[j].party_code or "independent")] += 1
    for j, p in enumerate(parties):
        fc = res.house.parties[p].seats.mean
        se = seats[:, j].std() / np.sqrt(200)
        assert abs(fc - seats[:, j].mean()) <= 4 * se + 1.5, (p, fc, seats[:, j].mean())
