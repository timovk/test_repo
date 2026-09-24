"""Polling database (FICTIONAL polls).  Pollster ratings / house effects are editable data."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Pollster(Base):
    __tablename__ = "pollster"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    rating: Mapped[float] = mapped_column(Float, default=1.0)  # aggregation weight multiplier (config data)
    rating_label: Mapped[str | None] = mapped_column(String(8))  # e.g. 'A', 'B+'
    default_method: Mapped[str | None] = mapped_column(String(24))
    is_fictional: Mapped[bool] = mapped_column(Boolean, default=True)

    house_effects: Mapped[list[PollsterHouseEffect]] = relationship(cascade="all, delete-orphan")


class PollsterHouseEffect(Base):
    """Configured (prior) house effect of a pollster toward a party, in percentage points."""

    __tablename__ = "pollster_house_effect"
    __table_args__ = (UniqueConstraint("pollster_id", "party_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    pollster_id: Mapped[int] = mapped_column(ForeignKey("pollster.id"), index=True)
    party_id: Mapped[int] = mapped_column(ForeignKey("party.id"))
    effect_pct: Mapped[float] = mapped_column(Float)


class Poll(Base):
    __tablename__ = "poll"

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[int | None] = mapped_column(ForeignKey("election.id"), index=True)
    race_id: Mapped[int | None] = mapped_column(ForeignKey("race.id"), index=True)
    pollster_id: Mapped[int] = mapped_column(ForeignKey("pollster.id"), index=True)
    #: national_president | province_president | house_district | senate | governor | generic_house | favorability
    poll_type: Mapped[str] = mapped_column(String(24), index=True)
    province_id: Mapped[int | None] = mapped_column(ForeignKey("province.id"))
    district_code: Mapped[str | None] = mapped_column(String(8))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    sample_size: Mapped[int] = mapped_column(Integer)
    population: Mapped[str] = mapped_column(String(4), default="LV")  # LV | RV | A
    method: Mapped[str] = mapped_column(String(24), default="online")  # online | phone | mixed | ivr | panel
    margin_of_error: Mapped[float | None] = mapped_column(Float)
    undecided_pct: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(Text)
    quality_rating: Mapped[float | None] = mapped_column(Float)  # manual per-poll override
    is_fictional: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    results: Mapped[list[PollResult]] = relationship(back_populates="poll", cascade="all, delete-orphan")


class PollResult(Base):
    __tablename__ = "poll_result"

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("poll.id"), index=True)
    party_id: Mapped[int | None] = mapped_column(ForeignKey("party.id"))
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidate.id"))
    label: Mapped[str] = mapped_column(String(120))
    value_pct: Mapped[float] = mapped_column(Float)

    poll: Mapped[Poll] = relationship(back_populates="results")
