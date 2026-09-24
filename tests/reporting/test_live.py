"""Live election-night engine: tallies, majorities, snapshots, determinism, overrides."""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.core.config import get_constitution
from app.core.constitution import DECIDED_STATUSES, RaceStatus
from app.core.errors import ElectionNightError, NotFoundError
from app.reporting.live import CallRecord, ManualCall, NightEngine
from app.reporting.timeline import Timeline, generate_timeline

FINAL_STATES = {RaceStatus.FINAL, RaceStatus.RECOUNT}


@pytest.fixture(scope="module")
def tl(synthetic, small_election, night_config) -> Timeline:  # type: ignore[no-untyped-def]
    return generate_timeline(synthetic.frame, small_election.ballots, night_config, seed=31)


@pytest.fixture(scope="module")
def finished(synthetic, small_election, night_config, tl) -> NightEngine:  # type: ignore[no-untyped-def]
    eng = NightEngine(
        synthetic.frame,
        tl,
        small_election.races,
        small_election.meta,
        night_config,
        seed=5,
        holdover_senate=small_election.holdover,
    )
    eng.finish()
    return eng


def _engine(synthetic, election, cfg, tl, **kw) -> NightEngine:  # type: ignore[no-untyped-def]
    return NightEngine(
        synthetic.frame,
        tl,
        election.races,
        election.meta,
        cfg,
        seed=5,
        holdover_senate=election.holdover,
        **kw,
    )


def test_initial_state_is_polls_closed(synthetic, small_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, small_election, night_config, tl)
    snap = eng.snapshot()
    assert snap["seq"] == 0 and snap["status"] == "polls_closed" and snap["clock"] == "21:00"
    assert all(r["status"] == RaceStatus.POLLS_CLOSED.value for r in snap["races"])
    assert snap["president"]["status"] == RaceStatus.POLLS_CLOSED.value
    assert snap["president"]["ev_decided_total"] == 0 and snap["reporting"]["units_reported"] == 0
    json.dumps(snap)


def test_finish_every_race_final_with_exact_totals(finished: NightEngine, small_election) -> None:  # type: ignore[no-untyped-def]
    assert finished.is_finished and finished.seq == finished.total_events
    for key, rv in small_election.races.items():
        st = finished.race_state(key)
        assert st.status in FINAL_STATES, key
        assert finished.counted_votes(key) == dict(zip(rv.line_keys, rv.totals().tolist(), strict=True))
        if st.status == RaceStatus.FINAL:
            assert st.key == rv.line_keys[int(np.argmax(rv.totals()))]
    snap = finished.snapshot("full")
    json.dumps(snap)
    assert snap["status"] == "complete"
    assert snap["reporting"]["pct_expected_ballots"] == pytest.approx(100.0)
    assert snap["reporting"]["units_reported"] == snap["reporting"]["units_total"]
    assert snap["reporting"]["municipalities_complete"] == snap["reporting"]["municipalities_total"]


def test_no_wrong_calls_in_the_night(finished: NightEngine, small_election) -> None:  # type: ignore[no-untyped-def]
    for rec in finished.call_history:
        if rec.race_key == "PRES" or rec.status not in (RaceStatus.PROJECTED, RaceStatus.CALLED):
            continue
        rv = small_election.races[rec.race_key]
        assert rec.key == rv.line_keys[int(np.argmax(rv.totals()))], rec.race_key
        assert rec.reporting_pct > 0.0  # never called at 0 % with the default configuration


