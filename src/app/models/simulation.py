"""Scenarios, campaigns, simulation runs and Monte Carlo outputs (FICTIONAL / SIMULATED)."""

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow


class Scenario(Base):
    """A human-readable scenario document (YAML) plus its metadata.

    The document is the natural unit of editing/import/export, so it is stored as text;
    everything the engine derives from it (parties, candidates, races …) is materialised
    relationally when an election is created.
    """

    __tablename__ = "scenario"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int] = mapped_column(Integer)
    seed: Mapped[int] = mapped_column(BigInteger)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("scenario.id"))
    document: Mapped[str] = mapped_column(Text)  # YAML
    document_hash: Mapped[str] = mapped_column(String(16))
    is_fictional: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Campaign(Base):
    __tablename__ = "campaign"
    __table_args__ = (UniqueConstraint("race_id", "ballot_candidate_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("election.id"), index=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("race.id"), index=True)
    ballot_candidate_id: Mapped[int] = mapped_column(ForeignKey("ballot_candidate.id"))
    party_id: Mapped[int | None] = mapped_column(ForeignKey("party.id"))
    budget: Mapped[float] = mapped_column(Float)  # abstract resource units
    strategy: Mapped[str] = mapped_column(String(24), default="balanced")
    seed: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    allocations: Mapped[list[CampaignAllocation]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignAllocation(Base):
    __tablename__ = "campaign_allocation"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaign.id"), index=True)
    target_level: Mapped[str] = mapped_column(String(12))  # national | province | district | municipality
    target_code: Mapped[str] = mapped_column(String(12))  # 'NL' | 'NB' | 'NB-07' | 'GM0855'
    action: Mapped[str] = mapped_column(
        String(20)
    )  # rally | advertising | field | visit | debate_prep | fundraising | gotv
    amount: Mapped[float] = mapped_column(Float)
    units: Mapped[int | None] = mapped_column(Integer)  # rallies / visits count
    proceeds: Mapped[float | None] = mapped_column(Float)  # fundraising proceeds
    week: Mapped[int | None] = mapped_column(Integer)
    expected_effect: Mapped[float | None] = mapped_column(Float)  # logit points, before uncertainty
    realized_effect: Mapped[float | None] = mapped_column(Float)  # logit points, after seeded draw
    turnout_effect: Mapped[float | None] = mapped_column(Float)

    campaign: Mapped[Campaign] = relationship(back_populates="allocations")


class SimulationRun(Base):
    """Audit record of any stochastic run: kind, seed, config hash, timing, summary."""

    __tablename__ = "simulation_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(
        String(24), index=True
    )  # election | forecast | night | districts | campaign | recount
    election_id: Mapped[int | None] = mapped_column(ForeignKey("election.id"), index=True)
    scenario_id: Mapped[int | None] = mapped_column(ForeignKey("scenario.id"))
    seed: Mapped[int] = mapped_column(BigInteger)
    n_simulations: Mapped[int | None] = mapped_column(Integer)
    config_hash: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | completed | failed
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_s: Mapped[float | None] = mapped_column(Float)
    summary_json: Mapped[str | None] = mapped_column(Text)
    code_version: Mapped[str | None] = mapped_column(String(40))


class ForecastSummary(Base):
    """Per race × ballot line Monte Carlo summary (model-generated estimate, not a prediction)."""

    __tablename__ = "forecast_summary"
    __table_args__ = (UniqueConstraint("run_id", "race_id", "ballot_candidate_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_run.id"), index=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("race.id"), index=True)
    ballot_candidate_id: Mapped[int] = mapped_column(ForeignKey("ballot_candidate.id"))
    win_probability: Mapped[float] = mapped_column(Float)
    mean_share: Mapped[float] = mapped_column(Float)
    p05_share: Mapped[float] = mapped_column(Float)
    p50_share: Mapped[float] = mapped_column(Float)
    p95_share: Mapped[float] = mapped_column(Float)
    mean_votes: Mapped[float | None] = mapped_column(Float)


class ForecastDistribution(Base):
    """Histogram rows: subject ∈ {ev, house_seats, senate_seats, popular_vote_share, governor_wins}."""

    __tablename__ = "forecast_distribution"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_run.id"), index=True)
    subject: Mapped[str] = mapped_column(String(24))
    key: Mapped[str] = mapped_column(String(24))  # party code or ticket (ballot_candidate id as str)
    value: Mapped[float] = mapped_column(Float)
    probability: Mapped[float] = mapped_column(Float)


class ForecastAggregate(Base):
    """Scalar outcome metrics per key: expected EV / seats, median, percentiles, majority odds."""

    __tablename__ = "forecast_aggregate"
    __table_args__ = (UniqueConstraint("run_id", "subject", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_run.id"), index=True)
    subject: Mapped[str] = mapped_column(String(24))  # ev | house_seats | senate_seats | popular_vote
    key: Mapped[str] = mapped_column(String(24))
    mean: Mapped[float] = mapped_column(Float)
    median: Mapped[float] = mapped_column(Float)
    p05: Mapped[float] = mapped_column(Float)
    p25: Mapped[float] = mapped_column(Float)
    p75: Mapped[float] = mapped_column(Float)
    p95: Mapped[float] = mapped_column(Float)
    prob_majority: Mapped[float | None] = mapped_column(Float)
    prob_plurality: Mapped[float | None] = mapped_column(Float)


class ForecastCombination(Base):
    """Most frequent Electoral College maps (province → winner) in a forecast run."""

    __tablename__ = "forecast_combination"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_run.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    combination_key: Mapped[str] = mapped_column(Text)  # 'GR:PA|FR:CVU|…'
    frequency: Mapped[float] = mapped_column(Float)
    ev_json: Mapped[str] = mapped_column(Text)  # {"<ticket key>": ev, ...}
