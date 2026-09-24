"""Engine types of the campaign system (FICTIONAL campaigns, SIMULATED effects)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from app.campaigns.config import LEVELS, NATIONAL_CODE
from app.core.constitution import DataCategory

TargetKey = tuple[str, str]  # (level, code)
EffectKind = Literal["persuasion", "turnout"]


@dataclass(frozen=True)
class Target:
    """A place a campaign can spend resources in.

    ``value`` is what winning it is worth (electoral votes for presidential province targets,
    seats for House districts / Senate provinces), ``competitiveness`` 0 (safe) … 1 (toss-up),
    ``expected_margin_pp`` the party's expected margin over its strongest opponent (+ = ahead),
    ``population`` the target's population (scales the cost of reaching voters).
    """

    level: str
    code: str
    value: float
    competitiveness: float
    expected_margin_pp: float
    population: float

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"unknown target level {self.level!r}")
        if not 0.0 <= self.competitiveness <= 1.0:
            raise ValueError(f"competitiveness must be in [0, 1], got {self.competitiveness}")
        if self.value < 0 or not math.isfinite(self.value):
            raise ValueError("target value must be a non-negative number")
        if self.population < 0 or not math.isfinite(self.population):
            raise ValueError("target population must be a non-negative number")
        if not math.isfinite(self.expected_margin_pp):
            raise ValueError("expected_margin_pp must be finite")

    @property
    def key(self) -> TargetKey:
        return (self.level, self.code)


@dataclass(frozen=True)
class Allocation:
    """Resources (abstract units) a party puts into one action at one target in one week.

    Weeks are numbered 1 … ``weeks`` (the last week ends on election day).  ``units`` counts
    rallies / visits for discrete actions; ``proceeds`` is the money a fundraising allocation
    raises (available from ``week + lag_weeks``).
    """

    level: str
    code: str
    action: str
    amount: float
    week: int
    party: str = ""
    units: int | None = None
    proceeds: float = 0.0
    population: float | None = None

    @property
    def key(self) -> TargetKey:
        return (self.level, self.code)


@dataclass(frozen=True)
class AllocationEffect:
    """Audit record: the share of its target's effect attributable to one allocation."""

    allocation: Allocation
    effective_spend: float  # amount decayed to election day
    expected_persuasion: float
    realized_persuasion: float
    expected_turnout: float
    realized_turnout: float


@dataclass
class CampaignEffects:
    """Realised (seeded) effects of one party's campaign, per target ``(level, code)``.

    ``persuasion`` is a logit shift of the party's utility, ``turnout`` a logit shift of its
    supporters' turnout.  ``expected_*`` are the pre-noise values.
    """

    party: str
    persuasion: dict[TargetKey, float]
    turnout: dict[TargetKey, float]
    expected_persuasion: dict[TargetKey, float]
    expected_turnout: dict[TargetKey, float]
    spend: dict[TargetKey, float]
    allocations: list[AllocationEffect]
    seed: int
    data_category: str = DataCategory.SIMULATED.value

    def total_spend(self) -> float:
        return float(sum(e.allocation.amount for e in self.allocations))

    def to_records(self) -> list[dict[str, Any]]:
        """Rows matching the ``campaign_allocation`` table columns."""
        return [
            {
                "target_level": e.allocation.level,
                "target_code": e.allocation.code,
                "action": e.allocation.action,
                "amount": e.allocation.amount,
                "week": e.allocation.week,
                "expected_effect": e.expected_persuasion,
                "realized_effect": e.realized_persuasion,
                "turnout_effect": e.realized_turnout,
            }
            for e in self.allocations
        ]


@dataclass
class CombinedCampaignEffects:
    """Effects of all parties' campaigns: ``persuasion[(level, code)][party]`` (logit)."""

    parties: list[str]
    persuasion: dict[TargetKey, dict[str, float]]
    turnout: dict[TargetKey, dict[str, float]]
    persuasion_cap: float
    turnout_cap: float
    backfire_floor: float
    data_category: str = DataCategory.SIMULATED.value
    by_party: dict[str, CampaignEffects] = field(default_factory=dict)

    def effect(
        self,
        level: str,
        code: str,
        party: str,
        kind: EffectKind = "persuasion",
        include_national: bool = True,
    ) -> float:
        """Total effect for ``party`` in a target (plus national effects), clipped to the cap."""
        table = self.persuasion if kind == "persuasion" else self.turnout
        cap = self.persuasion_cap if kind == "persuasion" else self.turnout_cap
        value = table.get((level, code), {}).get(party, 0.0)
        if include_national and level != "national":
            value += table.get(("national", NATIONAL_CODE), {}).get(party, 0.0)
        return float(np.clip(value, self.backfire_floor * cap, cap))

    def matrix(
        self,
        level: str,
        codes: list[str],
        parties: list[str] | None = None,
        kind: EffectKind = "persuasion",
        include_national: bool = True,
    ) -> np.ndarray:
        """``(len(codes), len(parties))`` array of effects for vectorised consumers."""
        parties = list(self.parties) if parties is None else list(parties)
        out = np.zeros((len(codes), len(parties)))
        for i, code in enumerate(codes):
            for j, party in enumerate(parties):
                out[i, j] = self.effect(level, code, party, kind, include_national)
        return out
