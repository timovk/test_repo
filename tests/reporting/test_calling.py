"""Probabilistic race calling: state machine, calibration, retraction, evidence."""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.core.constitution import DECIDED_STATUSES, RaceStatus
from app.core.rng import make_rng
from app.elections.types import RaceVotes
from app.reporting.calling import (
    CallState,
    RaceCaller,
    RaceProgress,
    RecountInput,
    allocate_counted,
)
from app.reporting.config import default_night_config
from app.reporting.timeline import Timeline, generate_timeline

CALLS = (RaceStatus.PROJECTED, RaceStatus.CALLED)


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


def _race(
    frame,  # type: ignore[no-untyped-def]
    key: str,
    units: np.ndarray,
    base: np.ndarray,
    seed: int,
    *,
    swing_sd: float = 0.0,
    swing: np.ndarray | None = None,
    geo_sd: float = 0.3,
    muni_sd: float = 0.03,
    unit_sd: float = 0.05,
) -> RaceVotes:
    """Votes drawn consistently with the expectation plus a race-level swing and local noise."""
    rng = make_rng(seed, "test-call-race", key)
    n, L = len(units), len(base)
    log_e = np.log(base)[None, :] + rng.normal(0, geo_sd, (n, L))
    e = _softmax(log_e)
    delta = rng.normal(0, swing_sd, L) if swing is None else np.asarray(swing, dtype=float)
    _, cl = np.unique(frame.unit_muni[units], return_inverse=True)
    noise = rng.normal(0, muni_sd, (cl.max() + 1, L))[cl] + rng.normal(0, unit_sd, (n, L))
    shares = _softmax(np.log(e) + delta + noise)
    el = frame.unit_eligible[units].astype(np.int64)
    et = np.clip(0.78 + rng.normal(0, 0.03, n), 0.4, 0.95)
    ballots = rng.binomial(el, np.clip(et * np.exp(rng.normal(0, 0.02) + rng.normal(0, 0.03, n)), 0, 0.99))
    blank = rng.binomial(ballots, 0.004)
    invalid = rng.binomial(ballots - blank, 0.003)
    votes = rng.multinomial(ballots - blank - invalid, shares).astype(np.int64)
    rv = RaceVotes(key, [f"L{i}" for i in range(L)], units, votes, ballots, blank, invalid, el, e, et)
    rv.check()
    return rv


def _winner(rv: RaceVotes) -> str:
    return rv.line_keys[int(np.argmax(rv.totals()))]


def _run(caller: RaceCaller, rv: RaceVotes, frame, tl: Timeline, seed: int = 9, evaluate_zero: bool = True):  # type: ignore[no-untyped-def]
    """Evaluate a race after every timeline event touching it; returns the decisions."""
    cl = frame.unit_muni[rv.unit_index]
    in_race = np.zeros(frame.n_units, dtype=bool)
    in_race[rv.unit_index] = True
    seqs = [s for s in range(1, tl.n_events + 1) if in_race[tl.event_units(s)[0]].any()]
    prior = None
    out = []
    for s in ([0] if evaluate_zero else []) + seqs:
        prog = RaceProgress.from_race_votes(rv, tl.fraction_after(s)[rv.unit_index], cl)
        d = caller.evaluate(prog, seed=seed, seq=s, prior=prior)
        out.append(d)
        prior = d.state()
    return out


@pytest.fixture(scope="module")
def tl(synthetic, small_election) -> Timeline:  # type: ignore[no-untyped-def]
    return generate_timeline(synthetic.frame, small_election.ballots, default_night_config(), seed=21)


@pytest.fixture(scope="module")
def caller() -> RaceCaller:
    return RaceCaller(default_night_config())


def test_allocate_counted_invariants() -> None:
    final = np.array([[10, 3, 0], [7, 7, 1], [1000, 999, 5]])
    for f in (np.array([0.0, 0.0, 0.0]), np.array([0.3, 0.7, 0.999999]), np.array([1.0, 1.0, 1.0])):
        c = allocate_counted(final, f)
        assert c.dtype == np.int64 and np.all(c <= final) and np.all(c >= 0)
    assert np.array_equal(allocate_counted(final, np.ones(3)), final)
    assert np.array_equal(allocate_counted(np.array([5, 9]), np.array([0.5, 1.0])), [2, 9])


