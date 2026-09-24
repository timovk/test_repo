"""Live election-night engine (SIMULATED).

:class:`NightEngine` replays a :class:`~app.reporting.timeline.Timeline` against the final
simulated results and maintains everything a broadcast needs: counted votes, race calls with
evidence, lead changes, Electoral College / House / Senate tallies, governors, the national
popular vote and reporting statistics.  Content is a pure function of
``(frame, timeline, races, race_meta, config, seed, manual_calls)`` and the event sequence
number — playback speed never changes what is shown at a given ``seq``.

Counting rule: after an event the counted votes of a unit are ``floor(f × final)`` per line
(:func:`~app.reporting.calling.allocate_counted`), so counted ≤ final always and counted ==
final exactly once ``f == 1``.  Only races touched by an event's units are recounted and
re-evaluated; FINAL/RECOUNT races are locked.

Snapshot schema (``snapshot(detail)``; every value is JSON-serialisable)::

    {
      "data_category": "SIMULATED", "seed": int,
      "seq": int, "total_events": int, "sim_time_s": float,
      "clock": "HH:MM", "timestamp": ISO-8601 local time,
      "status": "polls_closed" | "counting" | "complete",
      "reporting": {"pct_expected_ballots", "units_total", "units_reported", "units_partial",
                    "pct_units", "municipalities_total", "municipalities_reporting",
                    "municipalities_complete"},
      "popular_vote": {"lines": [{key, label, party, color, votes, pct, projected_votes,
                                  projected_pct}], "counted_valid", "projected_valid",
                       "outstanding_ballots_est", "reporting_pct"} | null,
      "president": {"race_key": "PRES", "status", "ev_total", "ev_needed", "ev_decided_total",
                    "ev_uncalled", "tickets": [{key, label, party, color, votes, pct,
                    ev_decided, ev_leading, ev_max_possible}], "winner",
                    "majority_reached_at_seq", "contingent_likely",
                    "contingent_likely_at_seq"} | null,
      "provinces": [{code, name, ev, race_key, status, leader, called_key, win_probability,
                     reporting_pct, margin_pct, votes: {key: int},
                     municipalities_total, municipalities_reporting, municipalities_complete}],
      "house": {"seats_total", "majority", "races", "called", "leading", "uncalled",
                "by_party": [{party, color, called, leading, total, incumbent_seats,
                              net_change_called, net_change_projected}],
                "control", "control_at_seq"} | null,
      "senate": {"seats_total", "majority", "up", "holdover_total", "called", "leading",
                 "uncalled", "by_party": [{party, color, holdover, called, leading,
                 total_decided, total_projected}], "control", "control_at_seq"} | null,
      "governors": [{key, province_code, status, leader, called_key, party, reporting_pct,
                     margin_pct, win_probability}],
      "races": [compact race: {key, type, name, parent, province_code, district_code,
                municipality_code, status, is_manual, leader, called_key, lean_key,
                win_probability, reporting_pct, margin_pct, projected_margin_pct, math_certain,
                votes: {key: int}}  (+ with detail="full": counted_valid, win_probabilities,
                projected_share_mean/p05/p95, projected_votes, outstanding_ballots_est,
                outstanding_ballots_upper, line_labels, line_parties, line_colors)],
      "recent_calls": [call record without evidence], "lead_changes": {"count", "recent"},
      # detail="full" only:
      "municipalities": [{code, name, province_code, reporting_pct, units_total,
                          units_reported, leader, margin_pct, race_key}]
    }

``race_detail(race_key)`` returns meta, the current decision (with evidence), the complete call
history (with evidence), lead changes and a per-municipality breakdown; ``municipality_detail
(code)`` returns reporting events so far and the counted result of every race in the
municipality.
"""

from __future__ import annotations

from bisect import insort
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

import numpy as np

from app.core.config import get_constitution
from app.core.constitution import DECIDED_STATUSES, RaceStatus, RaceType
from app.core.errors import ElectionNightError, NotFoundError
from app.core.logging import get_logger
from app.elections.types import RaceVotes
from app.geography.frame import GeographyFrame
from app.reporting.calling import (
    CALL_STATUSES,
    LOCKED_STATUSES,
    CallDecision,
    CallState,
    RaceCaller,
    RaceProgress,
    RecountCheck,
    initial_state,
)
from app.reporting.clock import PlaybackClock, PlaybackState
from app.reporting.config import NightConfig, load_night_config
from app.reporting.timeline import Timeline

log = get_logger(__name__)

__all__ = [
    "CallRecord",
    "LeadChange",
    "ManualCall",
    "NightEngine",
    "PlaybackClock",
    "PlaybackState",
    "RaceMeta",
]

#: Key used for the national presidential parent race.
PRESIDENT_RACE_KEY = "PRES"
_INDEPENDENT = "IND"
_TYPE_PRIORITY = {
    RaceType.PRESIDENT_PROVINCE: 0,
    RaceType.HOUSE: 1,
    RaceType.SENATE: 2,
    RaceType.GOVERNOR: 3,
    RaceType.PROVINCIAL_LEGISLATURE: 4,
    RaceType.MAYOR: 5,
    RaceType.MUNICIPAL_COUNCIL: 6,
    RaceType.PRESIDENT: 7,
}


# --------------------------------------------------------------------------- public records
@dataclass(frozen=True)
class RaceMeta:
    """Display / aggregation metadata of a race (FICTIONAL candidates and parties)."""

    race_type: RaceType
    electoral_votes: int | None = None
    province_code: str | None = None
    district_code: str | None = None
    municipality_code: str | None = None
    line_labels: Mapping[str, str] = field(default_factory=dict)
    line_parties: Mapping[str, str | None] = field(default_factory=dict)
    line_colors: Mapping[str, str] = field(default_factory=dict)
    parent: str | None = None  # e.g. 'PRES' for PRESIDENT_PROVINCE races
    incumbent_party: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class CallRecord:
    """A change of a race's published state (maps onto ``race_call``)."""

    race_key: str
    seq: int
    sim_time_s: float
    status: RaceStatus
    key: str | None
    win_probability: float | None
    reporting_pct: float
    margin_pct: float | None
    evidence: dict
    is_manual: bool = False
    override_reason: str | None = None
    retracted: bool = False
    previous_status: RaceStatus | None = None
    previous_key: str | None = None

    def to_dict(self, include_evidence: bool = True) -> dict:
        d = {
            "race_key": self.race_key,
            "seq": self.seq,
            "sim_time_s": self.sim_time_s,
            "status": self.status.value,
            "key": self.key,
            "win_probability": self.win_probability,
            "reporting_pct": round(self.reporting_pct, 3),
            "margin_pct": None if self.margin_pct is None else round(self.margin_pct, 4),
            "is_manual": self.is_manual,
            "override_reason": self.override_reason,
            "retracted": self.retracted,
            "previous_status": None if self.previous_status is None else self.previous_status.value,
            "previous_key": self.previous_key,
        }
        if include_evidence:
            d["evidence"] = self.evidence
        return d


