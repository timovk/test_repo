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
    #: Position (economic, social, europe — ``structural.IDEOLOGY_DIMS``) of a line that is not a
    #: party's or a known candidate's, e.g. the YES / NO side of a ballot measure or a recall
    #: (None: from the candidate or the party).
    ideology: tuple[float, ...] | None = None


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
    #: Yes/No contests (``ElectoralSystem.QUESTION``): the YES share needed to pass (0.5 = simple
    #: majority, 2/3 for some charter amendments and bonds).  None for candidate races.
    threshold: float | None = None
    #: Contest-specific data (measure text and topic, recall target, water board code …); JSON-safe.
    details: dict = field(default_factory=dict)

    @property
    def marks_per_ballot(self) -> int:
        """Votes a voter may cast: ``seats`` in a plurality-at-large race (vote for up to N), else 1."""
        return (
            int(self.seats)
            if ElectoralSystem(self.electoral_system) == ElectoralSystem.PLURALITY_AT_LARGE
            else 1
        )

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
    #: Seats filled: the top ``seats`` lines win (plurality at large); 1 for every other race.
    seats: int = 1
    #: Votes per ballot: ``seats`` in a plurality-at-large race (each voter marks up to N
    #: candidates), else 1.  ``votes`` then counts marks, and a ballot is valid when it marks at
    #: least one line (``valid_ballots``).
    marks_per_ballot: int = 1
    #: Yes/No contests: YES share of the valid votes needed to pass (line keys ``YES``/``NO``).
    threshold: float | None = None

    @property
    def valid(self) -> np.ndarray:
        """Valid votes per unit (marks in a plurality-at-large race)."""
        return self.votes.sum(axis=1)

    @property
    def valid_ballots(self) -> np.ndarray:
        """Valid ballots per unit (equal to ``valid`` when every ballot carries one vote)."""
        return self.ballots_cast - self.blank - self.invalid

    def totals(self) -> np.ndarray:
        return self.votes.sum(axis=0)

    def check(self) -> None:
        n, L = self.votes.shape
        assert len(self.line_keys) == L
        assert len(self.unit_index) == n == len(self.ballots_cast) == len(self.blank) == len(self.invalid)
        assert (self.votes >= 0).all()
        if self.marks_per_ballot <= 1:
            assert np.array_equal(self.votes.sum(axis=1) + self.blank + self.invalid, self.ballots_cast)
        else:
            vb = self.valid_ballots
            assert (vb >= 0).all()
            # every valid ballot marks 1..N candidates, each candidate at most once
            assert (self.votes.sum(axis=1) <= vb * self.marks_per_ballot).all()
            assert (self.votes.sum(axis=1) >= vb).all()
            assert (self.votes <= vb[:, None]).all()
        assert (self.ballots_cast <= self.eligible).all()

    def like(self, **arrays: np.ndarray) -> RaceVotes:
        """A copy with some arrays replaced, keeping keys, units and the contest rules."""
        fields = {
            "race_key": self.race_key,
            "line_keys": list(self.line_keys),
            "unit_index": self.unit_index,
            "votes": self.votes,
            "ballots_cast": self.ballots_cast,
            "blank": self.blank,
            "invalid": self.invalid,
            "eligible": self.eligible,
            "expected_shares": self.expected_shares,
            "expected_turnout": self.expected_turnout,
            "seats": self.seats,
            "marks_per_ballot": self.marks_per_ballot,
            "threshold": self.threshold,
        }
        fields.update(arrays)
        return RaceVotes(**fields)


def contest_rules(spec: RaceSpec) -> dict:
    """``seats`` / ``marks_per_ballot`` / ``threshold`` of a race, for building its RaceVotes."""
    at_large = ElectoralSystem(spec.electoral_system) == ElectoralSystem.PLURALITY_AT_LARGE
    return {
        "seats": int(spec.seats) if at_large else 1,
        "marks_per_ballot": spec.marks_per_ballot,
        "threshold": None if spec.threshold is None else float(spec.threshold),
    }


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
    #: How the winner was determined: 'popular_vote' | 'lot' (exact tie drawn by seeded lot) | None.
    decided_by: str | None = "popular_vote"
    #: Line indices involved in an exact first-place tie (empty when no tie).
    tied_lines: tuple[int, ...] = ()
    #: Every winning line: the top ``seats`` lines of a plurality-at-large race, else (winner,).
    winners: tuple[int, ...] = ()
    seats: int = 1
    #: Yes/No contests: whether the YES share reached the threshold (None for candidate races).
    passed: bool | None = None
    threshold: float | None = None

    @property
    def turnout_pct(self) -> float:
        return 100.0 * self.ballots_cast / self.eligible if self.eligible else 0.0