def test_never_calls_at_zero_reporting(synthetic, caller: RaceCaller) -> None:
    frame = synthetic.frame
    rv = _race(frame, "LOPSIDED", frame.units_in_province(3), np.array([0.72, 0.2, 0.08]), seed=1)
    prog = RaceProgress.from_race_votes(rv, np.zeros(len(rv.unit_index)), frame.unit_muni[rv.unit_index])
    d = caller.evaluate(prog, seed=1, seq=0)
    assert d.status == RaceStatus.POLLS_CLOSED and d.winner_key is None
    assert d.reporting_pct == 0.0 and d.counted_valid == 0
    assert d.win_probability["L0"] > 0.99  # the expectation is clear, but nothing is counted
    cfg = default_night_config()
    cfg.calling.call_at_poll_close = True
    d2 = RaceCaller(cfg).evaluate(prog, seed=1, seq=0)
    assert d2.status == RaceStatus.CALLED and d2.winner_key == "L0" and d2.basis == "poll_close"


def test_lopsided_race_is_called_early(synthetic, caller: RaceCaller, tl: Timeline) -> None:
    frame = synthetic.frame
    rv = _race(
        frame, "LOPSIDED", frame.units_in_province(6), np.array([0.66, 0.24, 0.10]), seed=2, swing_sd=0.03
    )
    decisions = _run(caller, rv, frame, tl)
    called = [d for d in decisions if d.status == RaceStatus.CALLED]
    assert called, "a 40-point race must be called"
    first = called[0]
    assert first.winner_key == _winner(rv)
    assert first.reporting_pct < 30.0
    assert all(d.winner_key == _winner(rv) for d in decisions if d.status in CALLS)
    assert decisions[-1].status == RaceStatus.FINAL and decisions[-1].winner_key == _winner(rv)
    # calls are sticky: once CALLED, the race stays called until FINAL
    i = decisions.index(first)
    assert all(d.status in (RaceStatus.CALLED, RaceStatus.FINAL) for d in decisions[i:])


def _knife_edge(frame) -> RaceVotes:  # type: ignore[no-untyped-def]
    units = frame.units_in_province(10)
    rv = _race(frame, "KNIFE", units, np.array([0.46, 0.46, 0.08]), seed=3, geo_sd=0.2)
    votes = rv.votes.copy()
    tot = votes.sum(axis=0)
    # rebalance the two leaders to a 0.08 % margin (keeps every unit's valid total)
    target = round(0.0008 * int(tot.sum()))
    shift = (tot[0] - tot[1] - target) // 2
    order = np.argsort(-votes[:, 0])
    for u in order:
        move = int(min(shift, votes[u, 0])) if shift > 0 else -int(min(-shift, votes[u, 1]))
        votes[u, 0] -= move
        votes[u, 1] += move
        shift -= move
        if shift == 0:
            break
    rv.votes = votes
    rv.check()
    t = rv.totals()
    assert 0 < (t[0] - t[1]) / t.sum() < 0.002
    return rv


def test_knife_edge_race_is_never_called_wrong(synthetic, caller: RaceCaller, tl: Timeline) -> None:
    frame = synthetic.frame
    rv = _knife_edge(frame)
    decisions = _run(caller, rv, frame, tl)
    winner = _winner(rv)
    assert not any(d.status in CALLS and d.winner_key != winner for d in decisions)
    assert any(d.status == RaceStatus.TOO_CLOSE for d in decisions[:-1])
    last = decisions[-1]
    assert last.status == RaceStatus.RECOUNT
    assert last.evidence["recount"]["reason"] == "fallback_margin"
    assert last.margin_pct < default_night_config().recount.margin_pct


def test_recount_check_is_injectable(synthetic) -> None:
    frame = synthetic.frame
    rv = _knife_edge(frame)
    prog = RaceProgress.from_race_votes(rv, np.ones(len(rv.unit_index)))
    seen: list[RecountInput] = []

    def never(r: RecountInput) -> bool:
        seen.append(r)
        return False

    d = RaceCaller(default_night_config(), recount_check=never).evaluate(prog, seed=1, seq=99)
    assert d.status == RaceStatus.FINAL and d.winner_key == _winner(rv)
    assert seen and seen[0].margin_votes == int(np.sort(rv.totals())[-1] - np.sort(rv.totals())[-2])
    d2 = RaceCaller(default_night_config(), recount_check=lambda r: True).evaluate(prog, seed=1, seq=99)
    assert d2.status == RaceStatus.RECOUNT and d2.evidence["recount"]["reason"] == "recount_check"
    d3 = RaceCaller(default_night_config(), recount_check=lambda r: (True, "margin ≤ 0.5 %")).evaluate(
        prog, seed=1, seq=99
    )
    assert (
        d3.status == RaceStatus.RECOUNT
        and d3.evidence["recount"]["reason"] == "recount_check: margin ≤ 0.5 %"
    )


