"""REAL geographic data: provinces, municipalities, CBS neighbourhoods (precincts), provenance.

Everything in this module is sourced from official Dutch open data (CBS / PDOK) or
deterministically derived from it (``DataCategory.DERIVED``, e.g. estimated eligible voters).
No fictional political data is stored here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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


class DataSource(Base):
    """Provenance record of an external (REAL) dataset."""

    __tablename__ = "data_source"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True)  # e.g. 'cbs_wijkenbuurten_2025'
    name: Mapped[str] = mapped_column(String(200))
    publisher: Mapped[str] = mapped_column(String(120))  # 'CBS via PDOK'
    url: Mapped[str] = mapped_column(Text)
    license: Mapped[str] = mapped_column(String(80))  # 'CC BY 4.0'
    data_category: Mapped[str] = mapped_column(String(16), default="REAL")
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    local_path: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


class GeoVintage(Base):
    """One edition of the geographic hierarchy (CBS Wijk- en Buurtkaart of a given year).

    Municipal mergers produce a new vintage; :class:`MunicipalityLineage` links them.
    """

    __tablename__ = "geo_vintage"

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, unique=True)
    label: Mapped[str] = mapped_column(String(120))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_source.id"))
    province_count: Mapped[int] = mapped_column(Integer, default=0)
    municipality_count: Mapped[int] = mapped_column(Integer, default=0)
    unit_count: Mapped[int] = mapped_column(Integer, default=0)
    population_total: Mapped[int] = mapped_column(Integer, default=0)
    processed_path: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    built_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source: Mapped[DataSource | None] = relationship()


class Province(Base):
    """A province (≙ U.S. state).  Identity is stable across vintages (ids 1..12)."""

    __tablename__ = "province"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(2), unique=True)  # 'NB'
    cbs_code: Mapped[str] = mapped_column(String(4), unique=True)  # 'PV30'
    name: Mapped[str] = mapped_column(String(60), unique=True)  # 'Noord-Brabant'
    name_en: Mapped[str | None] = mapped_column(String(60))
    capital: Mapped[str | None] = mapped_column(String(60))
    sort_order: Mapped[int] = mapped_column(Integer)

    municipalities: Mapped[list[Municipality]] = relationship(back_populates="province")


class ProvinceStats(Base):
    """Per-vintage REAL statistics and simplified geometry of a province."""

    __tablename__ = "province_stats"
    __table_args__ = (UniqueConstraint("province_id", "vintage_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"), index=True)
    vintage_id: Mapped[int] = mapped_column(ForeignKey("geo_vintage.id"), index=True)
    population: Mapped[int] = mapped_column(Integer)
    population_official: Mapped[int | None] = mapped_column(Integer)
    eligible_voters_est: Mapped[int] = mapped_column(Integer, default=0)
    area_km2: Mapped[float] = mapped_column(Float)
    land_area_km2: Mapped[float] = mapped_column(Float)
    density: Mapped[float] = mapped_column(Float)
    municipality_count: Mapped[int] = mapped_column(Integer)
    unit_count: Mapped[int] = mapped_column(Integer, default=0)
    centroid_lon: Mapped[float] = mapped_column(Float)
    centroid_lat: Mapped[float] = mapped_column(Float)
    geometry_wkb: Mapped[bytes | None] = mapped_column(LargeBinary)  # EPSG:4326, simplified


class Municipality(Base):
    """A municipality / gemeente (≙ U.S. county), REAL CBS data for one vintage."""

    __tablename__ = "municipality"
    __table_args__ = (UniqueConstraint("vintage_id", "cbs_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vintage_id: Mapped[int] = mapped_column(ForeignKey("geo_vintage.id"), index=True)
    cbs_code: Mapped[str] = mapped_column(String(6), index=True)  # 'GM0855'
    name: Mapped[str] = mapped_column(String(80), index=True)
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"), index=True)
    #: Sum of the municipality's CBS neighbourhood figures (canonical for all aggregation).
    population: Mapped[int] = mapped_column(Integer)
    #: Official CBS municipal total (neighbourhood figures are rounded by CBS, so tiny deltas exist).
    population_official: Mapped[int | None] = mapped_column(Integer)
    eligible_voters_est: Mapped[int] = mapped_column(Integer, default=0)  # DERIVED
    area_km2: Mapped[float] = mapped_column(Float)
    land_area_km2: Mapped[float] = mapped_column(Float)
    density: Mapped[float] = mapped_column(Float)  # inhabitants / km² land
    urbanity_class: Mapped[int | None] = mapped_column(Integer)  # CBS stedelijkheid 1 (very) .. 5 (not)
    address_density: Mapped[float | None] = mapped_column(Float)  # CBS omgevingsadressendichtheid
    unit_count: Mapped[int] = mapped_column(Integer, default=0)
    centroid_lon: Mapped[float] = mapped_column(Float)
    centroid_lat: Mapped[float] = mapped_column(Float)
    centroid_x: Mapped[float] = mapped_column(Float)  # EPSG:28992 (RD New) metres
    centroid_y: Mapped[float] = mapped_column(Float)
    geometry_wkb: Mapped[bytes | None] = mapped_column(LargeBinary)  # EPSG:4326, simplified

    province: Mapped[Province] = relationship(back_populates="municipalities")
    demographics: Mapped[MunicipalityDemographics | None] = relationship(
        back_populates="municipality", uselist=False, cascade="all, delete-orphan"
    )


class DemographicsMixin:
    """REAL CBS demographic indicators (percentages 0–100 unless noted).  NULL = unavailable."""

    pct_age_0_15: Mapped[float | None] = mapped_column(Float)
    pct_age_15_25: Mapped[float | None] = mapped_column(Float)
    pct_age_25_45: Mapped[float | None] = mapped_column(Float)
    pct_age_45_65: Mapped[float | None] = mapped_column(Float)
    pct_age_65_plus: Mapped[float | None] = mapped_column(Float)
    pct_single_households: Mapped[float | None] = mapped_column(Float)
    pct_households_with_children: Mapped[float | None] = mapped_column(Float)
    avg_household_size: Mapped[float | None] = mapped_column(Float)
    pct_origin_nl: Mapped[float | None] = mapped_column(Float)
    pct_origin_europe: Mapped[float | None] = mapped_column(Float)
    pct_origin_non_europe: Mapped[float | None] = mapped_column(Float)
    pct_education_low: Mapped[float | None] = mapped_column(Float)
    pct_education_mid: Mapped[float | None] = mapped_column(Float)
    pct_education_high: Mapped[float | None] = mapped_column(Float)
    income_per_capita_keur: Mapped[float | None] = mapped_column(Float)
    pct_owner_occupied: Mapped[float | None] = mapped_column(Float)
    #: Year of the CBS release each group was taken from (core / education / income).
    source_year_core: Mapped[int | None] = mapped_column(Integer)
    source_year_supplement: Mapped[int | None] = mapped_column(Integer)
    #: Comma-separated list of fields whose values were imputed (DERIVED, not REAL).
    imputed_fields: Mapped[str | None] = mapped_column(Text)


class MunicipalityDemographics(DemographicsMixin, Base):
    __tablename__ = "municipality_demographics"

    municipality_id: Mapped[int] = mapped_column(ForeignKey("municipality.id"), primary_key=True)
    municipality: Mapped[Municipality] = relationship(back_populates="demographics")


class GeoUnit(Base):
    """Smallest geographic unit — a CBS *buurt* (neighbourhood), used as the precinct.

    Dutch polling-station districts have no official polygons, so the CBS neighbourhood is
    the documented precinct substitute (docs/DATA_PROVENANCE.md).
    """

    __tablename__ = "geo_unit"
    __table_args__ = (UniqueConstraint("vintage_id", "cbs_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vintage_id: Mapped[int] = mapped_column(ForeignKey("geo_vintage.id"), index=True)
    cbs_code: Mapped[str] = mapped_column(String(10), index=True)  # 'BU08550101'
    name: Mapped[str] = mapped_column(String(120))
    unit_kind: Mapped[str] = mapped_column(String(16), default="buurt")
    wijk_code: Mapped[str | None] = mapped_column(String(8))
    municipality_id: Mapped[int] = mapped_column(ForeignKey("municipality.id"), index=True)
    province_id: Mapped[int] = mapped_column(ForeignKey("province.id"), index=True)
    population: Mapped[int] = mapped_column(Integer)
    eligible_voters_est: Mapped[int] = mapped_column(Integer, default=0)  # DERIVED
    area_km2: Mapped[float] = mapped_column(Float)
    land_area_km2: Mapped[float] = mapped_column(Float)
    density: Mapped[float] = mapped_column(Float)
    urbanity_class: Mapped[int | None] = mapped_column(Integer)
    address_density: Mapped[float | None] = mapped_column(Float)
    centroid_lon: Mapped[float] = mapped_column(Float)
    centroid_lat: Mapped[float] = mapped_column(Float)
    centroid_x: Mapped[float] = mapped_column(Float)
    centroid_y: Mapped[float] = mapped_column(Float)

    municipality: Mapped[Municipality] = relationship()
    demographics: Mapped[GeoUnitDemographics | None] = relationship(
        back_populates="unit", uselist=False, cascade="all, delete-orphan"
    )


class GeoUnitDemographics(DemographicsMixin, Base):
    __tablename__ = "geo_unit_demographics"

    geo_unit_id: Mapped[int] = mapped_column(ForeignKey("geo_unit.id"), primary_key=True)
    unit: Mapped[GeoUnit] = relationship(back_populates="demographics")


class MunicipalityLineage(Base):
    """Maps municipalities of an older vintage onto a newer one (mergers / boundary changes).

    ``population_weight`` is the share of the *old* municipality's population that moved to the
    new municipality (1.0 for a clean merger).  Used for cross-vintage swing comparisons.
    """

    __tablename__ = "municipality_lineage"
    __table_args__ = (UniqueConstraint("from_vintage_id", "to_vintage_id", "from_code", "to_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    from_vintage_id: Mapped[int] = mapped_column(ForeignKey("geo_vintage.id"))
    to_vintage_id: Mapped[int] = mapped_column(ForeignKey("geo_vintage.id"))
    from_code: Mapped[str] = mapped_column(String(6))
    to_code: Mapped[str] = mapped_column(String(6))
    population_weight: Mapped[float] = mapped_column(Float, default=1.0)
    event: Mapped[str] = mapped_column(String(24), default="merger")  # merger | split | rename | unchanged
    effective_date: Mapped[str | None] = mapped_column(String(10))
    notes: Mapped[str | None] = mapped_column(Text)
