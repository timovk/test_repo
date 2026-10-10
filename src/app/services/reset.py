"""Reset an election back to polls closing ("replay"), so its election night can be run again.

The simulated (hidden) result of an election is drawn once, when it is simulated; the election
night only *reveals* it, and certification (:func:`app.services.elections.finalize_election`)
adds the outcome on top: automatic recounts (audited corrections applied to the stored rows),
Electoral College allocations, a contingent election, race summaries, legislature seats, the
office terms it starts and the race calls.  Resetting removes exactly that layer, so the
election keeps its id and its hidden result, and a replay reveals the identical election:

* **Not reported yet** (``simulated`` / ``live``): the night's race calls and playback session
  are deleted and every race returns to SCHEDULED.
* **Reported** (``final`` / ``certified``): additionally the recount corrections are reversed
  from their audit rows (``recount_adjustment``), the recounts, Electoral College allocations,
  contingent election, legislature seats, race summaries and the stored night/certification runs
  are deleted, the office terms the election started are removed and the terms it ended are
  reopened.  Elections are certified in chronological order, so only the most recent reported
  election can be reset (a later reported election builds on its office holders).

Polls, campaigns, forecasts, the reporting timeline and the candidates are pre-election data and
are kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.constitution import ElectionStatus, RaceStatus, RaceType
from app.core.errors import ElectionError, NotFoundError
from app.core.logging import Timer, get_logger, log_ctx
from app.models import (
    ContingentElection,
    Election,
    ElectoralVoteAllocation,
    LegislatureSeatResult,
    NightSession,
    OfficeHolder,
    Race,
    RaceCall,
    Recount,
    RecountAdjustment,
    SimulationRun,
    Vacancy,
)

log = get_logger(__name__)

REPORTED = frozenset({ElectionStatus.FINAL.value, ElectionStatus.CERTIFIED.value})
#: ``simulation_run.kind`` rows written by certification and by a finished night.
CERTIFICATION_RUN_KINDS = ("election-final", "night")


@dataclass
class ResetResult:
    """Outcome of :func:`reset_election`."""

    election_id: int
    year: int
    previous_status: str
    mode: str  # "night" (unreported election) | "uncertified" (a reported election reopened)
    seconds: float = 0.0
    changed: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "election_id": self.election_id,
            "year": self.year,
            "previous_status": self.previous_status,
            "status": ElectionStatus.SIMULATED.value,
            "mode": self.mode,
            "seconds": round(self.seconds, 2),
            "changed": self.changed,
        }


def can_reset(session: Session, election_id: int) -> tuple[bool, str]:
    """Whether :func:`reset_election` accepts this election, with the reason (shown to users)."""
    el = session.get(Election, election_id)
    if el is None:
        return False, f"election {election_id} does not exist"
    if el.status == ElectionStatus.SCHEDULED.value:
        return False, "the election has not been simulated yet, so there is no election night to replay"
    if el.status not in REPORTED:
        return True, "the election night goes back to polls closing (same hidden result)"
    later = session.scalars(
        select(Election)
        .where(
            Election.id != el.id,
            Election.status.in_([*REPORTED, ElectionStatus.LIVE.value]),
            (Election.election_date > el.election_date)
            | ((Election.election_date == el.election_date) & (Election.id > el.id)),
        )
        .order_by(Election.election_date)
        .limit(1)
    ).first()
    if later is not None:
        what = (
            "has its election night under way"
            if later.status == ElectionStatus.LIVE.value
            else "is already reported"
        )
        return False, (
            f"the {later.year} election ({later.name}) {what} and builds on this one's office "
            "holders; only the most recent reported election can be reset"
        )
    return True, "the result is hidden again and the election night goes back to polls closing (same result)"


def reset_election(session: Session, election_id: int, *, night_manager: Any | None = None) -> ResetResult:
    """Reset ``election_id`` to polls closing (see the module docstring); raises
    :class:`ElectionError` when :func:`can_reset` refuses.  Flushes, does not commit.
    ``night_manager`` (optional) is told to drop the election's in-memory night."""
    with Timer(log, f"reset election {election_id}") as t:
        el = session.get(Election, election_id)
        if el is None:
            raise NotFoundError(f"election {election_id} not found")
        ok, reason = can_reset(session, election_id)
        if not ok:
            raise ElectionError(f"election {election_id} cannot be reset: {reason}")
        if night_manager is not None:
            night_manager.forget(election_id)
        previous = el.status
        changed: dict[str, int] = {}
        if previous in REPORTED:
            changed.update(_uncertify(session, el))
        changed.update(_reset_night(session, el))
        el.status = ElectionStatus.SIMULATED.value
        el.finalized_at = None
        session.flush()
        if night_manager is not None:
            night_manager.forget(election_id)
    result = ResetResult(
        election_id=el.id,
        year=el.year,
        previous_status=previous,
        mode="uncertified" if previous in REPORTED else "night",
        seconds=t.elapsed,
        changed={k: v for k, v in changed.items() if v},
    )
    log.info("election reset", extra=log_ctx(**result.to_dict()))
    return result


