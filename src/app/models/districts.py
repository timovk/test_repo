"""FICTIONAL electoral geography: apportionment, district plans, House districts, Senate seats."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow


class Apportionment(Base):
    """Allocation of House seats (and thus electoral votes) among provinces for one vintage."""

    __tablename__ = "apportionment"

    id: Mapped[int] = mapped_column(primary_key=True)
    vintage_id: Mapped[int] = mapped_column(ForeignKey("geo_vintage.id"), index=True)
    method: Mapped[str] = mapped_column(String(32))  # huntington_hill | hamilton | webster | jefferson | adams
    population_basis: Mapped[str] = mapped_column(String(32), default="population")
    total_seats: Mapped[int] = mapped_column(Integer)
    min_seats_per_province: Mapped[int] = mapped_column(Integer, default=1)
    senators_per_province: Mapped[int] = mapped_column(Integer, default=2)
    total_electoral_votes: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)

    seats: Mapped[list[ApportionmentSeat]] = relationship(
        back_populates="apportionment", cascade="all, delete-orphan", order_by="ApportionmentSeat.province_id"
    )


class ApportionmentSeat(Base):
    __tablename__ = "apportionment_seat"
    __table_args__ = (UniqueConstraint("apportionment_id", "province_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    apportionment_id: Mapped[int] = mapped_column(ForeignKey("apportionment.id"), index=True)
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"))
    population: Mapped[int] = mapped_column(Integer)
    quota: Mapped[float] = mapped_column(Float)  # exact (fractional) proportional share
    seats: Mapped[int] = mapped_column(Integer)
    electoral_votes: Mapped[int] = mapped_column(Integer)  # seats + senators_per_province
    persons_per_seat: Mapped[float] = mapped_column(Float)

    apportionment: Mapped[Apportionment] = relationship(back_populates="seats")


class DistrictPlan(Base):
    """A complete districting plan (House, or provincial-legislature/municipal-ward plans).

    Reproducible from (vintage, apportionment, seed, config_json); ``config_hash`` identifies
    the generation configuration including manual overrides.
    """

    __tablename__ = "district_plan"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    chamber: Mapped[str] = mapped_column(String(32), default="house")  # house | provincial_legislature | municipal_ward
    vintage_id: Mapped[int] = mapped_column(ForeignKey("geo_vintage.id"), index=True)
    apportionment_id: Mapped[int | None] = mapped_column(ForeignKey("apportionment.id"))
    year: Mapped[int | None] = mapped_column(Integer)  # first election year the plan applies to
    seed: Mapped[int] = mapped_column(BigInteger)
    method: Mapped[str] = mapped_column(String(48))
    config_json: Mapped[str | None] = mapped_column(Text)  # generation config document (JSON)
    config_hash: Mapped[str] = mapped_column(String(16))
    total_districts: Mapped[int] = mapped_column(Integer)
    max_abs_deviation_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mean_abs_deviation_pct: Mapped[float] = mapped_column(Float, default=0.0)
    split_municipalities: Mapped[int] = mapped_column(Integer, default=0)
    noncontiguous_districts: Mapped[int] = mapped_column(Integer, default=0)
    overrides_applied: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_fictional: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    generation_seconds: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    districts: Mapped[list[HouseDistrict]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="HouseDistrict.code"
    )


class HouseDistrict(Base):
    """A single-member district (House districts use codes like ``NB-07``)."""

    __tablename__ = "house_district"
    __table_args__ = (UniqueConstraint("plan_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("district_plan.id"), index=True)
    code: Mapped[str] = mapped_column(String(8), index=True)  # 'NB-07'
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(160))  # descriptive, e.g. "Tilburg-Noord"
    population: Mapped[int] = mapped_column(Integer)
    eligible_voters_est: Mapped[int] = mapped_column(Integer, default=0)
    target_population: Mapped[float] = mapped_column(Float)
    deviation_pct: Mapped[float] = mapped_column(Float)  # (pop - target) / target × 100
    area_km2: Mapped[float] = mapped_column(Float)
    polsby_popper: Mapped[float | None] = mapped_column(Float)
    reock: Mapped[float | None] = mapped_column(Float)
    convex_hull_ratio: Mapped[float | None] = mapped_column(Float)
    n_units: Mapped[int] = mapped_column(Integer)
    n_municipalities: Mapped[int] = mapped_column(Integer)
    n_split_municipalities: Mapped[int] = mapped_column(Integer)
    urban_share: Mapped[float | None] = mapped_column(Float)  # pop share in CBS urbanity 1–2
    rural_share: Mapped[float | None] = mapped_column(Float)  # pop share in CBS urbanity 4–5
    is_contiguous: Mapped[bool] = mapped_column(Boolean, default=True)
    n_components: Mapped[int] = mapped_column(Integer, default=1)
    centroid_lon: Mapped[float] = mapped_column(Float)
    centroid_lat: Mapped[float] = mapped_column(Float)
    geometry_wkb: Mapped[bytes | None] = mapped_column(LargeBinary)  # EPSG:4326, simplified

    plan: Mapped[DistrictPlan] = relationship(back_populates="districts")


class DistrictAssignment(Base):
    """Assignment of every geographic unit (CBS buurt) to exactly one district of a plan."""

    __tablename__ = "district_assignment"
    __table_args__ = (UniqueConstraint("plan_id", "geo_unit_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("district_plan.id"), index=True)
    geo_unit_id: Mapped[int] = mapped_column(ForeignKey("geo_unit.id"), index=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("house_district.id"), index=True)
    source: Mapped[str] = mapped_column(String(16), default="generated")  # generated | override


class DistrictMunicipality(Base):
    """District × municipality fragments (population-weighted), used for aggregation and stats."""

    __tablename__ = "district_municipality"
    __table_args__ = (UniqueConstraint("district_id", "municipality_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    district_id: Mapped[int] = mapped_column(ForeignKey("house_district.id"), index=True)
    municipality_id: Mapped[int] = mapped_column(ForeignKey("municipality.id"), index=True)
    population: Mapped[int] = mapped_column(Integer)
    n_units: Mapped[int] = mapped_column(Integer)
    share_of_municipality: Mapped[float] = mapped_column(Float)
    share_of_district: Mapped[float] = mapped_column(Float)


class DistrictAdjacency(Base):
    __tablename__ = "district_adjacency"
    __table_args__ = (UniqueConstraint("district_a_id", "district_b_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("district_plan.id"), index=True)
    district_a_id: Mapped[int] = mapped_column(ForeignKey("house_district.id"))
    district_b_id: Mapped[int] = mapped_column(ForeignKey("house_district.id"))
    shared_border_km: Mapped[float] = mapped_column(Float)
    via_water_link: Mapped[bool] = mapped_column(Boolean, default=False)


class SenateSeat(Base):
    """One of the 24 Senate seats: two per province, in different classes (I, II, III)."""

    __tablename__ = "senate_seat"
    __table_args__ = (UniqueConstraint("province_id", "seat_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True)  # 'SEN-NB-1'
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"), index=True)
    seat_number: Mapped[int] = mapped_column(Integer)  # 1 | 2
    senate_class: Mapped[int] = mapped_column(Integer, index=True)  # 1 | 2 | 3
    office_id: Mapped[int | None] = mapped_column(ForeignKey("office.id"))
