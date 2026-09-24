"""Election lifecycle on the synthetic sandbox world: creation per cycle, ballots, incumbents and
careers, results persistence and reconciliation, recounts, Electoral College, contingent
elections, office holders, race calls, determinism and the hidden-until-reported rule."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date

import numpy as np
import pytest
from sqlalchemy import func, select

from app.core.constitution import ELECTORAL_VOTES, HOUSE_SEATS, RaceStatus, RaceType
from app.core.errors import ElectionError
from app.elections.recount import RecountConfig, RecountThreshold
from app.elections.tabulation import reconcile
from app.models import (
    BallotCandidate,
    Candidate,
    CandidateAffiliation,
    ContingentElection,
    Election,
    ElectionResult,
    ElectoralVoteAllocation,
    Legislature,
    LegislatureSeatResult,
    Office,
    OfficeHolder,
    Party,
    PartyEvent,
    Poll,
    Race,
    RaceCall,
    Recount,
    RecountAdjustment,
    ReportingEvent,
    Scenario,
    SenateSeat,
    SimulationRun,
    TurnoutResult,
)
from app.reporting.live import CallRecord
from app.scenarios.loader import load_scenario
from app.scenarios.schema import ScenarioDocument
from app.services._store import race_levels
from app.services.elections import (
    create_election,
    election_summary,
    finalize_election,
    instant_finalize,
    list_elections,
    simulate_election,
)
from app.services.results import results_frame
from app.services.runtime import election_inputs, load_final_race_votes, load_timeline
from app.services.validation import reconcile_election, validate_election, validate_system

_J = Race.id == BallotCandidate.race_id


def race_counts(session, election_id: int) -> dict[str, int]:  # type: ignore[no-untyped-def]
    return dict(
        session.execute(
            select(Race.race_type, func.count())
            .where(Race.election_id == election_id)
            .group_by(Race.race_type)
        )
        .tuples()
        .all()
    )


def unit_votes(session, election_id: int) -> dict[tuple[str, str, int], int]:  # type: ignore[no-untyped-def]
    rows = session.execute(
        select(Race.code, BallotCandidate.line_key, ElectionResult.geo_unit_id, ElectionResult.votes)
        .join(Race, Race.id == ElectionResult.race_id)
        .join(BallotCandidate, BallotCandidate.id == ElectionResult.ballot_candidate_id)
        .where(Race.election_id == election_id, ElectionResult.level == "unit")
    ).all()
    return {(c, k, int(u)): int(v) for c, k, u, v in rows}


# =========================================================================== creation
def test_sandbox_builds_quickly(sandbox_file) -> None:  # type: ignore[no-untyped-def]
    # CPU time: the wall clock depends on how busy the (shared) machine is
    assert sandbox_file.cpu_seconds < 30.0
    assert sandbox_file.sandbox.founding_status == "final"
    assert sandbox_file.sandbox.second_status == "simulated"


def test_race_counts_per_cycle(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    assert race_counts(s, world.founding) == {
        "PRESIDENT": 1,
        "PRESIDENT_PROVINCE": 12,
        "HOUSE": HOUSE_SEATS,
        "SENATE": 24,  # founding election: every seat
        "GOVERNOR": 12,
        "PROVINCIAL_LEGISLATURE": 12,
    }
    assert race_counts(s, world.second) == {
        "PRESIDENT": 1,
        "PRESIDENT_PROVINCE": 12,
        "HOUSE": HOUSE_SEATS,
        "SENATE": 8,
        "GOVERNOR": 12,
        "PROVINCIAL_LEGISLATURE": 12,
    }
    classes = {
        c
        for (c,) in s.execute(
            select(SenateSeat.senate_class)
            .join(Race, Race.senate_seat_id == SenateSeat.id)
            .where(Race.election_id == world.second)
        )
    }
    assert classes == {2}
    pres = s.scalar(select(Race).where(Race.election_id == world.second, Race.code == "PRES"))
    children = s.scalars(select(Race).where(Race.parent_race_id == pres.id)).all()
    assert len(children) == 12 and sum(r.electoral_votes for r in children) == ELECTORAL_VOTES
    el = s.get(Election, world.second)
    assert el.previous_election_id == world.founding and el.election_type == "general"
    assert el.election_date == load_scenario("demo-2028").scenario.election_date  # the scenario's date


def test_ballot_snapshots_and_party_lineage(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    snap = dict(
        s.execute(
            select(Race.election_id, BallotCandidate.party_name_snapshot)
            .join(Race, _J)
            .where(BallotCandidate.party_code_snapshot == "PLB")
            .group_by(Race.election_id)
        )
        .tuples()
        .all()
    )
    old_name = load_scenario("founding-2024").party("PLB").name
    new = load_scenario("demo-2028").party("PLB")
    assert old_name != new.name  # the demo scenarios rename the agrarian party in 2027
    assert snap[world.founding] == old_name  # history is not rewritten
    assert snap[world.second] == new.name
    party = s.scalar(select(Party).where(Party.code == "PLB"))
    assert party.abbreviation == new.abbreviation
    ev = s.scalars(
        select(PartyEvent).where(PartyEvent.party_id == party.id, PartyEvent.event_type == "renamed")
    ).all()
    assert len(ev) == 1 and old_name in (ev[0].old_value or "")
    # every ballot line carries its key and the party identity used on that ballot
    missing = s.scalar(
        select(func.count()).select_from(BallotCandidate).where(BallotCandidate.line_key.is_(None))
    )
    assert missing == 0
    lines = s.execute(
        select(BallotCandidate.party_code_snapshot, BallotCandidate.party_color_snapshot).where(
            BallotCandidate.party_id.is_not(None)
        )
    ).all()
    assert all(code and color for code, color in lines)


def test_candidates_and_affiliations(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    n = s.scalar(select(func.count()).select_from(Candidate))
    assert s.scalar(select(func.count()).select_from(CandidateAffiliation)) >= n
    keys = [k for (k,) in s.execute(select(Candidate.key))]
    assert len(keys) == len(set(keys))
    verbeek = s.scalar(select(Candidate).where(Candidate.key == "charlotte-verbeek"))
    q24 = load_scenario("founding-2024").candidate("charlotte-verbeek").quality
    q28 = load_scenario("demo-2028").candidate("charlotte-verbeek").quality
    assert verbeek.quality == pytest.approx(q28)  # updated by the 2028 scenario
    q = dict(
        s.execute(
            select(Race.election_id, BallotCandidate.quality_snapshot)
            .join(Race, _J)
            .where(Race.code == "PRES", BallotCandidate.line_key == "charlotte-verbeek")
        )
        .tuples()
        .all()
    )
    assert q[world.founding] == pytest.approx(q24) and q[world.second] == pytest.approx(q28)


def test_incumbents_and_careers(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    races = {r.code: r for r in s.scalars(select(Race).where(Race.election_id == world.second))}
    # the founding winners hold the offices during the 2028 campaign
    holders = {
        code: cand
        for code, cand in s.execute(
            select(Office.code, OfficeHolder.candidate_id)
            .join(Office, Office.id == OfficeHolder.office_id)
            .where(OfficeHolder.term_start == date(2025, 1, 15))
        )
    }
    house = [r for r in races.values() if r.race_type == "HOUSE"]
    running = 0
    for r in house:
        holder = holders[f"HOUSE-{r.code.removeprefix('HOUSE-')}"]
        assert r.incumbent_candidate_id == holder
        inc_lines = s.scalars(
            select(BallotCandidate).where(BallotCandidate.race_id == r.id, BallotCandidate.is_incumbent)
        ).all()
        if inc_lines:
            running += 1
            assert inc_lines[0].candidate_id == holder  # the same person (career continues)
            assert not r.is_open_seat
        else:
            assert r.is_open_seat
        prev = s.get(Race, r.previous_race_id)
        assert prev.election_id == world.founding and prev.office_id == r.office_id
    p = load_scenario("demo-2028").house.incumbent_runs_again_prob
    assert abs(running / len(house) - p) < 0.15  # incumbents run again with the scenario's probability
    pres = races["PRES"]
    founding_pres = s.scalar(select(Race).where(Race.election_id == world.founding, Race.code == "PRES"))
    winner = s.get(BallotCandidate, founding_pres.winner_ballot_candidate_id)
    assert pres.incumbent_party_id == winner.party_id
    setup = s.scalars(
        select(SimulationRun).where(
            SimulationRun.election_id == world.second, SimulationRun.kind == "election-setup"
        )
    ).one()
    snap = json.loads(setup.summary_json)
    assert snap["president_party"] == winner.party_code_snapshot
    assert len(snap["holdover_senate"]) == 16  # classes 1 and 3 hold over


def test_polls_and_campaigns_are_stored(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    types = Counter(t for (t,) in s.execute(select(Poll.poll_type).where(Poll.election_id == world.second)))
    assert types["national_president"] > 0 and types["province_president"] > 0 and types["house_district"] > 0
    assert all(p.is_fictional for p in s.scalars(select(Poll).where(Poll.election_id == world.second)).all())
    summary = election_summary(s, world.second)
    assert summary["campaigns"] >= 6 and summary["polls"] == sum(types.values())


# =========================================================================== simulation
def test_results_reconcile_and_round_trip(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    for eid in (world.founding, world.second):
        assert reconcile_election(s, eid) == []
        assert validate_election(s, eid).ok
    inputs = election_inputs(s, world.second)
    votes = load_final_race_votes(s, world.second, inputs)
    assert set(votes) == set(inputs.races)
    # the reference reconciliation agrees on a sample of races
    for code in [
        "PRES",
        "PRES-NB",
        "HOUSE-ZH-01",
        "SEN-GR-1" if "SEN-GR-1" in votes else "GOV-GR",
        "PROVLEG-UT",
    ]:
        levels = race_levels(inputs, code, votes[code], strict_reconcile=True)
        assert reconcile(levels) == []
    # the national race is the union of the province contests
    pres = votes["PRES"]
    kids = [votes[c] for c in inputs.races_of(RaceType.PRESIDENT_PROVINCE)]
    assert pres.votes.sum() == sum(k.votes.sum() for k in kids)
    # re-simulating the same seed reproduces the stored draw bit for bit (incl. expectations)
    from app.simulation.voting import simulate_election as sim

    draw = sim(inputs.model, list(inputs.races.values()), inputs.seed, inputs.context)
    for code, rv in draw.races.items():
        got = votes[code]
        assert np.array_equal(rv.votes, got.votes), code
        assert np.array_equal(rv.ballots_cast, got.ballots_cast)
        assert np.array_equal(rv.invalid, got.invalid) and np.array_equal(rv.blank, got.blank)
        assert np.array_equal(rv.expected_shares, got.expected_shares)
        assert np.array_equal(rv.expected_turnout, got.expected_turnout)


def test_timeline_round_trip_is_exact(world) -> None:  # type: ignore[no-untyped-def]
    from app.reporting.config import load_night_config
    from app.reporting.timeline import generate_timeline
    from app.services.elections import _unit_wijk

    s = world.session
    inputs = election_inputs(s, world.second)
    from app.simulation.voting import simulate_election as sim

    draw = sim(inputs.model, list(inputs.races.values()), inputs.seed, inputs.context)
    el = s.get(Election, world.second)
    expected = generate_timeline(
        inputs.frame,
        draw.turnout.ballots_cast,
        load_night_config(),
        seed=int(el.seed),
        election_date=el.election_date,
        unit_wijk=_unit_wijk(s, inputs),
    )
    got = load_timeline(s, world.second, inputs.frame)
    for name in (
        "sim_time_s", "muni", "province", "ballots", "muni_fraction_after", "batch_index",
        "batches_in_muni", "unit_ptr", "units", "increments", "cumulative", "muni_close_offset_s", "units_mask",
    ):  # fmt: skip
        a, b = getattr(got, name), getattr(expected, name)
        assert a.dtype == b.dtype and np.array_equal(a, b), name
    assert got.reference_close == expected.reference_close and got.seed == expected.seed
    assert got.config_fingerprint == expected.config_fingerprint
    assert got.validate() == []
    assert (
        s.scalar(
            select(func.count()).select_from(ReportingEvent).where(ReportingEvent.election_id == world.second)
        )
        == got.n_events
    )


def test_election_inputs_for_the_night(world) -> None:  # type: ignore[no-untyped-def]
    inputs = election_inputs(world.session, world.second)
    meta = inputs.race_meta
    assert set(meta) == set(inputs.races)
    assert meta["PRES-NB"].parent == "PRES" and meta["PRES-NB"].electoral_votes == inputs.ev_by_province["NB"]
    assert sum(inputs.ev_by_province.values()) == ELECTORAL_VOTES
    assert sum(inputs.holdover_senate.values()) == 16
    line = next(iter(inputs.races["GOV-NB"].lines))
    assert line.running_mate_key is not None  # lieutenant governors run on the same ticket
    assert all(c.startswith("#") for c in meta["HOUSE-NB-01"].line_colors.values())
    assert inputs.line_ids["PRES"].keys() == {ln.key for ln in inputs.races["PRES"].lines}
    assert inputs.context.president_party is not None
    assert len(inputs.district_codes) == HOUSE_SEATS and (inputs.unit_district >= 0).all()


def test_hidden_until_reported(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    df = results_frame(s)
    assert set(df["election_id"]) == {world.founding}
    hidden = results_frame(s, include_hidden=True)
    assert set(hidden["election_id"]) == {world.founding, world.second}
    summary = election_summary(s, world.second)
    assert summary["status"] == "simulated" and "results" not in summary
    assert "results" in election_summary(s, world.founding)
    listing = list_elections(s)
    assert [e["id"] for e in listing] == [world.founding, world.second]
    assert listing[0]["reported"] and not listing[1]["reported"]


def test_results_frame_format(world) -> None:  # type: ignore[no-untyped-def]
    from app.analytics.results import RESULTS_COLUMNS, validate_results_frame

    s = world.session
    df = results_frame(s, levels=("unit", "municipality", "district", "province", "national"))
    assert list(df.columns) == list(RESULTS_COLUMNS)
    assert validate_results_frame(df) == []
    nat = df[(df["level"] == "national") & (df["race_code"] == "PRES")]
    assert nat["geo_code"].unique().tolist() == ["NL"] and nat["winner"].sum() == 1
    dist = df[(df["level"] == "district") & (df["race_type"] == "PRESIDENT_PROVINCE")]
    assert dist["geo_code"].nunique() == HOUSE_SEATS  # presidential results by House district
    house = results_frame(s, race_types=["HOUSE"], levels=("district",))
    assert set(house["race_type"]) == {"HOUSE"} and house["geo_code"].nunique() == HOUSE_SEATS


def test_resimulation_replaces_the_draw(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    before = unit_votes(s, world.second)
    rows = s.scalar(
        select(func.count()).select_from(TurnoutResult).where(TurnoutResult.election_id == world.second)
    )
    simulate_election(s, world.second, seed=777)
    el = s.get(Election, world.second)
    assert el.seed == 777 and el.status == "simulated"
    after = unit_votes(s, world.second)
    assert after.keys() == before.keys() and after != before
    assert (
        s.scalar(
            select(func.count()).select_from(TurnoutResult).where(TurnoutResult.election_id == world.second)
        )
        == rows
    )
    runs = s.scalars(
        select(SimulationRun.seed).where(
            SimulationRun.election_id == world.second, SimulationRun.kind == "election"
        )
    ).all()
    assert 777 in runs
    assert reconcile_election(s, world.second) == []


# =========================================================================== finalization
def test_finalize_general_election(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    finalize_election(s, world.second)
    el = s.get(Election, world.second)
    assert el.status == "final" and el.finalized_at is not None
    races = s.scalars(select(Race).where(Race.election_id == world.second)).all()
    assert all(r.status == RaceStatus.FINAL.value for r in races)
    assert all(r.winner_ballot_candidate_id is not None for r in races if r.race_type != "PRESIDENT")
    pres = next(r for r in races if r.code == "PRES")
    ev = s.scalar(
        select(func.sum(ElectoralVoteAllocation.electoral_votes)).where(
            ElectoralVoteAllocation.race_id == pres.id
        )
    )
    assert ev == ELECTORAL_VOTES
    assert pres.decided_by in ("electoral_college", "contingent")
    assert any(r.flipped is not None for r in races)
    # D'Hondt seats fill every provincial legislature exactly
    for lg in s.scalars(select(Legislature).where(Legislature.level == "provincial")):
        seats = s.scalar(
            select(func.sum(LegislatureSeatResult.seats))
            .join(Race, Race.id == LegislatureSeatResult.race_id)
            .where(LegislatureSeatResult.legislature_id == lg.id, Race.election_id == world.second)
        )
        assert seats == lg.seats
    assert validate_system(s).ok
    with pytest.raises(ElectionError):
        finalize_election(s, world.second)
    with pytest.raises(ElectionError):
        simulate_election(s, world.second)


def test_office_holders_and_terms(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    rows = s.execute(
        select(Office.code, Office.office_type, OfficeHolder)
        .join(Office, Office.id == OfficeHolder.office_id)
        .where(OfficeHolder.term_start < date(2026, 1, 1))
    ).all()
    by_type = Counter(t for _, t, _ in rows)
    assert by_type == {
        "PRESIDENT": 1,
        "VICE_PRESIDENT": 1,
        "HOUSE": 150,
        "SENATE": 24,
        "GOVERNOR": 12,
        "LIEUTENANT_GOVERNOR": 12,
    }
    seat_class = dict(s.execute(select(SenateSeat.code, SenateSeat.senate_class)).tuples().all())
    for code, otype, h in rows:
        if otype in ("PRESIDENT", "VICE_PRESIDENT"):
            assert (h.term_start, h.term_end) == (date(2025, 1, 15), date(2029, 1, 15))
        elif otype == "HOUSE":
            assert (h.term_start, h.term_end) == (date(2025, 1, 15), date(2027, 1, 15))
        elif otype == "SENATE":  # classes 1/2/3 serve 2/4/6 years after the founding election
            assert h.term_end == date(2025 + 2 * seat_class[code], 1, 15)
        else:
            assert (h.term_start, h.term_end) == (date(2025, 1, 1), date(2029, 1, 1))
        assert h.ended_on is None and h.start_reason == "elected"
    # after the 2028 election the successors take office and the predecessors' terms end
    finalize_election(s, world.second)
    pres = s.scalars(
        select(OfficeHolder)
        .join(Office, Office.id == OfficeHolder.office_id)
        .where(Office.code == "PRES")
        .order_by(OfficeHolder.term_start)
    ).all()
    assert len(pres) == 2 and pres[0].ended_on == pres[1].term_start == date(2029, 1, 15)
    assert pres[0].end_reason in ("term_expired", "defeated", "retired")
    lt = s.scalars(
        select(OfficeHolder)
        .join(Office, Office.id == OfficeHolder.office_id)
        .where(Office.code == "LTGOV-NB")
        .order_by(OfficeHolder.term_start)
    ).all()
    assert len(lt) == 2 and lt[1].term_start == date(2029, 1, 1)
    # a senator of class 1 or 3 was not up in 2028 and keeps serving
    serving = s.scalar(
        select(func.count())
        .select_from(OfficeHolder)
        .join(Office, Office.id == OfficeHolder.office_id)
        .where(Office.office_type == "SENATE", OfficeHolder.ended_on.is_(None))
    )
    assert serving == 24


def test_recount_audit(world) -> None:  # type: ignore[no-untyped-def]
    """Force recounts of every House and province contest: audited corrections are applied to the
    stored rows (never re-simulated) and every level still reconciles."""
    s = world.session
    inputs = election_inputs(s, world.second)
    before = load_final_race_votes(s, world.second, inputs, with_expectation=False)
    cfg = RecountConfig(
        thresholds={
            RaceType.HOUSE: RecountThreshold(margin_pct=100.0),
            RaceType.PRESIDENT_PROVINCE: RecountThreshold(margin_pct=100.0),
        }
    )
    finalize_election(s, world.second, recount_config=cfg)
    recounts = s.scalars(
        select(Recount).join(Race, Race.id == Recount.race_id).where(Race.election_id == world.second)
    ).all()
    assert len(recounts) == HOUSE_SEATS + 12
    assert all(r.status == "completed" and r.completed_at is not None for r in recounts)
    after = load_final_race_votes(s, world.second, election_inputs(s, world.second), with_expectation=False)
    f = inputs.frame
    changed_races = 0
    for rc in recounts:
        race = s.get(Race, rc.race_id)
        adj = s.scalars(select(RecountAdjustment).where(RecountAdjustment.recount_id == rc.id)).all()
        b, a = before[race.code], after[race.code]
        delta = a.votes - b.votes
        expect = np.zeros_like(delta)
        inv = np.zeros(len(b.unit_index), dtype=np.int64)
        lines = {
            bc.id: j
            for j, bc in enumerate(
                s.scalars(
                    select(BallotCandidate)
                    .where(BallotCandidate.race_id == race.id)
                    .order_by(BallotCandidate.ballot_order)
                )
            )
        }
        row_of = {int(u): i for i, u in enumerate(b.unit_index)}
        uidx = {int(uid): i for i, uid in enumerate(f.unit_ids.tolist())}
        for x in adj:
            r = row_of[uidx[x.geo_unit_id]]
            if x.pile == "invalid":
                assert x.ballot_candidate_id is None
                inv[r] += x.delta
            else:
                expect[r, lines[x.ballot_candidate_id]] += x.delta
        assert np.array_equal(delta, expect), race.code
        assert np.array_equal(a.invalid - b.invalid, inv)
        assert race.decided_by in ("recount", "lot")
        changed_races += bool(adj)
        assert rc.margin_before_votes == int(
            np.sort(b.votes.sum(axis=0))[-1] - np.sort(b.votes.sum(axis=0))[-2]
        )
    assert changed_races > 0
    # the national race follows its recounted province contests
    pres = after["PRES"]
    assert pres.votes.sum() == sum(after[c].votes.sum() for c in inputs.races_of(RaceType.PRESIDENT_PROVINCE))
    assert reconcile_election(s, world.second) == []
    assert validate_system(s).ok


def test_contingent_election(world) -> None:  # type: ignore[no-untyped-def]
    """Proportional EV allocation among six tickets leaves everyone short of 88 → contingent."""
    s = world.session
    data = load_scenario("demo-2028").model_dump(mode="json")
    data["scenario"]["slug"] = "contingent-2028"
    data["electoral_college"]["allocation"] = "proportional"
    doc = ScenarioDocument.model_validate(data)
    el = create_election(s, doc, strict=False)
    instant_finalize(s, el.id)
    pres = s.scalar(select(Race).where(Race.election_id == el.id, Race.code == "PRES"))
    ce = s.scalar(select(ContingentElection).where(ContingentElection.race_id == pres.id))
    assert ce is not None and ce.rounds >= 1
    audit = json.loads(ce.ballots_json)
    assert audit["mode"] == "province_delegations" and len(audit["finalists"]) == 3
    assert len(json.loads(ce.finalists_json)) == 3
    assert pres.decided_by == "contingent"
    ev = s.scalar(
        select(func.sum(ElectoralVoteAllocation.electoral_votes)).where(
            ElectoralVoteAllocation.race_id == pres.id
        )
    )
    assert ev == ELECTORAL_VOTES
    # the House that decides is the one elected with it: 150 delegation members in 12 provinces
    first = audit["rounds"][0]
    assert sum(sum(v.values()) for v in first["delegation_breakdown"].values()) == HOUSE_SEATS
    holders = s.execute(
        select(Office.code, OfficeHolder.candidate_id, OfficeHolder.start_reason)
        .join(Office, Office.id == OfficeHolder.office_id)
        .where(Office.code.in_(["PRES", "VP"]), OfficeHolder.election_race_id == pres.id)
    ).all()
    codes = {c for c, _, _ in holders}
    assert "VP" in codes and "PRES" in codes
    if ce.outcome != "vp_acts":
        winner = s.get(BallotCandidate, ce.winner_ballot_candidate_id)
        assert pres.winner_ballot_candidate_id == winner.id
        assert any(c == "PRES" and cid == winner.candidate_id for c, cid, _ in holders)
    assert ce.vp_winner_candidate_id in {cid for c, cid, _ in holders if c == "VP"}
    scen = s.scalar(select(Scenario).where(Scenario.slug == "contingent-2028"))
    assert scen is not None and s.get(Election, el.id).scenario_id == scen.id


def test_race_calls_are_stored(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    inputs = election_inputs(s, world.second)
    lines = inputs.races["PRES-NB"].line_keys
    records = [
        CallRecord("PRES-NB", 10, 600.0, RaceStatus.TOO_EARLY, None, None, 5.0, None, {"n": 1}),
        CallRecord("PRES-NB", 40, 3600.0, RaceStatus.PROJECTED, lines[0], 0.995, 55.0, 3.2, {"n": 2}),
        CallRecord(
            "HOUSE-NB-01",
            20,
            1800.0,
            RaceStatus.CALLED,
            inputs.races["HOUSE-NB-01"].line_keys[0],
            0.999,
            40.0,
            8.0,
            {},
        ),
    ]
    finalize_election(s, world.second, call_records=records)
    calls = s.scalars(
        select(RaceCall).where(RaceCall.election_id == world.second).order_by(RaceCall.seq)
    ).all()
    assert [c.seq for c in calls] == [10, 20, 40]
    nb = [c for c in calls if c.race_id == inputs.race_ids["PRES-NB"]]
    assert [c.superseded for c in nb] == [True, False]
    assert nb[1].ballot_candidate_id == inputs.line_ids["PRES-NB"][lines[0]]
    assert json.loads(nb[1].evidence_json) == {"n": 2}
    race = s.get(Race, inputs.race_ids["PRES-NB"])
    assert race.called_at == nb[1].called_at
    assert s.get(Race, inputs.race_ids["PRES-GR"]).called_at is None


def test_finalize_requires_simulation(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    data = load_scenario("demo-2028").model_dump(mode="json")
    data["scenario"]["slug"] = "unsimulated-2028"
    el = create_election(s, ScenarioDocument.model_validate(data), strict=False)
    with pytest.raises(ElectionError):
        finalize_election(s, el.id)
    assert el.status == "scheduled"


def test_strict_scenario_validation(world) -> None:  # type: ignore[no-untyped-def]
    from app.core.errors import ScenarioError

    with pytest.raises(ScenarioError):
        create_election(world.session, "demo-2028")  # real municipality codes are not in the toy country


def test_no_election_outside_the_calendar(world) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ElectionError):
        create_election(world.session, "demo-2028", year=2029, strict=False)


# =========================================================================== midterm / municipal
def test_midterm_with_municipal_elections(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    el = create_election(s, "midterm-2026", strict=False)
    counts = race_counts(s, el.id)
    n_munis = s.scalar(select(func.count()).select_from(Legislature).where(Legislature.level == "municipal"))
    assert counts == {"HOUSE": 150, "SENATE": 8, "MAYOR": n_munis, "MUNICIPAL_COUNCIL": n_munis}
    assert el.election_type == "midterm" and el.previous_election_id == world.founding
    classes = {
        c
        for (c,) in s.execute(
            select(SenateSeat.senate_class)
            .join(Race, Race.senate_seat_id == SenateSeat.id)
            .where(Race.election_id == el.id)
        )
    }
    assert classes == {1}
    # created after the 2028 election: the party keeps its current (2028) identity, while the
    # 2026 ballots show the name used in 2026 and no spurious rename is recorded
    plb = s.scalar(select(Party).where(Party.code == "PLB"))
    assert plb.abbreviation == load_scenario("demo-2028").party("PLB").abbreviation
    names = {
        n
        for (n,) in s.execute(
            select(BallotCandidate.party_name_snapshot)
            .join(Race, _J)
            .where(Race.election_id == el.id, BallotCandidate.party_code_snapshot == "PLB")
        )
    }
    assert names == {load_scenario("midterm-2026").party("PLB").name}
    assert (
        s.scalar(select(func.count()).select_from(PartyEvent).where(PartyEvent.event_type == "renamed")) == 1
    )
    council = s.scalar(select(Race).where(Race.election_id == el.id, Race.race_type == "MUNICIPAL_COUNCIL"))
    lines = s.scalars(select(BallotCandidate).where(BallotCandidate.race_id == council.id)).all()
    assert len(lines) >= 2 and all(
        b.candidate_id is None and b.line_key == b.party_code_snapshot for b in lines
    )
    assert council.electoral_system == "proportional_dhondt" and council.seats >= 9
    inputs = election_inputs(s, el.id)
    founding_winner = s.get(
        BallotCandidate,
        s.scalar(
            select(Race.winner_ballot_candidate_id).where(
                Race.election_id == world.founding, Race.code == "PRES"
            )
        ),
    )
    assert inputs.context.president_party == founding_winner.party_code_snapshot  # midterm penalty target
    simulate_election(s, el.id)
    finalize_election(s, el.id)
    # councils: D'Hondt seats equal the statutory size
    bad = s.execute(
        select(Legislature.jurisdiction_code, Legislature.seats, func.sum(LegislatureSeatResult.seats))
        .join(LegislatureSeatResult, LegislatureSeatResult.legislature_id == Legislature.id)
        .join(Race, Race.id == LegislatureSeatResult.race_id)
        .where(Race.election_id == el.id)
        .group_by(Legislature.id)
        .having(func.sum(LegislatureSeatResult.seats) != Legislature.seats)
    ).all()
    assert bad == []
    mayors = s.scalar(
        select(func.count())
        .select_from(OfficeHolder)
        .join(Office, Office.id == OfficeHolder.office_id)
        .where(Office.office_type == "MAYOR", OfficeHolder.term_start == date(2027, 1, 1))
    )
    assert mayors == n_munis
    assert validate_election(s, el.id).ok


# =========================================================================== determinism
def test_same_seed_same_stored_results(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.orm import sessionmaker

    from app.models import Base
    from app.services.bootstrap import setup_synthetic_system

    from .conftest import sqlite_engine

    snapshots = []
    for i in range(2):
        engine = sqlite_engine(tmp_path / f"det{i}.db")
        Base.metadata.create_all(engine)
        s = sessionmaker(bind=engine, expire_on_commit=False)()
        setup_synthetic_system(s, seed=5)
        el = create_election(s, "founding-2024", strict=False)
        simulate_election(s, el.id)
        cands = sorted(k for (k,) in s.execute(select(Candidate.key)))
        env = s.get(Election, el.id).national_environment_json
        snapshots.append((unit_votes(s, el.id), cands, env, results_frame(s, include_hidden=True)))
        s.close()
        engine.dispose()
    (v1, c1, e1, f1), (v2, c2, e2, f2) = snapshots
    assert v1 == v2 and c1 == c2 and e1 == e2
    assert f1.equals(f2)


def test_special_senate_election(world) -> None:  # type: ignore[no-untyped-def]
    """A seat outside the class up is filled by a special election for the rest of its term."""
    s = world.session
    seat = s.scalars(select(SenateSeat).where(SenateSeat.senate_class == 3).order_by(SenateSeat.code)).first()
    data = load_scenario("demo-2028").model_dump(mode="json")
    data["scenario"]["slug"] = "special-2028"
    data["senate"]["special_elections"] = [seat.code]
    el = create_election(s, ScenarioDocument.model_validate(data), strict=False)
    race = s.scalar(select(Race).where(Race.election_id == el.id, Race.code == seat.code))
    assert race is not None and race.is_special and "special" in race.name
    assert race_counts(s, el.id)["SENATE"] == 9
    instant_finalize(s, el.id)
    rows = s.scalars(
        select(OfficeHolder).where(OfficeHolder.office_id == seat.office_id).order_by(OfficeHolder.term_start)
    ).all()
    assert rows[-1].term_start == date(2029, 1, 15) and rows[-1].term_end == date(2031, 1, 15)
    assert rows[-2].ended_on == date(2029, 1, 15)


def test_scenario_moved_to_another_year(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    el = create_election(s, "demo-2028", year=2032, strict=False)
    assert el.year == 2032 and el.election_date == date(2032, 11, 3)  # calendar: Wed after 1st Mon
    assert el.name == "General Election 2032"
    classes = {
        c
        for (c,) in s.execute(
            select(SenateSeat.senate_class)
            .join(Race, Race.senate_seat_id == SenateSeat.id)
            .where(Race.election_id == el.id)
        )
    }
    assert classes == {1}
    first_poll = s.scalar(select(func.min(Poll.start_date)).where(Poll.election_id == el.id))
    assert first_poll >= date(2032, 1, 1)
    scen = s.get(Scenario, el.scenario_id)
    assert (
        scen.slug.startswith("demo-2028--") and scen.parent_id is not None
    )  # a variant of the stored document
