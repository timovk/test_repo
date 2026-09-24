"""Adversarial tests of the election-night engine: leakage, replay, manual overrides, tallies.

Each test tries to break one of the engine's contracts (``docs/RACE_CALLING.md``):

* the caller and every published number depend on counted votes only (no leakage of the
  final result of unreported ballots through any cached state);
* manual overrides replay bit-for-bit, from ``engine.manual_calls`` *and* from stored call
  records (``manual_calls_from_history``), and can never lock a race before the count ends;
* the presidency / chamber control follow retractions, and the national popular-vote race is
  never called in place of the Electoral College;
* rewinds from state checkpoints equal a fresh replay;
* snapshots are strict JSON; counted votes are monotone and exact at 100 %; corrupt or
  inconsistent inputs are rejected or can only lower win probabilities.
"""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

from app.core.config import get_constitution
from app.core.constitution import DECIDED_STATUSES, RaceStatus, RaceType
from app.core.errors import ElectionNightError
from app.reporting.live import (
    MANUAL_STATUSES,
    ManualCall,
    NightEngine,
    RaceMeta,
    manual_calls_from_history,
)
from app.reporting.timeline import Timeline, generate_timeline

FINAL_STATES = {RaceStatus.FINAL, RaceStatus.RECOUNT}


@pytest.fixture(scope="module")
def tl(synthetic, lite_election, night_config) -> Timeline:  # type: ignore[no-untyped-def]
    return generate_timeline(synthetic.frame, lite_election.ballots, night_config, seed=41)


def _engine(synthetic, election, cfg, tl, **kw) -> NightEngine:  # type: ignore[no-untyped-def]
    kw.setdefault("holdover_senate", election.holdover)
    return NightEngine(synthetic.frame, tl, election.races, election.meta, cfg, seed=13, **kw)


def _history(eng: NightEngine) -> str:
    return json.dumps([r.to_dict() for r in eng.call_history], sort_keys=True, allow_nan=False)


def _strict(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, allow_nan=False)


# --------------------------------------------------------------------------- leakage
def test_engine_output_does_not_depend_on_unreported_results(
    synthetic, lite_election, night_config, tl
) -> None:  # type: ignore[no-untyped-def]
    """Swap the final votes of every still-unreported unit: nothing published may change."""
    s = tl.n_events // 3
    f = tl.fraction_after(s)
    altered = {}
    for key, rv in lite_election.races.items():
        votes = rv.votes.copy()
        hidden = f[rv.unit_index] == 0.0
        assert hidden.any()
        votes[hidden] = votes[hidden][:, ::-1]  # radically different, same valid total per unit
        altered[key] = dataclasses.replace(rv, votes=votes)
    a = _engine(synthetic, lite_election, night_config, tl)
    b = NightEngine(synthetic.frame, tl, altered, lite_election.meta, night_config, seed=13)
    a.advance_to_seq(s)
    b.advance_to_seq(s)
    assert _strict(a.snapshot("full")) == _strict(b.snapshot("full"))
    assert _history(a) == _history(b)
    for key in a.race_keys:
        assert _strict(a.race_detail(key)) == _strict(b.race_detail(key))
    # …while the final results really differ
    b.finish()
    a.finish()
    assert any(a.counted_votes(k) != b.counted_votes(k) for k in a.race_keys)


