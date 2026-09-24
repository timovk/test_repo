"""Election-night reporting timeline, race calls, recounts (SIMULATED)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class ReportingEvent(Base):
    """One batch of counted ballots arriving on election night.

    A batch belongs to one municipality and completes (fractions of) one or more precincts
    (``ReportingEventUnit``).  ``sim_time_s`` is seconds after the first poll closing.
    """

    __tablename__ = "reporting_event"
    __table_args__ = (UniqueConstraint("election_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("election.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    sim_time_s: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(DateTime)  # simulated local wall-clock time
    municipality_id: Mapped[int] = mapped_column(ForeignKey("municipality.id"), index=True)
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"))
    kind: Mapped[str] = mapped_column(String(16), default="batch")  # batch | final | correction
    ballots_in_batch: Mapped[int] = mapped_column(Integer)
    municipality_fraction_after: Mapped[float] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(String(200))


class ReportingEventUnit(Base):
    __tablename__ = "reporting_event_unit"
    __table_args__ = (UniqueConstraint("event_id", "geo_unit_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("reporting_event.id"), index=True)
    geo_unit_id: Mapped[int] = mapped_column(ForeignKey("geo_unit.id"))
    fraction: Mapped[float] = mapped_column(Float)  # increment of the unit's ballots counted in this batch


class RaceCall(Base):
    """A change of a race's call state, with the exact evidence available at that moment."""

    __tablename__ = "race_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("race.id"), index=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("election.id"), index=True)
    status: Mapped[str] = mapped_column(String(24))  # RaceStatus
    ballot_candidate_id: Mapped[int | None] = mapped_column(ForeignKey("ballot_candidate.id"))
    seq: Mapped[int] = mapped_column(Integer)  # reporting-event sequence number at the call
    sim_time_s: Mapped[float] = mapped_column(Float)
    called_at: Mapped[datetime] = mapped_column(DateTime)  # simulated local time
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # real wall-clock
    reporting_pct: Mapped[float] = mapped_column(Float)
    leader_margin_pct: Mapped[float | None] = mapped_column(Float)
    win_probability: Mapped[float | None] = mapped_column(Float)
    evidence_json: Mapped[str] = mapped_column(Text)  # snapshot: counted, outstanding, quantiles …
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reason: Mapped[str | None] = mapped_column(Text)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)


class NightSession(Base):
    """Persistent state of the election-night playback for one election."""

    __tablename__ = "night_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("election.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="ready")  # ready | running | paused | finished
    current_seq: Mapped[int] = mapped_column(Integer, default=0)  # number of events applied
    sim_time_s: Mapped[float] = mapped_column(Float, default=0.0)
    speed: Mapped[float] = mapped_column(Float, default=1.0)
    seed: Mapped[int] = mapped_column(BigInteger)
    total_events: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Recount(Base):
    __tablename__ = "recount"

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("race.id"), index=True)
    reason: Mapped[str] = mapped_column(String(64))  # automatic_threshold | tie | manual
    threshold_pct: Mapped[float | None] = mapped_column(Float)
    margin_before_votes: Mapped[int] = mapped_column(Integer)
    margin_before_pct: Mapped[float] = mapped_column(Float)
    margin_after_votes: Mapped[int | None] = mapped_column(Integer)
    margin_after_pct: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | completed
    outcome_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    seed: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class RecountAdjustment(Base):
    """Auditable ballot adjustment produced by a recount (never a regenerated election)."""

    __tablename__ = "recount_adjustment"

    id: Mapped[int] = mapped_column(primary_key=True)
    recount_id: Mapped[int] = mapped_column(ForeignKey("recount.id"), index=True)
    geo_unit_id: Mapped[int] = mapped_column(ForeignKey("geo_unit.id"))
    #: NULL when the adjustment concerns the invalid-ballot pile (see ``pile``).
    ballot_candidate_id: Mapped[int | None] = mapped_column(ForeignKey("ballot_candidate.id"))
    pile: Mapped[str] = mapped_column(String(8), default="line")  # line | invalid | blank
    votes_before: Mapped[int] = mapped_column(Integer)
    votes_after: Mapped[int] = mapped_column(Integer)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(80))  # misread tally | uncounted ballot | ruled invalid