def test_electoral_votes_and_majority_detection(
    synthetic, small_election, night_config, tl, finished: NightEngine
) -> None:  # type: ignore[no-untyped-def]
    const = get_constitution()
    pres = finished.snapshot()["president"]
    assert pres["ev_needed"] == const.presidential_majority
    assert pres["ev_total"] == sum(small_election.ev.values()) == const.electoral_votes
    # winner-take-all: every FINAL province awards all its EV to its plurality winner
    expected = {}
    for key, rv in small_election.races.items():
        if key.startswith("PRES-") and finished.race_state(key).status == RaceStatus.FINAL:
            w = rv.line_keys[int(np.argmax(rv.totals()))]
            expected[w] = expected.get(w, 0) + small_election.ev[key]
    got = {t["key"]: t["ev_decided"] for t in pres["tickets"] if t["ev_decided"]}
    assert got == expected
    assert pres["ev_decided_total"] + pres["ev_uncalled"] == pres["ev_total"]
    winner = pres["winner"]
    assert (winner is not None) == (max(expected.values()) >= const.presidential_majority)
    if winner is not None:
        s = pres["majority_reached_at_seq"]
        eng = _engine(synthetic, small_election, night_config, tl)
        eng.advance_to_seq(s - 1)
        before = eng.snapshot()["president"]
        assert all(t["ev_decided"] < const.presidential_majority for t in before["tickets"])
        assert before["winner"] is None
        eng.advance(1)
        at = eng.snapshot()["president"]
        assert at["winner"] == winner and at["status"] == RaceStatus.CALLED.value
        ev_w = next(t["ev_decided"] for t in at["tickets"] if t["key"] == winner)
        assert ev_w >= const.presidential_majority
        pres_recs = [
            r for r in finished.call_history if r.race_key == "PRES" and r.status == RaceStatus.CALLED
        ]
        assert pres_recs and pres_recs[0].seq == s and pres_recs[0].key == winner


def test_contingent_election_detection(synthetic, election_builder, night_config) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    base = election_builder(frame, seed=4, house=False, senate=False, governors=False)
    # spread the provinces over three tickets so that nobody can reach the EV majority
    order = sorted(base.ev, key=lambda k: -base.ev[k])
    totals = [0, 0, 0]
    assign = {}
    for key in order:
        t = int(np.argmin(totals))
        totals[t] += base.ev[key]
        assign[key.split("-")[1]] = t
    assert max(totals) < get_constitution().presidential_majority
    el = election_builder(
        frame, seed=4, house=False, senate=False, governors=False, pres_province_winner=assign
    )
    tl = generate_timeline(frame, el.ballots, night_config, seed=8)
    eng = NightEngine(frame, tl, el.races, el.meta, night_config, seed=2)
    eng.finish()
    pres = eng.snapshot()["president"]
    assert pres["winner"] is None and pres["majority_reached_at_seq"] is None
    assert pres["contingent_likely"] is True
    assert pres["contingent_likely_at_seq"] is not None and pres["contingent_likely_at_seq"] <= eng.seq
    assert pres["status"] == RaceStatus.TOO_CLOSE.value


def test_house_and_senate_tallies(finished: NightEngine, small_election) -> None:  # type: ignore[no-untyped-def]
    const = get_constitution()
    snap = finished.snapshot()
    house = snap["house"]
    assert house["majority"] == const.house_majority and house["seats_total"] == const.house_seats
    assert house["races"] == const.house_seats
    winners: dict[str, int] = {}
    incumbents: dict[str, int] = {}
    for key, rv in small_election.races.items():
        if not key.startswith("HOUSE-"):
            continue
        meta = small_election.meta[key]
        incumbents[meta.incumbent_party] = incumbents.get(meta.incumbent_party, 0) + 1
        if finished.race_state(key).status == RaceStatus.FINAL:
            p = meta.line_parties[rv.line_keys[int(np.argmax(rv.totals()))]]
            winners[p] = winners.get(p, 0) + 1
    by = {r["party"]: r for r in house["by_party"]}
    assert {p: r["called"] for p, r in by.items() if r["called"]} == winners
    assert house["called"] + house["uncalled"] == house["races"]
    for p, r in by.items():
        assert r["incumbent_seats"] == incumbents.get(p, 0)
    ctrl = [p for p, n in winners.items() if n >= const.house_majority]
    assert house["control"] == (ctrl[0] if ctrl else None)
    senate = snap["senate"]
    assert senate["majority"] == const.senate_majority and senate["seats_total"] == const.senate_seats
    assert senate["up"] + senate["holdover_total"] == const.senate_seats
    sby = {r["party"]: r for r in senate["by_party"]}
    for p, h in small_election.holdover.items():
        assert sby[p]["holdover"] == h
    won = {}
    for key, rv in small_election.races.items():
        if key.startswith("SEN-") and finished.race_state(key).status == RaceStatus.FINAL:
            p = small_election.meta[key].line_parties[rv.line_keys[int(np.argmax(rv.totals()))]]
            won[p] = won.get(p, 0) + 1
    for p, r in sby.items():
        assert r["total_decided"] == small_election.holdover.get(p, 0) + won.get(p, 0)
    sctrl = [p for p, r in sby.items() if r["total_decided"] >= const.senate_majority]
    assert senate["control"] == (sctrl[0] if sctrl else None)
    assert len(snap["governors"]) == 12 and all(
        g["status"] in {"FINAL", "RECOUNT"} for g in snap["governors"]
    )


