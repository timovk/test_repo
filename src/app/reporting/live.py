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
                evaluated_seq, votes: {key: int}}  (+ with detail="full": counted_valid,
                win_probabilities, projected_share_mean/p05/p95, projected_votes,
                outstanding_ballots_est, outstanding_ballots_upper, line_labels, line_parties,
                line_colors)],
      "recent_calls": [call record without evidence], "lead_changes": {"count", "recent"},
      # detail="full" only:
      "municipalities": [{code, name, province_code, reporting_pct, units_total,
                          units_reported, leader, margin_pct, race_key}]
    }

Counted quantities (``votes``, ``reporting_pct``, ``margin_pct``, ``counted_valid``, ``leader``)
are always current; model outputs (``win_probability``, projections, ``math_certain``) are those
of the race's last evaluation, at ``evaluated_seq`` (see the evaluation schedule).  ``control``
is the party whose *called* seats (plus Senate holdovers) currently reach the majority — a
retraction below it clears it — and ``control_at_seq`` the seq at which it reached it.

``race_detail(race_key)`` returns meta, the current decision (with evidence), the complete call
history (with evidence), lead changes and a per-municipality breakdown; ``municipality_detail
(code)`` returns reporting events so far and the counted result of every race in the
municipality.
"""

from __future__ import annotations

from bisect import insort
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
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
    "MANUAL_STATUSES",
    "CallRecord",
    "LeadChange",
    "ManualCall",
    "NightEngine",
    "PlaybackClock",
    "PlaybackState",
    "RaceMeta",
    "manual_calls_from_history",
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
#: Statuses a producer may set by hand.  FINAL and RECOUNT are reached only by the count itself
#: (100 % reported); a manual FINAL would otherwise lock a race before its votes are counted.
MANUAL_STATUSES: frozenset[RaceStatus] = frozenset(
    {
        RaceStatus.POLLS_CLOSED,
        RaceStatus.TOO_EARLY,
        RaceStatus.TOO_CLOSE,
        RaceStatus.LEAN,
        RaceStatus.PROJECTED,
        RaceStatus.CALLED,
    }
)
#: Manual statuses that name a line (the others carry no key).
_KEYED_STATUSES: frozenset[RaceStatus] = frozenset({RaceStatus.LEAN, RaceStatus.PROJECTED, RaceStatus.CALLED})


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
    """A change of a race's published state (maps onto ``race_call``).

    ``seq``/``sim_time_s``/``timestamp`` locate the change in the night (``timestamp`` is the
    tz-aware simulated local time → ``race_call.called_at``); ``win_probability`` is the model
    probability of ``key`` (or of the counted leader when no line is named); ``margin_pct`` is the
    counted leader's margin.  A record produced by clearing a manual override carries
    ``evidence["manual_cleared"] = True`` and the clear's reason in ``override_reason``, so
    :func:`manual_calls_from_history` can rebuild every :class:`ManualCall` from stored rows.
    """

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
    timestamp: datetime | None = None

    @property
    def clears_manual(self) -> bool:
        """True when this record was produced by clearing a manual override."""
        return bool(self.evidence.get("manual_cleared", False))

    def to_dict(self, include_evidence: bool = True) -> dict:
        d = {
            "race_key": self.race_key,
            "seq": self.seq,
            "sim_time_s": self.sim_time_s,
            "timestamp": None if self.timestamp is None else self.timestamp.isoformat(),
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
    ``status`` must be one of :data:`MANUAL_STATUSES`; LEAN / PROJECTED / CALLED need a ``key``.
    ``sim_time_s`` is the simulated time the override was made at (the playback clock may be
    between two events); a replay stamps the override's records with it, so they are identical
    to the live ones.  ``None`` (e.g. a hand-written override) uses the time of event ``seq``.
    It does not take part in equality.
    """

    race_key: str
    seq: int
    status: RaceStatus | None
    key: str | None = None
    reason: str | None = None
    sim_time_s: float | None = field(default=None, compare=False)


