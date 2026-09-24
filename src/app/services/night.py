"""Election-night service (SIMULATED): live, persisted and restartable election nights.

The pure :class:`~app.reporting.live.NightEngine` replays a stored election's reporting timeline
against its stored (hidden) final result; this service builds that engine from the database,
drives it with a :class:`~app.reporting.clock.PlaybackClock` and persists everything the night
publishes:

* every :class:`~app.reporting.live.CallRecord` → one ``race_call`` row (``seq``, ``sim_time_s``,
  ``called_at`` = simulated local time, reporting %, counted leader margin, win probability,
  evidence JSON, ``is_manual`` / ``override_reason``, ``superseded`` for replaced states);
  ``race.status`` / ``race.called_at`` follow the latest published state;
* the playback state → ``night_session`` (status, ``current_seq``, ``sim_time_s``, speed);
* the election's status: ``simulated`` → ``live`` once the night starts → ``final`` when the last
  reporting event has been applied (:func:`app.services.elections.finalize_election` is called
  exactly once, with the night's call records).

**Determinism and restarts.**  What is shown at a given ``seq`` is a pure function of the stored
election, the night configuration, the election seed and the manual overrides; playback speed
and wall-clock time only decide *when* it appears.  After a process restart the engine is rebuilt
from the database, the persisted manual overrides are replayed (``manual_calls_from_history``)
and the night is fast-forwarded to the persisted ``current_seq`` — the rebuilt state equals the
original one.

**Hidden-until-reported rule.**  Nothing here reveals uncounted votes: counted votes are
``floor(f × final)`` of the reported fraction ``f`` of every unit, outstanding ballots are
estimated from the pre-election *expectation*, and the previous-election comparisons use only
reported (FINAL) elections.

Two entry points:

* :class:`NightManager` (``get_night_manager()``) — the process-wide registry used by the API and
  the CLI: ``state`` (auto-advances to the playback clock), ``control`` (start / pause / resume /
  speed / step / finish / reset), ``race_detail``, ``municipality_rows``, ``municipality_detail``,
  ``override_call``, ``is_live``, ``instant_finish``.  Each call runs in its own transaction.
* :func:`run_instant_night` — apply every event at once in the caller's session (the demo builder
  uses it for history elections).

History elections (FINAL / CERTIFIED) are immutable: their night can be displayed (finished), but
not reset, replayed or overridden.  Everything returned is SIMULATED data about FICTIONAL races.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_constitution
from app.core.constitution import DECIDED_STATUSES, ElectionStatus, RaceStatus
from app.core.errors import ElectionError, ElectionNightError, NotFoundError
from app.core.logging import Timer, get_logger, log_ctx
from app.elections.recount import RecountConfig, load_recount_config, needs_recount
from app.elections.tabulation import tabulate_totals
from app.elections.types import RaceVotes
from app.models import (
    BallotCandidate,
    Election,
    ElectionResult,
    NightSession,
    Race,
    RaceCall,
    utcnow,
)
from app.reporting.calling import RecountCheck, RecountInput, allocate_counted
from app.reporting.clock import PlaybackClock, PlaybackState
from app.reporting.config import NightConfig, load_night_config
from app.reporting.live import (
    MANUAL_STATUSES,
    CallRecord,
    ManualCall,
    NightEngine,
    manual_calls_from_history,
)
from app.services._common import REPORTED_STATUSES, bulk_insert, dumps, loads
from app.services.elections import finalize_election
from app.services.runtime import (
    ElectionInputs,
    election_inputs,
    get_election,
    load_final_race_votes,
    load_timeline,
    local_time,
    timeline_meta,
)

log = get_logger(__name__)

__all__ = [
    "ACTIONS",
    "CLEAR_STATUSES",
    "NightManager",
    "get_night_manager",
    "make_recount_check",
    "reset_night_manager",
    "run_instant_night",
]

#: Control actions accepted by :meth:`NightManager.control`.
ACTIONS: tuple[str, ...] = ("start", "pause", "resume", "speed", "step", "finish", "reset")
#: ``status`` values of :meth:`NightManager.override_call` that clear a manual override.
CLEAR_STATUSES: frozenset[str] = frozenset({"CLEAR", "MODEL", "AUTO", "NONE"})
#: Key of the national presidency (derived from the Electoral College by the engine).
PRESIDENT_KEY = "PRES"
_IND = "IND"
_SQL_CHUNK = 400
#: Substring of ``race_call.evidence_json`` marking a record produced by clearing an override
#: (``dumps`` writes sorted keys without spaces).
_CLEARED_MARK = '%"manual_cleared":true%'

SessionFactory = Callable[[], AbstractContextManager[Session]]


# =========================================================================== recount rule
def make_recount_check(config: RecountConfig | None = None) -> RecountCheck:
    """The night engine's automatic-recount rule: :func:`app.elections.recount.needs_recount`
    (``config/recount.yaml``) applied to the final tally of a race at 100 % reporting."""
    cfg = config or load_recount_config()

    def check(inp: RecountInput) -> tuple[bool, str]:
        if inp.race_type is None:
            return False, "race type unknown"
        tab = tabulate_totals(inp.race_key, inp.line_keys, inp.totals, resolve_ties=False)
        return needs_recount(tab, inp.race_type, cfg)

    return check


# =========================================================================== night state
@dataclass
class _Night:
    """One election night held in memory (engine + playback clock + persistence bookkeeping)."""

    election_id: int
    year: int
    name: str
    engine: NightEngine
    clock: PlaybackClock
    inputs: ElectionInputs
    votes: dict[str, RaceVotes]
    meta: dict[str, Any]
    final: bool
    #: Number of ``engine.call_history`` records already stored as ``race_call`` rows.
    persisted: int = 0
    #: Race code → (decided line key, simulated local time of that call) for ``race.called_at``.
    called: dict[str, tuple[str | None, datetime | None]] = field(default_factory=dict)
    #: (election status, night-session status, current seq) as last written or read.
    marker: tuple[Any, ...] = ()
    has_session: bool = False
    #: (race code, seq) → cached municipality rows; race code → previous-election results.
    muni_rows: dict[str, tuple[int, list[dict[str, Any]]]] = field(default_factory=dict)
    previous: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)


def _now_default() -> float:
    return time.monotonic()


def _session_row(session: Session, election_id: int) -> NightSession | None:
    return session.scalar(select(NightSession).where(NightSession.election_id == election_id))


def _marker(election: Election, ns: NightSession | None) -> tuple[Any, ...]:
    return (
        election.status,
        None if ns is None else ns.status,
        None if ns is None else int(ns.current_seq),
    )


def _stored_manual_calls(session: Session, election_id: int, inputs: ElectionInputs) -> list[ManualCall]:
    """Manual overrides (and clears) of a night rebuilt from its ``race_call`` rows."""
    code_of = {rid: code for code, rid in inputs.race_ids.items()}
    key_of = {bid: key for ids in inputs.line_ids.values() for key, bid in ids.items()}
    rows = session.execute(
        select(
            RaceCall.race_id,
            RaceCall.seq,
            RaceCall.sim_time_s,
            RaceCall.status,
            RaceCall.ballot_candidate_id,
            RaceCall.is_manual,
            RaceCall.override_reason,
            RaceCall.evidence_json,
        )
        .where(
            RaceCall.election_id == election_id,
            or_(RaceCall.is_manual.is_(True), RaceCall.evidence_json.like(_CLEARED_MARK)),
        )
        .order_by(RaceCall.seq, RaceCall.id)
    ).all()
    records = [
        {
            "race_key": code_of[int(rid)],
            "seq": int(seq),
            "sim_time_s": float(t),
            "status": status,
            "key": key_of.get(int(bid)) if bid is not None else None,
            "is_manual": bool(manual),
            "override_reason": reason,
            "evidence": loads(evidence),
        }
        for rid, seq, t, status, bid, manual, reason, evidence in rows
        if int(rid) in code_of
    ]
    return manual_calls_from_history(records)


def _restore_clock(night: _Night, ns: NightSession, now: float) -> None:
    """Playback clock of a persisted night: same status and speed, anchored at the persisted
    simulated time (wall-clock time restarts at ``now``)."""
    clock = night.clock
    speed = float(ns.speed or 1.0)
    if speed in clock.speeds:
        clock.speed = speed
    state = (
        PlaybackState(ns.status) if ns.status in {s.value for s in PlaybackState} else PlaybackState.PAUSED
    )
    if state == PlaybackState.FINISHED:
        clock.finish()
        return
    clock.state = state
    clock.anchor_sim = float(night.engine.sim_time_s)
    clock.anchor_wall = float(now)


def _build_night(
    session: Session,
    election: Election,
    ns: NightSession | None,
    *,
    now: float,
    config: NightConfig | None = None,
    recount_config: RecountConfig | None = None,
) -> _Night:
    """Build the engine and clock of an election night from the database and bring them to the
    persisted state (FINAL elections: the finished night, read-only)."""
    eid = election.id
    if election.status == ElectionStatus.SCHEDULED.value:
        raise ElectionError(f"election {eid} has not been simulated yet (simulate it first)")
    final = election.status in REPORTED_STATUSES
    with Timer(log, f"build election night {eid}"):
        inputs = election_inputs(session, eid)
        votes = load_final_race_votes(session, eid, inputs)
        timeline = load_timeline(session, eid, inputs.frame)
        meta = timeline_meta(session, eid)
        cfg = config or load_night_config()
        manual: list[ManualCall] = []
        if final or ns is not None:
            manual = _stored_manual_calls(session, eid, inputs)
        engine = NightEngine(
            inputs.frame,
            timeline,
            votes,
            inputs.race_meta,
            cfg,
            seed=int(election.seed),
            holdover_senate=inputs.holdover_senate,
            recount_check=make_recount_check(recount_config),
            manual_calls=manual,
        )
        clock = PlaybackClock.from_timeline(timeline, cfg)
        night = _Night(
            election_id=eid,
            year=int(election.year),
            name=election.name,
            engine=engine,
            clock=clock,
            inputs=inputs,
            votes=votes,
            meta=meta,
            final=final,
            has_session=ns is not None,
        )
        if final:
            engine.finish()
            clock.finish()
            night.persisted = len(engine.call_history)
        elif ns is not None:
            engine.advance_to_time(float(ns.sim_time_s))
            if engine.seq != int(ns.current_seq):
                log.warning(
                    "night %d: persisted sim time %.1f s does not match seq %d; using the seq",
                    eid,
                    float(ns.sim_time_s),
                    int(ns.current_seq),
                )
                engine.advance_to_seq(int(ns.current_seq))
            _restore_clock(night, ns, now)
    night.marker = _marker(election, ns)
    return night


# =========================================================================== persistence
def _call_row(night: _Night, rec: CallRecord) -> dict[str, Any]:
    inputs = night.inputs
    rid = inputs.race_ids.get(rec.race_key)
    if rid is None:
        raise ElectionError(f"call record for unknown race {rec.race_key}")
    return {
        "race_id": rid,
        "election_id": night.election_id,
        "status": RaceStatus(rec.status).value,
        "ballot_candidate_id": inputs.line_ids[rec.race_key].get(rec.key) if rec.key else None,
        "seq": int(rec.seq),
        "sim_time_s": float(rec.sim_time_s),
        "called_at": local_time(night.meta, rec.sim_time_s),
        "recorded_at": utcnow(),
        "reporting_pct": float(rec.reporting_pct),
        "leader_margin_pct": None if rec.margin_pct is None else float(rec.margin_pct),
        "win_probability": None if rec.win_probability is None else float(rec.win_probability),
        "evidence_json": dumps(rec.evidence),
        "is_manual": bool(rec.is_manual),
        "override_reason": rec.override_reason,
        "superseded": False,
    }


def _track_called(night: _Night, rec: CallRecord) -> None:
    """``race.called_at``: when the current decided winner was first called (None when undecided)."""
    status = RaceStatus(rec.status)
    if status in DECIDED_STATUSES and not rec.retracted:
        prev = night.called.get(rec.race_key)
        if prev is None or prev[0] != rec.key or prev[1] is None:
            night.called[rec.race_key] = (rec.key, local_time(night.meta, rec.sim_time_s))
    else:
        night.called[rec.race_key] = (None, None)


def _write_calls(session: Session, night: _Night, records: Sequence[CallRecord]) -> None:
    """Insert ``records`` as ``race_call`` rows (earlier rows of the same races superseded) and
    bring ``race.status`` / ``race.called_at`` up to date."""
    if not records:
        return
    rows: list[dict[str, Any]] = []
    last: dict[str, int] = {}
    status: dict[str, str] = {}
    for rec in records:
        row = _call_row(night, rec)
        if rec.race_key in last:
            rows[last[rec.race_key]]["superseded"] = True
        last[rec.race_key] = len(rows)
        rows.append(row)
        status[rec.race_key] = row["status"]
        _track_called(night, rec)
    ids = [night.inputs.race_ids[k] for k in last]
    for i in range(0, len(ids), _SQL_CHUNK):
        session.execute(
            update(RaceCall)
            .where(
                RaceCall.election_id == night.election_id,
                RaceCall.race_id.in_(ids[i : i + _SQL_CHUNK]),
                RaceCall.superseded.is_(False),
            )
            .values(superseded=True)
            .execution_options(synchronize_session=False)
        )
    bulk_insert(session, RaceCall, rows)
    race_rows = [
        {
            "id": night.inputs.race_ids[code],
            "status": status[code],
            "called_at": night.called.get(code, (None, None))[1],
        }
        for code in last
    ]
    session.execute(update(Race), race_rows)


def _rewrite_calls(session: Session, night: _Night) -> None:
    """Replace every ``race_call`` row of the night by the engine's complete history."""
    session.execute(delete(RaceCall).where(RaceCall.election_id == night.election_id))
    night.called = {}
    history = night.engine.call_history
    _write_calls(session, night, history)
    night.persisted = len(history)


