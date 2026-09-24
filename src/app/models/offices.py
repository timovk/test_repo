"""Offices, office holders, governor/mayor seats, legislatures, vacancies (FICTIONAL)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Office(Base):
    """An elected office.  House offices are keyed by district *code* so they survive redistricting."""

    __tablename__ = "office"

    id: Mapped[int] = mapped_column(primary_key=True)
    office_type: Mapped[str] = mapped_column(String(24), index=True)  # OfficeType
    code: Mapped[str] = mapped_column(
        String(24), unique=True
    )  # PRES, VP, HOUSE-NB-07, SEN-NB-1, GOV-NB, MAYOR-GM0855
    name: Mapped[str] = mapped_column(String(160))
    province_id: Mapped[int | None] = mapped_column(ForeignKey("province.id"), index=True)
    district_code: Mapped[str | None] = mapped_column(String(8))
    municipality_code: Mapped[str | None] = mapped_column(String(6))
    senate_class: Mapped[int | None] = mapped_column(Integer)
    term_years: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    holders: Mapped[list[OfficeHolder]] = relationship(
        back_populates="office", order_by="OfficeHolder.term_start"
    )


class OfficeHolder(Base):
    __tablename__ = "office_holder"

    id: Mapped[int] = mapped_column(primary_key=True)
    office_id: Mapped[int] = mapped_column(ForeignKey("office.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidate.id"), index=True)
    party_id: Mapped[int | None] = mapped_column(ForeignKey("party.id"))
    term_start: Mapped[date] = mapped_column(Date)
    term_end: Mapped[date | None] = mapped_column(Date)  # scheduled end
    ended_on: Mapped[date | None] = mapped_column(Date)  # actual end (None = serving)
    start_reason: Mapped[str] = mapped_column(
        String(24), default="elected"
    )  # elected | appointed | succeeded | founding
    end_reason: Mapped[str | None] = mapped_column(
        String(24)
    )  # term_expired | defeated | retired | resigned | died
    election_race_id: Mapped[int | None] = mapped_column(ForeignKey("race.id"))

    office: Mapped[Office] = relationship(back_populates="holders")


class GovernorSeat(Base):
    __tablename__ = "governor_seat"

    id: Mapped[int] = mapped_column(primary_key=True)
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"), unique=True)
    office_id: Mapped[int] = mapped_column(ForeignKey("office.id"))
    lt_office_id: Mapped[int | None] = mapped_column(ForeignKey("office.id"))
    first_election_year: Mapped[int] = mapped_column(Integer)
    approval: Mapped[float | None] = mapped_column(Integer)  # simulation variable (%), current governor


class MayorSeat(Base):
    __tablename__ = "mayor_seat"

    id: Mapped[int] = mapped_column(primary_key=True)
    municipality_code: Mapped[str] = mapped_column(String(6), unique=True)
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"))
    office_id: Mapped[int] = mapped_column(ForeignKey("office.id"))
    first_election_year: Mapped[int] = mapped_column(Integer)


class Legislature(Base):
    """Provincial legislature (Provinciale Staten ≙ state legislature) or municipal council."""

    __tablename__ = "legislature"
    __table_args__ = (UniqueConstraint("level", "jurisdiction_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(16))  # provincial | municipal
    jurisdiction_code: Mapped[str] = mapped_column(String(8))  # province code or CBS municipality code
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"))
    name: Mapped[str] = mapped_column(String(160))
    seats: Mapped[int] = mapped_column(Integer)
    electoral_system: Mapped[str] = mapped_column(String(32))  # ElectoralSystem
    size_rule: Mapped[str] = mapped_column(String(48))  # 'population_brackets' | 'fixed:<n>' | ...
    term_years: Mapped[int] = mapped_column(Integer, default=4)


class LegislatureSeatResult(Base):
    """Seats won per party in a legislature election (proportional or ward-based)."""

    __tablename__ = "legislature_seat_result"
    __table_args__ = (UniqueConstraint("race_id", "party_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("race.id"), index=True)
    legislature_id: Mapped[int] = mapped_column(ForeignKey("legislature.id"))
    party_id: Mapped[int] = mapped_column(ForeignKey("party.id"))
    votes: Mapped[int] = mapped_column(Integer)
    seats: Mapped[int] = mapped_column(Integer)


class Vacancy(Base):
    __tablename__ = "vacancy"

    id: Mapped[int] = mapped_column(primary_key=True)
    office_id: Mapped[int] = mapped_column(ForeignKey("office.id"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(32))  # died | resigned | appointed_elsewhere | removed
    appointed_holder_id: Mapped[int | None] = mapped_column(ForeignKey("office_holder.id"))
    special_race_id: Mapped[int | None] = mapped_column(ForeignKey("race.id"))
    resolved_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