# --------------------------------------------------------------------------- counting
def test_counted_votes_monotone_and_exact(synthetic, lite_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, lite_election, night_config, tl)
    finals = {k: rv.totals() for k, rv in lite_election.races.items()}
    prev = {k: np.zeros_like(v) for k, v in finals.items()}
    step = max(1, tl.n_events // 150)
    while not eng.is_finished:
        eng.advance(step)
        for k, fin in finals.items():
            cur = np.array(list(eng.counted_votes(k).values()))
            assert np.all(cur >= prev[k]) and np.all(cur <= fin), k
            prev[k] = cur
    for k, fin in finals.items():
        assert np.array_equal(prev[k], fin), k
        assert eng.race_state(k).status in FINAL_STATES


def test_snapshot_reporting_is_live_between_evaluations(synthetic, lite_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, lite_election, night_config, tl)
    stale_seen = False
    for s in np.linspace(1, tl.n_events - 1, 40).astype(int).tolist():
        eng.advance_to_seq(s)
        f = eng.reported_fraction
        snap = eng.snapshot()
        for row in snap["races"]:
            rv = lite_election.races[row["key"]]
            x = rv.expected_turnout * rv.eligible
            assert row["reporting_pct"] == pytest.approx(
                100.0 * float(f[rv.unit_index] @ x) / x.sum(), abs=1e-3
            )
            votes = np.array(list(row["votes"].values()))
            if votes.sum():
                top = np.sort(votes)[::-1]
                assert row["margin_pct"] == pytest.approx(100.0 * (top[0] - top[1]) / votes.sum(), abs=1e-3)
            assert row["evaluated_seq"] is None or row["evaluated_seq"] <= s
            stale_seen |= row["evaluated_seq"] is not None and row["evaluated_seq"] < s
        pv = snap["popular_vote"]
        for line in pv["lines"]:
            assert line["projected_votes"] >= line["votes"] - 0.1
    assert stale_seen, "some races should not have been re-evaluated at every event"


def test_snapshots_and_details_are_strict_json(synthetic, small_election, night_config) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    meta = {
        k: dataclasses.replace(m, electoral_votes=np.int64(m.electoral_votes))
        if m.electoral_votes is not None
        else m
        for k, m in small_election.meta.items()
    }
    tl = generate_timeline(frame, small_election.ballots, night_config, seed=6)
    eng = NightEngine(frame, tl, small_election.races, meta, night_config, seed=6)
    for s in (0, tl.n_events // 5, tl.n_events // 2, tl.n_events):
        eng.advance_to_seq(s)
        for detail in ("summary", "full"):
            _strict(eng.snapshot(detail))
        _strict(eng.race_detail("PRES"))
        for key in eng.race_keys[:: max(1, len(eng.race_keys) // 25)]:
            _strict(eng.race_detail(key))
        for code in frame.muni_codes[:: max(1, frame.n_munis // 10)]:
            _strict(eng.municipality_detail(code))
    _strict([r.to_dict() for r in eng.call_history])


# --------------------------------------------------------------------------- tallies
def test_electoral_votes_only_from_decided_races(synthetic, lite_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    const = get_constitution()
    eng = _engine(synthetic, lite_election, night_config, tl)
    ev = lite_election.ev
    for s in np.linspace(0, tl.n_events, 30).astype(int).tolist():
        eng.advance_to_seq(s)
        snap = eng.snapshot()
        pres = snap["president"]
        leaders = {row["key"]: row["leader"] for row in snap["races"]}
        decided: dict[str, int] = {}
        leading: dict[str, int] = {}
        for key in ev:
            st = eng.race_state(key)
            if st.status in DECIDED_STATUSES:
                decided[st.key] = decided.get(st.key, 0) + ev[key]
            else:
                leader = leaders[key]
                if leader is not None:
                    leading[leader] = leading.get(leader, 0) + ev[key]
        for t in pres["tickets"]:
            assert t["ev_decided"] == decided.get(t["key"], 0)
            assert t["ev_leading"] == leading.get(t["key"], 0)
        assert pres["ev_decided_total"] + pres["ev_uncalled"] == const.electoral_votes
        best = max(decided.values(), default=0)
        assert (pres["winner"] is not None) == (best >= const.presidential_majority)


def _call_all(eng: NightEngine, keys: list[str], status: RaceStatus, key: str | None) -> None:
    for k in keys:
        eng.apply_manual_call(k, status, key, reason="test")


def test_presidency_retraction_never_falls_back_to_too_early(
    synthetic, lite_election, night_config, tl
) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, lite_election, night_config, tl)
    eng.advance_to_seq(tl.n_events // 10)
    pres_keys = sorted(lite_election.ev)
    _call_all(eng, pres_keys, RaceStatus.CALLED, "T-ORANJE")
    assert eng.race_state("PRES").status == RaceStatus.CALLED and eng.race_state("PRES").key == "T-ORANJE"
    _call_all(eng, pres_keys, RaceStatus.TOO_CLOSE, None)
    history = eng.call_history
    first = next(i for i, r in enumerate(history) if r.race_key == "PRES" and r.retracted)
    assert history[first].status == RaceStatus.TOO_CLOSE
    assert history[first].evidence["retracted_key"] == "T-ORANJE"
    for k in pres_keys:
        eng.clear_manual_call(k, reason="back to the model")
    eng.finish()
    later = [r.status for r in eng.call_history[first + 1 :] if r.race_key == "PRES"]
    assert RaceStatus.TOO_EARLY not in later and RaceStatus.POLLS_CLOSED not in later
    assert eng.race_state("PRES").status in {RaceStatus.TOO_CLOSE, RaceStatus.CALLED, RaceStatus.FINAL}


def test_chamber_control_follows_retractions(synthetic, small_election, night_config) -> None:  # type: ignore[no-untyped-def]
    const = get_constitution()
    frame = synthetic.frame
    tl = generate_timeline(frame, small_election.ballots, night_config, seed=12)
    holdover = {"ORA": const.senate_majority - 2, "BLA": 3}
    eng = _engine(synthetic, small_election, night_config, tl, holdover_senate=holdover)
    eng.advance_to_seq(20)
    house = sorted(k for k in small_election.races if k.startswith("HOUSE-"))
    senate = sorted(k for k in small_election.races if k.startswith("SEN-"))
    _call_all(eng, house[: const.house_majority], RaceStatus.CALLED, "ORA-cand")
    _call_all(eng, senate[:2], RaceStatus.CALLED, "ORA-cand")
    snap = eng.snapshot()
    assert snap["house"]["control"] == "ORA" and snap["house"]["control_at_seq"] == 20
    assert snap["senate"]["control"] == "ORA" and snap["senate"]["control_at_seq"] == 20
    eng.advance_to_seq(30)
    _call_all(eng, house + senate, RaceStatus.TOO_CLOSE, None)  # every call retracted
    snap = eng.snapshot()
    assert snap["house"]["control"] is None and snap["house"]["control_at_seq"] is None
    assert snap["senate"]["control"] is None and snap["senate"]["control_at_seq"] is None
    eng.advance_to_seq(40)
    _call_all(eng, house[: const.house_majority], RaceStatus.CALLED, "ORA-cand")
    assert eng.snapshot()["house"]["control"] == "ORA" and eng.snapshot()["house"]["control_at_seq"] == 40


# --------------------------------------------------------------------------- manual overrides
def test_manual_final_or_recount_is_rejected(synthetic, lite_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, lite_election, night_config, tl)
    eng.advance_to_seq(40)
    for status in (RaceStatus.FINAL, RaceStatus.RECOUNT, RaceStatus.SCHEDULED):
        assert status not in MANUAL_STATUSES
        with pytest.raises(ElectionNightError):
            eng.apply_manual_call("GOV-UT", status, "BLA-cand")
    with pytest.raises(ElectionNightError):
        eng.apply_manual_call("GOV-UT", RaceStatus.CALLED, None)  # a call needs a line
    with pytest.raises(ElectionNightError):
        _engine(
            synthetic,
            lite_election,
            night_config,
            tl,
            manual_calls=[ManualCall("GOV-UT", 5, RaceStatus.FINAL, "BLA-cand")],
        )
    with pytest.raises(ElectionNightError):
        _engine(
            synthetic,
            lite_election,
            night_config,
            tl,
            manual_calls=[ManualCall("GOV-UT", tl.n_events + 1, None)],
        )
    assert eng.manual_calls == []  # rejected overrides are not recorded
    rec = eng.apply_manual_call("GOV-UT", RaceStatus.TOO_CLOSE, "BLA-cand", reason="hold")
    assert rec is not None and rec.key is None and eng.race_state("GOV-UT").key is None
    # the override never survives the end of the count
    eng.apply_manual_call("GOV-UT", RaceStatus.CALLED, "BLA-cand", reason="desk")
    eng.finish()
    st = eng.race_state("GOV-UT")
    rv = lite_election.races["GOV-UT"]
    assert st.status in FINAL_STATES and not st.is_manual
    if st.status == RaceStatus.FINAL:
        assert st.key == rv.line_keys[int(np.argmax(rv.totals()))]


def test_manual_overrides_replay_identically_from_records(synthetic, lite_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, lite_election, night_config, tl)
    k = next(i for i in range(tl.n_events // 5, tl.n_events) if tl.sim_time_s[i + 1] - tl.sim_time_s[i] > 20)
    t = float(tl.sim_time_s[k]) + 7.0  # the playback clock is between two events
    eng.advance_to_time(t)
    assert eng.seq == k + 1 and eng.sim_time_s == t
    rec = eng.apply_manual_call("GOV-UT", RaceStatus.CALLED, "ROO-cand", reason="desk decision")
    assert rec is not None and rec.sim_time_s == t and rec.is_manual
    assert rec.timestamp == tl.local_datetime(t) and rec.timestamp.tzinfo is not None
    eng.advance(30)
    cleared = eng.clear_manual_call("GOV-UT", reason="back to the model")
    assert cleared is not None and cleared.clears_manual and not cleared.is_manual
    assert cleared.override_reason == "back to the model"
    assert cleared.evidence["manual_cleared"] is True
    eng.advance(30)
    eng.apply_manual_call("PRES-NH", RaceStatus.LEAN, "T-BLAUW", reason="lean")
    eng.apply_manual_call("GOV-ZH", RaceStatus.TOO_CLOSE, reason="hold")
    eng.advance(10)
    eng.clear_manual_call("GOV-ZH")
    target = eng.seq
    live = _history(eng)

    replay = _engine(synthetic, lite_election, night_config, tl, manual_calls=eng.manual_calls)
    replay.advance_to_seq(target)
    assert _history(replay) == live

    rows = json.loads(live)  # e.g. race_call rows mapped back by services
    rebuilt = manual_calls_from_history(rows)
    assert rebuilt == eng.manual_calls
    assert [m.sim_time_s for m in rebuilt] == [m.sim_time_s for m in eng.manual_calls]
    assert rebuilt == manual_calls_from_history(eng.call_history)
    again = _engine(synthetic, lite_election, night_config, tl, manual_calls=rebuilt)
    again.advance_to_seq(target)
    assert _history(again) == live
    # rewinding past the overrides and replaying gives the same history too
    eng.advance_to_seq(3)
    eng.advance_to_seq(target)
    assert _history(eng) == live


def test_clearing_a_manual_call_before_results_does_not_keep_a_call(
    synthetic, lite_election, night_config, tl
) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, lite_election, night_config, tl)
    assert eng.seq == 0
    eng.apply_manual_call("GOV-FR", RaceStatus.CALLED, "ORA-cand", reason="prediction")
    assert eng.race_state("GOV-FR").status == RaceStatus.CALLED
    rec = eng.clear_manual_call("GOV-FR")
    assert rec is not None and rec.retracted and rec.status == RaceStatus.POLLS_CLOSED
    assert rec.evidence["retracted_key"] == "ORA-cand"
    assert eng.race_state("GOV-FR").status == RaceStatus.POLLS_CLOSED


def test_call_records_carry_what_services_persist(synthetic, lite_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, lite_election, night_config, tl)
    eng.advance_to_seq(tl.n_events // 2)
    recs = eng.call_history
    assert recs
    for r in recs:
        assert r.timestamp is not None and r.timestamp == tl.local_datetime(r.sim_time_s)
        assert 0 <= r.seq <= eng.seq and 0.0 <= r.reporting_pct <= 100.0
        assert r.evidence.get("data_category") == "SIMULATED"
        d = r.to_dict()
        assert {"seq", "sim_time_s", "timestamp", "status", "key", "win_probability", "reporting_pct"} <= set(
            d
        )
        assert {"margin_pct", "evidence", "is_manual", "override_reason", "retracted"} <= set(d)
        if r.status in DECIDED_STATUSES and r.race_key != "PRES":
            assert r.key is not None and r.win_probability is not None
    seqs = [r.seq for r in recs]
    assert seqs == sorted(seqs)


# --------------------------------------------------------------------------- input validation
def test_invalid_race_inputs_are_rejected(synthetic, lite_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    rv = lite_election.races["GOV-UT"]

    def build(bad) -> None:  # type: ignore[no-untyped-def]
        races = {**lite_election.races, "GOV-UT": bad}
        NightEngine(frame, tl, races, lite_election.meta, night_config, seed=1)

    over = rv.eligible.copy()
    over[0] = rv.votes[0].sum() - 1  # more ballots than eligible voters
    with pytest.raises(ElectionNightError):
        build(dataclasses.replace(rv, eligible=over))
    with pytest.raises(ElectionNightError):
        build(dataclasses.replace(rv, expected_shares=rv.expected_shares[:, :2]))
    with pytest.raises(ElectionNightError):
        build(dataclasses.replace(rv, line_keys=[rv.line_keys[0]] * len(rv.line_keys)))
    with pytest.raises(ElectionNightError):
        build(dataclasses.replace(rv, expected_turnout=rv.expected_turnout[:-1]))


def test_corrupt_expectations_never_produce_confident_calls(
    synthetic, lite_election, night_config, tl
) -> None:  # type: ignore[no-untyped-def]
    races = dict(lite_election.races)
    rv = races["GOV-UT"]
    shares = rv.expected_shares.copy()
    shares[::5] = np.nan
    turnout = rv.expected_turnout.copy()
    turnout[1::7] = np.nan
    races["GOV-UT"] = dataclasses.replace(rv, expected_shares=shares, expected_turnout=turnout)
    eng = NightEngine(synthetic.frame, tl, races, lite_election.meta, night_config, seed=3)
    winner = rv.line_keys[int(np.argmax(rv.totals()))]
    while not eng.is_finished:
        eng.advance(max(1, tl.n_events // 60))
        _strict(eng.snapshot())
        st = eng.race_state("GOV-UT")
        if st.status in (RaceStatus.PROJECTED, RaceStatus.CALLED):
            assert st.key == winner
        d = eng.decision("GOV-UT")
        assert d is not None and all(np.isfinite(list(d.win_probability.values())))
    assert eng.race_state("GOV-UT").status in FINAL_STATES


# --------------------------------------------------------------------------- rewinds
def _fingerprint(eng: NightEngine) -> str:
    return _strict(
        [
            eng.snapshot("full"),
            [r.to_dict() for r in eng.call_history],
            [lc.to_dict() for lc in eng.lead_changes],
            eng.race_detail("GOV-UT"),
            eng.race_detail("PRES"),
            eng.reported_fraction.tolist(),
        ]
    )


def test_rewinds_from_checkpoints_equal_a_fresh_replay(synthetic, lite_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, lite_election, night_config, tl)
    eng.checkpoint_every = 37
    eng.finish()
    assert len(eng._checkpoints) == tl.n_events // 37
    targets = [tl.n_events - 1, 36, 38, 0, 37, tl.n_events // 3, 2 * tl.n_events // 3, 75]
    fresh = _engine(synthetic, lite_election, night_config, tl)
    expected = {}
    for s in sorted(set(targets)):  # one forward pass without rewinds
        fresh.advance_to_seq(s)
        expected[s] = _fingerprint(fresh)
    for s in targets:  # jumps back and forth through the checkpoints
        eng.advance_to_seq(s)
        assert _fingerprint(eng) == expected[s], s
    # a manual call after a rewind invalidates the later checkpoints (they lack the override)
    s = 100
    eng.advance_to_seq(s)
    eng.apply_manual_call("GOV-UT", RaceStatus.CALLED, "GRO-cand", reason="rewound desk")
    assert all(c < s for c in eng._checkpoints)
    eng.advance_to_seq(tl.n_events // 2)
    eng.advance_to_seq(s + 50)
    fresh = _engine(synthetic, lite_election, night_config, tl, manual_calls=eng.manual_calls)
    fresh.advance_to_seq(s + 50)
    assert _fingerprint(eng) == _fingerprint(fresh)
    eng.advance_to_seq(s - 10)  # before the override: it is pending again
    fresh = _engine(synthetic, lite_election, night_config, tl, manual_calls=eng.manual_calls)
    fresh.advance_to_seq(s - 10)
    assert _fingerprint(eng) == _fingerprint(fresh)
    assert not eng.race_state("GOV-UT").is_manual
    eng.advance_to_seq(s)
    assert eng.race_state("GOV-UT").is_manual and eng.race_state("GOV-UT").key == "GRO-cand"


# --------------------------------------------------------------------------- the national race
RACE_ARRAYS = (
    "unit_index",
    "votes",
    "ballots_cast",
    "blank",
    "invalid",
    "eligible",
    "expected_shares",
    "expected_turnout",
)


def _national_race(election):  # type: ignore[no-untyped-def]
    """The national popular-vote race the simulation produces next to the province races."""
    parts = [election.races[k] for k in sorted(election.ev)]
    cat = {name: np.concatenate([getattr(rv, name) for rv in parts]) for name in RACE_ARRAYS}
    return dataclasses.replace(parts[0], race_key="PRES", **cat)


def test_national_presidential_race_follows_the_electoral_college(
    synthetic, lite_election, night_config, tl
) -> None:  # type: ignore[no-untyped-def]
    """The simulation also emits a national 'PRES' popular-vote race; it used to be called as a
    plurality race and its records were published under the Electoral College's 'PRES' key."""
    races = {**lite_election.races, "PRES": _national_race(lite_election)}
    meta = {**lite_election.meta, "PRES": RaceMeta(race_type=RaceType.PRESIDENT, name="President")}
    eng = NightEngine(synthetic.frame, tl, races, meta, night_config, seed=13)
    assert eng.derived_races == ["PRES"] and "PRES" not in eng.race_keys
    plain = _engine(synthetic, lite_election, night_config, tl)
    for s in (tl.n_events // 3, tl.n_events):
        eng.advance_to_seq(s)
        plain.advance_to_seq(s)
        assert _history(eng) == _history(plain)
        assert all(
            r.evidence["basis"] == "electoral_college" for r in eng.call_history if r.race_key == "PRES"
        )
        national = eng.counted_votes("PRES")
        assert national == {line["key"]: line["votes"] for line in eng.snapshot()["popular_vote"]["lines"]}
        assert eng.race_state("PRES") == plain.race_state("PRES")
    assert sum(eng.counted_votes("PRES").values()) == int(races["PRES"].votes.sum())
    with pytest.raises(ElectionNightError):  # 'PRES' is reserved for the Electoral College
        NightEngine(
            synthetic.frame,
            tl,
            {**lite_election.races, "PRES": races["GOV-UT"]},
            {**lite_election.meta, "PRES": lite_election.meta["GOV-UT"]},
            night_config,
        )
    with pytest.raises(ElectionNightError):  # docs/ARCHITECTURE.md calls this argument ev_by_race
        NightEngine(synthetic.frame, tl, lite_election.races, dict(lite_election.ev), night_config)