def test_exact_tie_goes_to_recount(synthetic) -> None:
    frame = synthetic.frame
    units = frame.units_in_province(0)[:3]
    votes = np.array([[100, 100], [50, 60], [60, 50]], dtype=np.int64)
    b = votes.sum(axis=1)
    z = np.zeros(3, dtype=np.int64)
    rv = RaceVotes("TIE", ["A", "B"], units, votes, b, z, z, b * 2, np.full((3, 2), 0.5), np.full(3, 0.5))
    d = RaceCaller(default_night_config()).evaluate(
        RaceProgress.from_race_votes(rv, np.ones(3)), seed=1, seq=5
    )
    assert d.status == RaceStatus.RECOUNT and d.winner_key is None
    assert d.evidence["final"]["tied"] and d.win_probability == {"A": 0.5, "B": 0.5}


def test_calibration_of_projections_and_calls(synthetic, caller: RaceCaller, tl: Timeline) -> None:
    """≥ 50 seeded races drawn from expectation + race-level swing: calls are right ≥ 99 %."""
    frame = synthetic.frame
    rng = make_rng(2024, "calibration")
    n_calls = n_correct = 0
    called_races = 0
    for r in range(60):
        p = r % frame.n_provinces
        units = frame.units_in_province(p)
        if r >= 30:  # district-sized races
            k = rng.integers(0, len(units) - 12)
            units = units[k : k + 12]
        margin = rng.uniform(-0.10, 0.10)
        base = np.array([0.42 + margin / 2, 0.42 - margin / 2, 0.10, 0.06])
        rv = _race(frame, f"CAL-{r}", units, base, seed=100 + r, swing_sd=0.06)
        winner = _winner(rv)
        decisions = _run(caller, rv, frame, tl, seed=r, evaluate_zero=False)
        prev: tuple[RaceStatus, str | None] | None = None
        race_called = False
        for d in decisions[:-1]:
            state = (d.status, d.winner_key)
            if d.status in CALLS and state != prev:
                n_calls += 1
                n_correct += int(d.winner_key == winner)
                race_called = True
            prev = state
        called_races += int(race_called)
        assert decisions[-1].status in (RaceStatus.FINAL, RaceStatus.RECOUNT)
    assert called_races >= 40, "most races should be projected before the count is complete"
    assert n_calls >= 50
    assert n_correct / n_calls >= 0.99


