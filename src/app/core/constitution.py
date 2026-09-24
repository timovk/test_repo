"""Constitutional constants of the (fictional) Federal Republic of the Netherlands.

Every institutional number used anywhere in the application is derived from this
module.  The canonical system is:

* 12 provinces (≙ U.S. states)
* Tweede Kamer (≙ House): 150 single-member districts, majority 76
* Eerste Kamer (≙ Senate): 2 senators per province = 24 seats, majority 13,
  six-year terms in three staggered classes of 8
* Electoral College: 150 + 24 = 174 electoral votes, majority 88

The values are configurable through :class:`ConstitutionConfig` (``config/constitution.yaml``)
so alternative constitutions can be explored, but the canonical default is validated
by :func:`validate_canonical` and by the test-suite.

All of this is FICTIONAL.  The real Netherlands is a parliamentary monarchy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Canonical constants (the "default constitution")
# ---------------------------------------------------------------------------
PROVINCE_COUNT: int = 12
HOUSE_SEATS: int = 150
SENATORS_PER_PROVINCE: int = 2
SENATE_SEATS: int = PROVINCE_COUNT * SENATORS_PER_PROVINCE  # 24
SENATE_CLASSES: int = 3
ELECTORAL_VOTES: int = HOUSE_SEATS + SENATE_SEATS  # 174


def majority_of(total: int) -> int:
    """Smallest integer strictly greater than half of ``total`` (absolute majority)."""
    if total <= 0:
        raise ValueError("total must be positive")
    return total // 2 + 1


PRESIDENTIAL_MAJORITY: int = majority_of(ELECTORAL_VOTES)  # 88
HOUSE_MAJORITY: int = majority_of(HOUSE_SEATS)  # 76
SENATE_MAJORITY: int = majority_of(SENATE_SEATS)  # 13

PRESIDENTIAL_TERM_YEARS: int = 4
HOUSE_TERM_YEARS: int = 2
SENATE_TERM_YEARS: int = 6
GOVERNOR_TERM_YEARS: int = 4
MAYOR_TERM_YEARS: int = 4

#: Minimum House seats guaranteed to every province (mirrors U.S. Art. I §2).
MIN_HOUSE_SEATS_PER_PROVINCE: int = 1

# Display strings used throughout the UI and CLI.
PRESIDENT_TO_WIN_LABEL = f"{PRESIDENTIAL_MAJORITY} TO WIN"
HOUSE_CONTROL_LABEL = f"{HOUSE_MAJORITY} FOR CONTROL"
SENATE_CONTROL_LABEL = f"{SENATE_MAJORITY} FOR CONTROL"


class OfficeType(StrEnum):
    PRESIDENT = "PRESIDENT"
    VICE_PRESIDENT = "VICE_PRESIDENT"
    HOUSE = "HOUSE"
    SENATE = "SENATE"
    GOVERNOR = "GOVERNOR"
    LIEUTENANT_GOVERNOR = "LIEUTENANT_GOVERNOR"
    PROVINCIAL_LEGISLATOR = "PROVINCIAL_LEGISLATOR"
    MAYOR = "MAYOR"
    COUNCIL_MEMBER = "COUNCIL_MEMBER"


class RaceType(StrEnum):
    """Kinds of races.  ``PRESIDENT`` is the national parent race; each province's
    winner-take-all contest for its electoral votes is a ``PRESIDENT_PROVINCE`` child race."""

    PRESIDENT = "PRESIDENT"
    PRESIDENT_PROVINCE = "PRESIDENT_PROVINCE"
    HOUSE = "HOUSE"
    SENATE = "SENATE"
    GOVERNOR = "GOVERNOR"
    PROVINCIAL_LEGISLATURE = "PROVINCIAL_LEGISLATURE"
    MAYOR = "MAYOR"
    MUNICIPAL_COUNCIL = "MUNICIPAL_COUNCIL"


class ElectionType(StrEnum):
    GENERAL = "general"  # presidential year
    MIDTERM = "midterm"
    SPECIAL = "special"
    MUNICIPAL = "municipal"
    PROVINCIAL = "provincial"


class ElectionStatus(StrEnum):
    SCHEDULED = "scheduled"  # created, no votes simulated yet
    SIMULATED = "simulated"  # true final result generated, not yet revealed
    LIVE = "live"  # election night in progress
    FINAL = "final"  # all ballots reported, calls resolved
    CERTIFIED = "certified"  # final + recounts complete; immutable history


class RaceStatus(StrEnum):
    """Election-night race states (see docs/RACE_CALLING.md)."""

    SCHEDULED = "SCHEDULED"
    POLLS_CLOSED = "POLLS_CLOSED"
    TOO_EARLY = "TOO_EARLY_TO_CALL"
    TOO_CLOSE = "TOO_CLOSE_TO_CALL"
    LEAN = "LEAN"
    PROJECTED = "PROJECTED_WINNER"
    CALLED = "CALLED"
    RECOUNT = "RECOUNT"
    FINAL = "FINAL"


#: States in which a winner is considered decided for seat / EV allocation.
DECIDED_STATUSES: frozenset[RaceStatus] = frozenset(
    {RaceStatus.PROJECTED, RaceStatus.CALLED, RaceStatus.FINAL}
)


class ElectoralSystem(StrEnum):
    FPTP = "fptp"  # plurality, single winner
    WINNER_TAKE_ALL = "winner_take_all"  # EV unit: plurality winner takes all EV
    PROPORTIONAL_DHONDT = "proportional_dhondt"
    PROPORTIONAL_SAINTE_LAGUE = "proportional_sainte_lague"
    DISTRICT_PLUS_STATEWIDE = "district_plus_statewide"  # Maine/Nebraska style EV split


class DataCategory(StrEnum):
    """The fundamental REAL vs FICTIONAL distinction (spec §43)."""

    REAL = "REAL"  # Dutch geography, population, official identifiers, CBS demographics
    DERIVED = "DERIVED"  # computed deterministically from REAL data (e.g. estimated eligible voters)
    FICTIONAL = "FICTIONAL"  # the constitutional system, districts, parties, candidates, baselines
    SIMULATED = "SIMULATED"  # model-generated votes, forecasts, election nights


# ---------------------------------------------------------------------------
# Configurable constitution
# ---------------------------------------------------------------------------
class ContingentElectionMode(StrEnum):
    PROVINCE_DELEGATIONS = "province_delegations"  # U.S. 12th Amendment analogue (default)
    HOUSE_MEMBERS = "house_members"  # each member of the Tweede Kamer votes individually
    NATIONAL_POPULAR_VOTE = "national_popular_vote"  # plurality of popular vote among the finalists
    NATIONAL_RUNOFF = "national_runoff"  # simulated national runoff between the top two


class EVAllocationMethod(StrEnum):
    WINNER_TAKE_ALL = "winner_take_all"  # default
    DISTRICT = "district"  # Maine/Nebraska style: 2 EV province-wide + 1 per House district
    PROPORTIONAL = "proportional"  # EV split proportionally (largest remainder) among candidates


class ContingentElectionConfig(BaseModel):
    mode: ContingentElectionMode = ContingentElectionMode.PROVINCE_DELEGATIONS
    finalists: int = Field(3, ge=2, description="Top-N EV recipients eligible (U.S.: 3)")
    max_ballots: int = Field(10, ge=1, description="Ballot rounds before the fallback applies")
    #: If no finalist wins after ``max_ballots``: 'vice_president_acts' (U.S. 20th Amendment analogue)
    #: or 'popular_vote' (plurality of the national popular vote among finalists).
    deadlock_fallback: str = Field("popular_vote", pattern="^(vice_president_acts|popular_vote)$")
    vice_president_by_senate: bool = True
    vice_president_finalists: int = Field(2, ge=2)


class ConstitutionConfig(BaseModel):
    """Configurable constitution.  Defaults are the canonical system."""

    province_count: int = PROVINCE_COUNT
    house_seats: int = HOUSE_SEATS
    senators_per_province: int = SENATORS_PER_PROVINCE
    senate_classes: int = SENATE_CLASSES
    min_house_seats_per_province: int = MIN_HOUSE_SEATS_PER_PROVINCE
    presidential_term_years: int = PRESIDENTIAL_TERM_YEARS
    house_term_years: int = HOUSE_TERM_YEARS
    senate_term_years: int = SENATE_TERM_YEARS
    governor_term_years: int = GOVERNOR_TERM_YEARS
    mayor_term_years: int = MAYOR_TERM_YEARS
    apportionment_method: str = "huntington_hill"
    ev_allocation: EVAllocationMethod = EVAllocationMethod.WINNER_TAKE_ALL
    #: Province tie in the EV contest after recount: resolved by seeded lot (documented).
    province_tie_rule: str = Field("lot", pattern="^(lot|contingent)$")
    contingent: ContingentElectionConfig = Field(default_factory=ContingentElectionConfig)
    lieutenant_governors: bool = True

    @property
    def senate_seats(self) -> int:
        return self.province_count * self.senators_per_province

    @property
    def electoral_votes(self) -> int:
        return self.house_seats + self.senate_seats

    @property
    def presidential_majority(self) -> int:
        return majority_of(self.electoral_votes)

    @property
    def house_majority(self) -> int:
        return majority_of(self.house_seats)

    @property
    def senate_majority(self) -> int:
        return majority_of(self.senate_seats)

    @property
    def seats_per_senate_class(self) -> int:
        return self.senate_seats // self.senate_classes

    @model_validator(mode="after")
    def _check(self) -> ConstitutionConfig:
        if self.senate_seats % self.senate_classes != 0:
            raise ValueError("Senate seats must divide evenly into classes")
        if self.senators_per_province > self.senate_classes:
            raise ValueError("A province cannot have more senators than there are classes")
        if self.senate_term_years != self.house_term_years * self.senate_classes:
            raise ValueError("Senate term must equal house term × number of classes for clean staggering")
        if self.house_seats < self.province_count * self.min_house_seats_per_province:
            raise ValueError("Not enough House seats to satisfy the per-province minimum")
        return self

    def is_canonical(self) -> bool:
        return (
            self.province_count == PROVINCE_COUNT
            and self.house_seats == HOUSE_SEATS
            and self.senate_seats == SENATE_SEATS
            and self.electoral_votes == ELECTORAL_VOTES
        )


@dataclass(frozen=True)
class ChamberThresholds:
    """Convenience bundle for UI/API: totals and majorities."""

    electoral_votes: int
    presidential_majority: int
    house_seats: int
    house_majority: int
    senate_seats: int
    senate_majority: int

    @classmethod
    def from_config(cls, cfg: ConstitutionConfig) -> ChamberThresholds:
        return cls(
            electoral_votes=cfg.electoral_votes,
            presidential_majority=cfg.presidential_majority,
            house_seats=cfg.house_seats,
            house_majority=cfg.house_majority,
            senate_seats=cfg.senate_seats,
            senate_majority=cfg.senate_majority,
        )


def validate_canonical() -> None:
    """Assert the canonical constitutional arithmetic (spec §33).  Raises AssertionError."""
    assert PROVINCE_COUNT == 12
    assert HOUSE_SEATS == 150
    assert SENATE_SEATS == 24
    assert ELECTORAL_VOTES == 174
    assert PRESIDENTIAL_MAJORITY == 88
    assert HOUSE_MAJORITY == 76
    assert SENATE_MAJORITY == 13
    assert SENATE_SEATS % SENATE_CLASSES == 0 and SENATE_SEATS // SENATE_CLASSES == 8


validate_canonical()