@dataclass(frozen=True)
class LeadChange:
    """The counted leader of a race changed after an event."""

    race_key: str
    seq: int
    sim_time_s: float
    from_key: str
    to_key: str
    reporting_pct: float

    def to_dict(self) -> dict:
        return {
            "race_key": self.race_key,
            "seq": self.seq,
            "sim_time_s": self.sim_time_s,
            "from_key": self.from_key,
            "to_key": self.to_key,
            "reporting_pct": round(self.reporting_pct, 3),
        }


@dataclass(frozen=True)
class ManualCall:
    """A producer's manual override, applied after event ``seq``.

    ``status=None`` clears the override: the race is re-evaluated at once and the model treats
    the former manual state as its own prior (a manual call it still believes stays called).
    """

    race_key: str
    seq: int
    status: RaceStatus | None
    key: str | None = None
    reason: str | None = None


# --------------------------------------------------------------------------- internal state
@dataclass
class _Race:
    key: str
    idx: int
    meta: RaceMeta
    line_keys: list[str]
    units: np.ndarray  # (n,) global unit index
    cluster: np.ndarray  # (n,) compact municipality id
    cluster_muni: np.ndarray  # (M_r,) municipality index of each cluster
    final: np.ndarray  # (n, L)
    final_blank: np.ndarray
    final_invalid: np.ndarray
    eligible: np.ndarray
    exp_shares: np.ndarray
    exp_ballots: np.ndarray
    f: np.ndarray
    counted: np.ndarray
    counted_ballots: np.ndarray
    totals: np.ndarray  # (L,) counted totals
    x_total: float = 0.0  # expected ballots of the race
    fx: float = 0.0  # Σ f · expected ballots (reporting numerator)
    n_incomplete: int = 0  # units with f < 1
    last_eval_share: float = 0.0  # reporting share at the last evaluation
    state: CallState = field(default_factory=initial_state)
    decision: CallDecision | None = None
    leader: str | None = None
    history: list[CallRecord] = field(default_factory=list)
    lead_changes: list[LeadChange] = field(default_factory=list)

    @property
    def locked(self) -> bool:
        return self.state.status in LOCKED_STATUSES

    def progress(self) -> RaceProgress:
        return RaceProgress(
            race_key=self.key,
            line_keys=self.line_keys,
            counted_votes=self.counted,
            reported_fraction=self.f,
            counted_ballots=self.counted_ballots,
            expected_shares=self.exp_shares,
            expected_ballots=self.exp_ballots,
            eligible=self.eligible,
            unit_cluster=self.cluster,
            n_clusters=len(self.cluster_muni),
            race_type=self.meta.race_type.value,
        )


