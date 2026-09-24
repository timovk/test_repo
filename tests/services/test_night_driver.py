"""Election-night service, integration properties added by the wave-2 review: the background
driver (a night advances and is certified without requests), read budgets, no rewinds on stale
clocks, the certified final state of reported elections (stored and served without rebuilding
the engine) and exact replays of finished nights whose races were recounted."""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.constitution import ElectionStatus, RaceType
from app.core.errors import ElectionError
from app.elections.recount import RecountThreshold, load_recount_config
from app.models import (
    BallotCandidate,
    Election,
    ElectoralVoteAllocation,
    Race,
    RaceCall,
    RecountAdjustment,
    SimulationRun,
)
from app.reporting.config import load_night_config
from app.services import night as night_service
from app.services.elections import finalize_election
from app.services.night import RUN_NIGHT, NightManager, call_log, run_instant_night
from app.services.runtime import election_inputs, int_rows, load_final_race_votes, map_ids


def _engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


@dataclass
class DB:
    path: Path
    engine: Engine
    election: int  # the SIMULATED election of the sandbox
    founding: int  # the FINAL founding election (finalized without a night)

    @contextmanager
    def scope(self) -> Iterator[Session]:
        s = sessionmaker(bind=self.engine, future=True, expire_on_commit=False)()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def manager(self, **kwargs: Any) -> NightManager:
        return NightManager(self.scope, **kwargs)

    def calls(self) -> list[tuple[Any, ...]]:
        with self.scope() as s:
            return [
                (c["race_key"], c["seq"], c["status"], c["key"], c["is_manual"], c["leader_margin_pct"])
                for c in call_log(s, self.election)
            ]


def _copy(sandbox_file: Any, dst: Path) -> DB:
    shutil.copyfile(sandbox_file.path, dst)
    sb = sandbox_file.sandbox
    return DB(dst, _engine(dst), sb.second_election_id, sb.founding_election_id)


@pytest.fixture()
def db(sandbox_file, tmp_path: Path) -> Iterator[DB]:  # type: ignore[no-untyped-def]
    d = _copy(sandbox_file, tmp_path / "driver.db")
    yield d
    d.engine.dispose()


def _history(m: NightManager, eid: int) -> list[tuple[Any, ...]]:
    return [
        (r.race_key, r.seq, str(getattr(r.status, "value", r.status)), r.key, r.is_manual, r.margin_pct)
        for r in m._nights[eid].engine.call_history
    ]


