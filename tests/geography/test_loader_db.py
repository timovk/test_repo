"""DB loader: synthetic geography into an in-memory SQLite session; idempotency; lineage rows."""

from __future__ import annotations

import pandas as pd
import pytest
import shapely
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.geography.lineage import compute_lineage
from app.geography.loader_db import ACTIVE_VINTAGE_KEY, load_tables_into_db, save_lineage
from app.geography.synthetic import SyntheticGeography
from app.models import (
    AppMeta,
    DataSource,
    GeoUnit,
    GeoUnitDemographics,
    GeoVintage,
    Municipality,
    MunicipalityDemographics,
    MunicipalityLineage,
    Province,
    ProvinceStats,
)

SOURCES = [
    {
        "key": "wijkenbuurten",
        "name": "CBS Wijk- en Buurtkaart 2025",
        "publisher": "CBS via PDOK",
        "url": "https://example.test/wb.gpkg",
        "license": "CC BY 4.0",
        "sha256": "0" * 64,
        "bytes": 123,
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "vintage": 2025,
        "path": "data/raw/wb.gpkg",
    }
]


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _load(
    session: Session, synthetic: SyntheticGeography, year: int = 2025, force: bool = False
) -> GeoVintage:
    return load_tables_into_db(
        session,
        year,
        synthetic.provinces,
        synthetic.municipalities,
        synthetic.units,
        sources=SOURCES,
        force=force,
    )


def test_load_synthetic(db_session: Session, synthetic: SyntheticGeography) -> None:
    vintage = _load(db_session, synthetic)
    assert vintage.year == 2025 and vintage.is_active
    assert vintage.unit_count == len(synthetic.units)
    assert vintage.municipality_count == len(synthetic.municipalities)
    assert vintage.population_total == int(synthetic.units["population"].sum())
    provinces = db_session.scalars(select(Province).order_by(Province.id)).all()
    assert [p.id for p in provinces] == list(range(1, 13))
    assert [p.code for p in provinces][:3] == ["GR", "FR", "DR"]
    assert provinces[10].name_en == "North Brabant" and provinces[10].capital == "'s-Hertogenbosch"
    assert _count(db_session, GeoUnit) == len(synthetic.units)
    assert _count(db_session, GeoUnitDemographics) == len(synthetic.units)
    assert _count(db_session, Municipality) == len(synthetic.municipalities)
    assert _count(db_session, MunicipalityDemographics) == len(synthetic.municipalities)
    stats = db_session.scalars(select(ProvinceStats)).all()
    assert len(stats) == 12
    geom = shapely.from_wkb(stats[0].geometry_wkb)
    assert geom.is_valid and -10 < geom.bounds[0] < 20  # EPSG:4326 degrees
    assert sum(s.population for s in stats) == vintage.population_total
    muni = db_session.scalar(select(Municipality).where(Municipality.cbs_code == "GM0101"))
    units_pop = int(synthetic.units.loc[synthetic.units["municipality_code"] == "GM0101", "population"].sum())
    assert muni.population == units_pop and muni.geometry_wkb is not None
    assert muni.demographics is not None and muni.demographics.pct_education_high is not None
    unit = db_session.scalar(select(GeoUnit).where(GeoUnit.cbs_code == "BU01000000"))
    assert unit.municipality_id == muni.id and unit.province_id == 1
    assert unit.demographics.pct_age_15_25 is not None
    assert db_session.get(AppMeta, ACTIVE_VINTAGE_KEY).value == "2025"
    src = db_session.scalar(select(DataSource))
    assert src.key == "wijkenbuurten_2025" and vintage.source_id == src.id


def test_load_is_idempotent_and_force_keeps_ids(db_session: Session, synthetic: SyntheticGeography) -> None:
    _load(db_session, synthetic)
    ids_before = dict(db_session.execute(select(GeoUnit.cbs_code, GeoUnit.id)).all())
    again = _load(db_session, synthetic)
    assert _count(db_session, GeoUnit) == len(synthetic.units)
    assert _count(db_session, GeoVintage) == 1 and _count(db_session, Province) == 12
    assert _count(db_session, DataSource) == 1
    # forced reload with a modified population re-syncs rows in place
    units = synthetic.units.copy()
    units.loc[units["code"] == "BU01000000", "population"] += 7
    load_tables_into_db(
        db_session, 2025, synthetic.provinces, synthetic.municipalities, units, sources=SOURCES, force=True
    )
    ids_after = dict(db_session.execute(select(GeoUnit.cbs_code, GeoUnit.id)).all())
    assert ids_after == ids_before
    assert _count(db_session, GeoUnitDemographics) == len(units)
    db_session.expire_all()
    unit = db_session.scalar(select(GeoUnit).where(GeoUnit.cbs_code == "BU01000000"))
    assert unit.population == int(synthetic.units.loc[0, "population"]) + 7
    assert db_session.get(GeoVintage, again.id).population_total == int(units["population"].sum())