def test_snapshot_structure_mid_night(synthetic, small_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, small_election, night_config, tl)
    eng.advance_to_seq(tl.n_events // 3)
    for detail in ("summary", "full"):
        snap = eng.snapshot(detail)
        json.dumps(snap)
        for key in (
            "data_category",
            "seq",
            "total_events",
            "sim_time_s",
            "clock",
            "status",
            "reporting",
            "popular_vote",
            "president",
            "provinces",
            "house",
            "senate",
            "governors",
            "races",
            "recent_calls",
            "lead_changes",
        ):
            assert key in snap, key
        assert snap["status"] == "counting" and snap["data_category"] == "SIMULATED"
        assert 0 < snap["reporting"]["pct_expected_ballots"] < 100
        assert len(snap["provinces"]) == 12
        assert {
            "key",
            "type",
            "status",
            "leader",
            "called_key",
            "reporting_pct",
            "margin_pct",
            "votes",
        } <= set(snap["races"][0])
        pv = snap["popular_vote"]
        assert pv["counted_valid"] == sum(line["votes"] for line in pv["lines"])
        assert pv["projected_valid"] >= pv["counted_valid"]
    full = eng.snapshot("full")
    assert "municipalities" in full and "projected_share_mean" in full["races"][0]
    with pytest.raises(ValueError):
        eng.snapshot("everything")
    # counted votes never exceed the final result mid-night
    for key, rv in small_election.races.items():
        counted = eng.counted_votes(key)
        assert all(counted[k] <= v for k, v in zip(rv.line_keys, rv.totals().tolist(), strict=True))


def test_details(finished: NightEngine, synthetic) -> None:  # type: ignore[no-untyped-def]
    d = finished.race_detail("PRES-NH")
    json.dumps(d)
    assert d["history"][0]["status"] == RaceStatus.POLLS_CLOSED.value and d["history"][0]["seq"] == 0
    assert d["history"][-1]["status"] in {"FINAL", "RECOUNT"}
    assert d["decision"]["evidence"]["data_category"] == "SIMULATED"
    assert sum(m["votes"][d["lines"][0]["key"]] for m in d["municipalities"]) == d["lines"][0]["votes"]
    p = finished.race_detail("PRES")
    json.dumps(p)
    assert p["history"]
    code = synthetic.frame.muni_codes[0]
    m = finished.municipality_detail(code)
    json.dumps(m)
    assert m["reporting_pct"] == pytest.approx(100.0) and m["events"] and m["races"]
    with pytest.raises(NotFoundError):
        finished.race_detail("NOPE")
    with pytest.raises(NotFoundError):
        finished.municipality_detail("GM9999")


def _state_fingerprint(eng: NightEngine) -> str:
    snap = eng.snapshot("full")
    hist = [r.to_dict() for r in eng.call_history]
    return json.dumps([snap, hist], sort_keys=True)


def test_determinism_independent_of_stepping(synthetic, lite_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    target = tl.n_events // 2
    a = _engine(synthetic, lite_election, night_config, tl)
    while a.seq < target:
        a.advance(1)
    b = _engine(synthetic, lite_election, night_config, tl)
    b.advance(37)
    b.advance_to_seq(target)
    c = _engine(synthetic, lite_election, night_config, tl)
    c.advance_to_time(float(tl.sim_time_s[target - 1]))
    assert c.seq == tl.seq_at_time(float(tl.sim_time_s[target - 1]))
    c.advance_to_seq(target)
    fa, fb, fc = _state_fingerprint(a), _state_fingerprint(b), _state_fingerprint(c)
    assert fa == fb == fc
    # rewinding replays deterministically
    b.advance_to_seq(target + 50)
    b.advance_to_seq(target)
    assert _state_fingerprint(b) == fa
    with pytest.raises(ElectionNightError):
        b.advance_to_seq(tl.n_events + 1)


def test_lead_changes_are_consistent(finished: NightEngine) -> None:
    lcs = finished.lead_changes
    assert all(lc.from_key != lc.to_key for lc in lcs)
    seqs = [lc.seq for lc in lcs]
    assert seqs == sorted(seqs)
    snap = finished.snapshot()
    assert snap["lead_changes"]["count"] == len(lcs)


def test_manual_override_and_replay(synthetic, lite_election, night_config, tl) -> None:  # type: ignore[no-untyped-def]
    eng = _engine(synthetic, lite_election, night_config, tl)
    eng.advance_to_seq(tl.n_events // 4)
    race = "GOV-UT"
    rec = eng.apply_manual_call(race, RaceStatus.CALLED, "BLA-cand", reason="desk decision")
    assert isinstance(rec, CallRecord) and rec.is_manual and rec.override_reason == "desk decision"
    st = eng.race_state(race)
    assert st.status == RaceStatus.CALLED and st.key == "BLA-cand" and st.is_manual
    at = eng.seq
    eng.advance(25)
    assert eng.race_state(race).is_manual and eng.race_state(race).key == "BLA-cand"
    manual = eng.manual_calls
    assert manual == [ManualCall(race, at, RaceStatus.CALLED, "BLA-cand", "desk decision")]
    # a rebuilt engine replays the override at the same seq
    other = _engine(synthetic, lite_election, night_config, tl, manual_calls=manual)
    other.advance_to_seq(eng.seq)
    assert _state_fingerprint(other) == _state_fingerprint(eng)
    eng.clear_manual_call(race, reason="back to the model")
    assert not eng.race_state(race).is_manual
    eng.finish()
    rv = lite_election.races[race]
    final = eng.race_state(race)
    assert final.status in FINAL_STATES
    if final.status == RaceStatus.FINAL:
        assert final.key == rv.line_keys[int(np.argmax(rv.totals()))]
    with pytest.raises(ElectionNightError):
        eng.apply_manual_call("GOV-NH", RaceStatus.CALLED, "nobody")


def test_timeline_must_cover_all_race_units(synthetic, small_election, night_config) -> None:  # type: ignore[no-untyped-def]
    frame = synthetic.frame
    mask = frame.unit_province != frame.province_index("ZE")
    tl = generate_timeline(frame, small_election.ballots, night_config, seed=1, units_mask=mask)
    with pytest.raises(ElectionNightError):
        NightEngine(frame, tl, small_election.races, small_election.meta, night_config, seed=1)


def test_recent_calls_feed(finished: NightEngine) -> None:
    feed = finished.snapshot()["recent_calls"]
    assert feed and all("evidence" not in r for r in feed)
    assert all(
        r["status"] in {"PROJECTED_WINNER", "CALLED", "RECOUNT", "FINAL"} or r["retracted"] or r["is_manual"]
        for r in feed
    )
    decided = [r for r in finished.call_history if r.status in DECIDED_STATUSES]
    assert decided