def _upsert_session_row(session: Session, night: _Night, election: Election) -> NightSession:
    ns = _session_row(session, night.election_id)
    if ns is None:
        ns = NightSession(election_id=night.election_id, seed=int(election.seed))
        session.add(ns)
    clock, engine = night.clock, night.engine
    ns.status = clock.state.value
    ns.current_seq = int(engine.seq)
    ns.sim_time_s = float(engine.sim_time_s)
    ns.speed = float(clock.speed)
    ns.seed = int(election.seed)
    ns.total_events = int(engine.total_events)
    ns.updated_at = utcnow()
    if clock.state != PlaybackState.READY and ns.started_at is None:
        ns.started_at = utcnow()
    if clock.state == PlaybackState.FINISHED and ns.finished_at is None:
        ns.finished_at = utcnow()
    night.has_session = True
    return ns


def _persist(session: Session, night: _Night) -> None:
    """Store the night's new call records and playback state (the election becomes LIVE once
    the night has started)."""
    election = get_election(session, night.election_id)
    history = night.engine.call_history
    if len(history) > night.persisted:
        _write_calls(session, night, history[night.persisted :])
        night.persisted = len(history)
    ns = _upsert_session_row(session, night, election)
    if night.clock.state != PlaybackState.READY and election.status == ElectionStatus.SIMULATED.value:
        election.status = ElectionStatus.LIVE.value
    session.flush()
    night.marker = _marker(election, ns)


