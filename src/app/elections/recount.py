"""Automatic recounts.

:func:`needs_recount` applies the thresholds of ``config/recount.yaml`` (:class:`RecountConfig`):
a race is recounted automatically when it is an exact tie, or when its margin is within
``margin_pct`` percentage points of valid votes and/or within ``margin_votes`` votes (combined
with ``or`` / ``and``).  Proportional contests (councils, provincial legislatures) and the
national presidential parent race have no automatic recount — the Electoral-College contests
(``PRESIDENT_PROVINCE``) do.

:func:`perform_recount` simulates a *recount of the same ballots*, never a new election: a
seeded sample of units (polling places) is re-examined and most counts are confirmed; a few
units receive small, individually recorded corrections of 1–``max_delta`` ballots:

* ``misread tally`` — ballots moved from one line to another (valid votes unchanged);
* ``uncounted ballot found`` — ballots found and added to a line (ballots cast +, never above
  eligible voters);
* ``ballot ruled invalid`` — ballots moved from a line to the invalid pile;
* ``invalid ballot ruled valid`` — ballots moved from the invalid pile to a line.

Every correction is a :class:`RecountAdjustment` (unit, line, before, after, delta, reason); all
counts stay non-negative, ``RaceVotes.check()`` holds and ballots cast never exceed eligible
voters.  If the recounted race is still an exact tie it is decided by seeded lot
(``decided_by='lot'``), otherwise the recounted result stands (``decided_by='recount'``).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import load_config
from app.core.constitution import RaceType
from app.core.errors import ElectionError
from app.core.logging import get_logger
from app.core.rng import make_rng
from app.elections.tabulation import TabulatedRaceResult, tabulate
from app.elections.types import RaceVotes, TabulatedRace

log = get_logger(__name__)

#: ``line_index`` of adjustments that change the invalid pile.
INVALID_PILE = -1
INVALID_KEY = "<invalid>"

MISREAD = "misread tally"
UNCOUNTED = "uncounted ballot found"
RULED_INVALID = "ballot ruled invalid"
RULED_VALID = "invalid ballot ruled valid"
ADJUSTMENT_KINDS: tuple[str, ...] = (MISREAD, UNCOUNTED, RULED_INVALID, RULED_VALID)
_KIND_KEYS = {
    "misread_tally": MISREAD,
    "uncounted_ballot_found": UNCOUNTED,
    "ballot_ruled_invalid": RULED_INVALID,
    "invalid_ballot_ruled_valid": RULED_VALID,
}


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecountThreshold(_Cfg):
    """Automatic-recount trigger for one race type."""

    margin_pct: float | None = Field(None, ge=0, description="margin ≤ this many pp of valid votes")
    margin_votes: int | None = Field(None, ge=0, description="margin ≤ this many votes")
    combine: str = Field("or", pattern="^(or|and)$")
    exact_tie: bool = True


class RecountProcedure(_Cfg):
    """How a recount re-examines the ballots."""

    unit_sample_fraction: float = Field(0.10, gt=0, le=1)
    min_units: int = Field(5, ge=1)
    max_units: int = Field(250, ge=1)
    confirm_probability: float = Field(0.6, ge=0, le=1, description="sampled unit confirmed unchanged")
    max_delta: int = Field(3, ge=1, le=20, description="ballots per correction (1..max_delta)")
    kind_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "misread_tally": 0.5,
            "uncounted_ballot_found": 0.2,
            "ballot_ruled_invalid": 0.2,
            "invalid_ballot_ruled_valid": 0.1,
        }
    )

    @model_validator(mode="after")
    def _check(self) -> RecountProcedure:
        unknown = set(self.kind_weights) - set(_KIND_KEYS)
        if unknown:
            raise ValueError(f"unknown adjustment kinds {sorted(unknown)}")
        if any(w < 0 for w in self.kind_weights.values()) or sum(self.kind_weights.values()) <= 0:
            raise ValueError("kind_weights must be non-negative with a positive sum")
        if self.min_units > self.max_units:
            raise ValueError("min_units exceeds max_units")
        return self


def _default_thresholds() -> dict[RaceType, RecountThreshold]:
    return {
        RaceType.PRESIDENT_PROVINCE: RecountThreshold(margin_pct=0.25),
        RaceType.HOUSE: RecountThreshold(margin_pct=0.5),
        RaceType.SENATE: RecountThreshold(margin_pct=0.25),
        RaceType.GOVERNOR: RecountThreshold(margin_pct=0.25),
        RaceType.MAYOR: RecountThreshold(margin_pct=0.5),
    }


class RecountConfig(_Cfg):
    """Schema of ``config/recount.yaml``."""

    thresholds: dict[RaceType, RecountThreshold] = Field(default_factory=_default_thresholds)
    procedure: RecountProcedure = Field(default_factory=RecountProcedure)


def load_recount_config(name: str | Path = "recount.yaml") -> RecountConfig:
    """``config/recount.yaml`` (defaults when absent)."""
    return load_config(name, RecountConfig, optional=True)


# --------------------------------------------------------------------------- trigger
def _top_two(tab: TabulatedRace) -> tuple[int, int]:
    totals = np.sort(np.asarray(tab.totals, dtype=np.int64))[::-1]
    return int(totals[0]), int(totals[1]) if len(totals) > 1 else 0


def needs_recount(
    tab: TabulatedRace, race_type: RaceType | str, config: RecountConfig | None = None
) -> tuple[bool, str]:
    """Whether ``tab`` triggers an automatic recount, and why.

    The margin is recomputed from the totals (a tie already resolved by lot at tabulation still
    counts as an exact tie).
    """
    cfg = config or load_recount_config()
    rtype = RaceType(race_type)
    rule = cfg.thresholds.get(rtype)
    if rule is None:
        return False, f"no automatic recount for {rtype.value} races"
    if len(tab.line_keys) < 2:
        return False, "uncontested race"
    first, second = _top_two(tab)
    valid = int(np.asarray(tab.totals, dtype=np.int64).sum())
    margin = first - second
    if margin == 0:
        if rule.exact_tie:
            return True, "exact tie"
        return False, "exact tie (no automatic recount configured)"
    pct = 100.0 * margin / valid if valid else 0.0
    checks: list[tuple[bool, str]] = []
    if rule.margin_pct is not None:
        checks.append(
            (pct <= rule.margin_pct + 1e-12, f"margin {pct:.3f} pp vs threshold {rule.margin_pct} pp")
        )
    if rule.margin_votes is not None:
        checks.append(
            (margin <= rule.margin_votes, f"margin {margin} votes vs threshold {rule.margin_votes} votes")
        )
    if not checks:
        return False, "no margin threshold configured"
    hit = all(c for c, _ in checks) if rule.combine == "and" else any(c for c, _ in checks)
    detail = "; ".join(msg for _, msg in checks)
    return (
        (True, f"within recount threshold ({detail})")
        if hit
        else (False, f"outside recount threshold ({detail})")
    )


# --------------------------------------------------------------------------- recount
@dataclass(frozen=True)
class RecountAdjustment:
    """One audited correction.  ``line_index == INVALID_PILE`` (−1) means the invalid pile."""

    unit_index: int  # index into GeographyFrame units
    line_index: int
    votes_before: int
    votes_after: int
    delta: int
    reason: str
    row: int  # row within the RaceVotes arrays
    line_key: str

    def __iter__(self) -> Iterator[int | str]:
        """Unpacks as the audit 6-tuple ``(unit_index, line_index, votes_before, votes_after,
        delta, reason)``."""
        return iter(self.as_tuple())

    def as_tuple(self) -> tuple[int, int, int, int, int, str]:
        """(unit_index, line_index, votes_before, votes_after, delta, reason)."""
        return (
            self.unit_index,
            self.line_index,
            self.votes_before,
            self.votes_after,
            self.delta,
            self.reason,
        )


@dataclass
class RecountOutcome:
    """Result of a recount (the certified count is ``recounted`` / ``after``)."""

    race_key: str
    seed: int
    adjustments: list[RecountAdjustment]
    recounted: RaceVotes
    before: TabulatedRaceResult
    after: TabulatedRaceResult
    winner_before: str | None
    winner_after: str | None
    margin_votes_before: int
    margin_votes_after: int
    margin_pct_before: float
    margin_pct_after: float
    outcome_changed: bool
    decided_by: str  # 'recount' | 'lot'
    units_examined: list[int] = field(default_factory=list)  # frame unit indices
    units_changed: int = 0
    net_change: dict[str, int] = field(default_factory=dict)  # line key (and '<invalid>') → Δ
    ballots_cast_change: int = 0

    def audit_rows(self) -> list[dict[str, object]]:
        """Adjustments as plain dicts (for persistence / export)."""
        return [
            {
                "unit_index": a.unit_index,
                "line_index": a.line_index,
                "line_key": a.line_key,
                "votes_before": a.votes_before,
                "votes_after": a.votes_after,
                "delta": a.delta,
                "reason": a.reason,
            }
            for a in self.adjustments
        ]


def _pick(rng: np.random.Generator, weights: np.ndarray, exclude: int | None = None) -> int | None:
    w = np.asarray(weights, dtype=float).copy()
    if exclude is not None:
        w[exclude] = 0.0
    total = w.sum()
    if total <= 0:
        return None
    return int(rng.choice(len(w), p=w / total))


def perform_recount(
    race_votes: RaceVotes,
    seed: int,
    config: RecountConfig | None = None,
    race_key: str | None = None,
    *,
    tie_seed: int | None = None,
) -> RecountOutcome:
    """Recount ``race_votes`` (see the module docstring).  Deterministic in ``seed`` + race key.

    The input is not modified; the recounted counts are returned in ``RecountOutcome.recounted``.
    ``tie_seed`` is the root seed of the drawing of lots on an exact tie, for both the
    provisional (``before``) and the recounted (``after``) result; pass the seed the official
    tabulation used so that ``winner_before`` equals the provisionally declared winner
    (default: ``seed``).
    """
    race_votes.check()
    cfg = config or load_recount_config()
    proc = cfg.procedure
    key = race_key or race_votes.race_key
    rng = make_rng(seed, "recount", key)

    votes = np.array(race_votes.votes, dtype=np.int64, copy=True)
    cast = np.array(race_votes.ballots_cast, dtype=np.int64, copy=True)
    invalid = np.array(race_votes.invalid, dtype=np.int64, copy=True)
    blank = np.array(race_votes.blank, dtype=np.int64, copy=True)
    eligible = np.asarray(race_votes.eligible, dtype=np.int64)
    uidx = np.asarray(race_votes.unit_index, dtype=np.int64)
    L = votes.shape[1]

    candidates = np.flatnonzero(cast > 0)
    k = round(proc.unit_sample_fraction * len(candidates))
    k = int(min(len(candidates), max(proc.min_units, min(proc.max_units, k))))
    sample = np.sort(rng.choice(candidates, size=k, replace=False)) if k else np.zeros(0, dtype=np.int64)

    kinds = [_KIND_KEYS[name] for name in proc.kind_weights]
    kw = np.array([proc.kind_weights[name] for name in proc.kind_weights], dtype=float)
    kw = kw / kw.sum()
    confirm = rng.random(len(sample)) < proc.confirm_probability
    kind_draw = rng.choice(len(kinds), size=len(sample), p=kw)
    deltas = rng.integers(1, proc.max_delta + 1, size=len(sample))

    adjustments: list[RecountAdjustment] = []

    def rec(row: int, line: int, before: int, after: int, reason: str) -> None:
        adjustments.append(
            RecountAdjustment(
                unit_index=int(uidx[row]),
                line_index=line,
                votes_before=int(before),
                votes_after=int(after),
                delta=int(after - before),
                reason=reason,
                row=int(row),
                line_key=INVALID_KEY if line == INVALID_PILE else race_votes.line_keys[line],
            )
        )

    changed_units = 0
    for j, row in enumerate(sample):
        if confirm[j]:
            continue
        row = int(row)
        first = kinds[int(kind_draw[j])]
        # fall back through the other kinds (fixed order) when the drawn one is infeasible
        order = [first] + [kd for kd in ADJUSTMENT_KINDS if kd != first and kd in kinds]
        want = int(deltas[j])
        pile = votes[row].astype(float)
        done = False
        for kind in order:
            if kind == MISREAD and L >= 2:
                a = _pick(rng, pile)
                if a is None:
                    continue
                b = _pick(rng, pile + 1.0, exclude=a)
                if b is None:
                    continue
                d = min(want, int(votes[row, a]))
                rec(row, a, votes[row, a], votes[row, a] - d, kind)
                rec(row, b, votes[row, b], votes[row, b] + d, kind)
                votes[row, a] -= d
                votes[row, b] += d
                done = True
            elif kind == UNCOUNTED and L >= 1:
                room = int(eligible[row] - cast[row])
                if room < 1:
                    continue
                b = _pick(rng, pile + 1.0)
                d = min(want, room)
                rec(row, b, votes[row, b], votes[row, b] + d, kind)
                votes[row, b] += d
                cast[row] += d
                done = True
            elif kind == RULED_INVALID and L >= 1:
                a = _pick(rng, pile)
                if a is None:
                    continue
                d = min(want, int(votes[row, a]))
                rec(row, a, votes[row, a], votes[row, a] - d, kind)
                rec(row, INVALID_PILE, invalid[row], invalid[row] + d, kind)
                votes[row, a] -= d
                invalid[row] += d
                done = True
            elif kind == RULED_VALID and L >= 1:
                if invalid[row] < 1:
                    continue
                b = _pick(rng, pile + 1.0)
                d = min(want, int(invalid[row]))
                rec(row, INVALID_PILE, invalid[row], invalid[row] - d, kind)
                rec(row, b, votes[row, b], votes[row, b] + d, kind)
                invalid[row] -= d
                votes[row, b] += d
                done = True
            if done:
                changed_units += 1
                break

    recounted = RaceVotes(
        race_key=race_votes.race_key,
        line_keys=list(race_votes.line_keys),
        unit_index=uidx.copy(),
        votes=votes,
        ballots_cast=cast,
        blank=blank,
        invalid=invalid,
        eligible=np.array(eligible, copy=True),
        expected_shares=None
        if race_votes.expected_shares is None
        else np.array(race_votes.expected_shares, copy=True),
        expected_turnout=None
        if race_votes.expected_turnout is None
        else np.array(race_votes.expected_turnout, copy=True),
    )
    try:
        recounted.check()
    except AssertionError as exc:  # pragma: no cover - guarded by construction
        raise ElectionError(f"{key}: recount produced inconsistent counts") from exc

    lot_seed = int(seed) if tie_seed is None else int(tie_seed)
    before = tabulate(race_votes, key, tie_seed=lot_seed, resolve_ties=True)
    after_raw = tabulate(recounted, key, resolve_ties=False)
    if after_raw.tied:
        after = tabulate(recounted, key, tie_seed=lot_seed, resolve_ties=True)
        how = "lot"
    else:
        after = after_raw
        how = "recount"

    net: dict[str, int] = {}
    diff = votes.sum(axis=0) - np.asarray(race_votes.votes, dtype=np.int64).sum(axis=0)
    for lk, dv in zip(race_votes.line_keys, diff, strict=True):
        net[lk] = int(dv)
    net[INVALID_KEY] = int(invalid.sum() - np.asarray(race_votes.invalid, dtype=np.int64).sum())

    outcome = RecountOutcome(
        race_key=key,
        seed=int(seed),
        adjustments=adjustments,
        recounted=recounted,
        before=before,
        after=after,
        winner_before=before.winner_key,
        winner_after=after.winner_key,
        margin_votes_before=before.margin_votes,
        margin_votes_after=after.margin_votes,
        margin_pct_before=before.margin_pct,
        margin_pct_after=after.margin_pct,
        outcome_changed=before.winner_key != after.winner_key,
        decided_by=how,
        units_examined=[int(uidx[r]) for r in sample],
        units_changed=changed_units,
        net_change=net,
        ballots_cast_change=int(cast.sum() - np.asarray(race_votes.ballots_cast, dtype=np.int64).sum()),
    )
    log.info(
        "recount completed",
        extra={
            "ctx": {
                "race": key,
                "units": len(sample),
                "adjustments": len(adjustments),
                "margin_before": before.margin_votes,
                "margin_after": after.margin_votes,
                "changed": outcome.outcome_changed,
            }
        },
    )
    return outcome


def recount_thresholds(config: RecountConfig | None = None) -> Mapping[RaceType, RecountThreshold]:
    """The configured automatic-recount thresholds per race type."""
    return (config or load_recount_config()).thresholds
