"""Engine-level (DB-independent) election types shared by the model, tabulation, forecasting,
election-night and race-calling engines.

Services translate ORM rows ⇄ these types; engines never import SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.core.constitution import ElectoralSystem, RaceType


@dataclass(frozen=True)
class BallotLine:
    """One line on a ballot (candidate or ticket), identified by a key unique within the race."""

    key: str  # e.g. candidate key 'maarten-van-den-berg' or 'RLP' for party-list contests
    party_code: str | None
    candidate_key: str | None = None
    running_mate_key: str | None = None
    label: str = ""  # display name
    quality: float = 0.0  # candidate quality z-score
    incumbent: bool = False
    home_province: str | None = None  # province code
    home_municipality: str | None = None  # CBS code
    running_mate_home_province: str | None = None
    withdrawn: bool = False


@dataclass
class RaceSpec:
    """A contest over a set of units (its jurisdiction)."""

    key: str  # race code: PRES-NB, HOUSE-NB-07, SEN-NB-1, GOV-NB, MAYOR-GM0855 …
    race_type: RaceType
    unit_index: np.ndarray  # (n,) int indices into GeographyFrame units (sorted)
    lines: list[BallotLine]
    electoral_system: ElectoralSystem = ElectoralSystem.FPTP
    seats: int = 1
    electoral_votes: int | None = None  # PRESIDENT_PROVINCE only
    province_code: str | None = None
    district_code: str | None = None
    municipality_code: str | None = None
    incumbent_party: str | None = None
    is_open_seat: bool = False
    is_special: bool = False

    @property
    def line_keys(self) -> list[str]:
        return [ln.key for ln in self.lines]

    @property
    def active_lines(self) -> list[int]:
        return [i for i, ln in enumerate(self.lines) if not ln.withdrawn]


@dataclass
class UnitTurnout:
    """Election-wide turnout per unit (one voter casts one ballot paper per race)."""

    eligible: np.ndarray  # (U,) int64
    ballots_cast: np.ndarray  # (U,) int64


@dataclass
class RaceVotes:
    """Final (or partial) vote counts of one race at unit level."""

    race_key: str
    line_keys: list[str]
    unit_index: np.ndarray  # (n,) indices into frame units
    votes: np.ndarray  # (n, L) int64 valid votes per line
    ballots_cast: np.ndarray  # (n,) int64 ballots cast in this race
    blank: np.ndarray  # (n,) int64
    invalid: np.ndarray  # (n,) int64
    eligible: np.ndarray  # (n,) int64
    #: Expected (model-baseline, pre-election) shares per unit and line — used by the calling engine.
    expected_shares: np.ndarray | None = None  # (n, L) float
    expected_turnout: np.ndarray | None = None  # (n,) float (share of eligible)

    @property
    def valid(self) -> np.ndarray:
        return self.votes.sum(axis=1)

    def totals(self) -> np.ndarray:
        return self.votes.sum(axis=0)

    def check(self) -> None:
        n, L = self.votes.shape
        assert len(self.line_keys) == L
        assert len(self.unit_index) == n == len(self.ballots_cast) == len(self.blank) == len(self.invalid)
        assert (self.votes >= 0).all()
        assert np.array_equal(self.votes.sum(axis=1) + self.blank + self.invalid, self.ballots_cast)
        assert (self.ballots_cast <= self.eligible).all()


@dataclass
class ElectionDraw:
    """One complete simulated election: turnout + votes for every race, plus audit info."""

    seed: int
    turnout: UnitTurnout
    races: dict[str, RaceVotes] = field(default_factory=dict)
    #: Realised random components for auditability (national shocks per party, etc.).
    environment: dict = field(default_factory=dict)


@dataclass
class TabulatedRace:
    """Aggregated result of one race (see :mod:`app.elections.tabulation`)."""

    race_key: str
    line_keys: list[str]
    totals: np.ndarray  # (L,) int64
    valid: int
    ballots_cast: int
    eligible: int
    blank: int
    invalid: int
    ranking: list[int]  # line indices sorted by votes desc (ties broken deterministically)
    winner: int | None  # line index; None when an exact tie remains unresolved
    tied: bool
    margin_votes: int  # winner − runner-up
    margin_pct: float  # in percentage points of valid votes
    shares: np.ndarray  # (L,) float

    @property
    def turnout_pct(self) -> float:
        return 100.0 * self.ballots_cast / self.eligible if self.eligible else 0.0
