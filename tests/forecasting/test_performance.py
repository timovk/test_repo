"""Throughput of the Monte Carlo engine on the synthetic country (full general election)."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from app.elections.types import RaceSpec
from app.forecasting import ForecastResult, load_forecast_config

from .conftest import HOLDOVER


def test_in_process_throughput(run: Callable[..., ForecastResult], full_races: list[RaceSpec]) -> None:
    """183 races (President, House, Senate class, governors), one process: > 400 draws/s even on a
    busy shared machine (≈ 1,500 draws/s when idle)."""
    t0 = time.perf_counter()
    res = run(full_races, 5000, 2, holdover_senate=HOLDOVER, workers=1)
    elapsed = time.perf_counter() - t0
    assert res.runtime["workers"] == 1
    assert elapsed < 12.5, f"{elapsed:.1f}s"


@pytest.mark.slow
def test_20k_simulations_under_20_seconds(
    run: Callable[..., ForecastResult], full_races: list[RaceSpec]
) -> None:
    """20,000 complete general elections with automatic worker selection (default configuration)."""
    t0 = time.perf_counter()
    res = run(full_races, 20_000, 3, holdover_senate=HOLDOVER, workers=0, config=load_forecast_config())
    elapsed = time.perf_counter() - t0
    print(f"20,000 draws: {elapsed:.1f}s, runtime={res.runtime}")
    assert res.n_simulations == 20_000
    assert elapsed < 20.0, f"{elapsed:.1f}s"