def test_retraction_on_adversarial_ordering(synthetic) -> None:
    """Early precincts favour A far beyond expectation; the late ones flip the race to B."""
    frame = synthetic.frame
    units = frame.units_in_province(5)
    n = len(units)
    rng = make_rng(7, "adversarial")
    el = frame.unit_eligible[units].astype(np.int64)
    ballots = (el * 0.8).astype(np.int64)
    z = np.zeros(n, dtype=np.int64)
    first = np.zeros(n, dtype=bool)
    first[rng.permutation(n)[: n // 2]] = True
    share_a = np.where(first, 0.60, 0.31)
    votes = np.column_stack([np.round(ballots * share_a), ballots - np.round(ballots * share_a)]).astype(
        np.int64
    )
    rv = RaceVotes("ADV", ["A", "B"], units, votes, ballots, z, z, el, np.full((n, 2), 0.5), np.full(n, 0.8))
    rv.check()
    assert _winner(rv) == "B"
    caller = RaceCaller(default_night_config())
    order = np.r_[np.flatnonzero(first), np.flatnonzero(~first)]
    f = np.zeros(n)
    prior = None
    decisions = []
    for step, u in enumerate(order, start=1):
        f[u] = 1.0
        d = caller.evaluate(
            RaceProgress.from_race_votes(rv, f, frame.unit_muni[units]), seed=3, seq=step, prior=prior
        )
        decisions.append(d)
        prior = d.state()
    called_a = [i for i, d in enumerate(decisions) if d.status in CALLS and d.winner_key == "A"]
    assert called_a, "the adversarial ordering should provoke a (wrong) call for A"
    retractions = [d for d in decisions if d.retracted]
    assert retractions, "the call for A must be retracted"
    r0 = retractions[0]
    assert r0.status == RaceStatus.TOO_CLOSE and r0.evidence["retracted_key"] == "A"
    assert r0.evidence["retracted"] is True and r0.win_probability["A"] < 0.90
    assert decisions.index(r0) > called_a[0]
    assert decisions[-1].status == RaceStatus.FINAL and decisions[-1].winner_key == "B"


def test_mathematical_certainty_and_uncontested(synthetic, caller: RaceCaller) -> None:
    frame = synthetic.frame
    units = frame.units_in_province(2)
    rv = _race(frame, "MATH", units, np.array([0.55, 0.45]), seed=4)
    f = np.ones(len(units))
    f[np.argmin(rv.eligible)] = 0.0
    d = caller.evaluate(RaceProgress.from_race_votes(rv, f), seed=1, seq=3)
    assert d.margin_votes > d.outstanding_ballots_upper > 0
    assert d.math_certain and d.status == RaceStatus.CALLED
    assert d.basis == "mathematical" and d.win_probability[d.winner_key] == 1.0
    one = _race(frame, "SOLO", units, np.array([1.0]), seed=5)
    prog0 = RaceProgress.from_race_votes(one, np.zeros(len(units)))
    assert caller.evaluate(prog0, seed=1, seq=0).status == RaceStatus.POLLS_CLOSED
    f1 = np.zeros(len(units))
    f1[:3] = 1.0
    d1 = caller.evaluate(RaceProgress.from_race_votes(one, f1), seed=1, seq=1)
    assert d1.status == RaceStatus.CALLED and d1.basis == "uncontested"
    assert (
        caller.evaluate(RaceProgress.from_race_votes(one, np.ones(len(units))), seed=1, seq=2).status
        == RaceStatus.FINAL
    )


def test_caller_never_sees_unreported_results(synthetic, caller: RaceCaller) -> None:
    frame = synthetic.frame
    units = frame.units_in_province(8)
    rv = _race(frame, "BLIND", units, np.array([0.45, 0.4, 0.15]), seed=6)
    f = np.zeros(len(units))
    f[: len(units) // 3] = 1.0
    f[len(units) // 3] = 0.4
    other = RaceVotes(**{**rv.__dict__})
    other.votes = rv.votes.copy()
    hidden = f == 0
    other.votes[hidden] = other.votes[hidden][:, ::-1]  # radically different unreported results
    cl = frame.unit_muni[units]
    d1 = caller.evaluate(RaceProgress.from_race_votes(rv, f, cl), seed=4, seq=10)
    d2 = caller.evaluate(RaceProgress.from_race_votes(other, f, cl), seed=4, seq=10)
    assert d1.to_dict() == d2.to_dict()


def test_evidence_is_complete_serialisable_and_deterministic(synthetic, caller: RaceCaller) -> None:
    frame = synthetic.frame
    units = frame.units_in_province(9)
    rv = _race(frame, "EVID", units, np.array([0.5, 0.35, 0.15]), seed=8)
    f = np.zeros(len(units))
    f[::3] = 1.0
    f[1::7] = 0.5
    prog = RaceProgress.from_race_votes(rv, f, frame.unit_muni[units])
    prog.validate()
    d = caller.evaluate(prog, seed=11, seq=42)
    payload = json.dumps(d.to_dict())
    assert "evidence" in payload
    ev = d.evidence
    for k in (
        "method",
        "rng_stream",
        "reporting",
        "counted",
        "expected",
        "turnout",
        "swing",
        "outstanding",
        "projection",
        "win_probability",
        "n_draws",
        "thresholds",
        "comeback",
        "math_certain",
    ):
        assert k in ev, k
    assert ev["data_category"] == "SIMULATED"
    assert ev["rng_stream"] == ["call", "EVID", 42]
    assert d.units_partial > 0 and 0 < d.reporting_pct < 100
    assert abs(sum(d.win_probability.values()) - 1.0) < 1e-6
    for k in rv.line_keys:
        assert d.projected_share_p05[k] <= d.projected_share_mean[k] + 1e-9 <= d.projected_share_p95[k] + 2e-9
    assert d.counted_valid <= rv.totals().sum() and d.outstanding_ballots_est > 0
    again = caller.evaluate(prog, seed=11, seq=42)
    assert again.to_dict() == d.to_dict()
    other = caller.evaluate(prog, seed=11, seq=43)
    assert other.evidence["rng_stream"] != ev["rng_stream"]


def test_manual_prior_is_respected(synthetic, caller: RaceCaller) -> None:
    frame = synthetic.frame
    units = frame.units_in_province(4)
    rv = _race(frame, "MANUAL", units, np.array([0.62, 0.38]), seed=12)
    f = np.zeros(len(units))
    f[: len(units) // 2] = 1.0
    prog = RaceProgress.from_race_votes(rv, f, frame.unit_muni[units])
    loser = next(k for k in rv.line_keys if k != _winner(rv))
    d = caller.evaluate(prog, seed=1, seq=7, prior=CallState(RaceStatus.CALLED, loser, 5, is_manual=True))
    assert d.status == RaceStatus.CALLED and d.winner_key == loser and d.basis == "manual"
    assert d.evidence["manual_call_at_risk"] is True
    assert d.state().is_manual
    final = caller.evaluate(
        RaceProgress.from_race_votes(rv, np.ones(len(units))),
        seed=1,
        seq=8,
        prior=CallState(RaceStatus.CALLED, loser, 5, is_manual=True),
    )
    assert final.status in (RaceStatus.FINAL, RaceStatus.RECOUNT) and final.winner_key == _winner(rv)


def test_close_band_and_decided_statuses(synthetic, caller: RaceCaller, tl: Timeline) -> None:
    frame = synthetic.frame
    rv = _race(
        frame, "BAND", frame.units_in_province(7), np.array([0.44, 0.435, 0.125]), seed=13, geo_sd=0.15
    )
    decisions = _run(caller, rv, frame, tl)
    statuses = {d.status for d in decisions}
    assert RaceStatus.TOO_CLOSE in statuses or any(d.status in CALLS for d in decisions)
    for d in decisions:
        if d.status in DECIDED_STATUSES:
            assert d.winner_key is not None
        if d.status == RaceStatus.TOO_CLOSE:
            assert d.winner_key is None


# --------------------------------------------------------------------------- adversarial
def _progress(
    counted: np.ndarray,
    f: np.ndarray,
    exp_shares: np.ndarray,
    eligible: np.ndarray,
    cluster: np.ndarray,
    turnout: float = 0.78,
    key: str = "ADV",
) -> RaceProgress:
    L = counted.shape[1]
    return RaceProgress(
        race_key=key,
        line_keys=[chr(ord("A") + i) for i in range(L)],
        counted_votes=counted.astype(np.int64),
        reported_fraction=f.astype(float),
        counted_ballots=counted.sum(axis=1).astype(np.int64),
        expected_shares=exp_shares,
        expected_ballots=turnout * eligible.astype(float),
        eligible=eligible.astype(np.int64),
        unit_cluster=cluster,
    )


def test_counted_leader_is_not_called_when_the_outstanding_vote_favours_the_trailer(
    caller: RaceCaller,
) -> None:
    """A's strongholds report first: A leads the count by ~35 pp, but the expectation of the
    (much larger) outstanding vote favours B — never call A just because A leads."""
    n_a, n_b = 6, 40
    cluster = np.r_[np.zeros(n_a, int), 1 + np.arange(n_b) // 4]
    eligible = np.full(n_a + n_b, 1000)
    exp = np.r_[np.tile([0.70, 0.30], (n_a, 1)), np.tile([0.36, 0.64], (n_b, 1))]
    final = np.floor(exp * 780).astype(np.int64)  # results exactly as expected: B wins
    f = np.r_[np.ones(n_a), np.zeros(n_b)]
    prog = _progress(np.floor(final * f[:, None]), f, exp, eligible, cluster)
    d = caller.evaluate(prog, seed=3, seq=6)
    assert d.leader_key == "A" and d.margin_pct > 30.0 and 10.0 < d.reporting_pct < 15.0
    assert d.winner_key != "A"  # (projecting the trailer B from the expectation is legitimate)
    assert d.win_probability["A"] < 0.05 and d.projected_share_mean["B"] > d.projected_share_mean["A"]
    assert d.comeback["plausible"] is True
    # a landslide lead in the first 3 % is still not enough to project anything
    f2 = np.zeros(n_a + n_b)
    f2[n_a : n_a + 1] = 1.0
    d2 = caller.evaluate(_progress(np.floor(final * f2[:, None]), f2, exp, eligible, cluster), seed=3, seq=1)
    assert d2.reporting_pct < 5.0 and d2.status not in DECIDED_STATUSES


def test_mathematical_certainty_is_strict(caller: RaceCaller) -> None:
    """Certainty needs a counted lead *strictly* above every ballot that could still exist."""
    exp = np.full((3, 2), 0.5)
    cluster = np.arange(3)
    counted = np.array([[900, 400], [0, 0], [0, 0]])
    f = np.array([1.0, 0.0, 0.0])
    for outstanding, certain in ((500, False), (499, True)):
        eligible = np.array([1500, outstanding // 2, outstanding - outstanding // 2])
        d = caller.evaluate(_progress(counted, f, exp, eligible, cluster), seed=1, seq=1)
        assert d.outstanding_ballots_upper == outstanding and d.margin_votes == 500
        assert d.math_certain is certain
        assert (d.basis == "mathematical") is certain
        assert d.comeback["mathematically_possible"] is False  # a tie is the best B can do
    counted_b = np.array([[900, 400], [0, 0], [0, 0]])
    d = caller.evaluate(_progress(counted_b, f, exp, np.array([1500, 300, 201]), cluster), seed=1, seq=1)
    assert d.comeback["mathematically_possible"] is True and not d.math_certain


def test_corrupt_expectations_cannot_force_a_call(caller: RaceCaller) -> None:
    """NaN / negative expectations used to make one line win every draw (a certain 'call' for
    the counted trailer); they now fall back to flat priors."""
    n = 40
    eligible = np.full(n, 1000)
    final = np.column_stack([np.full(n, 300), np.full(n, 480)])
    f = np.zeros(n)
    f[:10] = 1.0
    counted = np.floor(final * f[:, None])
    for poison in ("nan_row", "negative", "all_nan", "nan_turnout"):
        exp = np.full((n, 2), 0.5)
        turnout = 0.78
        if poison == "nan_row":
            exp[5] = np.nan
        elif poison == "negative":
            exp[12] = [-1.0, 2.0]
        elif poison == "all_nan":
            exp[:] = np.nan
        prog = _progress(counted, f, exp, eligible, np.arange(n) // 4, turnout=turnout)
        if poison == "nan_turnout":
            prog.expected_ballots[3::5] = np.nan
        d = caller.evaluate(prog, seed=1, seq=5)
        assert not (d.status in DECIDED_STATUSES and d.winner_key == "A"), poison
        assert all(np.isfinite(v) for v in d.win_probability.values()), poison
        assert d.win_probability["A"] < 0.5, poison
        json.dumps(d.to_dict(), allow_nan=False)


def test_evaluation_has_no_hidden_state(synthetic) -> None:  # type: ignore[no-untyped-def]
    """A caller that evaluated other races / seqs before gives exactly the fresh result."""
    frame = synthetic.frame
    units = frame.units_in_province(9)
    rv = _race(frame, "STATE", units, np.array([0.48, 0.42, 0.10]), seed=31)
    cl = frame.unit_muni[units]
    f1 = np.zeros(len(units))
    f1[::4] = 1.0
    f2 = f1.copy()
    f2[1::4] = 0.6
    used = RaceCaller(default_night_config())
    for s, f in ((3, f1), (4, np.ones(len(units))), (7, f2)):
        used.evaluate(RaceProgress.from_race_votes(rv, f, cl), seed=5, seq=s)
    a = used.evaluate(RaceProgress.from_race_votes(rv, f2, cl), seed=5, seq=9)
    b = RaceCaller(default_night_config()).evaluate(RaceProgress.from_race_votes(rv, f2, cl), seed=5, seq=9)
    assert a.to_dict() == b.to_dict()


def test_reporting_share_is_clamped_at_100(caller: RaceCaller) -> None:
    rng = make_rng(3, "clamp")
    n = 257
    eligible = rng.integers(200, 3000, n)
    exp = np.full((n, 2), 0.5)
    counted = np.column_stack([eligible // 3, eligible // 4])
    f = np.ones(n)
    prog = _progress(counted, f, exp, eligible, np.arange(n) // 9)
    prog.expected_ballots = rng.uniform(0.3, 0.97, n) * eligible  # awkward floating sums
    d = caller.evaluate(prog, seed=1, seq=1)
    assert d.status in (RaceStatus.FINAL, RaceStatus.RECOUNT) and d.reporting_pct <= 100.0
    prog.reported_fraction[0] = 0.5
    d = caller.evaluate(prog, seed=1, seq=2)
    assert 0.0 < d.reporting_pct < 100.0
