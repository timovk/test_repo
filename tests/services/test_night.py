"""Election-night service on the synthetic sandbox world: playback transitions, persistence of
race calls with evidence, manual overrides and their replay after a restart, determinism by
``seq``, finalization exactly once, resets and the live map rows."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.constitution import ElectionStatus, RaceStatus, RaceType
from app.core.errors import ElectionError, ElectionNightError, NotFoundError
from app.models import Election, NightSession, Race, RaceCall
from app.reporting.calling import RecountInput
from app.services import night as night_service
from app.services.night import NightManager, make_recount_check, run_instant_night
from app.services.validation import validate_election

MID_T = 5.0  # wall seconds at 25× → 125 simulated minutes (≈ 23:05)


def _sqlite_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


@dataclass
class NightDB:
    """A copy of a sandbox database with a session factory for the night manager."""

    path: Path
    engine: Engine
    founding: int
    second: int

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

    def count(self, *where: Any) -> int:
        with self.scope() as s:
            return int(s.scalar(select(func.count()).select_from(RaceCall).where(*where)) or 0)


def _copy(src: Path, dst: Path, founding: int, second: int) -> NightDB:
    shutil.copyfile(src, dst)
    return NightDB(dst, _sqlite_engine(dst), founding, second)


@pytest.fixture()
def night_db(sandbox_file, tmp_path: Path) -> Iterator[NightDB]:  # type: ignore[no-untyped-def]
    sb = sandbox_file.sandbox
    db = _copy(sandbox_file.path, tmp_path / "night.db", sb.founding_election_id, sb.second_election_id)
    yield db
    db.engine.dispose()


@dataclass
class Midnight:
    """A night paused at ≈ 23:05 with two manual overrides (one cleared) — read-only."""

    db: NightDB
    manager: NightManager
    full: dict[str, Any]
    history: list[dict[str, Any]]
    house_race: str
    house_key: str
    pres_race: str


@pytest.fixture(scope="module")
def midnight(sandbox_file, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Midnight]:  # type: ignore[no-untyped-def]
    sb = sandbox_file.sandbox
    db = _copy(
        sandbox_file.path,
        tmp_path_factory.mktemp("night") / "midnight.db",
        sb.founding_election_id,
        sb.second_election_id,
    )
    m = db.manager()
    eid = db.second
    m.control(eid, "start", speed=25, now=0.0)
    m.state(eid, now=3.0)
    house = m.race_detail(eid, "HOUSE-NB-01", now=3.0)
    leader = house["leader"]
    other = next(ln["key"] for ln in house["lines"] if ln["key"] != leader)
    m.override_call(eid, "HOUSE-NB-01", "CALLED", other, "desk call against the count", now=3.0)
    pres = m.race_detail(eid, "PRES-GE", now=4.0)
    m.override_call(eid, "PRES-GE", "projected", pres["lines"][0]["key"], "decision desk", now=4.0)
    m.state(eid, now=4.5)
    m.override_call(eid, "PRES-GE", "CLEAR", None, "back to the model", now=4.5)
    m.control(eid, "pause", now=MID_T)
    full = m.state(eid, "full", now=100.0)
    history = [r.to_dict() for r in m._nights[eid].engine.call_history]
    yield Midnight(db, m, full, history, "HOUSE-NB-01", other, "PRES-GE")
    db.engine.dispose()


# =========================================================================== initial state
def test_initial_state_is_polls_closing(night_db: NightDB) -> None:
    m = night_db.manager()
    st = m.state(night_db.second, now=0.0)
    assert st["election_id"] == night_db.second and st["data_category"] == "SIMULATED"
    assert st["election_status"] == ElectionStatus.SIMULATED.value
    clock = st["clock"]
    assert clock["status"] == "ready" and clock["seq"] == 0 and clock["sim_time_s"] == 0.0
    assert clock["clock"] == clock["polls_close_local"] == "21:00"
    assert clock["total_events"] > 0 and clock["speeds"] == [1.0, 2.0, 5.0, 10.0, 25.0]
    assert clock["base_rate"] == 60.0
    snap = st["snapshot"]
    pres = snap["president"]
    assert (pres["ev_total"], pres["ev_needed"], pres["ev_decided_total"], pres["ev_uncalled"]) == (
        174,
        88,
        0,
        174,
    )
    assert all(t["ev_decided"] == 0 and t["ev_leading"] == 0 for t in pres["tickets"])
    assert st["labels"] == {"president": "88 TO WIN", "house": "76 FOR CONTROL", "senate": "13 FOR CONTROL"}
    assert snap["house"]["majority"] == 76 and snap["house"]["called"] == 0
    assert snap["senate"]["majority"] == 13
    assert snap["reporting"]["pct_expected_ballots"] == 0.0
    assert {r["status"] for r in snap["races"]} == {RaceStatus.POLLS_CLOSED.value}
    # a read before the night starts writes nothing
    assert night_db.count() == 0
    with night_db.scope() as s:
        assert s.scalar(select(func.count()).select_from(NightSession)) == 0
        assert s.get(Election, night_db.second).status == ElectionStatus.SIMULATED.value
    assert m.is_live(night_db.second) is False
    # re-simulating the (not yet started) election makes the night in memory stale
    from app.services.elections import simulate_election

    with night_db.scope() as s:
        simulate_election(s, night_db.second, seed=99)
    assert m.state(night_db.second, now=0.0)["snapshot"]["seed"] == 99


# =========================================================================== transitions
def test_playback_transitions(night_db: NightDB) -> None:
    eid = night_db.second
    m = night_db.manager()
    with pytest.raises(ElectionNightError):
        m.control(eid, "pause", now=0.0)  # not started
    with pytest.raises(ElectionNightError):
        m.control(eid, "rewind", now=0.0)
    st = m.control(eid, "start", speed=25, now=0.0)
    assert st["clock"]["status"] == "running" and st["clock"]["speed"] == 25.0
    assert st["election_status"] == ElectionStatus.LIVE.value and m.is_live(eid)
    st = m.state(eid, now=2.0)
    assert st["clock"]["sim_time_s"] == pytest.approx(2.0 * 25 * 60)
    seq_running = st["clock"]["seq"]
    assert seq_running > 0
    st = m.control(eid, "pause", now=2.0)
    assert st["clock"]["status"] == "paused"
    paused = m.state(eid, now=60.0)
    assert paused["clock"]["seq"] == seq_running and paused["clock"]["sim_time_s"] == pytest.approx(3000.0)
    assert m.control(eid, "pause", now=61.0)["clock"]["status"] == "paused"  # idempotent
    st = m.control(eid, "resume", now=100.0)
    assert st["clock"]["status"] == "running" and st["clock"]["sim_time_s"] == pytest.approx(3000.0)
    st = m.control(eid, "speed", speed=5, now=100.4)
    assert st["clock"]["speed"] == 5.0
    assert st["clock"]["sim_time_s"] == pytest.approx(3000.0 + 0.4 * 25 * 60)  # no jump
    st = m.state(eid, now=101.4)
    assert st["clock"]["sim_time_s"] == pytest.approx(3600.0 + 5 * 60)
    with pytest.raises(ElectionNightError):
        m.control(eid, "speed", speed=3, now=101.4)
    with pytest.raises(ElectionNightError):
        m.control(eid, "speed", now=101.4)
    before = st["clock"]["seq"]
    st = m.control(eid, "step", now=102.0)
    assert st["clock"]["status"] == "paused" and st["clock"]["seq"] > before
    stepped = st["clock"]["seq"]
    assert m.state(eid, now=500.0)["clock"]["seq"] == stepped
    with night_db.scope() as s:
        ns = s.scalar(select(NightSession).where(NightSession.election_id == eid))
        assert (ns.status, ns.current_seq, ns.speed) == ("paused", stepped, 5.0)
        assert ns.started_at is not None and ns.finished_at is None
    # another process (a second manager) sees the persisted night and follows its changes
    other = night_db.manager()
    assert other.state(eid, now=0.0)["clock"]["seq"] == stepped
    m.control(eid, "resume", now=600.0)
    assert other.state(eid, now=0.0)["clock"]["status"] == "running"


def test_scheduled_election_has_no_night(night_db: NightDB) -> None:
    from app.services.elections import create_election

    with night_db.scope() as s:
        el = create_election(s, "demo-2028", year=2032, strict=False)
        eid = el.id
    with pytest.raises(ElectionError):
        night_db.manager().state(eid, now=0.0)
    with pytest.raises(NotFoundError):
        night_db.manager().state(99999, now=0.0)
    with pytest.raises(ElectionNightError):
        night_db.manager().state(night_db.second, detail="everything", now=0.0)


# =========================================================================== determinism
def _call_rows(db: NightDB, election_id: int) -> list[tuple]:
    with db.scope() as s:
        return [
            tuple(r)
            for r in s.execute(
                select(
                    RaceCall.race_id,
                    RaceCall.seq,
                    RaceCall.sim_time_s,
                    RaceCall.status,
                    RaceCall.ballot_candidate_id,
                    RaceCall.win_probability,
                    RaceCall.reporting_pct,
                    RaceCall.leader_margin_pct,
                    RaceCall.called_at,
                    RaceCall.evidence_json,
                    RaceCall.superseded,
                )
                .where(RaceCall.election_id == election_id)
                .order_by(RaceCall.id)
            ).all()
        ]


def test_calls_depend_on_seq_not_on_speed(night_db: NightDB) -> None:
    eid = night_db.second
    target = 6000.0  # simulated seconds (22:40)
    m = night_db.manager()
    m.control(eid, "start", speed=25, now=0.0)
    m.state(eid, now=1.0)
    m.state(eid, now=2.5)
    a = m.control(eid, "pause", now=target / 1500.0)
    rows_a = _call_rows(night_db, eid)
    m.control(eid, "reset", now=10.0)
    assert night_db.count() == 0
    # other speeds, other wall-clock times, a speed change and a pause on the way
    m.control(eid, "start", speed=2, now=50.0)
    m.state(eid, now=60.0)
    m.control(eid, "speed", speed=10, now=70.0)  # 2400 simulated seconds
    m.control(eid, "pause", now=71.0)
    m.control(eid, "resume", now=80.0)
    b = m.control(eid, "pause", now=80.0 + (target - 3000.0) / 600.0)
    rows_b = _call_rows(night_db, eid)
    assert a["clock"]["seq"] == b["clock"]["seq"] > 0
    assert a["snapshot"] == b["snapshot"]
    assert rows_a == rows_b and len(rows_a) > 195


# =========================================================================== persistence
def test_calls_are_persisted_with_evidence(midnight: Midnight) -> None:
    db, eid = midnight.db, midnight.db.second
    with db.scope() as s:
        calls = s.scalars(select(RaceCall).where(RaceCall.election_id == eid).order_by(RaceCall.id)).all()
        assert len(calls) == len(midnight.history)
        races = {r.id: r for r in s.scalars(select(Race).where(Race.election_id == eid))}
        latest: dict[int, RaceCall] = {}
        for c in calls:
            latest[c.race_id] = c
            ev = json.loads(c.evidence_json)
            assert ev["data_category"] == "SIMULATED"
            assert c.called_at is not None and 0.0 <= c.reporting_pct <= 100.0
        assert set(latest) == set(races)  # every race has a published state (incl. PRES)
        for rid, c in latest.items():
            assert c.superseded is False
            assert races[rid].status == c.status
        assert sum(1 for c in calls if not c.superseded) == len(races)
        decided = [c for c in latest.values() if c.status in ("PROJECTED_WINNER", "CALLED")]
        assert decided and all(races[c.race_id].called_at is not None for c in decided)
        assert all(
            races[rid].called_at is None for rid, c in latest.items() if c.status == "TOO_EARLY_TO_CALL"
        )
        prob = [c for c in calls if c.status == "CALLED" and not c.is_manual]
        assert prob and all(c.win_probability is not None and c.ballot_candidate_id is not None for c in prob)
        ev = json.loads(prob[0].evidence_json)
        assert {"reporting", "counted", "projection", "win_probability", "thresholds"} <= set(ev)
        ns = s.scalar(select(NightSession).where(NightSession.election_id == eid))
        assert ns.status == "paused" and ns.current_seq == midnight.full["clock"]["seq"]
        assert ns.sim_time_s == pytest.approx(MID_T * 1500.0) and ns.speed == 25.0
        assert ns.total_events == midnight.full["clock"]["total_events"]
        assert s.get(Election, eid).status == ElectionStatus.LIVE.value


def test_manual_overrides_are_persisted(midnight: Midnight) -> None:
    db, eid = midnight.db, midnight.db.second
    with db.scope() as s:
        manual = s.execute(
            select(Race.code, RaceCall.status, RaceCall.override_reason, RaceCall.superseded)
            .join(Race, Race.id == RaceCall.race_id)
            .where(RaceCall.election_id == eid, RaceCall.is_manual.is_(True))
            .order_by(RaceCall.id)
        ).all()
        assert [(c, st, why) for c, st, why, _ in manual] == [
            ("HOUSE-NB-01", "CALLED", "desk call against the count"),
            ("PRES-GE", "PROJECTED_WINNER", "decision desk"),
        ]
        cleared = s.scalars(
            select(RaceCall.evidence_json)
            .join(Race, Race.id == RaceCall.race_id)
            .where(RaceCall.election_id == eid, Race.code == "PRES-GE", RaceCall.is_manual.is_(False))
            .order_by(RaceCall.id.desc())
        ).first()
        assert json.loads(cleared)["manual_cleared"] is True
    races = {r["key"]: r for r in midnight.full["snapshot"]["races"]}
    house = races[midnight.house_race]
    assert house["is_manual"] and house["status"] == "CALLED" and house["called_key"] == midnight.house_key
    assert races[midnight.pres_race]["is_manual"] is False


def test_restart_rebuilds_an_identical_night(midnight: Midnight, tmp_path: Path) -> None:
    db = _copy(midnight.db.path, tmp_path / "restart.db", midnight.db.founding, midnight.db.second)
    try:
        night_service.reset_night_manager()
        fresh = db.manager()
        rebuilt = fresh.state(db.second, "full", now=12345.0)
        assert rebuilt == midnight.full
        engine = fresh._nights[db.second].engine
        assert [r.to_dict() for r in engine.call_history] == midnight.history
        assert len(engine.manual_calls) == 3
        # the rebuilt night continues and keeps the call log in step with the engine
        fresh.control(db.second, "resume", now=0.0)
        later = fresh.control(db.second, "pause", now=1.0)
        assert later["clock"]["seq"] > midnight.full["clock"]["seq"]
        assert db.count() == len(fresh._nights[db.second].engine.call_history)
        house = {r["key"]: r for r in later["snapshot"]["races"]}[midnight.house_race]
        # the override stands until the race is fully counted (100 % replaces a manual state)
        assert house["is_manual"] or house["status"] in ("FINAL", "RECOUNT")
    finally:
        db.engine.dispose()


# =========================================================================== live read models
def test_municipality_rows_and_details(midnight: Midnight) -> None:
    m, eid = midnight.manager, midnight.db.second
    snap = midnight.full["snapshot"]
    rows = m.municipality_rows(eid, "PRES", now=100.0)
    assert len(rows) == snap["reporting"]["municipalities_total"]
    assert {r["race_code"] for r in rows} == {"PRES"} and all(r["data_category"] == "SIMULATED" for r in rows)
    totals: dict[str, int] = {}
    for r in rows:
        assert 0.0 <= r["reporting_pct"] <= 100.0
        assert r["counted_valid"] == sum(r["votes"].values())
        assert r["outstanding_ballots_est"] <= r["expected_ballots"] + 1e-6
        if r["units_reported"] == 0 and r["units_partial"] == 0:
            assert r["counted_valid"] == 0 and r["leader"] is None  # nothing counted, nothing shown
        if r["leader"] is not None:
            assert r["shares"][r["leader"]] == max(r["shares"].values())
            assert r["leader_party"] is not None and r["leader_color"]
        for k, v in r["votes"].items():
            totals[k] = totals.get(k, 0) + v
    counted = {ln["key"]: ln["votes"] for ln in snap["popular_vote"]["lines"]}
    assert totals == counted  # the map adds up to the national counted popular vote
    with_prev = [r for r in rows if r["previous_winner_party"] is not None]
    assert len(with_prev) == len(rows)  # the founding election (FINAL) is the previous race
    assert all(r["previous_election_id"] == midnight.db.founding for r in with_prev)
    assert any(r["swing_pct"] is not None for r in rows)
    assert all((r["flipped"] is None) == (r["leader"] is None) for r in rows)
    house = m.municipality_rows(eid, "HOUSE-NB-01", now=100.0)
    assert 1 <= len(house) < len(rows) and all(r["province_code"] == "NB" for r in house)
    with pytest.raises(NotFoundError):
        m.municipality_rows(eid, "MAYOR-GM9999", now=100.0)

    detail = m.race_detail(eid, "HOUSE-NB-01", now=100.0)
    assert detail["election_id"] == eid and detail["state"]["is_manual"]
    assert detail["history"] and all("evidence" in h for h in detail["history"])
    pres = m.race_detail(eid, "PRES", now=100.0)
    assert pres["president"]["ev_total"] == 174 and pres["history"]
    with pytest.raises(NotFoundError):
        m.race_detail(eid, "HOUSE-XX-99", now=100.0)

    code = max(rows, key=lambda r: r["reporting_pct"])["code"]
    md = m.municipality_detail(eid, code, now=100.0)
    assert md["code"] == code and md["events"] and md["races"]
    assert all(set(race["lines"]) == set(race["votes"]) for race in md["races"])


# =========================================================================== manual override rules
def test_override_rules(night_db: NightDB) -> None:
    eid = night_db.second
    m = night_db.manager()
    with pytest.raises(ElectionNightError, match="start"):
        m.override_call(eid, "HOUSE-NB-01", "TOO_CLOSE_TO_CALL", None, "early", now=0.0)
    m.control(eid, "start", speed=25, now=0.0)
    with pytest.raises(ElectionNightError):
        m.override_call(eid, "PRES", "CALLED", None, "derived from the Electoral College", now=0.0)
    with pytest.raises(NotFoundError):
        m.override_call(eid, "HOUSE-XX-99", "TOO_CLOSE_TO_CALL", None, "unknown", now=0.0)
    with pytest.raises(ElectionNightError):
        m.override_call(eid, "HOUSE-NB-01", "FINAL", None, "not by hand", now=0.0)
    with pytest.raises(ElectionNightError):
        m.override_call(eid, "HOUSE-NB-01", "WINNER", None, "unknown status", now=0.0)
    with pytest.raises(ElectionNightError):
        m.override_call(eid, "HOUSE-NB-01", "CALLED", None, "a call needs a line", now=0.0)
    with pytest.raises(ElectionNightError):
        m.override_call(eid, "HOUSE-NB-01", "TOO_CLOSE_TO_CALL", None, "   ", now=0.0)
    out = m.override_call(eid, "HOUSE-NB-01", "too_close", None, "desk hold", now=0.0)
    assert out["applied"] and out["record"]["is_manual"] and out["state"]["status"] == "TOO_CLOSE_TO_CALL"
    assert night_db.count(RaceCall.is_manual.is_(True)) == 1
    out = m.override_call(eid, "HOUSE-NB-01", "clear", None, "model again", now=0.0)
    assert out["applied"] and out["state"]["is_manual"] is False


# =========================================================================== finish / reset
def test_finish_finalizes_exactly_once(
    midnight: Midnight, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _copy(midnight.db.path, tmp_path / "finish.db", midnight.db.founding, midnight.db.second)
    calls: list[int] = []
    real = night_service.finalize_election

    def counting(session, election_id, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(election_id)
        return real(session, election_id, **kwargs)

    monkeypatch.setattr(night_service, "finalize_election", counting)
    eid = db.second
    try:
        m = db.manager()
        m.instant_finish(eid)
        assert calls == [eid]
        st = m.state(eid, now=0.0)
        assert st["clock"]["status"] == "finished" and st["election_status"] == ElectionStatus.FINAL.value
        assert st["clock"]["seq"] == st["clock"]["total_events"]
        pres = st["snapshot"]["president"]
        assert pres["status"] in ("FINAL", "TOO_CLOSE_TO_CALL")
        assert m.control(eid, "finish", now=1.0)["clock"]["status"] == "finished"
        for action in ("start", "pause", "resume", "step"):
            with pytest.raises(ElectionNightError):
                m.control(eid, action, now=2.0)
        with pytest.raises(ElectionNightError):
            m.control(eid, "reset", now=2.0)
        with pytest.raises(ElectionNightError):
            m.override_call(eid, "HOUSE-NB-01", "TOO_CLOSE_TO_CALL", None, "too late", now=2.0)
        assert calls == [eid] and m.is_live(eid) is False
        # a restarted process shows the finished night without finalizing again
        other = db.manager()
        assert other.state(eid, now=0.0)["clock"]["status"] == "finished"
        assert calls == [eid]
        engine = m._nights[eid].engine
        with db.scope() as s:
            assert s.get(Election, eid).status == ElectionStatus.FINAL.value
            ns = s.scalar(select(NightSession).where(NightSession.election_id == eid))
            assert ns.status == "finished" and ns.finished_at is not None
            assert ns.current_seq == engine.total_events
            n_rows = s.scalar(select(func.count()).select_from(RaceCall).where(RaceCall.election_id == eid))
            assert n_rows == len(engine.call_history)
            manual = s.scalar(
                select(func.count())
                .select_from(RaceCall)
                .where(RaceCall.election_id == eid, RaceCall.is_manual.is_(True))
            )
            assert manual == 2
            statuses = set(s.scalars(select(Race.status).where(Race.election_id == eid)))
            assert statuses == {"FINAL"}
            assert validate_election(s, eid).ok
    finally:
        db.engine.dispose()


def test_run_instant_night_continues_a_live_night(midnight: Midnight, tmp_path: Path) -> None:
    db = _copy(midnight.db.path, tmp_path / "instant.db", midnight.db.founding, midnight.db.second)
    eid = db.second
    try:
        with db.scope() as s:
            out = run_instant_night(s, eid)
        assert out["election_id"] == eid and out["status"] == ElectionStatus.FINAL.value
        assert out["events"] > 0 and out["calls"] > 195 and out["data_category"] == "SIMULATED"
        assert sum(out["electoral_votes"].values()) <= 174
        with db.scope() as s:
            assert s.get(Election, eid).status == ElectionStatus.FINAL.value
            ns = s.scalar(select(NightSession).where(NightSession.election_id == eid))
            assert ns.status == "finished" and ns.current_seq == out["events"]
            n_rows = s.scalar(select(func.count()).select_from(RaceCall).where(RaceCall.election_id == eid))
            assert n_rows == out["calls"]
            reasons = set(
                s.scalars(
                    select(RaceCall.override_reason).where(
                        RaceCall.election_id == eid, RaceCall.is_manual.is_(True)
                    )
                )
            )
            assert reasons == {"desk call against the count", "decision desk"}
            with pytest.raises(ElectionError):
                run_instant_night(s, eid)
    finally:
        db.engine.dispose()


def test_reset_rules(midnight: Midnight, tmp_path: Path) -> None:
    db = _copy(midnight.db.path, tmp_path / "reset.db", midnight.db.founding, midnight.db.second)
    eid = db.second
    try:
        m = db.manager()
        with pytest.raises(ElectionNightError):
            m.control(db.founding, "reset", now=0.0)  # history is immutable
        st = m.control(eid, "reset", now=0.0)
        assert st["clock"]["status"] == "ready" and st["clock"]["seq"] == 0
        assert st["election_status"] == ElectionStatus.SIMULATED.value
        assert st["snapshot"]["president"]["ev_decided_total"] == 0
        assert db.count() == 0
        with db.scope() as s:
            assert s.scalar(select(func.count()).select_from(NightSession)) == 0
            assert set(s.scalars(select(Race.status).where(Race.election_id == eid))) == {"SCHEDULED"}
            assert set(s.scalars(select(Race.called_at).where(Race.election_id == eid))) == {None}
            assert s.get(Election, eid).status == ElectionStatus.SIMULATED.value
        # the manual overrides are gone with the reset
        m.control(eid, "start", speed=25, now=0.0)
        st = m.control(eid, "pause", now=MID_T)
        races = {r["key"]: r for r in st["snapshot"]["races"]}
        assert races["HOUSE-NB-01"]["is_manual"] is False
    finally:
        db.engine.dispose()


# =========================================================================== recount rule
def test_recount_check_uses_the_recount_thresholds() -> None:
    check = make_recount_check()
    close = RecountInput(
        "PRES-NB",
        ["a", "b"],
        np.array([100_050, 99_950]),
        200_000,
        201_000,
        "a",
        "b",
        100,
        0.05,
        RaceType.PRESIDENT_PROVINCE.value,
    )
    need, reason = check(close)
    assert need is True and "threshold" in reason
    clear = RecountInput(
        "PRES-NB",
        ["a", "b"],
        np.array([110_000, 90_000]),
        200_000,
        201_000,
        "a",
        "b",
        20_000,
        10.0,
        RaceType.PRESIDENT_PROVINCE.value,
    )
    assert check(clear)[0] is False
    council = RecountInput(
        "COUNCIL-GM0001",
        ["a", "b"],
        np.array([500, 500]),
        1000,
        1000,
        "a",
        "b",
        0,
        0.0,
        RaceType.MUNICIPAL_COUNCIL.value,
    )
    # party lists have no automatic recount (exact ties are handled by the night engine itself)
    assert check(council)[0] is False
    governor = RecountInput(
        "GOV-NB",
        ["a", "b"],
        np.array([500_000, 500_000]),
        1_000_000,
        1_010_000,
        "a",
        "b",
        0,
        0.0,
        RaceType.GOVERNOR.value,
    )
    assert check(governor) == (True, "exact tie")
