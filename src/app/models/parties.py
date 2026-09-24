"""FICTIONAL political actors: parties (with lineage) and candidates (with careers)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow


class Party(Base):
    """A political party.  Parties can be renamed/merged/split/dissolved via :class:`PartyEvent`
    without corrupting history: ballot rows keep a snapshot of the name/colour used at the time."""

    __tablename__ = "party"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True)  # stable internal id, e.g. 'RLP'
    name: Mapped[str] = mapped_column(String(120))
    abbreviation: Mapped[str] = mapped_column(String(16))
    color: Mapped[str] = mapped_column(String(9))  # '#1f4e9c'
    color_secondary: Mapped[str | None] = mapped_column(String(9))
    family: Mapped[str | None] = mapped_column(String(40))  # ideological family label
    description: Mapped[str | None] = mapped_column(Text)
    ideology_economic: Mapped[float] = mapped_column(Float, default=0.0)  # −1 left … +1 right
    ideology_social: Mapped[float] = mapped_column(Float, default=0.0)  # −1 progressive … +1 conservative
    ideology_europe: Mapped[float] = mapped_column(Float, default=0.0)  # −1 eurosceptic … +1 pro-EU
    base_support: Mapped[float] = mapped_column(Float, default=0.0)  # baseline national share (0–1)
    turnout_propensity: Mapped[float] = mapped_column(Float, default=0.0)  # logit shift of supporters' turnout
    founded_year: Mapped[int | None] = mapped_column(Integer)
    dissolved_year: Mapped[int | None] = mapped_column(Integer)
    successor_party_id: Mapped[int | None] = mapped_column(ForeignKey("party.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_fictional: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    modifiers: Mapped[list[PartyModifier]] = relationship(back_populates="party", cascade="all, delete-orphan")


class PartyModifier(Base):
    """Normalised FICTIONAL model parameter of a party.

    ``scope`` ∈ {province, municipality, region, urbanity, demographic, candidate}; ``key`` names
    the target (province code, CBS municipality code, region id, urbanity class, demographic
    variable); ``value`` is a logit-scale utility shift (demographic: coefficient per SD).
    """

    __tablename__ = "party_modifier"
    __table_args__ = (UniqueConstraint("party_id", "scope", "key", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    party_id: Mapped[int] = mapped_column(ForeignKey("party.id"), index=True)
    scope: Mapped[str] = mapped_column(String(16), index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(80), default="config")

    party: Mapped[Party] = relationship(back_populates="modifiers")


class PartyEvent(Base):
    """Lineage events: founded, renamed, recolored, merged_into, split_from, dissolved."""

    __tablename__ = "party_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    party_id: Mapped[int] = mapped_column(ForeignKey("party.id"), index=True)
    year: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(24))
    related_party_id: Mapped[int | None] = mapped_column(ForeignKey("party.id"))
    old_value: Mapped[str | None] = mapped_column(String(160))
    new_value: Mapped[str | None] = mapped_column(String(160))
    notes: Mapped[str | None] = mapped_column(Text)


class Candidate(Base):
    """A FICTIONAL person who can run in many elections over a career."""

    __tablename__ = "candidate"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Stable slug identifying the person across elections (scenario candidate key).
    key: Mapped[str] = mapped_column(String(80), unique=True)
    first_name: Mapped[str] = mapped_column(String(60))
    last_name: Mapped[str] = mapped_column(String(80))
    full_name: Mapped[str] = mapped_column(String(160), index=True)
    gender: Mapped[str | None] = mapped_column(String(8))
    birth_date: Mapped[date | None] = mapped_column(Date)
    home_municipality_code: Mapped[str | None] = mapped_column(String(6))  # CBS code (vintage-independent)
    home_province_id: Mapped[int | None] = mapped_column(ForeignKey("province.id"))
    party_id: Mapped[int | None] = mapped_column(ForeignKey("party.id"), index=True)  # current affiliation
    ideology_economic: Mapped[float] = mapped_column(Float, default=0.0)
    ideology_social: Mapped[float] = mapped_column(Float, default=0.0)
    quality: Mapped[float] = mapped_column(Float, default=0.0)  # z-score-like; +1 = strong candidate
    campaign_strength: Mapped[float] = mapped_column(Float, default=0.0)
    fundraising: Mapped[float] = mapped_column(Float, default=1.0)  # abstract resource units (1 = typical)
    favorability: Mapped[float | None] = mapped_column(Float)  # net favorability, percentage points
    approval: Mapped[float | None] = mapped_column(Float)  # job approval % if an office holder
    bio: Mapped[str | None] = mapped_column(Text)
    portrait_key: Mapped[str | None] = mapped_column(String(40))  # deterministic placeholder avatar
    is_fictional: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    affiliations: Mapped[list[CandidateAffiliation]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )


class CandidateAffiliation(Base):
    """Party membership history (supports party switches)."""

    __tablename__ = "candidate_affiliation"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidate.id"), index=True)
    party_id: Mapped[int | None] = mapped_column(ForeignKey("party.id"))
    from_year: Mapped[int] = mapped_column(Integer)
    to_year: Mapped[int | None] = mapped_column(Integer)

    candidate: Mapped[Candidate] = relationship(back_populates="affiliations")