def manual_calls_from_history(records: Iterable[CallRecord | Mapping]) -> list[ManualCall]:
    """Rebuild the :class:`ManualCall` list of a night from its call records.

    Accepts :class:`CallRecord` objects or mappings with the keys of :meth:`CallRecord.to_dict`
    (``race_key``, ``seq``, ``sim_time_s``, ``status``, ``key``, ``is_manual``,
    ``override_reason`` and ``evidence``), e.g. ``race_call`` rows mapped back by services.
    Manual records become overrides; records carrying ``evidence["manual_cleared"]`` become
    clears (``status=None``).  Order is preserved (sort the input by ``seq`` and insertion).
    """
    out: list[ManualCall] = []
    for rec in records:
        if isinstance(rec, CallRecord):
            row: Mapping = {
                "race_key": rec.race_key,
                "seq": rec.seq,
                "sim_time_s": rec.sim_time_s,
                "status": rec.status,
                "key": rec.key,
                "is_manual": rec.is_manual,
                "override_reason": rec.override_reason,
                "evidence": rec.evidence,
            }
        else:
            row = rec
        evidence = row.get("evidence") or {}
        t = row.get("sim_time_s")
        common = {
            "race_key": str(row["race_key"]),
            "seq": int(row["seq"]),
            "reason": row.get("override_reason"),
            "sim_time_s": None if t is None else float(t),
        }
        if row.get("is_manual"):
            out.append(ManualCall(status=RaceStatus(row["status"]), key=row.get("key"), **common))
        elif isinstance(evidence, Mapping) and evidence.get("manual_cleared"):
            out.append(ManualCall(status=None, key=None, **common))
    return out


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
    #: The caller's per-race invariants (normalised expectation, clusters); they depend only on
    #: arrays that never change, so the cache survives resets, rewinds and checkpoints.
    caller_cache: dict = field(default_factory=dict, repr=False)

    @property
    def locked(self) -> bool:
        return self.state.status in LOCKED_STATUSES

    def reporting_pct(self) -> float:
        """Current reporting (% of expected ballots), exact — not the last evaluation's value."""
        if self.x_total > 0:
            return min(100.0, 100.0 * float(self.f @ self.exp_ballots) / self.x_total)
        return 100.0 * float(self.f.mean()) if len(self.f) else 0.0

    def counted_margin(self) -> tuple[int, float]:
        """Current counted margin of the top line over the runner-up: ``(votes, pp of valid)``."""
        tot = self.totals
        valid = int(tot.sum())
        if valid <= 0:
            return 0, 0.0
        top2 = np.sort(tot)[::-1][:2]
        margin = int(top2[0] - (top2[1] if len(top2) > 1 else 0))
        return margin, 100.0 * margin / valid

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
            cache=self.caller_cache,
        )


@dataclass
class _Checkpoint:
    """Complete mutable state of a :class:`NightEngine` after event ``seq`` (fast rewinds)."""

    seq: int
    sim_time_s: float
    f: np.ndarray
    races: list[tuple]
    records: list[CallRecord]
    lead_changes: list[LeadChange]
    national: tuple


