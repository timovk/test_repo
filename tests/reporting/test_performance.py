"""Performance smoke tests of the election-night engine."""

from __future__ import annotations

import json
import time

import numpy as np
import pytest

from app.core.constitution import RaceStatus
from app.reporting.config import default_night_config
from app.reporting.live import NightEngine
from app.reporting.timeline import generate_timeline


def test_synthetic_night_performance(synthetic, small_election) -> None:  # type: ignore[no-untyped-def]
    """~1 200 units, 182 races: the whole night runs in seconds, a typical event in ms."""
    cfg = default_night_config()
    frame = synthetic.frame
    t0 = time.perf_counter()
    tl = generate_timeline(frame, small_election.ballots, cfg, seed=77)
    assert time.perf_counter() - t0 < 5.0
    eng = NightEngine(
        frame,
        tl,
        small_election.races,
        small_election.meta,
        cfg,
        seed=77,
        holdover_senate=small_election.holdover,
    )
    per_event = []
    t1 = time.perf_counter()
    while not eng.is_finished:
        s = time.perf_counter()
        eng.advance(1)
        per_event.append(time.perf_counter() - s)
    total = time.perf_counter() - t1
    assert total < 45.0, f"full night took {total:.1f}s"
    assert float(np.median(per_event)) < 0.020
    t2 = time.perf_counter()
    json.dumps(eng.snapshot("full"))
    assert time.perf_counter() - t2 < 2.0
    assert all(eng.race_state(k).status in {RaceStatus.FINAL, RaceStatus.RECOUNT} for k in eng.race_keys)


@pytest.mark.realdata
def test_real_geography_night(real_frame, election_builder) -> None:  # type: ignore[no-untyped-def]
    """Full night on the REAL CBS geography with synthetic (FICTIONAL) races: < 60 s."""
    cfg = default_night_config()
    el = election_builder(real_frame, seed=3)
    tl = generate_timeline(real_frame, el.ballots, cfg, seed=3)
    assert tl.validate() == []
    first = tl.muni_first_time_s()
    ballots = np.bincount(real_frame.unit_muni, weights=el.ballots, minlength=real_frame.n_munis)
    small = ballots <= np.quantile(ballots, 0.25)
    large = ballots >= np.quantile(ballots, 0.75)
    assert np.nanmean(first[small]) < np.nanmean(first[large])
    assert 1.0 <= tl.end_time_s / 3600.0 <= 9.0
    eng = NightEngine(real_frame, tl, el.races, el.meta, cfg, seed=3, holdover_senate=el.holdover)
    t = time.perf_counter()
    eng.finish()
    assert time.perf_counter() - t < 60.0
    json.dumps(eng.snapshot("full"))
    assert all(eng.race_state(k).status in {RaceStatus.FINAL, RaceStatus.RECOUNT} for k in eng.race_keys)