def _finalize(session: Session, night: _Night) -> None:
    """The last event has been applied: certify the election with the night's calls (once)."""
    election = get_election(session, night.election_id)
    if election.status in REPORTED_STATUSES:
        night.final = True
        return
    night.clock.finish()
    history = night.engine.call_history
    with Timer(log, f"finalize election {night.election_id} after its night"):
        finalize_election(session, night.election_id, call_records=history)
    night.persisted = len(history)
    night.called = {}
    for rec in history:
        _track_called(night, rec)
    night.final = True
    ns = _upsert_session_row(session, night, election)
    session.flush()
    night.marker = _marker(election, ns)
    snap = night.engine.snapshot("summary")
    pres = snap.get("president") or {}
    log.info(
        "election night finished",
        extra=log_ctx(
            election_id=night.election_id,
            events=night.engine.total_events,
            calls=len(history),
            president=pres.get("winner"),
        ),
    )


def _advance(session: Session, night: _Night, now: float, *, force: bool = False) -> None:
    """Advance the engine to the playback clock at ``now``; persist what changed; finalize the
    election when the last reporting event has been applied."""
    if night.final:
        return
    engine, clock = night.engine, night.clock
    before = (engine.seq, clock.state, clock.speed)
    clock.tick(now)
    if clock.state == PlaybackState.FINISHED:
        engine.finish()
    elif clock.state != PlaybackState.READY:
        engine.advance_to_time(clock.target_sim_time(now))
    if engine.is_finished:
        # finalize_election stores the complete call log itself (it replaces the rows)
        _finalize(session, night)
        return
    changed = (engine.seq, clock.state, clock.speed) != before or len(engine.call_history) > night.persisted
    if force or (changed and (clock.state != PlaybackState.READY or night.has_session)):
        _persist(session, night)