# =========================================================================== background driver
def test_background_driver_plays_and_certifies_the_night(
    db: DB,
    sandbox_file,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    base = load_night_config()
    fast = base.model_copy(update={"playback": base.playback.model_copy(update={"base_rate": 50_000.0})})
    m = db.manager(config=fast, background=True, read_budget_s=0.002, slice_s=0.01)
    eid = db.election
    try:
        m.control(eid, "start", speed=25)
        seqs: list[int] = []
        lagged = False
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            st = m.state(eid)
            seqs.append(st["clock"]["seq"])
            lagged |= st["clock"]["lag_events"] > 0
            if st["election_status"] == ElectionStatus.FINAL.value:
                break
            time.sleep(0.05)
        assert st["election_status"] == ElectionStatus.FINAL.value, "the driver did not finish the night"
        assert seqs == sorted(seqs), "the night went backwards"
        assert lagged, "the whole night was due at once: the reads must have lagged behind the clock"
        # the driver stops once no night is running
        stop = time.monotonic() + 10.0
        while m._driver is not None and time.monotonic() < stop:
            time.sleep(0.02)
        assert m._driver is None
        driven = db.calls()
    finally:
        m.close()
    # same stored calls as the same night applied at once (content depends on seq only)
    other = _copy(sandbox_file, tmp_path / "instant.db")
    try:
        with other.scope() as s:
            run_instant_night(s, other.election)
        assert other.calls() == driven
    finally:
        other.engine.dispose()


def test_read_budget_lags_and_controls_catch_up(db: DB) -> None:
    eid = db.election
    m = db.manager(read_budget_s=0.0)
    m.control(eid, "start", speed=25, now=0.0)
    st = m.state(eid, now=2.0)
    assert st["clock"]["seq"] == 0 and st["clock"]["lag_events"] > 0
    assert st["clock"]["next_event_in_s"] == 0.0
    st = m.control(eid, "pause", now=2.0)
    assert st["clock"]["seq"] > 0 and st["clock"]["lag_events"] == 0
    reference = db.manager()
    reference.control(eid, "reset", now=0.0)
    reference.control(eid, "start", speed=25, now=0.0)
    assert reference.control(eid, "pause", now=2.0)["clock"]["seq"] == st["clock"]["seq"]


def test_a_stale_clock_never_rewinds_the_night(db: DB) -> None:
    eid = db.election
    m = db.manager()
    m.control(eid, "start", speed=25, now=0.0)
    ahead = m.state(eid, now=4.0)
    n_calls = len(m._nights[eid].engine.call_history)
    # a request that read its clock before another one advanced the night
    behind = m.state(eid, now=1.0)
    assert behind["clock"]["seq"] == ahead["clock"]["seq"]
    assert behind["clock"]["sim_time_s"] == ahead["clock"]["sim_time_s"]
    assert len(m._nights[eid].engine.call_history) == n_calls
    later = m.state(eid, now=5.0)
    assert later["clock"]["seq"] >= ahead["clock"]["seq"]


# =========================================================================== reported elections
def _certified_ev(s: Session, eid: int) -> dict[str, int]:
    rows = s.execute(
        select(BallotCandidate.line_key, func.sum(ElectoralVoteAllocation.electoral_votes))
        .join(BallotCandidate, BallotCandidate.id == ElectoralVoteAllocation.ballot_candidate_id)
        .join(Race, Race.id == ElectoralVoteAllocation.race_id)
        .where(Race.election_id == eid)
        .group_by(BallotCandidate.line_key)
    ).all()
    return {k: int(v) for k, v in rows}


def _seats(s: Session, eid: int, race_type: RaceType) -> dict[str, int]:
    rows = s.execute(
        select(BallotCandidate.party_code_snapshot, func.count())
        .join(Race, Race.winner_ballot_candidate_id == BallotCandidate.id)
        .where(Race.election_id == eid, Race.race_type == race_type.value)
        .group_by(BallotCandidate.party_code_snapshot)
    ).all()
    return {p or "IND": int(n) for p, n in rows}


def test_final_state_is_certified_and_served_without_the_engine(
    db: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    eid = db.election
    m = db.manager()
    m.instant_finish(eid)
    st = m.state(eid, now=0.0)
    snap = st["snapshot"]
    assert snap["certified"]["final"] is True
    with db.scope() as s:
        ev = _certified_ev(s, eid)
        house = _seats(s, eid, RaceType.HOUSE)
        senate = _seats(s, eid, RaceType.SENATE)
        winners = dict(
            s.execute(
                select(Race.code, BallotCandidate.line_key)
                .join(BallotCandidate, BallotCandidate.id == Race.winner_ballot_candidate_id)
                .where(Race.election_id == eid)
            ).all()
        )
        n_runs = s.scalar(
            select(func.count())
            .select_from(SimulationRun)
            .where(SimulationRun.election_id == eid, SimulationRun.kind == RUN_NIGHT)
        )
    assert n_runs == 1
    pres = snap["president"]
    assert sum(ev.values()) == pres["ev_total"] == 174
    assert {t["key"]: t["ev_decided"] for t in pres["tickets"] if t["ev_decided"]} == {
        k: v for k, v in ev.items() if v
    }
    assert pres["ev_decided_total"] == 174 and pres["ev_uncalled"] == 0
    assert pres["winner"] == winners["PRES"] and pres["status"] == "FINAL"
    assert snap["house"]["called"] == 150 and snap["house"]["uncalled"] == 0
    assert {r["party"]: r["called"] for r in snap["house"]["by_party"] if r["called"]} == house
    sen = snap["senate"]
    assert sen["uncalled"] == 0 and sen["called"] == sen["up"]
    assert {r["party"]: r["called"] for r in sen["by_party"] if r["called"]} == senate
    assert sum(r["total_decided"] for r in sen["by_party"]) == 24
    assert {r["status"] for r in snap["races"]} == {"FINAL"}
    assert all(r["called_key"] == winners[r["key"]] for r in snap["races"])
    assert {p["status"] for p in snap["provinces"]} == {"FINAL"}

    # a new process serves the stored state without rebuilding the night
    def no_engine(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the engine must not be rebuilt for a stored final state")

    monkeypatch.setattr(night_service, "_build_night", no_engine)
    again = db.manager().state(eid)
    assert again["snapshot"] == snap and again["election_status"] == ElectionStatus.FINAL.value


def test_a_reported_election_without_a_night_is_replayed_once_and_stored(db: DB) -> None:
    eid = db.founding  # finalized without an election night
    st = db.manager().state(eid, now=0.0)
    assert st["election_status"] == ElectionStatus.FINAL.value and st["snapshot"]["certified"]["final"]
    with db.scope() as s:
        run = s.scalar(
            select(SimulationRun).where(SimulationRun.election_id == eid, SimulationRun.kind == RUN_NIGHT)
        )
        assert run is not None and '"source":"replay"' in (run.summary_json or "")
    assert db.manager().state(eid)["snapshot"] == st["snapshot"]


def test_replayed_final_night_matches_its_stored_calls_after_recounts(
    db: DB, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = load_recount_config()
    everything = base.model_copy(
        update={
            "thresholds": {rt: RecountThreshold(margin_pct=100.0, exact_tie=True) for rt in base.thresholds}
        }
    )
    eid = db.election
    db.manager(recount_config=everything).instant_finish(eid)
    stored = db.calls()
    with db.scope() as s:
        adjustments = s.scalar(select(func.count()).select_from(RecountAdjustment))
    assert adjustments and adjustments > 0
    replay = db.manager(recount_config=everything)
    replay.race_detail(eid, "PRES-NB")  # rebuilds the finished night from the database
    assert _history(replay, eid) == stored
    # replaying the recounted rows instead of the election-night count would differ
    monkeypatch.setattr(night_service, "_night_count", lambda s, e, i, votes: dict(votes))
    naive = db.manager(recount_config=everything)
    naive.race_detail(eid, "PRES-NB")
    assert _history(naive, eid) != stored


def test_certification_keeps_when_live_calls_were_recorded(db: DB) -> None:
    eid = db.election
    m = db.manager()
    m.control(eid, "start", speed=25, now=0.0)
    m.control(eid, "pause", now=3.0)
    with db.scope() as s:
        before = {
            (c.race_id, c.seq, c.status): c.recorded_at
            for c in s.scalars(select(RaceCall).where(RaceCall.election_id == eid))
        }
    assert before
    time.sleep(0.05)
    m.control(eid, "finish", now=4.0)
    with db.scope() as s:
        after = {
            (c.race_id, c.seq, c.status): c.recorded_at
            for c in s.scalars(select(RaceCall).where(RaceCall.election_id == eid))
        }
        assert s.get(Election, eid).status == ElectionStatus.FINAL.value
    assert all(after[k] == v for k, v in before.items())
    assert max(after.values()) > max(before.values())


# =========================================================================== chronological order
def test_elections_that_can_never_be_certified_are_refused(db: DB) -> None:
    from app.core.errors import ElectionNightError
    from app.services.elections import create_election, instant_finalize

    with db.scope() as s:
        with pytest.raises(ElectionError):
            create_election(s, "founding-2024", strict=False)  # 2024 is already final
        later = create_election(s, "demo-2028", year=2032, strict=False)
        instant_finalize(s, later.id)
    # the SIMULATED 2028 election can no longer be certified: its night must not start
    m = db.manager()
    with pytest.raises(ElectionNightError):
        m.control(db.election, "start", speed=25, now=0.0)
    with db.scope() as s:
        assert s.get(Election, db.election).status == ElectionStatus.SIMULATED.value
        with pytest.raises(ElectionNightError):
            run_instant_night(s, db.election)


# =========================================================================== finalize inputs
def test_finalize_rejects_votes_of_another_election(db: DB) -> None:
    with db.scope() as s:
        inputs = election_inputs(s, db.founding)
        votes = load_final_race_votes(s, db.founding, inputs, with_expectation=False)
        with pytest.raises(ElectionError):
            finalize_election(s, db.election, inputs=inputs, votes=votes)
        assert s.get(Election, db.election).status == ElectionStatus.SIMULATED.value


def test_row_helpers() -> None:
    rows = [(1, 2, 3), (4, 5, 6)]
    assert int_rows(rows, 3).tolist() == [[1, 2, 3], [4, 5, 6]]
    assert int_rows([], 2).shape == (0, 2)
    ids = np.array([7, 3, 7, 9], dtype=np.int64)
    assert map_ids(ids, {3: 0, 7: 1, 9: 2}).tolist() == [1, 0, 1, 2]
    with pytest.raises(ElectionError):
        map_ids(ids, {3: 0})
