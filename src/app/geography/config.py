"""Validated schema of ``config/geography.yaml`` (REAL data sources and the canonical provinces).

from app.geography.config import load_geography_config
cfg = load_geography_config()
cfg.province_codes          # ['GR', 'FR', …] canonical order
cfg.source("supplement")    # SourceSpec
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.core.config import load_config
from app.core.constitution import PROVINCE_COUNT

SourceKind = Literal["file", "wfs_geojson", "odata"]


class SourceSpec(BaseModel):
    """One external dataset (downloaded to ``data/raw``)."""

    name: str
    publisher: str
    url: str
    license: str = "CC BY 4.0"
    filename: str
    #: Vintage of the dataset when it differs from the geography year (e.g. the 2022 supplement).
    year: int | None = None
    #: How to fetch it; inferred from the URL when omitted.
    kind: SourceKind | None = None
    #: Page size used for WFS paging (``count``/``startIndex``) when the server caps results.
    page_size: int = Field(1000, ge=1)

    def resolved_kind(self) -> SourceKind:
        if self.kind:
            return self.kind
        url = self.url.lower()
        if "odatafeed" in url or "/odata/" in url:
            return "odata"
        if "service=wfs" in url and "json" in url:
            return "wfs_geojson"
        return "file"

    def url_for(self, year: int) -> str:
        return self.url.replace("{year}", str(year))

    def filename_for(self, year: int) -> str:
        return self.filename.replace("{year}", str(year))

    def name_for(self, year: int) -> str:
        return self.name.replace("{year}", str(year))

    def vintage_for(self, year: int) -> int:
        return self.year if self.year is not None else year


class EligibleVotersConfig(BaseModel):
    #: Share of adult residents assumed to be registered citizens (flat, documented assumption).
    citizenship_factor: float = Field(0.93, gt=0, le=1)
    #: Share of the 15–25 age band assumed to be 18 or older.
    adult_share_15_25: float = Field(0.7, ge=0, le=1)


class SimplifyConfig(BaseModel):
    provinces_m: float = Field(150.0, ge=0)
    municipalities_m: float = Field(80.0, ge=0)
    districts_m: float = Field(80.0, ge=0)
    units_m: float = Field(20.0, ge=0)


class AdjacencyConfig(BaseModel):
    snap_buffer_m: float = Field(2.0, ge=0)
    min_shared_border_m: float = Field(20.0, ge=0)


class ProvinceSpec(BaseModel):
    code: str = Field(..., pattern=r"^[A-Z]{2}$")
    cbs_code: str = Field(..., pattern=r"^PV\d{2}$")
    name: str
    name_en: str | None = None
    capital: str | None = None


class GeographyConfig(BaseModel):
    year: int = 2025
    sources: dict[str, SourceSpec] = Field(default_factory=dict)
    eligible_voters: EligibleVotersConfig = Field(default_factory=EligibleVotersConfig)
    simplify: SimplifyConfig = Field(default_factory=SimplifyConfig)
    adjacency: AdjacencyConfig = Field(default_factory=AdjacencyConfig)
    provinces: list[ProvinceSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> GeographyConfig:
        codes = [p.code for p in self.provinces]
        if len(codes) != PROVINCE_COUNT:
            raise ValueError(f"expected {PROVINCE_COUNT} provinces, got {len(codes)}")
        if len(set(codes)) != len(codes) or len({p.cbs_code for p in self.provinces}) != len(codes):
            raise ValueError("duplicate province codes")
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def province_codes(self) -> list[str]:
        return [p.code for p in self.provinces]

    @property
    def cbs_to_code(self) -> dict[str, str]:
        return {p.cbs_code: p.code for p in self.provinces}

    def province(self, code: str) -> ProvinceSpec:
        for p in self.provinces:
            if p.code == code:
                return p
        raise KeyError(code)

    def source(self, key: str) -> SourceSpec:
        try:
            return self.sources[key]
        except KeyError as exc:
            raise KeyError(f"source {key!r} not configured in geography.yaml") from exc


def load_geography_config() -> GeographyConfig:
    """Load and validate ``config/geography.yaml`` (cached by :func:`app.core.config.load_config`)."""
    return load_config("geography.yaml", GeographyConfig)