# =========================================================================== read models
def _clock_dict(night: _Night, now: float) -> dict[str, Any]:
    engine, clock = night.engine, night.clock
    tl = engine.timeline
    return {
        "status": clock.state.value,
        "speed": clock.speed,
        "speeds": list(clock.speeds),
        "seq": int(engine.seq),
        "total_events": int(engine.total_events),
        "sim_time_s": float(engine.sim_time_s),
        "clock": tl.local_clock(engine.sim_time_s),
        "polls_close_local": tl.local_clock(0.0),
        "base_rate": clock.base_rate,
        "end_sim_time_s": clock.end_sim,
        "end_clock": tl.local_clock(clock.end_sim),
        "next_event_in_s": None if night.final else clock.seconds_until_next_event(now),
    }


def _labels() -> dict[str, str]:
    cons = get_constitution()
    return {
        "president": f"{cons.presidential_majority} TO WIN",
        "house": f"{cons.house_majority} FOR CONTROL",
        "senate": f"{cons.senate_majority} FOR CONTROL",
    }


def _state_dict(night: _Night, election_status: str, detail: str, now: float) -> dict[str, Any]:
    return {
        "election_id": night.election_id,
        "year": night.year,
        "name": night.name,
        "election_status": election_status,
        "clock": _clock_dict(night, now),
        "labels": _labels(),
        "snapshot": night.engine.snapshot(detail),
        "data_category": "SIMULATED",
    }


def _previous_results(session: Session, night: _Night, race_code: str) -> dict[int, dict[str, Any]]:
    """Municipality results of the previous comparable race (``race.previous_race_id``) when its
    election has been reported, keyed by frame municipality index."""
    cached = night.previous.get(race_code)
    if cached is not None:
        return cached
    out: dict[int, dict[str, Any]] = {}
    rid = night.inputs.race_ids[race_code]
    prev_id = session.scalar(select(Race.previous_race_id).where(Race.id == rid))
    prev = session.get(Race, prev_id) if prev_id is not None else None
    prev_el = session.get(Election, prev.election_id) if prev is not None else None
    if prev is not None and prev_el is not None and prev_el.status in REPORTED_STATUSES:
        rows = session.execute(
            select(
                ElectionResult.municipality_id,
                BallotCandidate.party_code_snapshot,
                func.sum(ElectionResult.votes),
            )
            .join(BallotCandidate, BallotCandidate.id == ElectionResult.ballot_candidate_id)
            .where(ElectionResult.race_id == prev.id, ElectionResult.level == "municipality")
            .group_by(ElectionResult.municipality_id, BallotCandidate.party_code_snapshot)
        ).all()
        frame = night.inputs.frame
        index = (
            {int(i): m for m, i in enumerate(frame.muni_ids.tolist())} if frame.muni_ids is not None else {}
        )
        per: dict[int, dict[str, int]] = {}
        for mid, party, votes in rows:
            m = index.get(int(mid)) if mid is not None else None
            if m is None:
                continue
            key = party or _IND
            tally = per.setdefault(m, {})
            tally[key] = tally.get(key, 0) + int(votes or 0)
        for m, votes in per.items():
            valid = sum(votes.values())
            if valid <= 0:
                continue
            ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
            second = ranked[1][1] if len(ranked) > 1 else 0
            out[m] = {
                "election_id": prev_el.id,
                "year": int(prev_el.year),
                "race_code": prev.code,
                "winner_party": ranked[0][0],
                "margin_pct": round(100.0 * (ranked[0][1] - second) / valid, 4),
                "shares": {p: v / valid for p, v in votes.items()},
            }
    night.previous[race_code] = out
    return out


