"""Elections, races, ballots and results (all SIMULATED / FICTIONAL).

Result hierarchy: unit (CBS buurt ≙ precinct) → municipality → province → national, plus
district aggregates for House races.  Every level is materialised so it is directly queryable,
and :mod:`app.elections.tabulation` guarantees that the levels reconcile exactly.

``geo_key`` encodes the geographic level uniquely: ``U:<geo_unit_id>``, ``M:<municipality_id>``,
``D:<district_id>``, ``P:<province_id>`` or ``N`` (national).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow


class Election(Base):
    """An election day.  General elections contain President + House + a Senate class (+ governors)."""

    __tablename__ = "election"

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    election_date: Mapped[date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(160))
    election_type: Mapped[str] = mapped_column(String(16))  # ElectionType
    status: Mapped[str] = mapped_column(String(16), default="scheduled")  # ElectionStatus
    scenario_id: Mapped[int | None] = mapped_column(ForeignKey("scenario.id"))
    seed: Mapped[int] = mapped_column(BigInteger)
    vintage_id: Mapped[int] = mapped_column(ForeignKey("geo_vintage.id"))
    apportionment_id: Mapped[int | None] = mapped_column(ForeignKey("apportionment.id"))
    district_plan_id: Mapped[int | None] = mapped_column(ForeignKey("district_plan.id"))
    previous_election_id: Mapped[int | None] = mapped_column(ForeignKey("election.id"))
    polls_close_local: Mapped[str] = mapped_column(String(5), default="21:00")
    timezone: Mapped[str] = mapped_column(String(40), default="Europe/Amsterdam")
    national_environment_json: Mapped[str | None] = mapped_column(Text)  # realised shocks (audit)
    is_fictional: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    simulated_at: Mapped[datetime | None] = mapped_column(DateTime)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    races: Mapped[list[Race]] = relationship(back_populates="election", cascade="all, delete-orphan")


class Race(Base):
    """One contest.  ``PRESIDENT`` is the national parent; its 12 ``PRESIDENT_PROVINCE`` children
    are the winner-take-all electoral-vote contests."""

    __tablename__ = "race"
    __table_args__ = (UniqueConstraint("election_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("election.id"), index=True)
    race_type: Mapped[str] = mapped_column(String(24), index=True)  # RaceType
    code: Mapped[str] = mapped_column(
        String(24)
    )  # PRES, PRES-NB, HOUSE-NB-07, SEN-NB-1, GOV-NB, MAYOR-GM0855
    name: Mapped[str] = mapped_column(String(160))
    parent_race_id: Mapped[int | None] = mapped_column(ForeignKey("race.id"), index=True)
    office_id: Mapped[int | None] = mapped_column(ForeignKey("office.id"))
    province_id: Mapped[int | None] = mapped_column(ForeignKey("province.id"), index=True)
    district_id: Mapped[int | None] = mapped_column(ForeignKey("house_district.id"), index=True)
    municipality_id: Mapped[int | None] = mapped_column(ForeignKey("municipality.id"), index=True)
    senate_seat_id: Mapped[int | None] = mapped_column(ForeignKey("senate_seat.id"))
    electoral_votes: Mapped[int | None] = mapped_column(Integer)
    seats: Mapped[int] = mapped_column(Integer, default=1)
    electoral_system: Mapped[str] = mapped_column(String(32), default="fptp")
    is_special: Mapped[bool] = mapped_column(Boolean, default=False)
    is_open_seat: Mapped[bool] = mapped_column(Boolean, default=False)
    incumbent_candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidate.id"))
    incumbent_party_id: Mapped[int | None] = mapped_column(ForeignKey("party.id"))
    previous_race_id: Mapped[int | None] = mapped_column(ForeignKey("race.id"))
    status: Mapped[str] = mapped_column(String(24), default="SCHEDULED")  # RaceStatus
    winner_ballot_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("ballot_candidate.id", use_alter=True, name="fk_race_winner_ballot_candidate")
    )
    winner_party_id: Mapped[int | None] = mapped_column(ForeignKey("party.id"))
    total_votes: Mapped[int | None] = mapped_column(Integer)
    margin_votes: Mapped[int | None] = mapped_column(Integer)
    margin_pct: Mapped[float | None] = mapped_column(Float)  # winner − runner-up, pct points of valid votes
    turnout_pct: Mapped[float | None] = mapped_column(Float)
    flipped: Mapped[bool | None] = mapped_column(Boolean)
    decided_by: Mapped[str | None] = mapped_column(String(24))  # popular_vote | contingent | lot | recount
    called_at: Mapped[datetime | None] = mapped_column(DateTime)

    election: Mapped[Election] = relationship(back_populates="races")
    ballot: Mapped[list[BallotCandidate]] = relationship(
        back_populates="race",
        cascade="all, delete-orphan",
        foreign_keys="BallotCandidate.race_id",
        order_by="BallotCandidate.ballot_order",
    )
    children: Mapped[list[Race]] = relationship(foreign_keys=[parent_race_id])


class BallotCandidate(Base):
    """A line on the ballot: candidate (+ running mate) with a snapshot of the party identity."""

    __tablename__ = "ballot_candidate"
    __table_args__ = (UniqueConstraint("race_id", "ballot_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("race.id"), index=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidate.id"), index=True)
    running_mate_id: Mapped[int | None] = mapped_column(ForeignKey("candidate.id"))
    party_id: Mapped[int | None] = mapped_column(ForeignKey("party.id"), index=True)
    ballot_order: Mapped[int] = mapped_column(Integer)
    ballot_name: Mapped[str] = mapped_column(String(200))
    #: Stable key of the line within its race (candidate key, ticket = presidential candidate key,
    #: party code for party-list contests); the engines' ``BallotLine.key``.
    line_key: Mapped[str | None] = mapped_column(String(80))
    #: Candidate quality used for this ballot (a person's quality may change in later scenarios).
    quality_snapshot: Mapped[float | None] = mapped_column(Float)
    party_code_snapshot: Mapped[str | None] = mapped_column(String(12))
    party_name_snapshot: Mapped[str | None] = mapped_column(String(120))
    party_abbr_snapshot: Mapped[str | None] = mapped_column(String(16))
    party_color_snapshot: Mapped[str | None] = mapped_column(String(9))
    is_incumbent: Mapped[bool] = mapped_column(Boolean, default=False)
    is_write_in: Mapped[bool] = mapped_column(Boolean, default=False)
    withdrawn: Mapped[bool] = mapped_column(Boolean, default=False)
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)

    race: Mapped[Race] = relationship(back_populates="ballot", foreign_keys=[race_id])


class ElectionResult(Base):
    """Votes for one ballot line at one geographic level."""

    __tablename__ = "election_result"
    __table_args__ = (
        UniqueConstraint("race_id", "ballot_candidate_id", "geo_key"),
        Index("ix_election_result_race_level", "race_id", "level"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("race.id"))
    ballot_candidate_id: Mapped[int] = mapped_column(ForeignKey("ballot_candidate.id"), index=True)
    level: Mapped[str] = mapped_column(String(12))  # unit | municipality | district | province | national
    geo_key: Mapped[str] = mapped_column(String(16))
    geo_unit_id: Mapped[int | None] = mapped_column(ForeignKey("geo_unit.id"), index=True)
    municipality_id: Mapped[int | None] = mapped_column(ForeignKey("municipality.id"), index=True)
    district_id: Mapped[int | None] = mapped_column(ForeignKey("house_district.id"))
    province_id: Mapped[int | None] = mapped_column(ForeignKey("province.id"))
    votes: Mapped[int] = mapped_column(Integer)
    share: Mapped[float] = mapped_column(Float)  # of valid votes at this level (0–1)


class TurnoutResult(Base):
    """Ballots cast / valid / blank / invalid at one level for one race."""

    __tablename__ = "turnout_result"
    __table_args__ = (
        UniqueConstraint("race_id", "geo_key"),
        Index("ix_turnout_result_race_level", "race_id", "level"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("election.id"), index=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("race.id"))
    level: Mapped[str] = mapped_column(String(12))
    geo_key: Mapped[str] = mapped_column(String(16))
    geo_unit_id: Mapped[int | None] = mapped_column(ForeignKey("geo_unit.id"))
    municipality_id: Mapped[int | None] = mapped_column(ForeignKey("municipality.id"))
    district_id: Mapped[int | None] = mapped_column(ForeignKey("house_district.id"))
    province_id: Mapped[int | None] = mapped_column(ForeignKey("province.id"))
    eligible_voters: Mapped[int] = mapped_column(Integer)
    ballots_cast: Mapped[int] = mapped_column(Integer)
    valid_votes: Mapped[int] = mapped_column(Integer)
    blank_votes: Mapped[int] = mapped_column(Integer, default=0)
    invalid_votes: Mapped[int] = mapped_column(Integer, default=0)
    turnout_pct: Mapped[float] = mapped_column(Float)


class ElectoralVoteAllocation(Base):
    """Electoral votes awarded in a presidential election (one row per province × recipient)."""

    __tablename__ = "electoral_vote_allocation"
    __table_args__ = (UniqueConstraint("race_id", "province_id", "ballot_candidate_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("race.id"), index=True)  # the PRESIDENT parent race
    province_race_id: Mapped[int] = mapped_column(ForeignKey("race.id"))
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"))
    ballot_candidate_id: Mapped[int] = mapped_column(ForeignKey("ballot_candidate.id"))  # parent-race line
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidate.id"))
    electoral_votes: Mapped[int] = mapped_column(Integer)


class ContingentElection(Base):
    """Record of a contingent election when no ticket reached the EV majority."""

    __tablename__ = "contingent_election"

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("race.id"), unique=True)
    mode: Mapped[str] = mapped_column(String(32))
    finalists_json: Mapped[str] = mapped_column(Text)  # list of ballot_candidate ids
    ballots_json: Mapped[str] = mapped_column(Text)  # per-round tallies (audit trail)
    winner_ballot_candidate_id: Mapped[int | None] = mapped_column(ForeignKey("ballot_candidate.id"))
    vp_winner_candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidate.id"))
    rounds: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32))  # elected | fallback_popular_vote | vp_acts
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