# --------------------------------------------------------------------------- engine
class NightEngine:
    """Deterministic live election night over a timeline and final results.

    Args:
        frame: the geography the races and timeline refer to.
        timeline: reporting events (:func:`~app.reporting.timeline.generate_timeline`).
        races: race key → final :class:`RaceVotes` (with ``expected_shares``/``expected_turnout``
            = pre-election expectation, used by the caller).
        race_meta: race key → :class:`RaceMeta`.
        config: night configuration (default ``config/night.yaml``).
        seed: root seed of the night (call draws use ``make_rng(seed, "call", race, seq)``).
        holdover_senate: party → Senate seats not up in this election.
        recount_check: injected automatic-recount rule (fallback: ``recount.margin_pct``).
        manual_calls: manual overrides to apply at their ``seq`` (replayed on rebuild).
    """

    def __init__(
        self,
        frame: GeographyFrame,
        timeline: Timeline,
        races: Mapping[str, RaceVotes],
        race_meta: Mapping[str, RaceMeta],
        config: NightConfig | None = None,
        seed: int = 0,
        holdover_senate: Mapping[str, int] | None = None,
        recount_check: RecountCheck | None = None,
        manual_calls: Sequence[ManualCall] = (),
    ) -> None:
        self.frame = frame
        self.timeline = timeline
        self.config = config or load_night_config()
        self.seed = int(seed)
        self.caller = RaceCaller(self.config, recount_check)
        self.constitution = get_constitution()
        self.holdover_senate = {str(k): int(v) for k, v in (holdover_senate or {}).items()}
        if timeline.n_units != frame.n_units:
            raise ElectionNightError("timeline and frame have different numbers of units")
        missing = sorted(set(races) - set(race_meta))
        if missing:
            raise ElectionNightError(f"race_meta missing for {missing[:5]}")
        self._races: list[_Race] = []
        self._by_key: dict[str, _Race] = {}
        for i, key in enumerate(sorted(races)):
            r = self._build_race(i, key, races[key], race_meta[key])
            self._races.append(r)
            self._by_key[key] = r
        self._build_index()
        self._pres_races = [r for r in self._races if r.meta.race_type == RaceType.PRESIDENT_PROVINCE]
        self._house_races = [r for r in self._races if r.meta.race_type == RaceType.HOUSE]
        self._senate_races = [r for r in self._races if r.meta.race_type == RaceType.SENATE]
        self._gov_races = [r for r in self._races if r.meta.race_type == RaceType.GOVERNOR]
        tickets: list[str] = []
        for r in self._pres_races:
            for k in r.line_keys:
                if k not in tickets:
                    tickets.append(k)
        self._tickets = tickets
        self._manual_all: list[ManualCall] = sorted(manual_calls, key=lambda m: m.seq)  # stable
        for m in self._manual_all:
            if m.race_key not in self._by_key:
                raise ElectionNightError(f"manual call for unknown race {m.race_key}")
        self.reset()

    # ------------------------------------------------------------------ construction
    def _build_race(self, idx: int, key: str, rv: RaceVotes, meta: RaceMeta) -> _Race:
        units = np.asarray(rv.unit_index, dtype=np.int64)
        if units.size and not self.timeline.units_mask[units].all():
            raise ElectionNightError(f"{key}: some units are not covered by the timeline")
        n, L = rv.votes.shape
        if len(rv.line_keys) != L:
            raise ElectionNightError(f"{key}: line_keys do not match the votes array")
        if rv.expected_shares is None or rv.expected_turnout is None:
            log.warning("race %s has no pre-election expectation; the caller uses flat priors", key)
        exp_shares = (
            np.asarray(rv.expected_shares, dtype=np.float64)
            if rv.expected_shares is not None
            else np.full((n, L), 1.0 / L)
        )
        exp_turnout = (
            np.asarray(rv.expected_turnout, dtype=np.float64)
            if rv.expected_turnout is not None
            else np.full(n, 0.78)
        )
        munis = self.frame.unit_muni[units]
        cluster_muni, cluster = np.unique(munis, return_inverse=True)
        final = np.asarray(rv.votes, dtype=np.int64)
        return _Race(
            key=key,
            idx=idx,
            meta=meta,
            line_keys=list(rv.line_keys),
            units=units,
            cluster=cluster.astype(np.int64),
            cluster_muni=cluster_muni.astype(np.int64),
            final=final,
            final_blank=np.asarray(rv.blank, dtype=np.int64),
            final_invalid=np.asarray(rv.invalid, dtype=np.int64),
            eligible=np.asarray(rv.eligible, dtype=np.int64),
            exp_shares=exp_shares,
            exp_ballots=exp_turnout * np.asarray(rv.eligible, dtype=np.float64),
            f=np.zeros(n),
            counted=np.zeros((n, L), dtype=np.int64),
            counted_ballots=np.zeros(n, dtype=np.int64),
            totals=np.zeros(L, dtype=np.int64),
            x_total=float((exp_turnout * np.asarray(rv.eligible, dtype=np.float64)).sum()),
            n_incomplete=n,
        )

    def _build_index(self) -> None:
        U = self.frame.n_units
        if self._races:
            ent_unit = np.concatenate([r.units for r in self._races])
            ent_race = np.concatenate([np.full(len(r.units), r.idx, dtype=np.int64) for r in self._races])
            ent_row = np.concatenate([np.arange(len(r.units), dtype=np.int64) for r in self._races])
        else:
            ent_unit = ent_race = ent_row = np.zeros(0, dtype=np.int64)
        order = np.lexsort((ent_race, ent_unit))
        self._ent_race, self._ent_row = ent_race[order], ent_row[order]
        self._ent_ptr = np.searchsorted(ent_unit[order], np.arange(U + 1)).astype(np.int64)
        # expected ballots per unit for national reporting (first race by type priority)
        x = np.full(U, np.nan)
        for r in sorted(self._races, key=lambda r: (_TYPE_PRIORITY.get(r.meta.race_type, 9), r.key)):
            sel = np.isnan(x[r.units])
            x[r.units[sel]] = r.exp_ballots[sel]
        fallback = (
            float(np.nanmean(x / np.maximum(self.frame.unit_eligible, 1))) if np.isfinite(x).any() else 0.78
        )
        miss = np.isnan(x)
        x[miss] = self.frame.unit_eligible[miss] * (fallback if np.isfinite(fallback) else 0.78)
        x[~self.timeline.units_mask] = 0.0
        self._unit_expected = x
        self._muni_expected = np.bincount(self.frame.unit_muni, weights=x, minlength=self.frame.n_munis)
        self._muni_units = np.bincount(
            self.frame.unit_muni[self.timeline.units_mask], minlength=self.frame.n_munis
        ).astype(np.int64)

    # ------------------------------------------------------------------ lifecycle
    def reset(self) -> None:
        """Back to polls closing (seq 0); all races re-evaluated with no results."""
        U = self.frame.n_units
        self._f = np.zeros(U)
        self.seq = 0
        self.sim_time_s = 0.0
        self._records: list[CallRecord] = []
        self._lead_changes: list[LeadChange] = []
        self._manual_pending = list(self._manual_all)
        for r in self._races:
            r.f[:] = 0.0
            r.counted[:] = 0
            r.counted_ballots[:] = 0
            r.totals[:] = 0
            r.fx = 0.0
            r.n_incomplete = len(r.units)
            r.last_eval_share = 0.0
            r.state = initial_state()
            r.decision = None
            r.leader = None
            r.history = []
            r.lead_changes = []
        self._pres_state = CallState(RaceStatus.POLLS_CLOSED)
        self._pres_history: list[CallRecord] = []
        self._pres_winner: str | None = None
        self._pres_majority_seq: int | None = None
        self._contingent_seq: int | None = None
        self._contingent = False
        self._house_control: tuple[str | None, int | None] = (None, None)
        self._senate_control: tuple[str | None, int | None] = (None, None)
        for r in self._races:
            self._evaluate(r)
        self._update_aggregates()
        self._apply_manual_until(0)

    @property
    def total_events(self) -> int:
        return self.timeline.n_events

    @property
    def is_finished(self) -> bool:
        return self.seq >= self.timeline.n_events

    @property
    def race_keys(self) -> list[str]:
        return [r.key for r in self._races]

    @property
    def call_history(self) -> list[CallRecord]:
        """All state changes of all races (including the national 'PRES'), in order."""
        return list(self._records)

    @property
    def lead_changes(self) -> list[LeadChange]:
        return list(self._lead_changes)

    @property
    def reported_fraction(self) -> np.ndarray:
        """(U,) cumulative reported fraction per unit (read-only copy)."""
        return self._f.copy()

    def decision(self, race_key: str) -> CallDecision | None:
        return self._race(race_key).decision

    def race_state(self, race_key: str) -> CallState:
        if race_key == PRESIDENT_RACE_KEY and race_key not in self._by_key:
            return self._pres_state
        return self._race(race_key).state

    def counted_votes(self, race_key: str) -> dict[str, int]:
        r = self._race(race_key)
        return dict(zip(r.line_keys, r.totals.tolist(), strict=True))

    def _race(self, race_key: str) -> _Race:
        try:
            return self._by_key[race_key]
        except KeyError:
            raise NotFoundError(f"unknown race {race_key}") from None

    # ------------------------------------------------------------------ advancing
    def advance(self, n: int = 1) -> int:
        """Apply the next ``n`` events; returns the new ``seq``."""
        return self.advance_to_seq(min(self.seq + max(int(n), 0), self.timeline.n_events))

    def advance_to_seq(self, seq: int) -> int:
        """Move to exactly ``seq`` (rewinding by deterministic replay when ``seq`` < current)."""
        seq = int(seq)
        if not 0 <= seq <= self.timeline.n_events:
            raise ElectionNightError(f"seq {seq} out of range 0..{self.timeline.n_events}")
        if seq < self.seq:
            self.reset()
        while self.seq < seq:
            self._apply_event(self.seq + 1)
        if seq > 0:
            self.sim_time_s = max(self.sim_time_s, float(self.timeline.sim_time_s[seq - 1]))
        return self.seq

    def advance_to(self, seq: int) -> int:
        """Alias of :meth:`advance_to_seq` (name used in ``docs/ARCHITECTURE.md``)."""
        return self.advance_to_seq(seq)

    def advance_to_time(self, sim_time_s: float) -> int:
        """Apply every event with ``sim_time_s ≤ t``; the clock shows ``t`` afterwards."""
        target = self.timeline.seq_at_time(sim_time_s)
        if target < self.seq:
            self.advance_to_seq(target)
            self.sim_time_s = float(sim_time_s)
        else:
            self.advance_to_seq(target)
            self.sim_time_s = max(self.sim_time_s, float(sim_time_s))
        return self.seq

    def finish(self) -> int:
        """Apply all remaining events (every race ends FINAL or RECOUNT)."""
        return self.advance_to_seq(self.timeline.n_events)

    def _apply_event(self, seq: int) -> None:
        units, cum = self.timeline.event_units(seq)
        self._f[units] = cum
        a = self._ent_ptr[units]
        counts = self._ent_ptr[units + 1] - a
        total = int(counts.sum())
        self.seq = seq
        self.sim_time_s = float(self.timeline.sim_time_s[seq - 1])
        if total:
            starts = np.repeat(a - (np.cumsum(counts) - counts), counts)
            ent = starts + np.arange(total)
            race_ids = self._ent_race[ent]
            rows = self._ent_row[ent]
            fvals = np.repeat(cum, counts)
            order = np.argsort(race_ids, kind="stable")
            race_ids, rows, fvals = race_ids[order], rows[order], fvals[order]
            bounds = np.flatnonzero(np.r_[True, race_ids[1:] != race_ids[:-1], True])
            touched_pres = touched_house = touched_senate = False
            min_inc = self.config.calling.min_eval_increment_pct / 100.0
            called_inc = self.config.calling.called_eval_increment_pct / 100.0
            for b0, b1 in pairwise(bounds.tolist()):
                r = self._races[int(race_ids[b0])]
                self._update_counts(r, rows[b0:b1], fvals[b0:b1])
                self._update_leader(r)
                if r.locked:
                    continue
                share = r.fx / r.x_total if r.x_total > 0 else float(r.f.mean())
                inc = called_inc if r.state.status == RaceStatus.CALLED and not r.state.is_manual else min_inc
                due = r.n_incomplete == 0 or r.last_eval_share <= 0.0 or share - r.last_eval_share >= inc
                if not due:
                    continue
                r.last_eval_share = share
                if self._evaluate(r):
                    t = r.meta.race_type
                    touched_pres |= t == RaceType.PRESIDENT_PROVINCE
                    touched_house |= t == RaceType.HOUSE
                    touched_senate |= t == RaceType.SENATE
            if touched_pres:
                self._update_president()
            if touched_house:
                self._update_house_control()
            if touched_senate:
                self._update_senate_control()
        self._apply_manual_until(seq)

    @staticmethod
    def _update_counts(r: _Race, rows: np.ndarray, fvals: np.ndarray) -> None:
        old = r.counted[rows].sum(axis=0)
        old_f = r.f[rows]
        r.n_incomplete -= int(np.count_nonzero((old_f < 1.0) & (fvals >= 1.0)))
        r.fx += float((fvals - old_f) @ r.exp_ballots[rows])
        r.f[rows] = fvals
        fin = r.final[rows]
        new = np.floor(fin * fvals[:, None]).astype(np.int64)
        r.counted[rows] = new
        r.counted_ballots[rows] = (
            new.sum(axis=1)
            + np.floor(r.final_blank[rows] * fvals).astype(np.int64)
            + np.floor(r.final_invalid[rows] * fvals).astype(np.int64)
        )
        r.totals += new.sum(axis=0) - old

    def _update_leader(self, r: _Race) -> None:
        """Counted leader (ties keep the previous leader); records lead changes."""
        if r.totals.sum() <= 0:
            return
        top = int(np.argmax(r.totals))
        if r.leader is not None:
            cur = r.line_keys.index(r.leader)
            if r.totals[cur] == r.totals[top]:
                return
        new = r.line_keys[top]
        if r.leader is not None and new != r.leader:
            pct = 100.0 * r.fx / r.x_total if r.x_total > 0 else 100.0 * float(r.f.mean())
            lc = LeadChange(r.key, self.seq, self.sim_time_s, r.leader, new, pct)
            r.lead_changes.append(lc)
            self._lead_changes.append(lc)
        r.leader = new

    # ------------------------------------------------------------------ evaluation
    def _evaluate(self, r: _Race) -> bool:
        """Evaluate one race at the current seq; returns True when its published state changed."""
        d = self.caller.evaluate(r.progress(), self.seed, self.seq, r.state)
        r.decision = d
        prev = r.state
        if prev.is_manual and d.status not in LOCKED_STATUSES:
            return False  # the manual override stays published; the model opinion is in r.decision
        changed = (d.status, d.winner_key) != (prev.status, prev.key) or not r.history
        if not changed:
            return False
        r.state = CallState(status=d.status, key=d.winner_key, seq=self.seq)
        rec = CallRecord(
            race_key=r.key,
            seq=self.seq,
            sim_time_s=self.sim_time_s,
            status=d.status,
            key=d.winner_key,
            win_probability=d.win_probability_of(d.winner_key or d.leader_key),
            reporting_pct=d.reporting_pct,
            margin_pct=d.margin_pct,
            evidence=d.evidence,
            retracted=d.retracted,
            previous_status=prev.status if r.history else None,
            previous_key=prev.key if r.history else None,
        )
        r.history.append(rec)
        self._records.append(rec)
        return True

    # ------------------------------------------------------------------ manual overrides
    def apply_manual_call(
        self, race_key: str, status: RaceStatus | None, key: str | None = None, reason: str | None = None
    ) -> CallRecord | None:
        """Apply a manual override now (``status=None`` clears it).  The override is kept in
        :attr:`manual_calls` so a rebuilt engine replays it at the same ``seq``."""
        r = self._race(race_key)
        if status is not None and key is not None and key not in r.line_keys:
            raise ElectionNightError(f"{race_key}: unknown line {key}")
        m = ManualCall(race_key=race_key, seq=self.seq, status=status, key=key, reason=reason)
        insort(self._manual_all, m, key=lambda c: c.seq)
        return self._apply_manual(m)

    def clear_manual_call(self, race_key: str, reason: str | None = None) -> CallRecord | None:
        """Return a race to the model (see :class:`ManualCall`)."""
        return self.apply_manual_call(race_key, None, None, reason)

    @property
    def manual_calls(self) -> list[ManualCall]:
        return list(self._manual_all)

    def _apply_manual_until(self, seq: int) -> None:
        while self._manual_pending and self._manual_pending[0].seq <= seq:
            self._apply_manual(self._manual_pending.pop(0))

    def _apply_manual(self, m: ManualCall) -> CallRecord | None:
        r = self._race(m.race_key)
        if r.locked:
            return None
        prev = r.state
        if m.status is None:
            if not prev.is_manual:
                return None
            r.state = CallState(prev.status, prev.key, prev.seq, is_manual=False)
            before = len(r.history)
            self._evaluate(r)
            self._refresh_after_change(r)
            if len(r.history) > before:
                return r.history[-1]
            return self._manual_record(r, m, prev, is_manual=False)
        r.state = CallState(m.status, m.key, self.seq, is_manual=True)
        rec = self._manual_record(r, m, prev, is_manual=True)
        self._refresh_after_change(r)
        return rec

    def _manual_record(self, r: _Race, m: ManualCall, prev: CallState, *, is_manual: bool) -> CallRecord:
        d = r.decision
        model_ev = {} if d is None else {"model_status": d.status.value, "model_key": d.winner_key}
        rec = CallRecord(
            race_key=r.key,
            seq=self.seq,
            sim_time_s=self.sim_time_s,
            status=r.state.status,
            key=r.state.key,
            win_probability=None if d is None else d.win_probability_of(r.state.key),
            reporting_pct=0.0 if d is None else d.reporting_pct,
            margin_pct=None if d is None else d.margin_pct,
            evidence={
                "basis": "manual" if is_manual else "manual_cleared",
                "data_category": "SIMULATED",
                "reason": m.reason,
                **model_ev,
                "model_win_probability": {} if d is None else d.win_probability,
            },
            is_manual=is_manual,
            override_reason=m.reason,
            previous_status=prev.status,
            previous_key=prev.key,
        )
        r.history.append(rec)
        self._records.append(rec)
        return rec

    def _refresh_after_change(self, r: _Race) -> None:
        t = r.meta.race_type
        if t == RaceType.PRESIDENT_PROVINCE:
            self._update_president()
        elif t == RaceType.HOUSE:
            self._update_house_control()
        elif t == RaceType.SENATE:
            self._update_senate_control()

    # ------------------------------------------------------------------ national aggregates
    def _update_aggregates(self) -> None:
        self._update_president()
        self._update_house_control()
        self._update_senate_control()

    def _ev_tallies(self) -> dict:
        decided = dict.fromkeys(self._tickets, 0)
        leading = dict.fromkeys(self._tickets, 0)
        possible = dict.fromkeys(self._tickets, 0)
        uncalled = 0
        total = 0
        for r in self._pres_races:
            ev = int(r.meta.electoral_votes or 0)
            total += ev
            st = r.state
            if st.status in DECIDED_STATUSES and st.key is not None:
                decided[st.key] += ev
                possible[st.key] += ev
            else:
                uncalled += ev
                if r.leader is not None:
                    leading[r.leader] += ev
                for k in r.line_keys:
                    possible[k] += ev
        return {
            "decided": decided,
            "leading": leading,
            "possible": possible,
            "uncalled": uncalled,
            "total": total,
        }

    def _update_president(self) -> None:
        if not self._pres_races:
            return
        maj = self.constitution.presidential_majority
        t = self._ev_tallies()
        winner = next((k for k in self._tickets if t["decided"][k] >= maj), None)
        contingent = bool(self._tickets) and all(v < maj for v in t["possible"].values())
        self._contingent = contingent
        if contingent and self._contingent_seq is None:
            self._contingent_seq = self.seq
        if winner is not None and self._pres_majority_seq is None:
            self._pres_majority_seq = self.seq
        any_results = any(r.f.any() for r in self._pres_races)
        all_locked = all(r.locked for r in self._pres_races)
        ev_final = sum(
            int(r.meta.electoral_votes or 0)
            for r in self._pres_races
            if r.state.status == RaceStatus.FINAL and r.state.key == winner
        )
        prev = self._pres_state
        retracted = False
        if winner is not None:
            status = RaceStatus.FINAL if all_locked and ev_final >= maj else RaceStatus.CALLED
        elif prev.status in (RaceStatus.CALLED, RaceStatus.FINAL) and prev.key is not None:
            status, retracted = RaceStatus.TOO_CLOSE, True
        elif contingent:
            status = RaceStatus.TOO_CLOSE
        elif not any_results:
            status = RaceStatus.POLLS_CLOSED
        elif all_locked:
            status = RaceStatus.TOO_CLOSE
        else:
            status = RaceStatus.TOO_EARLY
        self._pres_winner = winner
        new = CallState(status, winner, self.seq)
        if (new.status, new.key) == (prev.status, prev.key) and self._pres_history:
            return
        self._pres_state = new
        evidence = {
            "basis": "electoral_college",
            "data_category": "SIMULATED",
            "ev_needed": maj,
            "ev_total": t["total"],
            "ev_decided": t["decided"],
            "ev_leading": t["leading"],
            "ev_max_possible": t["possible"],
            "ev_uncalled": t["uncalled"],
            "contingent_election_likely": contingent,
            "retracted": retracted,
            "retracted_key": prev.key if retracted else None,
        }
        rec = CallRecord(
            race_key=PRESIDENT_RACE_KEY,
            seq=self.seq,
            sim_time_s=self.sim_time_s,
            status=status,
            key=winner,
            win_probability=None,
            reporting_pct=self._pres_reporting_pct(),
            margin_pct=None,
            evidence=evidence,
            retracted=retracted,
            previous_status=prev.status if self._pres_history else None,
            previous_key=prev.key if self._pres_history else None,
        )
        self._pres_history.append(rec)
        self._records.append(rec)
        if winner is not None and status == RaceStatus.CALLED and prev.status != RaceStatus.CALLED:
            log.info("presidency decided", extra={"ctx": {"ticket": winner, "seq": self.seq}})

    def _pres_reporting_pct(self) -> float:
        x = sum(float(r.exp_ballots.sum()) for r in self._pres_races)
        done = sum(float(r.f @ r.exp_ballots) for r in self._pres_races)
        return 100.0 * done / x if x > 0 else 0.0

    def _party_of(self, r: _Race, key: str | None) -> str | None:
        if key is None:
            return None
        p = r.meta.line_parties.get(key)
        return p if p else _INDEPENDENT

    def _seat_tallies(self, races: list[_Race]) -> tuple[dict[str, dict[str, int]], int, int, int]:
        by: dict[str, dict[str, int]] = {}

        def slot(p: str) -> dict[str, int]:
            return by.setdefault(
                p, {"called": 0, "leading": 0, "incumbent": 0, "inc_decided": 0, "inc_all": 0}
            )

        called = leading = uncalled = 0
        for r in races:
            inc = r.meta.incumbent_party
            if inc:
                slot(inc)["incumbent"] += 1
            st = r.state
            if st.status in DECIDED_STATUSES and st.key is not None:
                p = self._party_of(r, st.key)
                assert p is not None
                slot(p)["called"] += 1
                called += 1
                if inc:
                    slot(inc)["inc_decided"] += 1
                    slot(inc)["inc_all"] += 1
            else:
                uncalled += 1
                if r.leader is not None:
                    p = self._party_of(r, r.leader)
                    assert p is not None
                    slot(p)["leading"] += 1
                    leading += 1
                    if inc:
                        slot(inc)["inc_all"] += 1
        return by, called, leading, uncalled

    def _update_house_control(self) -> None:
        if not self._house_races or self._house_control[0] is not None:
            return
        by, *_ = self._seat_tallies(self._house_races)
        maj = self.constitution.house_majority
        for party, v in sorted(by.items()):
            if v["called"] >= maj:
                self._house_control = (party, self.seq)
                log.info("House control decided", extra={"ctx": {"party": party, "seq": self.seq}})
                return

    def _update_senate_control(self) -> None:
        if (not self._senate_races and not self.holdover_senate) or self._senate_control[0] is not None:
            return
        by, *_ = self._seat_tallies(self._senate_races)
        maj = self.constitution.senate_majority
        for party in sorted(set(by) | set(self.holdover_senate)):
            if self.holdover_senate.get(party, 0) + by.get(party, {}).get("called", 0) >= maj:
                self._senate_control = (party, self.seq)
                return

    # ------------------------------------------------------------------ snapshot
    def _line_info(self, key: str) -> dict:
        for r in self._pres_races:
            if key in r.line_keys:
                m = r.meta
                return {
                    "label": m.line_labels.get(key, key),
                    "party": m.line_parties.get(key),
                    "color": m.line_colors.get(key),
                }
        return {"label": key, "party": None, "color": None}

    def _party_color(self, party: str) -> str | None:
        for r in self._races:
            for k, p in r.meta.line_parties.items():
                if p == party and k in r.meta.line_colors:
                    return r.meta.line_colors[k]
        return None

    def _compact(self, r: _Race, full: bool) -> dict:
        d = r.decision
        st = r.state
        called_key = st.key if st.status in DECIDED_STATUSES else None
        lean_key = st.key if st.status == RaceStatus.LEAN else None
        wp_key = st.key or r.leader
        out = {
            "key": r.key,
            "type": r.meta.race_type.value,
            "name": r.meta.name,
            "parent": r.meta.parent,
            "province_code": r.meta.province_code,
            "district_code": r.meta.district_code,
            "municipality_code": r.meta.municipality_code,
            "status": st.status.value,
            "is_manual": st.is_manual,
            "leader": r.leader,
            "called_key": called_key,
            "lean_key": lean_key,
            "win_probability": None if d is None else d.win_probability_of(wp_key),
            "reporting_pct": 0.0 if d is None else round(d.reporting_pct, 3),
            "margin_pct": 0.0 if d is None else round(d.margin_pct, 4),
            "projected_margin_pct": 0.0 if d is None else round(d.projected_margin_pct, 4),
            "math_certain": False if d is None else d.math_certain,
            "votes": dict(zip(r.line_keys, r.totals.tolist(), strict=True)),
        }
        if full and d is not None:
            out.update(
                {
                    "counted_valid": d.counted_valid,
                    "win_probabilities": d.win_probability,
                    "projected_share_mean": d.projected_share_mean,
                    "projected_share_p05": d.projected_share_p05,
                    "projected_share_p95": d.projected_share_p95,
                    "projected_votes": d.projected_votes,
                    "outstanding_ballots_est": round(d.outstanding_ballots_est, 1),
                    "outstanding_ballots_upper": d.outstanding_ballots_upper,
                    "line_labels": {k: r.meta.line_labels.get(k, k) for k in r.line_keys},
                    "line_parties": {k: r.meta.line_parties.get(k) for k in r.line_keys},
                    "line_colors": {k: r.meta.line_colors.get(k) for k in r.line_keys},
                }
            )
        return out

    def _reporting(self) -> dict:
        mask = self.timeline.units_mask
        f = self._f
        x_tot = float(self._unit_expected.sum())
        um = self.frame.unit_muni
        m_rep = np.bincount(um, weights=(f > 0) & mask, minlength=self.frame.n_munis)
        m_done = np.bincount(um, weights=(f >= 1.0) & mask, minlength=self.frame.n_munis)
        active = self._muni_units > 0
        n_units = int(mask.sum())
        reported = int(np.count_nonzero((f >= 1.0) & mask))
        return {
            "pct_expected_ballots": round(100.0 * float(f @ self._unit_expected) / x_tot, 3)
            if x_tot > 0
            else 0.0,
            "units_total": n_units,
            "units_reported": reported,
            "units_partial": int(np.count_nonzero((f > 0) & (f < 1.0) & mask)),
            "pct_units": round(100.0 * reported / n_units, 3) if n_units else 0.0,
            "municipalities_total": int(active.sum()),
            "municipalities_reporting": int(np.count_nonzero(active & (m_rep > 0))),
            "municipalities_complete": int(np.count_nonzero(active & (m_done >= self._muni_units))),
        }

    def _popular_vote(self) -> dict | None:
        if not self._pres_races:
            return None
        votes = dict.fromkeys(self._tickets, 0)
        proj = dict.fromkeys(self._tickets, 0.0)
        outstanding = 0.0
        for r in self._pres_races:
            for k, v in zip(r.line_keys, r.totals.tolist(), strict=True):
                votes[k] += v
            d = r.decision
            if d is not None:
                for k, v in d.projected_votes.items():
                    proj[k] += v
                outstanding += d.outstanding_ballots_est
        tot = sum(votes.values())
        ptot = sum(proj.values())
        lines = []
        for k in self._tickets:
            info = self._line_info(k)
            lines.append(
                {
                    "key": k,
                    **info,
                    "votes": votes[k],
                    "pct": round(100.0 * votes[k] / tot, 3) if tot else 0.0,
                    "projected_votes": round(proj[k], 1),
                    "projected_pct": round(100.0 * proj[k] / ptot, 3) if ptot else 0.0,
                }
            )
        lines.sort(key=lambda d: -d["votes"])
        return {
            "lines": lines,
            "counted_valid": tot,
            "projected_valid": round(ptot, 1),
            "outstanding_ballots_est": round(outstanding, 1),
            "reporting_pct": round(self._pres_reporting_pct(), 3),
        }

    def _president(self) -> dict | None:
        if not self._pres_races:
            return None
        t = self._ev_tallies()
        pv = self._popular_vote() or {"lines": []}
        pv_by_key = {ln["key"]: ln for ln in pv["lines"]}
        tickets = []
        for k in self._tickets:
            info = self._line_info(k)
            tickets.append(
                {
                    "key": k,
                    **info,
                    "votes": pv_by_key.get(k, {}).get("votes", 0),
                    "pct": pv_by_key.get(k, {}).get("pct", 0.0),
                    "ev_decided": t["decided"][k],
                    "ev_leading": t["leading"][k],
                    "ev_max_possible": t["possible"][k],
                }
            )
        tickets.sort(key=lambda d: (-d["ev_decided"], -d["ev_leading"], -d["votes"]))
        return {
            "race_key": PRESIDENT_RACE_KEY,
            "status": self._pres_state.status.value,
            "ev_total": t["total"],
            "ev_needed": self.constitution.presidential_majority,
            "ev_decided_total": sum(t["decided"].values()),
            "ev_uncalled": t["uncalled"],
            "tickets": tickets,
            "winner": self._pres_winner,
            "majority_reached_at_seq": self._pres_majority_seq,
            "contingent_likely": self._contingent and self._pres_winner is None,
            "contingent_likely_at_seq": self._contingent_seq,
        }

    def _provinces(self) -> list[dict]:
        pres_by_prov = {r.meta.province_code: r for r in self._pres_races}
        um, f, mask = self.frame.unit_muni, self._f, self.timeline.units_mask
        m_rep = np.bincount(um, weights=(f > 0) & mask, minlength=self.frame.n_munis)
        m_done = np.bincount(um, weights=(f >= 1.0) & mask, minlength=self.frame.n_munis)
        p_x = np.bincount(
            self.frame.unit_province, weights=self._unit_expected, minlength=self.frame.n_provinces
        )
        p_done = np.bincount(
            self.frame.unit_province, weights=f * self._unit_expected, minlength=self.frame.n_provinces
        )
        out = []
        for p, code in enumerate(self.frame.province_codes):
            munis = np.flatnonzero((self.frame.muni_province == p) & (self._muni_units > 0))
            r = pres_by_prov.get(code)
            row = {
                "code": code,
                "name": self.frame.province_names[p],
                "ev": None if r is None else r.meta.electoral_votes,
                "race_key": None if r is None else r.key,
                "status": None if r is None else r.state.status.value,
                "leader": None if r is None else r.leader,
                "called_key": None if r is None or r.state.status not in DECIDED_STATUSES else r.state.key,
                "win_probability": None,
                "reporting_pct": round(100.0 * float(p_done[p] / p_x[p]), 3) if p_x[p] > 0 else 0.0,
                "margin_pct": None,
                "votes": {},
                "municipalities_total": len(munis),
                "municipalities_reporting": int(np.count_nonzero(m_rep[munis] > 0)),
                "municipalities_complete": int(np.count_nonzero(m_done[munis] >= self._muni_units[munis])),
            }
            if r is not None and r.decision is not None:
                d = r.decision
                row["win_probability"] = d.win_probability_of(r.state.key or r.leader)
                row["reporting_pct"] = round(d.reporting_pct, 3)
                row["margin_pct"] = round(d.margin_pct, 4)
                row["votes"] = dict(zip(r.line_keys, r.totals.tolist(), strict=True))
            out.append(row)
        return out

    def _house(self) -> dict | None:
        if not self._house_races:
            return None
        by, called, leading, uncalled = self._seat_tallies(self._house_races)
        rows = []
        for party, v in by.items():
            rows.append(
                {
                    "party": party,
                    "color": self._party_color(party),
                    "called": v["called"],
                    "leading": v["leading"],
                    "total": v["called"] + v["leading"],
                    "incumbent_seats": v["incumbent"],
                    "net_change_called": v["called"] - v["inc_decided"],
                    "net_change_projected": v["called"] + v["leading"] - v["inc_all"],
                }
            )
        rows.sort(key=lambda d: (-d["called"], -d["total"], d["party"]))
        party, at = self._house_control
        return {
            "seats_total": self.constitution.house_seats,
            "majority": self.constitution.house_majority,
            "races": len(self._house_races),
            "called": called,
            "leading": leading,
            "uncalled": uncalled,
            "by_party": rows,
            "control": party,
            "control_at_seq": at,
        }

    def _senate(self) -> dict | None:
        if not self._senate_races and not self.holdover_senate:
            return None
        by, called, leading, uncalled = self._seat_tallies(self._senate_races)
        rows = []
        for party in sorted(set(by) | set(self.holdover_senate)):
            v = by.get(party, {"called": 0, "leading": 0})
            h = self.holdover_senate.get(party, 0)
            rows.append(
                {
                    "party": party,
                    "color": self._party_color(party),
                    "holdover": h,
                    "called": v["called"],
                    "leading": v["leading"],
                    "total_decided": h + v["called"],
                    "total_projected": h + v["called"] + v["leading"],
                }
            )
        rows.sort(key=lambda d: (-d["total_decided"], -d["total_projected"], d["party"]))
        party, at = self._senate_control
        return {
            "seats_total": self.constitution.senate_seats,
            "majority": self.constitution.senate_majority,
            "up": len(self._senate_races),
            "holdover_total": sum(self.holdover_senate.values()),
            "called": called,
            "leading": leading,
            "uncalled": uncalled,
            "by_party": rows,
            "control": party,
            "control_at_seq": at,
        }

    def _governors(self) -> list[dict]:
        out = []
        for r in self._gov_races:
            d = r.decision
            st = r.state
            called_key = st.key if st.status in DECIDED_STATUSES else None
            out.append(
                {
                    "key": r.key,
                    "province_code": r.meta.province_code,
                    "status": st.status.value,
                    "leader": r.leader,
                    "called_key": called_key,
                    "party": self._party_of(r, called_key or r.leader),
                    "reporting_pct": 0.0 if d is None else round(d.reporting_pct, 3),
                    "margin_pct": 0.0 if d is None else round(d.margin_pct, 4),
                    "win_probability": None if d is None else d.win_probability_of(st.key or r.leader),
                }
            )
        return out

    def _recent_calls(self) -> list[dict]:
        n = self.config.snapshot.recent_calls
        out: list[dict] = []
        for rec in reversed(self._records):
            if len(out) >= n:
                break
            notable = rec.status in (RaceStatus.PROJECTED, RaceStatus.CALLED, RaceStatus.RECOUNT) or (
                rec.retracted or rec.is_manual
            )
            if rec.status == RaceStatus.FINAL:
                notable = not (rec.previous_status in CALL_STATUSES and rec.previous_key == rec.key)
            if notable:
                out.append(rec.to_dict(include_evidence=False))
        return out

    def _municipalities(self) -> list[dict]:
        um, f, mask = self.frame.unit_muni, self._f, self.timeline.units_mask
        M = self.frame.n_munis
        m_done = np.bincount(um, weights=f * self._unit_expected, minlength=M)
        u_rep = np.bincount(um, weights=(f >= 1.0) & mask, minlength=M)
        headline: dict[int, tuple[str, str | None, float]] = {}
        heads = self._pres_races or self._gov_races or self._races
        for r in heads:
            n_cl = len(r.cluster_muni)
            L = len(r.line_keys)
            flat = (r.cluster[:, None] * L + np.arange(L)).ravel()
            cm = np.bincount(flat, weights=r.counted.ravel(), minlength=n_cl * L).reshape(n_cl, L)
            tot = cm.sum(axis=1)
            for c, m in enumerate(r.cluster_muni.tolist()):
                if m in headline:
                    continue
                if tot[c] > 0:
                    o = np.argsort(-cm[c], kind="stable")
                    lead = r.line_keys[int(o[0])]
                    marg = 100.0 * float(cm[c, o[0]] - (cm[c, o[1]] if L > 1 else 0)) / float(tot[c])
                    headline[m] = (r.key, lead, marg)
                else:
                    headline[m] = (r.key, None, 0.0)
        out = []
        for m in np.flatnonzero(self._muni_units > 0).tolist():
            rk, lead, marg = headline.get(m, (None, None, 0.0))
            out.append(
                {
                    "code": self.frame.muni_codes[m],
                    "name": self.frame.muni_names[m],
                    "province_code": self.frame.province_codes[int(self.frame.muni_province[m])],
                    "reporting_pct": round(100.0 * float(m_done[m] / self._muni_expected[m]), 3)
                    if self._muni_expected[m] > 0
                    else (100.0 if u_rep[m] >= self._muni_units[m] else 0.0),
                    "units_total": int(self._muni_units[m]),
                    "units_reported": int(u_rep[m]),
                    "race_key": rk,
                    "leader": lead,
                    "margin_pct": round(marg, 4),
                }
            )
        return out

    def snapshot(self, detail: str = "summary") -> dict:
        """JSON-serialisable state of the night (schema in the module docstring)."""
        if detail not in ("summary", "full"):
            raise ValueError("detail must be 'summary' or 'full'")
        full = detail == "full"
        n = self.timeline.n_events
        status = "polls_closed" if self.seq == 0 else ("complete" if self.seq >= n else "counting")
        lc_n = self.config.snapshot.recent_lead_changes
        snap = {
            "data_category": "SIMULATED",
            "seed": self.seed,
            "seq": self.seq,
            "total_events": n,
            "sim_time_s": float(self.sim_time_s),
            "clock": self.timeline.local_clock(self.sim_time_s),
            "timestamp": self.timeline.local_datetime(self.sim_time_s).isoformat(),
            "status": status,
            "reporting": self._reporting(),
            "popular_vote": self._popular_vote(),
            "president": self._president(),
            "provinces": self._provinces(),
            "house": self._house(),
            "senate": self._senate(),
            "governors": self._governors(),
            "races": [self._compact(r, full) for r in self._races],
            "recent_calls": self._recent_calls(),
            "lead_changes": {
                "count": len(self._lead_changes),
                "recent": [lc.to_dict() for lc in self._lead_changes[-lc_n:]][::-1] if lc_n else [],
            },
        }
        if full:
            snap["municipalities"] = self._municipalities()
        return snap

    def race_detail(self, race_key: str) -> dict:
        """Everything about one race: meta, current decision + evidence, full call history,
        lead changes, per-municipality counted results."""
        if race_key == PRESIDENT_RACE_KEY and race_key not in self._by_key:
            return {
                "data_category": "SIMULATED",
                "key": PRESIDENT_RACE_KEY,
                "type": RaceType.PRESIDENT.value,
                "president": self._president(),
                "history": [rec.to_dict() for rec in self._pres_history],
            }
        r = self._race(race_key)
        n_cl = len(r.cluster_muni)
        L = len(r.line_keys)
        flat = (r.cluster[:, None] * L + np.arange(L)).ravel()
        cm = np.bincount(flat, weights=r.counted.ravel(), minlength=n_cl * L).reshape(n_cl, L)
        cx = np.bincount(r.cluster, weights=r.exp_ballots, minlength=n_cl)
        cd = np.bincount(r.cluster, weights=r.f * r.exp_ballots, minlength=n_cl)
        munis = []
        for c, m in enumerate(r.cluster_muni.tolist()):
            munis.append(
                {
                    "code": self.frame.muni_codes[m],
                    "name": self.frame.muni_names[m],
                    "reporting_pct": round(100.0 * float(cd[c] / cx[c]), 3) if cx[c] > 0 else 0.0,
                    "votes": dict(zip(r.line_keys, cm[c].astype(np.int64).tolist(), strict=True)),
                }
            )
        meta = r.meta
        return {
            "data_category": "SIMULATED",
            "key": r.key,
            "type": meta.race_type.value,
            "name": meta.name,
            "parent": meta.parent,
            "province_code": meta.province_code,
            "district_code": meta.district_code,
            "municipality_code": meta.municipality_code,
            "electoral_votes": meta.electoral_votes,
            "incumbent_party": meta.incumbent_party,
            "lines": [
                {
                    "key": k,
                    "label": meta.line_labels.get(k, k),
                    "party": meta.line_parties.get(k),
                    "color": meta.line_colors.get(k),
                    "votes": int(v),
                }
                for k, v in zip(r.line_keys, r.totals.tolist(), strict=True)
            ],
            "state": {
                "status": r.state.status.value,
                "key": r.state.key,
                "seq": r.state.seq,
                "is_manual": r.state.is_manual,
            },
            "leader": r.leader,
            "decision": None if r.decision is None else r.decision.to_dict(include_evidence=True),
            "history": [rec.to_dict() for rec in r.history],
            "lead_changes": [lc.to_dict() for lc in r.lead_changes],
            "municipalities": munis,
        }

    def municipality_detail(self, code: str) -> dict:
        """Reporting events so far and counted results of every race in one municipality."""
        try:
            m = self.frame.muni_index(code)
        except KeyError:
            raise NotFoundError(f"unknown municipality {code}") from None
        tl = self.timeline
        ev_idx = np.flatnonzero(tl.muni[: self.seq] == m)
        events = [
            {
                "seq": int(i + 1),
                "sim_time_s": float(tl.sim_time_s[i]),
                "clock": tl.local_clock(float(tl.sim_time_s[i])),
                "ballots": int(tl.ballots[i]),
                "municipality_fraction_after": round(float(tl.muni_fraction_after[i]), 6),
                "batch_index": int(tl.batch_index[i]),
                "batches": int(tl.batches_in_muni[i]),
                "units": int(tl.unit_ptr[i + 1] - tl.unit_ptr[i]),
            }
            for i in ev_idx.tolist()
        ]
        units = np.flatnonzero((self.frame.unit_muni == m) & tl.units_mask)
        f = self._f[units]
        x = self._unit_expected[units]
        races = []
        for r in self._races:
            c = np.flatnonzero(r.cluster_muni == m)
            if c.size == 0:
                continue
            rows = np.flatnonzero(r.cluster == int(c[0]))
            votes = r.counted[rows].sum(axis=0)
            tot = int(votes.sum())
            o = np.argsort(-votes, kind="stable")
            races.append(
                {
                    "key": r.key,
                    "type": r.meta.race_type.value,
                    "status": r.state.status.value,
                    "votes": dict(zip(r.line_keys, votes.tolist(), strict=True)),
                    "leader": r.line_keys[int(o[0])] if tot > 0 else None,
                    "reporting_pct": round(
                        100.0 * float(r.f[rows] @ r.exp_ballots[rows]) / float(r.exp_ballots[rows].sum()), 3
                    )
                    if r.exp_ballots[rows].sum() > 0
                    else 0.0,
                }
            )
        return {
            "data_category": "SIMULATED",
            "code": code,
            "name": self.frame.muni_names[m],
            "province_code": self.frame.province_codes[int(self.frame.muni_province[m])],
            "reporting_pct": round(100.0 * float(f @ x) / float(x.sum()), 3) if x.sum() > 0 else 0.0,
            "units_total": len(units),
            "units_reported": int(np.count_nonzero(f >= 1.0)),
            "units_partial": int(np.count_nonzero((f > 0) & (f < 1.0))),
            "events": events,
            "races": races,
        }