def _municipality_rows(session: Session, night: _Night, race_code: str) -> list[dict[str, Any]]:
    """Live per-municipality rows of one race (counted votes only; see the module docstring)."""
    rv = night.votes.get(race_code)
    if rv is None:
        raise NotFoundError(f"race {race_code} is not part of election {night.election_id}")
    engine = night.engine
    cached = night.muni_rows.get(race_code)
    if cached is not None and cached[0] == engine.seq:
        return [dict(r) for r in cached[1]]
    frame = night.inputs.frame
    meta = night.inputs.race_meta[race_code]
    keys = list(rv.line_keys)
    L = len(keys)
    units = np.asarray(rv.unit_index, dtype=np.int64)
    f = engine.reported_fraction[units]
    counted = allocate_counted(rv.votes, f)
    exp_t = (
        np.asarray(rv.expected_turnout, dtype=np.float64)
        if rv.expected_turnout is not None
        else np.full(len(units), np.nan)
    )
    # the engine's expected ballots: expected turnout (flat prior when unknown) × eligible voters
    exp_t = np.where(np.isfinite(exp_t), np.clip(exp_t, 0.0, 1.0), 0.78)
    x = exp_t * np.asarray(rv.eligible, dtype=np.float64)
    muni = frame.unit_muni[units]
    M = frame.n_munis
    flat = (muni[:, None] * L + np.arange(L)).ravel()
    cv = np.bincount(flat, weights=counted.ravel(), minlength=M * L).reshape(M, L).astype(np.int64)
    xs = np.bincount(muni, weights=x, minlength=M)
    fx = np.bincount(muni, weights=f * x, minlength=M)
    n_units = np.bincount(muni, minlength=M)
    n_done = np.bincount(muni, weights=(f >= 1.0).astype(np.float64), minlength=M)
    n_partial = np.bincount(muni, weights=((f > 0) & (f < 1.0)).astype(np.float64), minlength=M)
    prev = _previous_results(session, night, race_code)
    parties = [meta.line_parties.get(k) or _IND for k in keys]
    rows: list[dict[str, Any]] = []
    for m in np.flatnonzero(n_units > 0).tolist():
        votes = cv[m]
        valid = int(votes.sum())
        order = np.argsort(-votes, kind="stable")
        leader = keys[int(order[0])] if valid > 0 else None
        margin_votes = int(votes[order[0]] - (votes[order[1]] if L > 1 else 0)) if valid > 0 else 0
        party_share: dict[str, float] = {}
        for j, p in enumerate(parties):
            party_share[p] = party_share.get(p, 0.0) + (float(votes[j]) / valid if valid > 0 else 0.0)
        leader_party = parties[int(order[0])] if valid > 0 else None
        p_row = prev.get(m)
        swing = flipped = None
        if p_row is not None and leader_party is not None:
            swing = round(100.0 * (party_share[leader_party] - p_row["shares"].get(leader_party, 0.0)), 4)
            flipped = leader_party != p_row["winner_party"]
        rows.append(
            {
                "code": frame.muni_codes[m],
                "name": frame.muni_names[m],
                "province_code": frame.province_codes[int(frame.muni_province[m])],
                "race_code": race_code,
                "reporting_pct": round(100.0 * float(fx[m] / xs[m]), 3)
                if xs[m] > 0
                else (100.0 if n_done[m] >= n_units[m] else 0.0),
                "units_total": int(n_units[m]),
                "units_reported": int(n_done[m]),
                "units_partial": int(n_partial[m]),
                "counted_valid": valid,
                "votes": {k: int(v) for k, v in zip(keys, votes.tolist(), strict=True)},
                "shares": {
                    k: (round(float(v) / valid, 6) if valid > 0 else 0.0)
                    for k, v in zip(keys, votes.tolist(), strict=True)
                },
                "leader": leader,
                "leader_party": None if leader is None else meta.line_parties.get(leader),
                "leader_label": None if leader is None else meta.line_labels.get(leader, leader),
                "leader_color": None if leader is None else meta.line_colors.get(leader),
                "margin_votes": margin_votes,
                "margin_pct": round(100.0 * margin_votes / valid, 4) if valid > 0 else None,
                "expected_ballots": round(float(xs[m]), 1),
                "outstanding_ballots_est": round(float(xs[m] - fx[m]), 1),
                "previous_election_id": None if p_row is None else p_row["election_id"],
                "previous_winner_party": None if p_row is None else p_row["winner_party"],
                "previous_margin_pct": None if p_row is None else p_row["margin_pct"],
                "swing_pct": swing,
                "flipped": flipped,
                "data_category": "SIMULATED",
            }
        )
    night.muni_rows[race_code] = (engine.seq, rows)
    return [dict(r) for r in rows]


def _line_info(night: _Night, race_code: str) -> dict[str, dict[str, Any]]:
    meta = night.inputs.race_meta[race_code]
    return {
        k: {
            "label": meta.line_labels.get(k, k),
            "party": meta.line_parties.get(k),
            "color": meta.line_colors.get(k),
        }
        for k in night.inputs.races[race_code].line_keys
    }


def _parse_status(status: str | RaceStatus | None) -> RaceStatus | None:
    """A manual status (enum value or name, case-insensitive); None/CLEAR clears the override."""
    if status is None:
        return None
    if isinstance(status, RaceStatus):
        rs = status
    else:
        text = str(status).strip().upper()
        if text in CLEAR_STATUSES:
            return None
        try:
            rs = RaceStatus(text)
        except ValueError:
            try:
                rs = RaceStatus[text]
            except KeyError:
                allowed = sorted(s.value for s in MANUAL_STATUSES)
                raise ElectionNightError(
                    f"unknown race status {status!r} (allowed: {allowed} or CLEAR)"
                ) from None
    if rs not in MANUAL_STATUSES:
        allowed = sorted(s.value for s in MANUAL_STATUSES)
        raise ElectionNightError(
            f"{rs.value} cannot be set manually (allowed: {allowed}); FINAL/RECOUNT come from the count"
        )
    return rs