def test_second_vintage_with_merger_and_lineage(db_session: Session, synthetic: SyntheticGeography) -> None:
    _load(db_session, synthetic, 2025)
    # 2027: GM0102 merges into GM0101 (same neighbourhood codes kept)
    units = synthetic.units.copy()
    units.loc[units["municipality_code"] == "GM0102", "municipality_code"] = "GM0101"
    munis = units.dissolve(
        by="municipality_code", aggfunc={"population": "sum", "province_code": "first"}
    ).reset_index()
    munis = munis.rename(columns={"municipality_code": "code"})
    munis["name"] = "Gemeente " + munis["code"]
    v2 = load_tables_into_db(db_session, 2027, synthetic.provinces, munis, units)
    assert v2.is_active and v2.municipality_count == len(synthetic.municipalities) - 1
    assert db_session.get(AppMeta, ACTIVE_VINTAGE_KEY).value == "2027"
    active = db_session.scalars(select(GeoVintage).where(GeoVintage.is_active)).all()
    assert [v.year for v in active] == [2027]
    lin = compute_lineage(
        pd.DataFrame(synthetic.units.drop(columns="geometry")), pd.DataFrame(units.drop(columns="geometry"))
    )
    merged = lin[lin["to_code"] == "GM0101"]
    assert set(merged["from_code"]) == {"GM0101", "GM0102"} and set(merged["event"]) == {"merger"}
    n = save_lineage(db_session, 2025, 2027, lin)
    assert n == len(lin) == _count(db_session, MunicipalityLineage)
    assert save_lineage(db_session, 2025, 2027, lin) == n and _count(db_session, MunicipalityLineage) == n


# --------------------------------------------------------------------------- review regressions
def test_reload_detects_changed_content(db_session: Session, synthetic: SyntheticGeography) -> None:
    code = "BU01000000"

    def income() -> float:
        db_session.expire_all()
        unit = db_session.scalar(select(GeoUnit).where(GeoUnit.cbs_code == code))
        return float(unit.demographics.income_per_capita_keur)

    def load(units: pd.DataFrame, fingerprint: str | None) -> None:
        load_tables_into_db(
            db_session,
            2025,
            synthetic.provinces,
            synthetic.municipalities,
            units,
            SOURCES,
            fingerprint=fingerprint,
        )

    load(synthetic.units, "fp-1")
    base = income()
    changed = synthetic.units.copy()
    changed.loc[changed["code"] == code, "income_per_capita_keur"] = base + 5.0
    load(changed, "fp-1")  # same fingerprint and counts → skipped
    assert income() == pytest.approx(base)
    load(changed, "fp-2")  # new store content → re-synchronised in place
    assert income() == pytest.approx(base + 5.0)
    # without fingerprints a changed population total is still detected
    pop_changed = changed.copy()
    pop_changed.loc[pop_changed["code"] == code, "population"] += 3
    load(pop_changed, None)
    vintage = db_session.scalar(select(GeoVintage).where(GeoVintage.year == 2025))
    assert vintage.population_total == int(pop_changed["population"].sum())
    assert vintage.source_id is not None
    # a reload without provenance keeps the recorded source
    load_tables_into_db(
        db_session, 2025, synthetic.provinces, synthetic.municipalities, synthetic.units, force=True
    )
    assert db_session.get(GeoVintage, vintage.id).source_id == vintage.source_id


def test_invalid_web_geometry_is_repaired_on_load(db_session: Session, synthetic: SyntheticGeography) -> None:
    from shapely.geometry import Polygon

    provinces = synthetic.provinces.to_crs(4326)
    x0, y0, x1, y1 = provinces.geometry.iloc[0].bounds
    bowtie = Polygon([(x0, y0), (x1, y1), (x1, y0), (x0, y1)])  # self-intersecting
    provinces.loc[provinces.index[0], "geometry"] = bowtie
    assert not bowtie.is_valid
    load_tables_into_db(db_session, 2025, provinces, synthetic.municipalities, synthetic.units, SOURCES)
    for stats in db_session.scalars(select(ProvinceStats)):
        geom = shapely.from_wkb(stats.geometry_wkb)
        assert geom.is_valid and not geom.is_empty