# ---------------------------------------------------------------------------------- the night
def _reset_night(session: Session, el: Election) -> dict[str, int]:
    calls = _run(session, delete(RaceCall).where(RaceCall.election_id == el.id))
    _run(session, delete(NightSession).where(NightSession.election_id == el.id))
    _run(
        session,
        update(Race)
        .where(Race.election_id == el.id)
        .values(status=RaceStatus.SCHEDULED.value, called_at=None),
    )
    return {"race_calls": calls}


# ---------------------------------------------------------------------------------- certification
def _uncertify(session: Session, el: Election) -> dict[str, int]:
    race_ids = select(Race.id).where(Race.election_id == el.id)
    out = {"recounts_reversed": _reverse_recounts(session, el)}
    recount_ids = select(Recount.id).where(Recount.race_id.in_(race_ids))
    _run(session, delete(RecountAdjustment).where(RecountAdjustment.recount_id.in_(recount_ids)))
    _run(session, delete(Recount).where(Recount.race_id.in_(race_ids)))
    out["electoral_vote_allocations"] = _run(
        session, delete(ElectoralVoteAllocation).where(ElectoralVoteAllocation.race_id.in_(race_ids))
    )
    out["contingent_elections"] = _run(
        session, delete(ContingentElection).where(ContingentElection.race_id.in_(race_ids))
    )
    out["legislature_seat_rows"] = _run(
        session, delete(LegislatureSeatResult).where(LegislatureSeatResult.race_id.in_(race_ids))
    )
    out["runs"] = _run(
        session,
        delete(SimulationRun).where(
            SimulationRun.election_id == el.id, SimulationRun.kind.in_(CERTIFICATION_RUN_KINDS)
        ),
    )
    out.update(_undo_office_terms(session, el))
    out.update(_undo_local_outcomes(session, el))
    _run(
        session,
        update(Race)
        .where(Race.election_id == el.id)
        .values(
            winner_ballot_candidate_id=None,
            winner_party_id=None,
            total_votes=None,
            margin_votes=None,
            margin_pct=None,
            turnout_pct=None,
            flipped=None,
            decided_by=None,
        ),
    )
    return out


def _undo_office_terms(session: Session, el: Election) -> dict[str, int]:
    """Remove the terms the election started; reopen the terms those replaced (a replaced term
    ended on the day the new one started, in the same office)."""
    race_ids = select(Race.id).where(Race.election_id == el.id)
    started = session.execute(
        select(OfficeHolder.id, OfficeHolder.office_id, OfficeHolder.term_start).where(
            OfficeHolder.election_race_id.in_(race_ids)
        )
    ).all()
    if not started:
        return {}
    ids = [int(i) for i, _, _ in started]
    reopened = 0
    for office_id, start in sorted({(int(o), s) for _, o, s in started}):
        reopened += _run(
            session,
            update(OfficeHolder)
            .where(
                OfficeHolder.office_id == office_id,
                OfficeHolder.ended_on == start,
                OfficeHolder.id.not_in(ids),
            )
            .values(ended_on=None, end_reason=None),
        )
    for i in range(0, len(ids), 500):
        part = ids[i : i + 500]
        _run(
            session,
            update(Vacancy).where(Vacancy.appointed_holder_id.in_(part)).values(appointed_holder_id=None),
        )
        _run(session, delete(OfficeHolder).where(OfficeHolder.id.in_(part)))
    return {"office_terms_removed": len(ids), "office_terms_reopened": reopened}


