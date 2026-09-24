"""Internals of :func:`app.services.elections.finalize_election` (private).

Order of operations (all deterministic in the election seed):

1. tabulate every race (exact ties by seeded lot);
2. automatic recounts (``needs_recount`` → ``perform_recount``) — the audited corrections are
   applied to the stored unit rows, the aggregates are recomputed and replaced (never re-simulated);
   a recounted province contest also updates the national ``PRES`` race it is part of;
3. the Electoral College (``allocate``) and, without a majority, the contingent election with the
   NEW House (this election's winners) and the post-election Senate (holdovers + winners);
4. race summaries (winner, totals, margins, turnout, flips, ``decided_by``);
5. D'Hondt seats of party-list races; 6. office holders; 7. race calls (when given).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.constitution import (
    DECIDED_STATUSES,
    ContingentElectionMode,
    EVAllocationMethod,
    OfficeType,
    RaceStatus,
    RaceType,
)
from app.core.errors import ElectionError
from app.core.logging import get_logger
from app.core.rng import derive_seed
from app.elections.calendar import ElectionCalendar
from app.elections.contingent import ContingentResult, run_contingent_election
from app.elections.electoral_college import EVOutcome, allocate
from app.elections.recount import INVALID_PILE, RecountConfig, needs_recount, perform_recount
from app.elections.seats import dhondt
from app.elections.tabulation import TabulatedRaceResult, tabulate, tabulate_totals
from app.elections.types import RaceVotes
from app.models import (
    BallotCandidate,
    ContingentElection,
    Election,
    ElectoralVoteAllocation,
    Legislature,
    LegislatureSeatResult,
    Office,
    OfficeHolder,
    Race,
    RaceCall,
    Recount,
    RecountAdjustment,
    SenateSeat,
    utcnow,
)
from app.reporting.live import CallRecord
from app.services._common import (
    PRESIDENT_OFFICE,
    VICE_PRESIDENT_OFFICE,
    bulk_insert,
    dumps,
    lt_governor_office_code,
    seed62,
)
from app.services._create import holders_at
from app.services._store import replace_race_results, stitch_parent
from app.services.runtime import ElectionInputs, local_time, timeline_meta
from app.simulation.races import presidential_races
from app.simulation.voting import simulate_election as simulate_votes

log = get_logger(__name__)

DECIDED_EC = "electoral_college"
DECIDED_CONTINGENT = "contingent"
_PARTY_LIST = frozenset({RaceType.PROVINCIAL_LEGISLATURE, RaceType.MUNICIPAL_COUNCIL})
_OFFICE_TYPE = {
    RaceType.PRESIDENT: OfficeType.PRESIDENT,
    RaceType.HOUSE: OfficeType.HOUSE,
    RaceType.SENATE: OfficeType.SENATE,
    RaceType.GOVERNOR: OfficeType.GOVERNOR,
    RaceType.MAYOR: OfficeType.MAYOR,
}


@dataclass
class _Line:
    ballot_id: int
    candidate_id: int | None
    running_mate_id: int | None
    party_id: int | None


@dataclass
class FinalizeResult:
    """Summary of a finalization (stored in the ``election-final`` run)."""

    recounts: list[dict[str, Any]] = field(default_factory=list)
    electoral_votes: dict[str, int] = field(default_factory=dict)
    president: str | None = None
    decided_by: str | None = None
    contingent: dict[str, Any] | None = None
    office_holders: int = 0
    legislature_rows: int = 0
    calls: int = 0


class Finalizer:
    """Finalize one simulated election (see the module docstring)."""

    def __init__(
        self,
        session: Session,
        election: Election,
        inputs: ElectionInputs,
        votes: dict[str, RaceVotes],
        recount_config: RecountConfig,
    ) -> None:
        self.session = session
        self.election = election
        self.inputs = inputs
        self.votes = votes
        self.recount_config = recount_config
        self.seed = int(inputs.seed)
        self.lot_seed = self.seed
        self.tabs: dict[str, TabulatedRaceResult] = {}
        self.decided: dict[str, str | None] = {}
        self.winners: dict[str, str | None] = {}
        self.result = FinalizeResult()
        self.ev_outcome: EVOutcome | None = None
        self.contingent: ContingentResult | None = None
        rows = session.execute(
            select(
                BallotCandidate.id,
                BallotCandidate.candidate_id,
                BallotCandidate.running_mate_id,
                BallotCandidate.party_id,
            ).where(BallotCandidate.race_id.in_(list(inputs.race_ids.values())))
        ).all()
        self.lines = {int(r[0]): _Line(int(r[0]), r[1], r[2], r[3]) for r in rows}

    # ------------------------------------------------------------------ helpers
    def line(self, code: str, key: str | None) -> _Line | None:
        if key is None:
            return None
        bid = self.inputs.line_ids[code].get(key)
        return self.lines.get(bid) if bid is not None else None

    def line_party(self, code: str, key: str | None) -> str | None:
        if key is None:
            return None
        for ln in self.inputs.races[code].lines:
            if ln.key == key:
                return ln.party_code
        return None

    # ------------------------------------------------------------------ 1–2 tabulation / recounts
    def tabulate_and_recount(self) -> None:
        inputs = self.inputs
        recount_seed = seed62(derive_seed(self.seed, "recount"))
        children_changed = False
        for code, spec in inputs.races.items():
            rt = RaceType(spec.race_type)
            if rt == RaceType.PRESIDENT:
                continue
            rv = self.votes[code]
            tab = tabulate(rv, code, tie_seed=self.lot_seed)
            decided = tab.decided_by or "popular_vote"
            need, reason = needs_recount(tab, rt, self.recount_config)
            if need:
                out = perform_recount(rv, recount_seed, self.recount_config, code, tie_seed=self.lot_seed)
                self._store_recount(code, rt, out, reason)
                replace_race_results(self.session, inputs, code, rv, out.recounted)
                self.votes[code] = out.recounted
                tab = out.after
                decided = out.decided_by
                children_changed |= rt == RaceType.PRESIDENT_PROVINCE
            self.tabs[code] = tab
            self.decided[code] = decided
            self.winners[code] = tab.winner_key
        if "PRES" in inputs.races:
            if children_changed:
                children = [self.votes[c] for c in inputs.races_of(RaceType.PRESIDENT_PROVINCE)]
                parent = stitch_parent(inputs.races["PRES"], children)
                replace_race_results(self.session, inputs, "PRES", self.votes["PRES"], parent)
                self.votes["PRES"] = parent
            self.tabs["PRES"] = tabulate(self.votes["PRES"], "PRES", tie_seed=self.lot_seed)

    def _store_recount(self, code: str, rt: RaceType, out: Any, reason: str) -> None:
        rule = self.recount_config.thresholds.get(rt)
        row = Recount(
            race_id=self.inputs.race_ids[code],
            reason="tie" if reason.startswith("exact tie") else "automatic_threshold",
            threshold_pct=rule.margin_pct if rule is not None else None,
            margin_before_votes=int(out.margin_votes_before),
            margin_before_pct=float(out.margin_pct_before),
            margin_after_votes=int(out.margin_votes_after),
            margin_after_pct=float(out.margin_pct_after),
            status="completed",
            outcome_changed=bool(out.outcome_changed),
            seed=int(out.seed),
            started_at=utcnow(),
            completed_at=utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        f = self.inputs.frame
        lids = self.inputs.line_ids[code]
        rows = [
            {
                "recount_id": row.id,
                "geo_unit_id": int(f.unit_ids[a.unit_index]),
                "ballot_candidate_id": None if a.line_index == INVALID_PILE else lids[a.line_key],
                "pile": "invalid" if a.line_index == INVALID_PILE else "line",
                "votes_before": int(a.votes_before),
                "votes_after": int(a.votes_after),
                "delta": int(a.delta),
                "reason": str(a.reason)[:80],
            }
            for a in out.adjustments
        ]
        bulk_insert(self.session, RecountAdjustment, rows)
        self.result.recounts.append(
            {
                "race": code,
                "reason": reason,
                "adjustments": len(rows),
                "margin_before": int(out.margin_votes_before),
                "margin_after": int(out.margin_votes_after),
                "outcome_changed": bool(out.outcome_changed),
                "decided_by": out.decided_by,
            }
        )

    # ------------------------------------------------------------------ 3 Electoral College
    def _allocation_method(self) -> EVAllocationMethod:
        ec = self.inputs.scenario.electoral_college
        if "allocation" in ec.model_fields_set:
            return EVAllocationMethod(ec.allocation)
        return EVAllocationMethod(self.inputs.constitution.ev_allocation)

    def _contingent_config(self) -> Any:
        ec = self.inputs.scenario.electoral_college
        return ec.contingent if "contingent" in ec.model_fields_set else self.inputs.constitution.contingent

    def electoral_college(self) -> None:
        inputs = self.inputs
        if "PRES" not in inputs.races:
            return
        method = self._allocation_method()
        province_tabs = {
            pv: self.tabs[f"PRES-{pv}"] for pv in inputs.ev_by_province if f"PRES-{pv}" in self.tabs
        }
        district_tabs = district_province = None
        if method is EVAllocationMethod.DISTRICT:
            district_tabs, district_province = self._district_tabs()
        outcome = allocate(
            province_tabs,
            inputs.ev_by_province,
            method,
            district_tabs,
            district_province,
            inputs.constitution,
            tie_seed=self.lot_seed,
        )
        self.ev_outcome = outcome
        pres_id = inputs.race_ids["PRES"]
        prov_ids = dict(zip(inputs.frame.province_codes, inputs.frame.province_ids.tolist(), strict=True))
        rows = []
        for pv, key, ev in outcome.allocations:
            ln = self.line("PRES", key)
            if ln is None:
                raise ElectionError(f"electoral votes for unknown ticket {key}")
            rows.append(
                {
                    "race_id": pres_id,
                    "province_race_id": inputs.race_ids[f"PRES-{pv}"],
                    "province_id": prov_ids[pv],
                    "ballot_candidate_id": ln.ballot_id,
                    "candidate_id": ln.candidate_id,
                    "electoral_votes": int(ev),
                }
            )
        bulk_insert(self.session, ElectoralVoteAllocation, rows)
        self.result.electoral_votes = {k: int(v) for k, v in outcome.ev_by_line.items()}
        if outcome.winner is not None:
            self.winners["PRES"] = outcome.winner
            self.decided["PRES"] = DECIDED_EC
        else:
            self._contingent_election(outcome)
        self.result.president = self.winners.get("PRES")
        self.result.decided_by = self.decided.get("PRES")

    def _district_tabs(self) -> tuple[dict[str, TabulatedRaceResult], dict[str, str]]:
        inputs = self.inputs
        parent = self.votes["PRES"]
        d_idx = inputs.unit_district[parent.unit_index]
        D = len(inputs.district_codes)
        totals = np.zeros((D, len(parent.line_keys)), dtype=np.int64)
        np.add.at(totals, d_idx, parent.votes)
        prov = inputs.frame.unit_province[parent.unit_index]
        d_prov = np.zeros(D, dtype=np.int64)
        d_prov[d_idx] = prov
        tabs = {
            code: tabulate_totals(f"PRES-{code}", parent.line_keys, totals[i], tie_seed=self.lot_seed)
            for i, code in enumerate(inputs.district_codes)
        }
        dp = {
            code: inputs.frame.province_codes[int(d_prov[i])] for i, code in enumerate(inputs.district_codes)
        }
        return tabs, dp

    def _post_election_chambers(self) -> tuple[dict[str, list[str | None]], list[str | None]]:
        """(NEW House delegations by province, post-election Senate members' parties)."""
        inputs = self.inputs
        delegations: dict[str, list[str | None]] = {pv: [] for pv in inputs.frame.province_codes}
        house = inputs.races_of(RaceType.HOUSE)
        if house:
            for code in house:
                pv = str(inputs.races[code].province_code)
                delegations[pv].append(self.line_party(code, self.winners.get(code)))
        else:
            serving = holders_at(self.session, self.election.election_date)
            prov_of = dict(
                self.session.execute(
                    select(Office.code, Office.province_id).where(
                        Office.office_type == OfficeType.HOUSE.value
                    )
                )
                .tuples()
                .all()
            )
            code_of = dict(zip(inputs.frame.province_ids.tolist(), inputs.frame.province_codes, strict=True))
            for code, h in serving.items():
                if code.startswith("HOUSE-") and prov_of.get(code) in code_of:
                    delegations[code_of[prov_of[code]]].append(h.seat_party)
        senate: list[str | None] = list(inputs.holdover_seats.values())
        for code in inputs.races_of(RaceType.SENATE):
            senate.append(self.line_party(code, self.winners.get(code)))
        return delegations, senate

    def _runoff(self, finalists: list[str]) -> dict[str, int]:
        """A simulated national runoff between the finalists (their tickets only)."""
        inputs = self.inputs
        lines = [ln for ln in inputs.races["PRES"].lines if ln.key in finalists]
        races = presidential_races(
            inputs.frame, lines, inputs.ev_by_province, incumbent_party=inputs.races["PRES"].incumbent_party
        )
        draw = simulate_votes(inputs.model, races, seed62(derive_seed(self.seed, "runoff")), inputs.context)
        rv = draw.races["PRES"]
        return {k: int(v) for k, v in zip(rv.line_keys, rv.totals(), strict=True)}

    def _contingent_election(self, outcome: EVOutcome) -> None:
        inputs = self.inputs
        pres = inputs.races["PRES"]
        tab = self.tabs["PRES"]
        pv = {k: int(v) for k, v in zip(tab.line_keys, tab.totals, strict=True)}
        delegations, senate = self._post_election_chambers()
        line_party = {ln.key: ln.party_code for ln in pres.lines}
        ideology = {c: inputs.model.ideology[i].tolist() for i, c in enumerate(inputs.model.party_codes)}
        vp_candidates = {ln.key: ln.running_mate_key for ln in pres.lines}
        cfg = self._contingent_config()
        runoff = (
            self._runoff
            if ContingentElectionMode(cfg.mode) is ContingentElectionMode.NATIONAL_RUNOFF
            else None
        )
        result = run_contingent_election(
            outcome,
            pv,
            None,
            delegations,
            senate,
            line_party,
            ideology,
            vp_candidates,
            cfg,
            seed62(derive_seed(self.seed, "contingent")),
            runoff,
        )
        self.contingent = result
        winner_line = self.line("PRES", result.winner)
        vp_line = self.line("PRES", result.vice_president.winner_line)
        self.session.add(
            ContingentElection(
                race_id=inputs.race_ids["PRES"],
                mode=str(result.mode.value if hasattr(result.mode, "value") else result.mode),
                finalists_json=dumps([inputs.line_ids["PRES"][k] for k in result.finalists]),
                ballots_json=dumps(result.to_dict()),
                winner_ballot_candidate_id=winner_line.ballot_id if winner_line is not None else None,
                vp_winner_candidate_id=vp_line.running_mate_id if vp_line is not None else None,
                rounds=int(result.ballots),
                outcome=result.outcome,
            )
        )
        self.winners["PRES"] = result.winner
        self.decided["PRES"] = DECIDED_CONTINGENT
        self.result.contingent = {
            "mode": str(result.mode),
            "finalists": list(result.finalists),
            "winner": result.winner,
            "outcome": result.outcome,
            "decided_by": result.decided_by,
            "ballots": int(result.ballots),
            "vice_president_line": result.vice_president.winner_line,
        }

    # ------------------------------------------------------------------ 4 race summaries
    def race_summaries(self) -> None:
        inputs = self.inputs
        races = {
            r.code: r
            for r in self.session.scalars(select(Race).where(Race.election_id == inputs.election_id))
        }
        prev_ids = [r.previous_race_id for r in races.values() if r.previous_race_id is not None]
        prev_party = (
            dict(
                self.session.execute(select(Race.id, Race.winner_party_id).where(Race.id.in_(prev_ids)))
                .tuples()
                .all()
            )
            if prev_ids
            else {}
        )
        for code, spec in inputs.races.items():
            row = races[code]
            tab = self.tabs[code]
            winner = self.winners.get(code)
            ln = self.line(code, winner)
            valid = int(tab.valid)
            if winner is not None and RaceType(spec.race_type) == RaceType.PRESIDENT:
                j = tab.line_keys.index(winner)
                others = np.delete(np.asarray(tab.totals, dtype=np.int64), j)
                margin = int(tab.totals[j] - (others.max() if len(others) else 0))
            else:
                margin = int(tab.margin_votes)
            row.status = RaceStatus.FINAL.value
            row.winner_ballot_candidate_id = ln.ballot_id if ln is not None else None
            row.winner_party_id = ln.party_id if ln is not None else None
            row.total_votes = valid
            row.margin_votes = margin
            row.margin_pct = 100.0 * margin / valid if valid > 0 else 0.0
            row.turnout_pct = float(tab.turnout_pct)
            base = prev_party.get(row.previous_race_id) if row.previous_race_id is not None else None
            if base is None:
                base = row.incumbent_party_id
            row.flipped = None if base is None or ln is None else bool(ln.party_id != base)
            row.decided_by = self.decided.get(code) or ("vice_president_acts" if winner is None else None)
            if row.decided_by is not None:
                row.decided_by = row.decided_by[:24]
        self.session.flush()

    # ------------------------------------------------------------------ 5 legislatures
    def legislature_seats(self) -> None:
        inputs = self.inputs
        legs = {(lg.level, lg.jurisdiction_code): lg.id for lg in self.session.scalars(select(Legislature))}
        rows = []
        for code, spec in inputs.races.items():
            rt = RaceType(spec.race_type)
            if rt not in _PARTY_LIST:
                continue
            key = (
                ("provincial", str(spec.province_code))
                if rt == RaceType.PROVINCIAL_LEGISLATURE
                else (
                    "municipal",
                    str(spec.municipality_code),
                )
            )
            leg_id = legs.get(key)
            if leg_id is None:
                raise ElectionError(f"{code}: no legislature {key}")
            tab = self.tabs[code]
            votes = {ln.party_code or ln.key: int(t) for ln, t in zip(spec.lines, tab.totals, strict=True)}
            if sum(votes.values()) <= 0:
                continue
            seats = dhondt(votes, int(spec.seats), tie_seed=self.lot_seed, lot_key=code)
            for ln in spec.lines:
                line = self.line(code, ln.key)
                if line is None or line.party_id is None:
                    continue
                rows.append(
                    {
                        "race_id": inputs.race_ids[code],
                        "legislature_id": leg_id,
                        "party_id": line.party_id,
                        "votes": votes[ln.party_code or ln.key],
                        "seats": int(seats.get(ln.party_code or ln.key, 0)),
                    }
                )
        bulk_insert(self.session, LegislatureSeatResult, rows)
        self.result.legislature_rows = len(rows)

    # ------------------------------------------------------------------ 6 office holders
    def office_holders(self) -> None:
        inputs, s = self.inputs, self.session
        calendar = ElectionCalendar.from_config(constitution=inputs.constitution)
        year = inputs.year
        offices = {o.code: o for o in s.scalars(select(Office))}
        office_code_of = {o.id: c for c, o in offices.items()}
        seat_class = dict(s.execute(select(SenateSeat.code, SenateSeat.senate_class)).tuples().all())
        race_rows = {r.code: r for r in s.scalars(select(Race).where(Race.election_id == inputs.election_id))}
        installs: list[tuple[str, int, int | None, date, date, str, int | None]] = []

        def add(
            office_code: str,
            candidate_id: int | None,
            party_id: int | None,
            bounds: tuple[date, date],
            reason: str,
            race_code: str | None,
        ) -> None:
            if candidate_id is None or office_code not in offices:
                return
            rid = race_rows[race_code].id if race_code else None
            installs.append((office_code, candidate_id, party_id, bounds[0], bounds[1], reason, rid))

        for code, spec in inputs.races.items():
            rt = RaceType(spec.race_type)
            winner = self.winners.get(code)
            if rt == RaceType.PRESIDENT:
                self._executive(add, calendar, year)
                continue
            otype = _OFFICE_TYPE.get(rt)
            row = race_rows[code]
            if otype is None or row.office_id is None or winner is None:
                continue
            ln = self.line(code, winner)
            if ln is None:
                continue
            office_code = office_code_of.get(row.office_id)
            if office_code is None:
                continue
            if rt == RaceType.SENATE:
                cls = int(seat_class.get(office_code, 0))
                if spec.is_special or cls not in calendar.senate_classes_up(year):
                    start = calendar.term_bounds(OfficeType.HOUSE, year)[0]
                    end = calendar.term_bounds(
                        OfficeType.SENATE, calendar.next_senate_election(cls, year), cls
                    )[0]
                    bounds = (start, end)
                else:
                    bounds = calendar.term_bounds(OfficeType.SENATE, year, senate_class=cls)
            else:
                bounds = calendar.term_bounds(otype, year)
            add(office_code, ln.candidate_id, ln.party_id, bounds, "elected", code)
            if rt == RaceType.GOVERNOR and ln.running_mate_id is not None:
                lt = lt_governor_office_code(str(spec.province_code))
                add(
                    lt,
                    ln.running_mate_id,
                    ln.party_id,
                    calendar.term_bounds(OfficeType.LIEUTENANT_GOVERNOR, year),
                    "elected",
                    code,
                )
        self._install(installs, offices)

    def _executive(self, add: Any, calendar: ElectionCalendar, year: int) -> None:
        bounds = calendar.term_bounds(OfficeType.PRESIDENT, year)
        vp_bounds = calendar.term_bounds(OfficeType.VICE_PRESIDENT, year)
        winner = self.winners.get("PRES")
        if self.contingent is not None:
            vp_line = self.line("PRES", self.contingent.vice_president.winner_line)
        else:
            vp_line = self.line("PRES", winner)
        pres_line = self.line("PRES", winner)
        if pres_line is not None:
            add(PRESIDENT_OFFICE, pres_line.candidate_id, pres_line.party_id, bounds, "elected", "PRES")
        elif vp_line is not None:  # deadlock: the Vice-President-elect acts as President
            add(PRESIDENT_OFFICE, vp_line.running_mate_id, vp_line.party_id, bounds, "acting", "PRES")
        if vp_line is not None:
            add(
                VICE_PRESIDENT_OFFICE, vp_line.running_mate_id, vp_line.party_id, vp_bounds, "elected", "PRES"
            )

    def _install(
        self,
        installs: Sequence[tuple[str, int, int | None, date, date, str, int | None]],
        offices: Mapping[str, Office],
    ) -> None:
        s = self.session
        if not installs:
            return
        office_ids = [offices[c].id for c, *_ in installs]
        serving = s.scalars(
            select(OfficeHolder).where(
                OfficeHolder.office_id.in_(office_ids), OfficeHolder.ended_on.is_(None)
            )
        ).all()
        by_office: dict[int, list[OfficeHolder]] = {}
        for h in serving:
            by_office.setdefault(h.office_id, []).append(h)
        on_ballot: dict[int, set[int]] = {}
        for code, spec in self.inputs.races.items():
            rid = self.inputs.race_ids[code]
            ids = set()
            for ln in spec.lines:
                line = self.line(code, ln.key)
                if line is not None:
                    ids |= {c for c in (line.candidate_id, line.running_mate_id) if c is not None}
            on_ballot[rid] = ids
        rows = []
        for office_code, cand, party, start, end, reason, race_id in installs:
            oid = offices[office_code].id
            for h in by_office.get(oid, []):
                if h.term_start >= start:
                    continue
                h.ended_on = start
                if h.candidate_id == cand:
                    h.end_reason = "term_expired"
                elif race_id is not None and h.candidate_id in on_ballot.get(race_id, set()):
                    h.end_reason = "defeated"
                else:
                    h.end_reason = "retired"
            rows.append(
                {
                    "office_id": oid,
                    "candidate_id": cand,
                    "party_id": party,
                    "term_start": start,
                    "term_end": end,
                    "ended_on": None,
                    "start_reason": reason,
                    "end_reason": None,
                    "election_race_id": race_id,
                }
            )
        bulk_insert(s, OfficeHolder, rows)
        s.flush()
        self.result.office_holders = len(rows)

    # ------------------------------------------------------------------ 7 race calls
    def store_calls(self, records: Sequence[CallRecord]) -> None:
        inputs, s = self.inputs, self.session
        meta = timeline_meta(s, inputs.election_id)
        s.execute(delete(RaceCall).where(RaceCall.election_id == inputs.election_id))
        ordered = sorted(enumerate(records), key=lambda t: (t[1].seq, t[0]))
        last_index: dict[str, int] = {}
        for i, (_, rec) in enumerate(ordered):
            last_index[rec.race_key] = i
        rows = []
        called_at: dict[str, Any] = {}
        state: dict[str, tuple[str | None, Any]] = {}
        for i, (_, rec) in enumerate(ordered):
            if rec.race_key not in inputs.race_ids:
                raise ElectionError(f"call record for unknown race {rec.race_key}")
            when = local_time(meta, rec.sim_time_s)
            status = RaceStatus(rec.status)
            decided = status in DECIDED_STATUSES and not rec.retracted
            prev = state.get(rec.race_key)
            if decided:
                if prev is None or prev[0] != rec.key:
                    state[rec.race_key] = (rec.key, when)
            else:
                state.pop(rec.race_key, None)
            rows.append(
                {
                    "race_id": inputs.race_ids[rec.race_key],
                    "election_id": inputs.election_id,
                    "status": status.value,
                    "ballot_candidate_id": inputs.line_ids[rec.race_key].get(rec.key) if rec.key else None,
                    "seq": int(rec.seq),
                    "sim_time_s": float(rec.sim_time_s),
                    "called_at": when,
                    "recorded_at": utcnow(),
                    "reporting_pct": float(rec.reporting_pct),
                    "leader_margin_pct": None if rec.margin_pct is None else float(rec.margin_pct),
                    "win_probability": None if rec.win_probability is None else float(rec.win_probability),
                    "evidence_json": dumps(rec.evidence),
                    "is_manual": bool(rec.is_manual),
                    "override_reason": rec.override_reason,
                    "superseded": i != last_index[rec.race_key],
                }
            )
        for code, (_key, when) in state.items():
            called_at[code] = when
        bulk_insert(s, RaceCall, rows)
        for r in s.scalars(select(Race).where(Race.election_id == inputs.election_id)):
            r.called_at = called_at.get(r.code)
        s.flush()
        self.result.calls = len(rows)
