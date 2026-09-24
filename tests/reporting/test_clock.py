"""Playback clock: pure wall-clock → simulated-time mapping with injectable ``now``."""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.core.errors import ElectionNightError
from app.reporting.clock import PlaybackClock, PlaybackState
from app.reporting.config import default_night_config
from app.reporting.live import NightEngine
from app.reporting.timeline import generate_timeline

TIMES = np.array([1800.0, 1800.0, 2400.0, 3000.0, 9000.0, 20000.0])


def _clock(**kw) -> PlaybackClock:  # type: ignore[no-untyped-def]
    return PlaybackClock(event_times=TIMES, **kw)


def test_running_pause_resume_speed_arithmetic() -> None:
    c = _clock()
    assert c.state == PlaybackState.READY and c.target_sim_time(123.0) == 0.0 and c.target_seq(5.0) == 0
    c.start(100.0)
    assert c.state == PlaybackState.RUNNING
    assert c.target_sim_time(110.0) == pytest.approx(600.0)  # 10 s × 1 × 60
    assert c.target_seq(130.0) == 2  # sim 1800 → both events at 1800 are due
    c.pause(110.0)
    assert c.state == PlaybackState.PAUSED
    assert c.target_sim_time(500.0) == pytest.approx(600.0)
    c.resume(200.0)
    assert c.target_sim_time(205.0) == pytest.approx(900.0)
    c.set_speed(205.0, 10)
    assert c.target_sim_time(205.0) == pytest.approx(900.0)  # no jump when changing speed
    assert c.target_sim_time(206.0) == pytest.approx(1500.0)  # 1 s × 10 × 60
    assert c.seconds_until_next_event(206.0) == pytest.approx((1800.0 - 1500.0) / 600.0)
    c.set_speed(206.0, 25)
    assert c.rate == 25 * 60
    assert c.target_sim_time(10_000.0) == c.end_sim == 20000.0  # clamped at the last event
    assert c.tick(10_000.0) == len(TIMES) and c.state == PlaybackState.FINISHED
    with pytest.raises(ElectionNightError):
        c.set_speed(10_000.0, 3)
    with pytest.raises(ElectionNightError):
        c.pause(10_001.0)


def test_step_seek_finish_and_invalid_transitions() -> None:
    c = _clock()
    assert c.step() == 2  # the first step reveals both simultaneous events
    assert c.state == PlaybackState.PAUSED and c.target_sim_time(0.0) == 1800.0
    assert c.step() == 3 and c.step() == 4
    c.start(50.0)  # a paused clock resumes
    assert c.state == PlaybackState.RUNNING and c.target_sim_time(51.0) == pytest.approx(3060.0)
    assert c.step(51.0) == 5 and c.state == PlaybackState.PAUSED
    c.seek(100.0)
    assert c.target_seq(0.0) == 0
    c.finish()
    assert c.state == PlaybackState.FINISHED and c.target_seq(0.0) == len(TIMES)
    assert c.step() == len(TIMES)
    with pytest.raises(ElectionNightError):
        c.start(1.0)
    with pytest.raises(ElectionNightError):
        _clock().resume(1.0)
    with pytest.raises(ElectionNightError):
        PlaybackClock(event_times=np.array([5.0, 1.0]))
    with pytest.raises(ElectionNightError):
        _clock(speed=3.0)


def test_persistence_roundtrip() -> None:
    c = _clock(speed=5.0)
    c.start(1000.0)
    c.set_speed(1002.0, 2)
    state = json.loads(json.dumps(c.to_dict()))
    d = PlaybackClock.from_dict(state, TIMES)
    for now in (1002.0, 1010.0, 1100.0):
        assert d.target_sim_time(now) == c.target_sim_time(now)
        assert d.target_seq(now) == c.target_seq(now)


def test_content_is_independent_of_playback_speed(synthetic, lite_election) -> None:  # type: ignore[no-untyped-def]
    cfg = default_night_config()
    tl = generate_timeline(synthetic.frame, lite_election.ballots, cfg, seed=3)
    clocks = {}
    for speed in (1.0, 25.0):
        c = PlaybackClock.from_timeline(tl, cfg, speed=speed)
        c.start(0.0)
        # reach the same simulated time: 3 600 s after polls close
        clocks[speed] = c.target_seq(3600.0 / (speed * cfg.playback.base_rate))
    assert clocks[1.0] == clocks[25.0] == tl.seq_at_time(3600.0)
    snaps = []
    for path in ((clocks[1.0],), (5, 17, clocks[1.0])):
        eng = NightEngine(synthetic.frame, tl, lite_election.races, lite_election.meta, cfg, seed=4)
        for s in path:
            eng.advance_to_seq(s)
        snaps.append(json.dumps(eng.snapshot("full"), sort_keys=True))
    assert snaps[0] == snaps[1]