def _undo_local_outcomes(session: Session, el: Election) -> dict[str, int]:
    """Local elections: reopen the term ended at a vacancy (special elections) and drop the
    certification results stored in ``race.details_json`` (winners, passed, …)."""
    from app.services._common import dumps, loads
    from app.services._local_finalize import CERTIFICATION_KEYS

    reopened = 0
    for race in session.scalars(
        select(Race).where(Race.election_id == el.id, Race.details_json.is_not(None))
    ):
        d = loads(race.details_json) or {}
        ended = d.get("ended_holder_id")
        if ended is not None:
            h = session.get(OfficeHolder, int(ended))
            if h is not None and h.ended_on is not None:
                h.ended_on = None
                h.end_reason = None
                reopened += 1
        if any(k in d for k in CERTIFICATION_KEYS):
            race.details_json = dumps({k: v for k, v in d.items() if k not in CERTIFICATION_KEYS}) or None
    session.flush()
    return {"vacated_terms_reopened": reopened}


def _reverse_recounts(session: Session, el: Election) -> int:
    """Put the stored results back to the count before the automatic recounts, from the audit
    rows (votes and the invalid pile minus each adjustment's delta, ballots cast minus the net
    delta of the unit), and re-stitch the national presidential race when a province changed."""
    recounts = session.scalars(
        select(Recount)
        .join(Race, Race.id == Recount.race_id)
        .where(Race.election_id == el.id)
        .order_by(Recount.id)
    ).all()
    if not recounts:
        return 0
    from app.services._store import replace_race_results, stitch_parent
    from app.services.runtime import election_inputs, load_final_race_votes

    inputs = election_inputs(session, el.id)
    votes = load_final_race_votes(session, el.id, inputs, with_expectation=False)
    code_of = {int(rid): code for code, rid in inputs.race_ids.items()}
    unit_pos = {int(g): i for i, g in enumerate(np.asarray(inputs.frame.unit_ids).tolist())}
    children_changed = False
    for rc in recounts:
        code = code_of.get(int(rc.race_id))
        if code is None or code not in votes:
            raise ElectionError(f"election {el.id}: recount {rc.id} does not belong to a stored race")
        rv = votes[code]
        row_of = {int(u): r for r, u in enumerate(np.asarray(rv.unit_index).tolist())}
        col_of = {
            int(bid): rv.line_keys.index(k) for k, bid in inputs.line_ids[code].items() if k in rv.line_keys
        }
        v = np.array(rv.votes, dtype=np.int64, copy=True)
        cast = np.array(rv.ballots_cast, dtype=np.int64, copy=True)
        invalid = np.array(rv.invalid, dtype=np.int64, copy=True)
        for a in session.scalars(select(RecountAdjustment).where(RecountAdjustment.recount_id == rc.id)):
            r = row_of[unit_pos[int(a.geo_unit_id)]]
            d = int(a.delta)
            if a.pile == "invalid" or a.ballot_candidate_id is None:
                invalid[r] -= d
            else:
                v[r, col_of[int(a.ballot_candidate_id)]] -= d
            cast[r] -= d
        original = rv.like(
            unit_index=np.array(rv.unit_index, copy=True),
            votes=v,
            ballots_cast=cast,
            blank=np.array(rv.blank, copy=True),
            invalid=invalid,
            eligible=np.array(rv.eligible, copy=True),
        )
        original.check()
        replace_race_results(session, inputs, code, rv, original)
        votes[code] = original
        children_changed |= RaceType(inputs.races[code].race_type) == RaceType.PRESIDENT_PROVINCE
    if children_changed and "PRES" in inputs.races:
        children = [votes[c] for c in inputs.races_of(RaceType.PRESIDENT_PROVINCE)]
        parent = stitch_parent(inputs.races["PRES"], children)
        replace_race_results(session, inputs, "PRES", votes["PRES"], parent)
    return len(recounts)


def _run(session: Session, stmt: Any) -> int:
    return int(session.execute(stmt.execution_options(synchronize_session=False)).rowcount or 0)
