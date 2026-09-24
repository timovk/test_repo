"""Election-night reporting timeline: completeness, exactness, determinism, realism."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from app.core.errors import ElectionNightError
from app.reporting.calling import allocate_counted
from app.reporting.config import default_night_config
from app.reporting.timeline import (
    FRACTION_QUANTUM,
    Timeline,
    _municipality_batches,
    default_wijk_keys,
    generate_timeline,
    polls_close_times,
)


@pytest.fixture(scope="module")
def tl(synthetic, small_election, night_config) -> Timeline:  # type: ignore[no-untyped-def]
    return generate_timeline(synthetic.frame, small_election.ballots, night_config, seed=11)


def test_timeline_is_complete_and_exact(synthetic, tl: Timeline) -> None:
    frame = synthetic.frame
    assert tl.validate() == []
    assert tl.n_events > frame.n_munis  # several batches for bigger municipalities
    assert np.array_equal(tl.seq, np.arange(1, tl.n_events + 1))
    assert np.all(np.diff(tl.sim_time_s) >= 0)
    # every unit's increments sum to exactly 1 and its last cumulative value is exactly 1.0
    inc = np.bincount(tl.units, weights=tl.increments, minlength=frame.n_units)
    assert np.all(inc == 1.0)
    f_end = tl.fraction_after(tl.n_events)
    assert np.all(f_end == 1.0)
    last = tl.unit_last_seq()
    assert np.all(last >= 1) and np.all(last <= tl.n_events)
    # cumulative fractions are exact binary fractions
    q = tl.cumulative / FRACTION_QUANTUM
    assert np.all(q == np.round(q))


def test_counted_equals_final_at_the_end_and_monotone(synthetic, small_election, tl: Timeline) -> None:
    race = small_election.races["PRES-ZH"]
    prev = np.zeros_like(race.votes)
    for s in np.linspace(0, tl.n_events, 25).astype(int):
        counted = allocate_counted(race.votes, tl.fraction_after(int(s))[race.unit_index])
        assert np.all(counted <= race.votes)
        assert np.all(counted >= prev)
        prev = counted
    assert np.array_equal(prev, race.votes)


def test_fraction_after_matches_bruteforce(synthetic, tl: Timeline) -> None:
    f = np.zeros(synthetic.frame.n_units)
    checks = {1, 7, tl.checkpoint_every, tl.checkpoint_every + 1, tl.n_events // 2, tl.n_events}
    for s in range(1, tl.n_events + 1):
        units, cum = tl.event_units(s)
        f[units] = cum
        if s in checks:
            assert np.array_equal(f, tl.fraction_after(s))
    assert np.all(tl.fraction_after(0) == 0.0)


def test_timeline_is_deterministic(synthetic, small_election, night_config) -> None:
    a = generate_timeline(synthetic.frame, small_election.ballots, night_config, seed=5)
    b = generate_timeline(synthetic.frame, small_election.ballots, night_config, seed=5)
    c = generate_timeline(synthetic.frame, small_election.ballots, night_config, seed=6)
    for name in ("sim_time_s", "muni", "ballots", "unit_ptr", "units", "increments", "cumulative"):
        assert np.array_equal(getattr(a, name), getattr(b, name)), name
    assert not np.array_equal(a.sim_time_s, c.sim_time_s[: len(a.sim_time_s)]) or a.n_events != c.n_events


def test_small_municipalities_report_earlier(synthetic, small_election, tl: Timeline) -> None:
    frame = synthetic.frame
    ballots = np.bincount(frame.unit_muni, weights=small_election.ballots, minlength=frame.n_munis)
    first = tl.muni_first_time_s()
    last = tl.muni_last_time_s()
    order = np.argsort(ballots)
    q = len(order) // 4
    small, large = order[:q], order[-q:]
    assert np.mean(first[small]) < np.mean(first[large])
    assert np.mean(last[small]) < np.mean(last[large])
    # larger municipalities report in more batches
    nb = np.bincount(tl.muni, minlength=frame.n_munis)
    assert nb[large].mean() > nb[small].mean()
    rank_corr = np.corrcoef(np.argsort(np.argsort(ballots)), np.argsort(np.argsort(first)))[0, 1]
    assert rank_corr > 0.3


def test_night_window_is_realistic(tl: Timeline) -> None:
    first = tl.sim_time_s[0] / 60.0
    assert 20 <= first <= 60  # first results ~21:20–22:00
    median_first = np.nanmedian(tl.muni_first_time_s()) / 60.0
    assert 45 <= median_first <= 110  # most municipalities first report 21:45–22:50
    end_h = tl.end_time_s / 3600.0
    assert 7.0 <= end_h <= 9.25  # last results between 04:00 and 06:15
    s = tl.summary()
    assert s["data_category"] == "SIMULATED" and s["events"] == tl.n_events


def test_partial_precincts_and_zero_ballot_units(synthetic, small_election, night_config) -> None:
    frame = synthetic.frame
    ballots = small_election.ballots.copy()
    zero = np.arange(0, frame.n_units, 37)
    ballots[zero] = 0
    tl = generate_timeline(frame, ballots, night_config, seed=3)
    assert tl.validate() == []
    assert np.all(tl.fraction_after(tl.n_events)[zero] == 1.0)
    pieces = np.bincount(tl.units, minlength=frame.n_units)
    assert (pieces > 1).any(), "big units should be split across batches"
    assert np.all(pieces[ballots < night_config.reporting.partial_precinct.min_ballots] == 1)
    # batch ballots add up to the municipality totals
    per_muni = np.bincount(tl.muni, weights=tl.ballots, minlength=frame.n_munis)
    assert np.array_equal(per_muni, np.bincount(frame.unit_muni, weights=ballots, minlength=frame.n_munis))
    # the municipality fraction reaches exactly 1.0 at its final batch
    final = tl.batch_index == tl.batches_in_muni
    assert np.all(tl.muni_fraction_after[final] == 1.0)


def _whole_wijk_share(tl: Timeline, wijk: list[str]) -> float:
    first_seq = tl.unit_first_seq()
    wijk_arr = np.asarray(wijk)
    multi = whole = 0
    for w in np.unique(wijk_arr):
        members = np.flatnonzero(wijk_arr == w)
        if len(members) < 2:
            continue
        multi += 1
        whole += int(len(set(first_seq[members].tolist())) == 1)
    return whole / multi


def test_batches_follow_wijk_clusters(synthetic, small_election) -> None:
    frame = synthetic.frame
    wijk = synthetic.units.set_index("code").loc[frame.unit_codes, "wijk_code"].tolist()
    shares = []
    for tol in (0.0, 1.0):
        cfg = default_night_config()
        cfg.reporting.wijk_snap_tolerance = tol
        cfg.reporting.partial_precinct.enabled = False
        tl = generate_timeline(frame, small_election.ballots, cfg, seed=4, unit_wijk=wijk)
        assert tl.validate() == []
        shares.append(_whole_wijk_share(tl, wijk))
    # snapping batch cuts to wijk boundaries keeps neighbourhood clusters together
    assert shares[1] > shares[0] + 0.1


def test_units_mask(synthetic, small_election, night_config) -> None:
    frame = synthetic.frame
    mask = frame.unit_province == frame.province_index("UT")
    tl = generate_timeline(frame, small_election.ballots, night_config, seed=2, units_mask=mask)
    assert tl.validate() == []
    assert set(np.unique(tl.units).tolist()) == set(np.flatnonzero(mask).tolist())
    with pytest.raises(ElectionNightError):
        generate_timeline(
            frame, small_election.ballots, night_config, seed=2, units_mask=np.zeros(frame.n_units, bool)
        )
    with pytest.raises(ElectionNightError):
        generate_timeline(frame, small_election.ballots[:-1], night_config, seed=2)
    bad = small_election.ballots.astype(float)
    bad[3] = np.nan
    with pytest.raises(ElectionNightError):
        generate_timeline(frame, bad, night_config, seed=2)


def test_poll_close_overrides_and_dst_clock(synthetic, small_election) -> None:
    frame = synthetic.frame
    cfg = default_night_config()
    cfg.polls_close.provinces = {"GR": "20:00"}
    tl = generate_timeline(frame, small_election.ballots, cfg, seed=1, election_date=date(2028, 10, 28))
    assert tl.reference_close.hour == 20
    gr = frame.province_index("GR")
    assert tl.muni_close_offset_s[frame.munis_in_province(gr)].max() == 0.0
    assert np.all(tl.muni_close_offset_s[frame.munis_in_province(frame.province_index("NH"))] == 3600.0)
    # 28 Oct 2028 20:00 CEST (UTC+2) + 7 h = 29 Oct 02:00 CET (clocks went back at 03:00 CEST)
    assert tl.local_clock(7 * 3600) == "02:00"
    assert tl.local_datetime(7 * 3600).utcoffset().total_seconds() == 3600
    gr_first = np.nanmin(tl.muni_first_time_s()[frame.munis_in_province(gr)])
    nh_first = np.nanmin(tl.muni_first_time_s()[frame.munis_in_province(frame.province_index("NH"))])
    assert gr_first < nh_first


def test_rows_roundtrip_is_bit_exact(synthetic, tl: Timeline) -> None:
    frame = synthetic.frame
    rows = tl.to_event_rows(frame)
    unit_rows = tl.to_unit_rows(frame)
    assert rows[0]["seq"] == 1 and len(rows) == tl.n_events
    assert all(r["timestamp"].tzinfo is not None for r in rows[:5])
    back = Timeline.from_records(
        frame, rows, unit_rows, reference_close=tl.reference_close, timezone=tl.timezone
    )
    assert back.validate() == []
    assert np.array_equal(back.cumulative, tl.cumulative)
    assert np.array_equal(back.units, tl.units)
    assert np.array_equal(back.sim_time_s, tl.sim_time_s)
    assert np.array_equal(back.fraction_after(tl.n_events // 3), tl.fraction_after(tl.n_events // 3))


def test_default_wijk_keys() -> None:
    assert default_wijk_keys(["BU03630001", "BU03630002", "BU03631005", "X1"]) == [
        "BU036300",
        "BU036300",
        "BU036310",
        "X1",
    ]


def test_event_access(tl: Timeline) -> None:
    ev = tl.event(1)
    assert ev.seq == 1 and ev.ballots >= 0 and len(ev.units) == len(ev.cumulative) > 0
    assert ev.timestamp.tzinfo is not None
    assert tl.seq_at_time(tl.sim_time_s[0] - 1) == 0
    assert tl.seq_at_time(tl.end_time_s) == tl.n_events
    with pytest.raises(ElectionNightError):
        tl.event(0)
    with pytest.raises(ElectionNightError):
        tl.fraction_after(tl.n_events + 1)


def test_rows_roundtrip_restores_poll_closing_offsets(synthetic, small_election) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    cfg = default_night_config()
    cfg.polls_close.provinces = {"ZE": "20:00"}
    tl = generate_timeline(frame, small_election.ballots, cfg, seed=2)
    ref, offsets = polls_close_times(frame, cfg)
    assert ref == tl.reference_close and np.array_equal(offsets, tl.muni_close_offset_s)
    back = Timeline.from_records(
        frame,
        tl.to_event_rows(frame),
        tl.to_unit_rows(frame),
        reference_close=ref,
        timezone=tl.timezone,
        muni_close_offset_s=offsets,
    )
    assert np.array_equal(back.muni_close_offset_s, tl.muni_close_offset_s)
    with pytest.raises(ElectionNightError):
        Timeline.from_records(
            frame, tl.to_event_rows(frame), [(tl.n_events + 1, frame.unit_codes[0], 1.0)], reference_close=ref
        )


class _RiggedRng:
    """Generator stand-in that returns crafted Dirichlet batch proportions."""

    def __init__(self, props: np.ndarray) -> None:
        self.props = props
        self.g = np.random.default_rng(0)

    def permutation(self, n: int) -> np.ndarray:
        return np.arange(n)

    def dirichlet(self, alpha: np.ndarray) -> np.ndarray:
        return self.props

    def uniform(self, size: int) -> np.ndarray:
        return self.g.uniform(size=size)


def test_batch_cuts_closer_than_one_ballot_are_merged() -> None:
    """Two batch cuts inside one big unit that are < 1 ballot apart used to give the unit two
    pieces with the same quantised cumulative fraction (a zero increment)."""
    rep = default_night_config().reporting
    props = np.array([0.4, 0.2, 1e-8, 0.4 - 1e-8])
    mb = _municipality_batches(
        _RiggedRng(props), np.array([7]), np.array([10_000]), np.array([0]), 4, 1000.0, 3600.0, rep
    )
    assert np.all(np.diff(mb.piece_cum) > 0) and np.all(mb.piece_inc > 0)
    assert mb.piece_cum[-1] == 1.0 and len(mb.times) == len(np.unique(mb.piece_batch)) == 3
    assert mb.ballots.sum() == 10_000 and mb.frac_after[-1] == 1.0


@pytest.mark.realdata
def test_real_geography_reporting_curve(real_frame, election_builder) -> None:  # type: ignore[no-untyped-def]
    """Defaults on the REAL 2025 geography: small municipalities from about 21:25, cities through
    the night, last results 05:00-06:00 (FICTIONAL counting model; docs/RACE_CALLING.md section 1)."""
    el = election_builder(real_frame, seed=3, house=False, senate=False, governors=False)
    cfg = default_night_config()
    B = el.ballots.astype(float)
    Bm = np.bincount(real_frame.unit_muni, weights=B, minlength=real_frame.n_munis)
    small = Bm <= np.quantile(Bm, 0.1)
    cities = np.argsort(-Bm)[:4]
    for seed in (3, 11):
        tl = generate_timeline(real_frame, el.ballots, cfg, seed=seed)
        assert tl.validate() == []
        assert 2_000 <= tl.n_events <= 3_000  # the event count (cost of a night) is unchanged

        def counted_by(clock: str, tl: Timeline = tl) -> float:
            h, m = map(int, clock.split(":"))
            t = 60.0 * ((h * 60 + m - 21 * 60) % (24 * 60))
            return float(tl.fraction_after(tl.seq_at_time(t)) @ B) / B.sum()

        assert "21:20" <= tl.local_clock(tl.sim_time_s[0]) <= "21:45"
        assert counted_by("22:00") < 0.05
        assert 0.10 <= counted_by("23:00") <= 0.35
        assert 0.35 <= counted_by("00:00") <= 0.65
        assert counted_by("03:00") >= 0.80
        assert "05:00" <= tl.local_clock(tl.end_time_s) <= "06:00"
        first, last = tl.muni_first_time_s(), tl.muni_last_time_s()
        assert np.nanmedian(first[small]) / 60 < 80  # small municipalities first report by ~22:20
        assert np.nanmedian(first[small]) < np.nanmin(first[cities])
        assert np.all(last[cities] / 3600 >= 5.0)  # the big cities count through the night