# =========================================================================== manager
class NightManager:
    """Process-wide registry of election nights (thread-safe, one lock per election).

    Every public method runs in its own transaction from ``session_factory`` (default:
    :func:`app.db.session.session_scope` of the configured database) and first brings the night
    up to the playback clock at ``now`` (``time.monotonic()`` when omitted).  Nights are rebuilt
    from the database when they are not in memory, or when another process changed them.

    Args:
        session_factory: context-manager factory yielding a transactional session.
        max_nights: number of nights kept in memory (least recently used ones are dropped; they
            are rebuilt from the database on demand).
        config: night configuration (default ``config/night.yaml``).
        recount_config: automatic-recount rule of the night (default ``config/recount.yaml``).
        clock: wall-clock source used when ``now`` is omitted.
    """

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        *,
        max_nights: int = 4,
        config: NightConfig | None = None,
        recount_config: RecountConfig | None = None,
        clock: Callable[[], float] = _now_default,
    ) -> None:
        self._factory = session_factory
        self._max = max(int(max_nights), 1)
        self._config = config
        self._recount_config = recount_config
        self._clock = clock
        self._lock = threading.RLock()
        self._locks: dict[int, threading.RLock] = {}
        self._nights: OrderedDict[int, _Night] = OrderedDict()

    # ------------------------------------------------------------------ plumbing
    def _session(self) -> AbstractContextManager[Session]:
        if self._factory is not None:
            return self._factory()
        from app.db.session import session_scope

        return session_scope()

    def _election_lock(self, election_id: int) -> threading.RLock:
        with self._lock:
            lock = self._locks.get(election_id)
            if lock is None:
                lock = self._locks[election_id] = threading.RLock()
            return lock

    def _time(self, now: float | None) -> float:
        return float(self._clock() if now is None else now)

    def _evict(self, election_id: int) -> None:
        with self._lock:
            self._nights.pop(election_id, None)

    def _remember(self, night: _Night) -> None:
        with self._lock:
            self._nights[night.election_id] = night
            self._nights.move_to_end(night.election_id)
            while len(self._nights) > self._max:
                old, _ = self._nights.popitem(last=False)
                log.info("election night %d dropped from memory (rebuilt on demand)", old)

    def _night(self, session: Session, election_id: int, now: float) -> tuple[_Night, Election]:
        """The in-memory night, rebuilt when missing or changed in the database."""
        election = get_election(session, election_id)
        ns = _session_row(session, election_id)
        with self._lock:
            night = self._nights.get(election_id)
        if night is not None and night.marker == _marker(election, ns):
            with self._lock:
                self._nights.move_to_end(election_id)
            return night, election
        if night is not None:
            log.info("election night %d changed in the database; rebuilding", election_id)
        if ns is None and election.status not in REPORTED_STATUSES:
            stale = session.scalar(
                select(func.count()).select_from(RaceCall).where(RaceCall.election_id == election_id)
            )
            if stale:
                log.warning("election %d: %d race calls without a night session removed", election_id, stale)
                session.execute(delete(RaceCall).where(RaceCall.election_id == election_id))
        night = _build_night(
            session, election, ns, now=now, config=self._config, recount_config=self._recount_config
        )
        if ns is not None and not night.final:
            stored = session.scalar(
                select(func.count()).select_from(RaceCall).where(RaceCall.election_id == election_id)
            )
            history = night.engine.call_history
            if int(stored or 0) != len(history):
                log.warning(
                    "night %d: %d stored race calls, %d replayed; rewriting the call log",
                    election_id,
                    int(stored or 0),
                    len(history),
                )
                _rewrite_calls(session, night)
            else:
                night.persisted = len(history)
                for rec in history:
                    _track_called(night, rec)
        self._remember(night)
        return night, election

    def _run(self, election_id: int, now: float | None, fn: Callable[[Session, _Night, float], Any]) -> Any:
        """Run ``fn`` on the up-to-date night inside a transaction (the night is dropped from
        memory when anything fails, so the next call starts again from the database)."""
        t = self._time(now)
        with self._election_lock(int(election_id)):
            try:
                with self._session() as session:
                    night, _ = self._night(session, int(election_id), t)
                    _advance(session, night, t)
                    return fn(session, night, t)
            except Exception:
                self._evict(int(election_id))
                raise

    # ------------------------------------------------------------------ public API
    def state(self, election_id: int, detail: str = "summary", now: float | None = None) -> dict[str, Any]:
        """The live state of an election night after advancing it to the clock at ``now``:
        ``{"election_id", "clock": {status, speed, speeds, seq, total_events, sim_time_s, clock,
        polls_close_local, base_rate, …}, "snapshot": NightEngine.snapshot(detail), "labels",
        "election_status", "data_category": "SIMULATED"}``."""
        if detail not in ("summary", "full"):
            raise ElectionNightError("detail must be 'summary' or 'full'")

        def read(session: Session, night: _Night, t: float) -> dict[str, Any]:
            return _state_dict(night, get_election(session, night.election_id).status, detail, t)

        return self._run(election_id, now, read)

    def control(
        self, election_id: int, action: str, speed: float | None = None, now: float | None = None
    ) -> dict[str, Any]:
        """Apply a playback action and return :meth:`state`.

        * ``start`` / ``resume`` — run the night (READY or paused → running; the election becomes
          LIVE); on a running night they only apply ``speed``.
        * ``pause`` — freeze at the simulated time reached at ``now`` (no-op when paused; refused
          before the night has started).
        * ``speed`` — change the speed (one of the configured speeds) without a time jump.
        * ``step`` — reveal the next reporting event(s) and pause there.
        * ``finish`` — apply every remaining event; the election is finalized (FINAL).  A
          finished night accepts ``finish`` again as a no-op; every other action is refused.
        * ``reset`` — back to polls closing: the night's race calls and session are deleted
          (refused for FINAL elections — history is immutable).

        Invalid actions, speeds and transitions raise :class:`ElectionNightError`.
        """
        act = str(action).strip().lower()
        if act not in ACTIONS:
            raise ElectionNightError(f"unknown action {action!r}; expected one of {list(ACTIONS)}")
        t = self._time(now)
        eid = int(election_id)
        with self._election_lock(eid):
            if act == "reset":
                self._reset(eid, t)
                return self.state(eid, now=t)
            try:
                with self._session() as session:
                    night, _ = self._night(session, eid, t)
                    _advance(session, night, t)
                    if night.final:
                        if act != "finish":
                            raise ElectionNightError(
                                f"election {eid} is final: its election night is over (history is immutable)"
                            )
                    else:
                        self._apply(night, act, speed, t)
                        _advance(session, night, t, force=True)
            except Exception:
                self._evict(eid)
                raise
            return self.state(eid, now=t)

    @staticmethod
    def _apply(night: _Night, action: str, speed: float | None, now: float) -> None:
        clock = night.clock
        if action in ("start", "resume"):
            # idempotent: starting or resuming a running night only applies the speed
            if clock.state == PlaybackState.RUNNING:
                if speed is not None:
                    clock.set_speed(now, float(speed))
            else:
                clock.start(now, None if speed is None else float(speed))
        elif action == "pause":
            if clock.state == PlaybackState.READY:
                raise ElectionNightError("the election night has not started")
            if clock.state == PlaybackState.RUNNING:
                clock.pause(now)
        elif action == "speed":
            if speed is None:
                raise ElectionNightError("the 'speed' action needs a speed")
            clock.set_speed(now, float(speed))
        elif action == "step":
            clock.step(now)
        elif action == "finish":
            clock.finish()
        log.info(
            "election night control",
            extra=log_ctx(
                election_id=night.election_id, action=action, status=clock.state.value, speed=clock.speed
            ),
        )

    def _reset(self, election_id: int, now: float) -> None:
        try:
            with self._session() as session:
                election = get_election(session, election_id)
                if election.status in REPORTED_STATUSES:
                    raise ElectionNightError(
                        f"election {election_id} is {election.status}: its night cannot be reset "
                        "(history is immutable)"
                    )
                if election.status == ElectionStatus.SCHEDULED.value:
                    raise ElectionError(f"election {election_id} has not been simulated yet")
                session.execute(delete(RaceCall).where(RaceCall.election_id == election_id))
                session.execute(delete(NightSession).where(NightSession.election_id == election_id))
                session.execute(
                    update(Race)
                    .where(Race.election_id == election_id)
                    .values(status=RaceStatus.SCHEDULED.value, called_at=None)
                    .execution_options(synchronize_session=False)
                )
                election.status = ElectionStatus.SIMULATED.value
                session.flush()
        finally:
            self._evict(election_id)
        log.info("election night reset", extra=log_ctx(election_id=election_id))

    def race_detail(self, election_id: int, race_code: str, now: float | None = None) -> dict[str, Any]:
        """Live detail of one race: meta, current decision with evidence, call history with
        evidence, lead changes and per-municipality counted results (``PRES``: the Electoral
        College state and its history)."""

        def read(session: Session, night: _Night, t: float) -> dict[str, Any]:
            if race_code not in night.inputs.races:
                raise NotFoundError(f"race {race_code} is not part of election {night.election_id}")
            out = night.engine.race_detail(race_code)
            out["election_id"] = night.election_id
            out["seq"] = int(night.engine.seq)
            out["clock"] = night.engine.timeline.local_clock(night.engine.sim_time_s)
            return out

        return self._run(election_id, now, read)

    def municipality_rows(
        self, election_id: int, race_code: str = PRESIDENT_KEY, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Live map rows of one race, one per municipality in its jurisdiction: code, name,
        province, reporting %, units, counted votes and shares per line, leader (line, party,
        label, colour), counted margin, expected and outstanding (expected, not actual) ballots,
        and — when the previous comparable race has been reported — its winning party, margin,
        the swing of the current leader's party and whether the municipality flips."""
        return self._run(election_id, now, lambda s, night, t: _municipality_rows(s, night, race_code))

    def municipality_detail(self, election_id: int, code: str, now: float | None = None) -> dict[str, Any]:
        """Reporting events so far and the counted result of every race in one municipality."""

        def read(session: Session, night: _Night, t: float) -> dict[str, Any]:
            out = night.engine.municipality_detail(code)
            out["election_id"] = night.election_id
            out["seq"] = int(night.engine.seq)
            for race in out.get("races", []):
                race["lines"] = _line_info(night, race["key"])
            return out

        return self._run(election_id, now, read)

    def override_call(
        self,
        election_id: int,
        race_code: str,
        status: str,
        line_key: str | None,
        reason: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Manually override the published state of a race (``status`` one of the manual
        statuses — LEAN / PROJECTED_WINNER / CALLED need ``line_key`` — or ``CLEAR`` to hand the
        race back to the model).  Persisted as an ``is_manual`` race call and replayed on rebuild.
        Returns ``{"election_id", "race_key", "applied", "record", "state"}`` (``applied`` is
        False when nothing changed, e.g. the race is already FINAL)."""
        rs = _parse_status(status)
        text = (reason or "").strip()
        if not text:
            raise ElectionNightError("a manual call needs a reason")

        def apply(session: Session, night: _Night, t: float) -> dict[str, Any]:
            eid = night.election_id
            if night.final:
                raise ElectionNightError(f"election {eid} is final: race calls can no longer be changed")
            if night.clock.state == PlaybackState.READY:
                raise ElectionNightError("start the election night before overriding race calls")
            if race_code not in night.engine.race_keys:
                if race_code in night.engine.derived_races:
                    raise ElectionNightError(
                        f"{race_code} follows the Electoral College and cannot be called by hand"
                    )
                raise NotFoundError(f"race {race_code} is not part of election {eid}")
            rec = night.engine.apply_manual_call(race_code, rs, line_key, text)
            _advance(session, night, t, force=True)
            log.info(
                "manual race call",
                extra=log_ctx(
                    election_id=eid,
                    race=race_code,
                    status=None if rs is None else rs.value,
                    key=line_key,
                    applied=rec is not None,
                ),
            )
            detail = night.engine.race_detail(race_code)
            return {
                "election_id": eid,
                "race_key": race_code,
                "applied": rec is not None,
                "record": None if rec is None else rec.to_dict(),
                "state": detail["state"],
                "data_category": "SIMULATED",
            }

        return self._run(election_id, now, apply)

    def is_live(self, election_id: int) -> bool:
        """True while the election's night is in progress (election status LIVE)."""
        with self._session() as session:
            return get_election(session, int(election_id)).status == ElectionStatus.LIVE.value

    def instant_finish(self, election_id: int) -> None:
        """Apply every remaining event at once and finalize the election."""
        self.control(election_id, "finish")

    def forget(self, election_id: int | None = None) -> None:
        """Drop one (or every) night from memory; the database is untouched."""
        with self._lock:
            if election_id is None:
                self._nights.clear()
            else:
                self._nights.pop(int(election_id), None)

    @property
    def loaded(self) -> list[int]:
        """Election ids of the nights currently held in memory."""
        with self._lock:
            return list(self._nights)


_manager: NightManager | None = None
_manager_lock = threading.Lock()


def get_night_manager() -> NightManager:
    """The process-wide :class:`NightManager` (configured database)."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = NightManager()
        return _manager


def reset_night_manager() -> None:
    """Forget the process-wide manager and every night in memory (tests, simulated restarts)."""
    global _manager
    with _manager_lock:
        _manager = None


# =========================================================================== instant night
def run_instant_night(
    session: Session,
    election_id: int,
    *,
    config: NightConfig | None = None,
    recount_config: RecountConfig | None = None,
) -> dict[str, Any]:
    """Run an election night to the end at once in the caller's transaction: every reporting
    event is applied (continuing a persisted night and its manual overrides, if any), every call
    record is stored and the election is finalized.  Returns a summary of the night."""
    t0 = time.perf_counter()
    election = get_election(session, int(election_id))
    if election.status in REPORTED_STATUSES:
        raise ElectionError(f"election {election.id} is already {election.status}")
    ns = _session_row(session, election.id)
    night = _build_night(session, election, ns, now=0.0, config=config, recount_config=recount_config)
    with Timer(log, f"instant election night {election.id}"):
        night.engine.finish()
        night.clock.finish()
    snap = night.engine.snapshot("summary")
    history = night.engine.call_history
    finalize_election(session, election.id, call_records=history)
    night.final = True
    ns = _upsert_session_row(session, night, election)
    session.flush()
    president = snap.get("president") or {}
    house = snap.get("house") or {}
    senate = snap.get("senate") or {}
    return {
        "election_id": election.id,
        "year": int(election.year),
        "status": election.status,
        "events": int(night.engine.total_events),
        "calls": len(history),
        "races": len(night.engine.race_keys),
        "president": president.get("winner"),
        "electoral_votes": {t["key"]: t["ev_decided"] for t in president.get("tickets", [])},
        "house_control": house.get("control"),
        "senate_control": senate.get("control"),
        "end_clock": night.engine.timeline.local_clock(night.engine.sim_time_s),
        "seconds": round(time.perf_counter() - t0, 3),
        "data_category": "SIMULATED",
    }


def night_sessions(session: Session) -> Iterator[dict[str, Any]]:
    """Every stored night session (election id, status, seq, speed, timestamps)."""
    for ns in session.scalars(select(NightSession).order_by(NightSession.election_id)):
        yield {
            "election_id": ns.election_id,
            "status": ns.status,
            "current_seq": int(ns.current_seq),
            "total_events": int(ns.total_events),
            "sim_time_s": float(ns.sim_time_s),
            "speed": float(ns.speed),
            "started_at": None if ns.started_at is None else ns.started_at.isoformat(),
            "finished_at": None if ns.finished_at is None else ns.finished_at.isoformat(),
        }


def call_log(session: Session, election_id: int) -> list[dict[str, Any]]:
    """The stored race-call log of an election (chronological, without evidence)."""
    rows = session.execute(
        select(RaceCall, Race.code, BallotCandidate.line_key)
        .join(Race, Race.id == RaceCall.race_id)
        .outerjoin(BallotCandidate, BallotCandidate.id == RaceCall.ballot_candidate_id)
        .where(RaceCall.election_id == int(election_id))
        .order_by(RaceCall.seq, RaceCall.id)
    ).all()
    return [
        {
            "race_key": code,
            "seq": int(rc.seq),
            "sim_time_s": float(rc.sim_time_s),
            "called_at": rc.called_at.isoformat() if rc.called_at else None,
            "status": rc.status,
            "key": key,
            "reporting_pct": rc.reporting_pct,
            "leader_margin_pct": rc.leader_margin_pct,
            "win_probability": rc.win_probability,
            "is_manual": bool(rc.is_manual),
            "override_reason": rc.override_reason,
            "superseded": bool(rc.superseded),
        }
        for rc, code, key in rows
    ]