# --------------------------------------------------------------------------- engine
class NightEngine:
    """Deterministic live election night over a timeline and final results.

    Args:
        frame: the geography the races and timeline refer to.
        timeline: reporting events (:func:`~app.reporting.timeline.generate_timeline`).
        races: race key → final :class:`RaceVotes` (with ``expected_shares``/``expected_turnout``
            = pre-election expectation, used by the caller).  A national ``PRESIDENT`` race
            next to ``PRESIDENT_PROVINCE`` races is not called: the presidency ('PRES') follows
            the Electoral College (see :attr:`derived_races`).
        race_meta: race key → :class:`RaceMeta`.
        config: night configuration (default ``config/night.yaml``).
        seed: root seed of the night (call draws use ``make_rng(seed, "call", race, seq)``).
        holdover_senate: party → Senate seats not up in this election.
        recount_check: injected automatic-recount rule (fallback: ``recount.margin_pct``).
        manual_calls: manual overrides to apply at their ``seq`` (replayed on rebuild).

    Moving backwards restores the nearest state checkpoint (taken every
    :attr:`checkpoint_every` events while playing forward) and replays from there, so a rewind
    costs at most ``checkpoint_every`` events instead of the whole night.
    """

    #: Events between two state checkpoints (a few MB each on the real geography).
    checkpoint_every: int = 250

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
        for key in races:
            if not isinstance(race_meta[key], RaceMeta):
                raise ElectionNightError(
                    f"race_meta[{key!r}] must be a RaceMeta (race type, electoral votes, lines …), "
                    f"got {type(race_meta[key]).__name__}"
                )
        # With an Electoral College the national presidency is *derived* from the province races
        # (published under 'PRES').  The national popular-vote race the simulation also produces
        # must not be called as a plurality race: its calls would be published under the same
        # key and could name the popular-vote winner who loses the Electoral College.
        ec = any(race_meta[k].race_type == RaceType.PRESIDENT_PROVINCE for k in races)
        self.derived_races: list[str] = sorted(
            k for k in races if ec and race_meta[k].race_type == RaceType.PRESIDENT
        )
        if ec and PRESIDENT_RACE_KEY in races and PRESIDENT_RACE_KEY not in self.derived_races:
            raise ElectionNightError(
                f"race key {PRESIDENT_RACE_KEY!r} is reserved for the Electoral College result"
            )
        if self.derived_races:
            log.info(
                "national presidential race(s) %s follow the Electoral College; not called by popular vote",
                self.derived_races,
            )
        self._races: list[_Race] = []
        self._by_key: dict[str, _Race] = {}
        for key in sorted(races):
            if key in self.derived_races:
                continue
            r = self._build_race(len(self._races), key, races[key], race_meta[key])
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
        for i, m in enumerate(self._manual_all):
            if m.race_key not in self._by_key:
                raise ElectionNightError(f"manual call for unknown race {m.race_key}")
            if not 0 <= int(m.seq) <= timeline.n_events:
                raise ElectionNightError(f"manual call for {m.race_key} at seq {m.seq} is outside the night")
            key = self._check_manual(self._by_key[m.race_key], m.status, m.key)
            status = None if m.status is None else RaceStatus(m.status)
            if key != m.key or type(m.status) is not type(status) or int(m.seq) != m.seq:
                self._manual_all[i] = ManualCall(m.race_key, int(m.seq), status, key, m.reason, m.sim_time_s)
        self._checkpoints: dict[int, _Checkpoint] = {}
        self.reset()

    # ------------------------------------------------------------------ construction
    def _build_race(self, idx: int, key: str, rv: RaceVotes, meta: RaceMeta) -> _Race:
        units = np.asarray(rv.unit_index, dtype=np.int64)
        if units.size and not self.timeline.units_mask[units].all():
            raise ElectionNightError(f"{key}: some units are not covered by the timeline")
        n, L = rv.votes.shape
        if len(rv.line_keys) != L:
            raise ElectionNightError(f"{key}: line_keys do not match the votes array")
        if L == 0:
            raise ElectionNightError(f"{key}: race has no ballot lines")
        if len(set(rv.line_keys)) != L:
            raise ElectionNightError(f"{key}: duplicate line keys")
        for name in ("blank", "invalid", "eligible"):
            if np.shape(getattr(rv, name)) != (n,):
                raise ElectionNightError(f"{key}: {name} must have shape ({n},)")
        if len(units) != n:
            raise ElectionNightError(f"{key}: unit_index must have {n} entries")
        if rv.expected_shares is not None and np.shape(rv.expected_shares) != (n, L):
            raise ElectionNightError(f"{key}: expected_shares must have shape ({n}, {L})")
        if rv.expected_turnout is not None and np.shape(rv.expected_turnout) != (n,):
            raise ElectionNightError(f"{key}: expected_turnout must have shape ({n},)")
        if (rv.votes < 0).any() or (np.asarray(rv.blank) < 0).any() or (np.asarray(rv.invalid) < 0).any():
            raise ElectionNightError(f"{key}: negative vote counts")
        # the mathematical-certainty bound (eligible − counted ballots) needs ballots ≤ eligible
        cast = rv.votes.sum(axis=1) + np.asarray(rv.blank) + np.asarray(rv.invalid)
        if (cast > np.asarray(rv.eligible)).any():
            raise ElectionNightError(f"{key}: ballots exceed eligible voters in some units")
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
        if not np.isfinite(exp_turnout).all() or not np.isfinite(exp_shares).all():
            log.warning("race %s has non-finite expectations; those units use flat priors", key)
        exp_turnout = np.where(np.isfinite(exp_turnout), np.clip(exp_turnout, 0.0, 1.0), 0.78)
        exp_ballots = exp_turnout * np.asarray(rv.eligible, dtype=np.float64)
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
            exp_ballots=exp_ballots,
            f=np.zeros(n),
            counted=np.zeros((n, L), dtype=np.int64),
            counted_ballots=np.zeros(n, dtype=np.int64),
            totals=np.zeros(L, dtype=np.int64),
            x_total=float(exp_ballots.sum()),
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
        """Counted votes per line; for 'PRES' (Electoral College) the national popular vote."""
        if race_key == PRESIDENT_RACE_KEY and race_key not in self._by_key and self._pres_races:
            votes = dict.fromkeys(self._tickets, 0)
            for r in self._pres_races:
                for k, v in zip(r.line_keys, r.totals.tolist(), strict=True):
                    votes[k] += v
            return votes
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
        """Move to exactly ``seq`` (rewinding by deterministic replay from the nearest earlier
        state checkpoint when ``seq`` < current)."""
        seq = int(seq)
        if not 0 <= seq <= self.timeline.n_events:
            raise ElectionNightError(f"seq {seq} out of range 0..{self.timeline.n_events}")
        if seq < self.seq:
            self._rewind(seq)
        every = max(int(self.checkpoint_every), 1)
        while self.seq < seq:
            self._apply_event(self.seq + 1)
            if self.seq % every == 0 and self.seq not in self._checkpoints:
                self._checkpoints[self.seq] = self._capture()
        if seq > 0:
            self.sim_time_s = max(self.sim_time_s, float(self.timeline.sim_time_s[seq - 1]))
        return self.seq

    def _capture(self) -> _Checkpoint:
        races = [
            (
                r.f.copy(),
                r.counted.copy(),
                r.counted_ballots.copy(),
                r.totals.copy(),
                r.fx,
                r.n_incomplete,
                r.last_eval_share,
                r.state,
                r.decision,
                r.leader,
                list(r.history),
                list(r.lead_changes),
            )
            for r in self._races
        ]
        national = (
            self._pres_state,
            list(self._pres_history),
            self._pres_winner,
            self._pres_majority_seq,
            self._contingent_seq,
            self._contingent,
            self._house_control,
            self._senate_control,
        )
        return _Checkpoint(
            self.seq,
            self.sim_time_s,
            self._f.copy(),
            races,
            list(self._records),
            list(self._lead_changes),
            national,
        )

    def _restore(self, cp: _Checkpoint) -> None:
        self.seq, self.sim_time_s = cp.seq, cp.sim_time_s
        self._f = cp.f.copy()
        for r, st in zip(self._races, cp.races, strict=True):
            f, counted, cballots, totals = st[:4]
            r.f[:], r.counted[:], r.counted_ballots[:], r.totals[:] = f, counted, cballots, totals
            r.fx, r.n_incomplete, r.last_eval_share, r.state, r.decision, r.leader = st[4:10]
            r.history, r.lead_changes = list(st[10]), list(st[11])
        self._records, self._lead_changes = list(cp.records), list(cp.lead_changes)
        (
            self._pres_state,
            pres_history,
            self._pres_winner,
            self._pres_majority_seq,
            self._contingent_seq,
            self._contingent,
            self._house_control,
            self._senate_control,
        ) = cp.national
        self._pres_history = list(pres_history)
        # every override up to the checkpoint is already in the restored state
        self._manual_pending = [m for m in self._manual_all if m.seq > cp.seq]

    def _rewind(self, seq: int) -> None:
        """Go back to the latest checkpoint at or before ``seq`` (or to polls closing)."""
        usable = [c for c in self._checkpoints if c <= seq]
        if usable:
            self._restore(self._checkpoints[max(usable)])
        else:
            self.reset()

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
    def _evaluate(self, r: _Race, cleared: ManualCall | None = None) -> bool:
        """Evaluate one race at the current seq; returns True when its published state changed.

        ``cleared`` is the manual clear that triggered the evaluation (its record is marked)."""
        d = self.caller.evaluate(r.progress(), self.seed, self.seq, r.state)
        r.decision = d
        prev = r.state
        if prev.is_manual and d.status not in LOCKED_STATUSES:
            return False  # the manual override stays published; the model opinion is in r.decision
        changed = (d.status, d.winner_key) != (prev.status, prev.key) or not r.history
        if not changed:
            return False
        r.state = CallState(status=d.status, key=d.winner_key, seq=self.seq)
        evidence = d.evidence
        if cleared is not None:
            evidence = {**evidence, "manual_cleared": True, "manual_reason": cleared.reason}
        rec = CallRecord(
            race_key=r.key,
            seq=self.seq,
            sim_time_s=self.sim_time_s,
            status=d.status,
            key=d.winner_key,
            win_probability=d.win_probability_of(d.winner_key or d.leader_key),
            reporting_pct=d.reporting_pct,
            margin_pct=d.margin_pct,
            evidence=evidence,
            override_reason=None if cleared is None else cleared.reason,
            retracted=d.retracted,
            previous_status=prev.status if r.history else None,
            previous_key=prev.key if r.history else None,
            timestamp=self._local_now(),
        )
        r.history.append(rec)
        self._records.append(rec)
        return True

    def _local_now(self) -> datetime:
        """Simulated local wall-clock time (tz-aware) of the engine's current ``sim_time_s``."""
        return self.timeline.local_datetime(self.sim_time_s)

    # ------------------------------------------------------------------ manual overrides
    def _check_manual(self, r: _Race, status: RaceStatus | None, key: str | None) -> str | None:
        """Validate a manual override; returns the key to store (None for keyless statuses)."""
        if status is None:
            return None
        status = RaceStatus(status)
        if status not in MANUAL_STATUSES:
            allowed = sorted(s.value for s in MANUAL_STATUSES)
            raise ElectionNightError(
                f"{r.key}: {status.value} cannot be set manually (allowed: {allowed}); "
                "FINAL/RECOUNT are reached when the count is complete"
            )
        if status in _KEYED_STATUSES:
            if key is None:
                raise ElectionNightError(f"{r.key}: a manual {status.value} needs a line key")
            if key not in r.line_keys:
                raise ElectionNightError(f"{r.key}: unknown line {key}")
            return key
        return None

    def apply_manual_call(
        self, race_key: str, status: RaceStatus | None, key: str | None = None, reason: str | None = None
    ) -> CallRecord | None:
        """Apply a manual override now (``status=None`` clears it).  The override is kept in
        :attr:`manual_calls` so a rebuilt engine replays it at the same ``seq``.

        ``status`` must be one of :data:`MANUAL_STATUSES` (LEAN / PROJECTED / CALLED need a
        ``key`` of the race; other statuses carry no key).  Returns the published record, or
        None when nothing changed (the race is already FINAL/RECOUNT, or a clear without an
        active override); such no-ops are not added to :attr:`manual_calls`."""
        r = self._race(race_key)
        key = self._check_manual(r, status, key)
        m = ManualCall(
            race_key=race_key,
            seq=self.seq,
            status=None if status is None else RaceStatus(status),
            key=key,
            reason=reason,
            sim_time_s=float(self.sim_time_s),
        )
        rec = self._apply_manual(m)
        if rec is not None:  # no-ops (locked race, clear without override) are not replayed
            insort(self._manual_all, m, key=lambda c: c.seq)
            # checkpoints taken at or after this seq do not contain the new override
            self._checkpoints = {c: cp for c, cp in self._checkpoints.items() if c < self.seq}
        return rec

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
        # records of the override carry the time it was made at (identical live and on replay)
        event_time = self.sim_time_s
        if m.sim_time_s is not None:
            self.sim_time_s = float(m.sim_time_s)
        try:
            prev = r.state
            if m.status is None:
                if not prev.is_manual:
                    return None
                r.state = CallState(prev.status, prev.key, prev.seq, is_manual=False)
                before = len(r.history)
                self._evaluate(r, cleared=m)
                if len(r.history) > before:
                    rec = r.history[-1]
                else:
                    rec = self._manual_record(r, m, prev, is_manual=False)
                self._refresh_after_change(r)
                return rec
            r.state = CallState(m.status, m.key, self.seq, is_manual=True)
            rec = self._manual_record(r, m, prev, is_manual=True)
            self._refresh_after_change(r)
            return rec
        finally:
            self.sim_time_s = event_time

    def _manual_record(self, r: _Race, m: ManualCall, prev: CallState, *, is_manual: bool) -> CallRecord:
        d = r.decision
        model_ev = {} if d is None else {"model_status": d.status.value, "model_key": d.winner_key}
        margin_votes, margin_pct = r.counted_margin()
        evidence = {
            "basis": "manual" if is_manual else "manual_cleared",
            "data_category": "SIMULATED",
            "reason": m.reason,
            **model_ev,
            "model_win_probability": {} if d is None else d.win_probability,
            "model_seq": None if d is None else d.seq,
            "counted": {"votes": dict(zip(r.line_keys, r.totals.tolist(), strict=True))},
            "leader": {"key": r.leader, "margin_votes": margin_votes, "margin_pct": round(margin_pct, 4)},
        }
        if not is_manual:
            evidence.update({"manual_cleared": True, "manual_reason": m.reason})
        rec = CallRecord(
            race_key=r.key,
            seq=self.seq,
            sim_time_s=self.sim_time_s,
            status=r.state.status,
            key=r.state.key,
            win_probability=None if d is None else d.win_probability_of(r.state.key or r.leader),
            reporting_pct=r.reporting_pct(),
            margin_pct=margin_pct,
            evidence=evidence,
            is_manual=is_manual,
            override_reason=m.reason,
            previous_status=prev.status,
            previous_key=prev.key,
            timestamp=self._local_now(),
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
        # the previously declared winner lost the majority (a province call was retracted)
        lost = prev.status in (RaceStatus.CALLED, RaceStatus.FINAL) and prev.key not in (None, winner)
        retracted = lost
        if winner is not None:
            status = RaceStatus.FINAL if all_locked and ev_final >= maj else RaceStatus.CALLED
        elif lost or contingent or prev.status == RaceStatus.TOO_CLOSE:
            # like a race, the presidency never goes back from TOO_CLOSE to TOO_EARLY
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
            timestamp=self._local_now(),
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
        """Party whose called (decided) seats reach ``house_majority`` — recomputed after every
        House state change, so a retraction below the majority clears it again."""
        if not self._house_races:
            return
        by, *_ = self._seat_tallies(self._house_races)
        maj = self.constitution.house_majority
        party = next((p for p, v in sorted(by.items()) if v["called"] >= maj), None)
        self._house_control = self._control_change("House", self._house_control, party)

    def _update_senate_control(self) -> None:
        """Party whose holdovers + called seats reach ``senate_majority`` (recomputed)."""
        if not self._senate_races and not self.holdover_senate:
            return
        by, *_ = self._seat_tallies(self._senate_races)
        maj = self.constitution.senate_majority
        party = next(
            (
                p
                for p in sorted(set(by) | set(self.holdover_senate))
                if self.holdover_senate.get(p, 0) + by.get(p, {}).get("called", 0) >= maj
            ),
            None,
        )
        self._senate_control = self._control_change("Senate", self._senate_control, party)

    def _control_change(
        self, chamber: str, current: tuple[str | None, int | None], party: str | None
    ) -> tuple[str | None, int | None]:
        """``(party, seq at which it reached the majority)``; unchanged while the same party holds it."""
        if party == current[0]:
            return current
        if party is not None:
            log.info(f"{chamber} control decided", extra={"ctx": {"party": party, "seq": self.seq}})
        else:
            log.info(f"{chamber} control retracted", extra={"ctx": {"party": current[0], "seq": self.seq}})
        return (party, self.seq if party is not None else None)

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
            # counted quantities are live; model outputs are those of the last evaluation
            "reporting_pct": round(r.reporting_pct(), 3),
            "margin_pct": round(r.counted_margin()[1], 4),
            "projected_margin_pct": 0.0 if d is None else round(d.projected_margin_pct, 4),
            "math_certain": False if d is None else d.math_certain,
            "evaluated_seq": None if d is None else d.seq,
            "votes": dict(zip(r.line_keys, r.totals.tolist(), strict=True)),
        }
        if full and d is not None:
            out.update(
                {
                    "counted_valid": int(r.totals.sum()),
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
                # the projection is from the last evaluation: never below what is counted now
                for k, v in zip(r.line_keys, r.totals.tolist(), strict=True):
                    proj[k] += max(float(d.projected_votes.get(k, 0.0)), float(v))
                outstanding += d.outstanding_ballots_est
            else:
                for k, v in zip(r.line_keys, r.totals.tolist(), strict=True):
                    proj[k] += float(v)
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
                "ev": None if r is None or r.meta.electoral_votes is None else int(r.meta.electoral_votes),
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
                row["reporting_pct"] = round(r.reporting_pct(), 3)
                row["margin_pct"] = round(r.counted_margin()[1], 4)
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
                    "reporting_pct": round(r.reporting_pct(), 3),
                    "margin_pct": round(r.counted_margin()[1], 4),
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
            "electoral_votes": None if meta.electoral_votes is None else int(meta.electoral_votes),
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
