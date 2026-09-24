"""Load a geography vintage into the database (persistence module — imports SQLAlchemy).

:func:`load_tables_into_db` upserts provinces (ids 1..12 in canonical order), data-source
provenance, the :class:`GeoVintage`, per-vintage province statistics (with simplified EPSG:4326 WKB
geometry), municipalities and CBS neighbourhoods (units) with their demographics.  It works for any
tables with the store schema — the REAL processed store (:func:`load_into_db`) as well as the
synthetic test geography.

Rows are written with bulk ``INSERT``/``UPDATE`` statements (≈15k units load in seconds).  The
operation is idempotent: re-loading an unchanged vintage is a no-op; ``force=True`` re-synchronises
rows *in place* (database ids of unchanged codes are kept, so district plans referencing units stay
valid).  The caller owns the transaction (the function flushes but does not commit).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from pyproj import Transformer
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.core.logging import Timer, get_logger, log_ctx
from app.geography.config import GeographyConfig, load_geography_config
from app.geography.simplify import WEB_CRS, to_web_geometries
from app.geography.topology import polygonal
from app.models import (
    AppMeta,
    DataSource,
    GeoUnit,
    GeoUnitDemographics,
    GeoVintage,
    Municipality,
    MunicipalityDemographics,
    Province,
    ProvinceStats,
    utcnow,
)

log = get_logger(__name__)

ACTIVE_VINTAGE_KEY = "active_vintage_year"
#: ``AppMeta`` key prefix recording the content fingerprint each vintage was loaded from.
FINGERPRINT_KEY_PREFIX = "geo_vintage_fingerprint_"

#: Demographic columns persisted in ``*_demographics`` tables (subset present in the input is used).
DB_DEMOGRAPHICS: tuple[str, ...] = (
    "pct_age_0_15",
    "pct_age_15_25",
    "pct_age_25_45",
    "pct_age_45_65",
    "pct_age_65_plus",
    "pct_single_households",
    "pct_households_with_children",
    "avg_household_size",
    "pct_origin_nl",
    "pct_origin_europe",
    "pct_origin_non_europe",
    "pct_education_low",
    "pct_education_mid",
    "pct_education_high",
    "income_per_capita_keur",
    "pct_owner_occupied",
)
_TO_WGS84 = Transformer.from_crs(28992, 4326, always_xy=True)


# --------------------------------------------------------------------------- small helpers
def _col(df: pd.DataFrame, name: str, default: Any = None) -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series([default] * len(df), index=df.index, dtype=object)


def _num(value: Any) -> float | None:
    if value is None or value is pd.NA:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else f


def _int(value: Any) -> int | None:
    f = _num(value)
    return None if f is None else round(f)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


def _web_wkb(gdf: gpd.GeoDataFrame, tolerance_m: float) -> list[bytes | None]:
    """Simplified EPSG:4326 WKB per row (geometries already in EPSG:4326 are used as they are,
    except that invalid ones are repaired so the database never stores invalid polygons)."""
    if "geometry" not in gdf.columns or gdf.geometry.isna().all():
        return [None] * len(gdf)
    if gdf.crs is not None and gdf.crs.to_epsg() == 4326:
        geoms = np.asarray(gdf.geometry.values, dtype=object)
    else:
        geoms = np.asarray(to_web_geometries(gdf, tolerance_m).geometry.values, dtype=object)
    invalid = ~shapely.is_valid(geoms) & ~shapely.is_missing(geoms)
    if invalid.any():
        geoms = geoms.copy()
        geoms[invalid] = [polygonal(g) for g in shapely.make_valid(geoms[invalid])]
    return [None if g is None or g.is_empty else shapely.to_wkb(g) for g in geoms]


def _centroids_lonlat(gdf: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(x, y, lon, lat) of polygon centroids; x/y in EPSG:28992."""
    geoms = gdf.geometry
    projected = geoms.to_crs(28992) if geoms.crs is not None and geoms.crs.to_epsg() != 28992 else geoms
    c = shapely.centroid(np.asarray(projected.values, dtype=object))
    x, y = shapely.get_x(c), shapely.get_y(c)
    lon, lat = _TO_WGS84.transform(x, y)
    return np.asarray(x), np.asarray(y), np.asarray(lon), np.asarray(lat)


def _source_dicts(sources: Any) -> list[dict[str, Any]]:
    if sources is None:
        return []
    if isinstance(sources, Mapping):
        items: Iterable[Any] = [
            {"key": k, **(v if isinstance(v, Mapping) else v.to_json())} for k, v in sources.items()
        ]
    else:
        items = sources
    out = []
    for item in items:
        data = dict(item) if isinstance(item, Mapping) else dict(item.to_json())
        out.append(data)
    return out


# --------------------------------------------------------------------------- upserts
def upsert_provinces(session: Session, cfg: GeographyConfig) -> dict[str, int]:
    """Provinces with ids 1..12 in canonical order.  Returns ``{code: id}``."""
    existing = {p.code: p for p in session.scalars(select(Province))}
    for i, spec in enumerate(cfg.provinces, start=1):
        values = {
            "code": spec.code,
            "cbs_code": spec.cbs_code,
            "name": spec.name,
            "name_en": spec.name_en,
            "capital": spec.capital,
            "sort_order": i,
        }
        row = existing.get(spec.code)
        if row is None:
            session.execute(insert(Province), [{"id": i, **values}])
        else:
            for k, v in values.items():
                setattr(row, k, v)
    session.flush()
    return {code: pid for code, pid in session.execute(select(Province.code, Province.id))}


def upsert_sources(session: Session, year: int, sources: Any) -> dict[str, int]:
    """DataSource rows keyed ``<source key>_<year>``.  Returns ``{source key: id}``."""
    ids: dict[str, int] = {}
    for data in _source_dicts(sources):
        src_key = str(data.get("key"))
        db_key = f"{src_key}_{year}"[:80]
        vintage = data.get("vintage")
        values = {
            "key": db_key,
            "name": str(data.get("name", src_key))[:200],
            "publisher": str(data.get("publisher", ""))[:120],
            "url": str(data.get("url", "")),
            "license": str(data.get("license", ""))[:80],
            "data_category": "REAL",
            "retrieved_at": _parse_time(data.get("retrieved_at")),
            "sha256": data.get("sha256"),
            "size_bytes": _int(data.get("bytes", data.get("size_bytes"))),
            "local_path": data.get("path"),
            "notes": f"vintage {vintage}; used for geography {year}"
            if vintage
            else f"used for geography {year}",
        }
        row = session.scalar(select(DataSource).where(DataSource.key == db_key))
        if row is None:
            row = DataSource(**values)
            session.add(row)
        else:
            for k, v in values.items():
                setattr(row, k, v)
        session.flush()
        ids[src_key] = row.id
    return ids


def _sync_rows(
    session: Session,
    model: Any,
    vintage_id: int,
    rows: list[dict[str, Any]],
    delete_stale: bool = True,
) -> dict[str, int]:
    """Insert / update-by-id (/ delete) rows of ``model`` for one vintage, keyed by ``cbs_code``."""
    existing = dict(
        session.execute(select(model.cbs_code, model.id).where(model.vintage_id == vintage_id)).all()
    )
    new_codes = {r["cbs_code"] for r in rows}
    stale = [i for c, i in existing.items() if c not in new_codes]
    if stale and delete_stale:
        session.execute(delete(model).where(model.id.in_(stale)))
    updates = [{"id": existing[r["cbs_code"]], **r} for r in rows if r["cbs_code"] in existing]
    inserts = [r for r in rows if r["cbs_code"] not in existing]
    if updates:
        session.execute(update(model), updates)
    if inserts:
        session.execute(insert(model), inserts)
    session.flush()
    ids = dict(session.execute(select(model.cbs_code, model.id).where(model.vintage_id == vintage_id)).all())
    return {c: i for c, i in ids.items() if c in new_codes}


def _demographic_rows(
    df: pd.DataFrame, id_col: str, ids: Sequence[int], years: Mapping[str, int | None]
) -> list[dict[str, Any]]:
    cols = [c for c in DB_DEMOGRAPHICS if c in df.columns]
    values = {c: df[c].to_numpy(dtype=float) for c in cols}
    flags = _col(df, "imputed_fields", "").fillna("").astype(str).to_numpy()
    out = []
    for i, ident in enumerate(ids):
        row: dict[str, Any] = {id_col: int(ident)}
        for c in cols:
            v = values[c][i]
            row[c] = None if not np.isfinite(v) else float(v)
        row["source_year_core"] = years.get("core")
        row["source_year_supplement"] = years.get("supplement")
        row["imputed_fields"] = flags[i] or None
        out.append(row)
    return out


# --------------------------------------------------------------------------- public API
def load_tables_into_db(
    session: Session,
    year: int,
    provinces_gdf: gpd.GeoDataFrame,
    municipalities_gdf: gpd.GeoDataFrame,
    units_gdf: pd.DataFrame,
    sources: Any = None,
    force: bool = False,
    *,
    source_years: Mapping[str, int | None] | None = None,
    processed_path: str | None = None,
    label: str | None = None,
    config: GeographyConfig | None = None,
    fingerprint: str | None = None,
) -> GeoVintage:
    """Upsert one geography vintage from store-schema tables.

    ``units_gdf`` only needs attributes (geometry is not stored for units).  Province/municipality
    geometries are stored as simplified EPSG:4326 WKB: inputs already in EPSG:4326 are used as-is,
    projected inputs are coverage-simplified with the ``simplify`` tolerances of the geography config.
    ``sources`` accepts download records, their JSON dicts or a ``{key: record}`` mapping.

    Without ``force`` an already complete vintage is left untouched — unless its row counts or
    population total differ from the input, or ``fingerprint`` (the store manifest fingerprint)
    differs from the one recorded at the previous load; the rows are then re-synchronised.
    """
    cfg = config or load_geography_config()
    years = dict(source_years or {"core": year, "supplement": None})
    units = (
        pd.DataFrame(units_gdf.drop(columns="geometry", errors="ignore"))
        .sort_values("code")
        .reset_index(drop=True)
    )
    munis = municipalities_gdf.sort_values("code").reset_index(drop=True)
    with Timer(log, f"load geography {year} into db"):
        province_ids = upsert_provinces(session, cfg)
        vintage = session.scalar(select(GeoVintage).where(GeoVintage.year == year))
        total_pop = int(pd.to_numeric(units["population"]).sum())
        if (
            vintage is not None
            and not force
            and _vintage_complete(session, vintage, len(munis), len(units), total_pop)
            and (fingerprint is None or _stored_fingerprint(session, year) == fingerprint)
        ):
            _activate(session, vintage)
            log.info("geography %d already loaded (vintage id %d) — skipped", year, vintage.id)
            return vintage
        source_ids = upsert_sources(session, year, sources)
        primary_source = source_ids.get("wijkenbuurten") or (
            next(iter(source_ids.values())) if source_ids else None
        )

        # ---------------------------------------------------------- aggregates from units
        unit_muni = units["municipality_code"].astype(str)
        pop = units["population"].astype(np.int64)
        elig = units["eligible_voters_est"].astype(np.int64)
        muni_pop = pop.groupby(unit_muni).sum()
        muni_elig = elig.groupby(unit_muni).sum()
        muni_units = unit_muni.value_counts()
        muni_area = units["area_km2"].groupby(unit_muni).sum()
        muni_land = units["land_area_km2"].groupby(unit_muni).sum()

        if vintage is None:
            vintage = GeoVintage(year=year, label=label or f"CBS Wijk- en Buurtkaart {year}")
            session.add(vintage)
        vintage.label = label or vintage.label or f"CBS Wijk- en Buurtkaart {year}"
        if primary_source is not None:  # a reload without provenance keeps the recorded source
            vintage.source_id = primary_source
        vintage.province_count = len(cfg.provinces)
        vintage.municipality_count = len(munis)
        vintage.unit_count = len(units)
        vintage.population_total = int(pop.sum())
        vintage.processed_path = processed_path
        vintage.built_at = utcnow()
        session.flush()

        # ---------------------------------------------------------- province stats
        prov = provinces_gdf.set_index("code").reindex(cfg.province_codes).reset_index()
        prov = gpd.GeoDataFrame(prov, geometry="geometry", crs=provinces_gdf.crs)
        prov_wkb = _web_wkb(prov, cfg.simplify.provinces_m)
        if {"centroid_lon", "centroid_lat"} <= set(prov.columns):
            plon, plat = (
                prov["centroid_lon"].to_numpy(dtype=float),
                prov["centroid_lat"].to_numpy(dtype=float),
            )
        else:
            _, _, plon, plat = _centroids_lonlat(prov)
        muni_prov = munis.set_index("code")["province_code"].astype(str)
        unit_prov = unit_muni.map(muni_prov)
        official = pd.to_numeric(_col(munis, "population_official"), errors="coerce")
        existing_stats = {
            s.province_id: s
            for s in session.scalars(select(ProvinceStats).where(ProvinceStats.vintage_id == vintage.id))
        }
        for i, code in enumerate(cfg.province_codes):
            in_prov = unit_prov == code
            p_pop = int(pop[in_prov].sum())
            p_land = float(units.loc[in_prov, "land_area_km2"].sum())
            p_off = official[munis["province_code"] == code]
            values = {
                "province_id": province_ids[code],
                "vintage_id": vintage.id,
                "population": p_pop,
                "population_official": int(p_off.sum()) if p_off.notna().any() else None,
                "eligible_voters_est": int(elig[in_prov].sum()),
                "area_km2": float(units.loc[in_prov, "area_km2"].sum()),
                "land_area_km2": p_land,
                "density": p_pop / p_land if p_land > 0 else 0.0,
                "municipality_count": int((munis["province_code"] == code).sum()),
                "unit_count": int(in_prov.sum()),
                "centroid_lon": float(plon[i]),
                "centroid_lat": float(plat[i]),
                "geometry_wkb": prov_wkb[i],
            }
            row = existing_stats.get(province_ids[code])
            if row is None:
                session.add(ProvinceStats(**values))
            else:
                for k, v in values.items():
                    setattr(row, k, v)
        session.flush()

        # ---------------------------------------------------------- municipalities
        muni_wkb = _web_wkb(munis, cfg.simplify.municipalities_m)
        if {"centroid_x", "centroid_y", "centroid_lon", "centroid_lat"} <= set(munis.columns):
            mx, my = munis["centroid_x"].to_numpy(dtype=float), munis["centroid_y"].to_numpy(dtype=float)
            mlon, mlat = (
                munis["centroid_lon"].to_numpy(dtype=float),
                munis["centroid_lat"].to_numpy(dtype=float),
            )
        else:
            mx, my, mlon, mlat = _centroids_lonlat(munis)
        muni_rows = []
        for i, r in enumerate(munis.itertuples(index=False)):
            code = str(r.code)
            m_pop = int(muni_pop.get(code, 0))
            m_land = float(muni_land.get(code, 0.0))
            muni_rows.append(
                {
                    "vintage_id": vintage.id,
                    "cbs_code": code,
                    "name": str(getattr(r, "name", code))[:80],
                    "province_id": province_ids[str(r.province_code)],
                    "population": m_pop,
                    "population_official": _int(getattr(r, "population_official", None)),
                    "eligible_voters_est": int(muni_elig.get(code, 0)),
                    "area_km2": float(muni_area.get(code, 0.0)),
                    "land_area_km2": m_land,
                    "density": m_pop / m_land if m_land > 0 else 0.0,
                    "urbanity_class": _int(getattr(r, "urbanity_class", None)),
                    "address_density": _num(getattr(r, "address_density", None)),
                    "unit_count": int(muni_units.get(code, 0)),
                    "centroid_lon": float(mlon[i]),
                    "centroid_lat": float(mlat[i]),
                    "centroid_x": float(mx[i]),
                    "centroid_y": float(my[i]),
                    "geometry_wkb": muni_wkb[i],
                }
            )

        # demographics are leaf rows: drop and re-insert for this vintage
        muni_ids_old = select(Municipality.id).where(Municipality.vintage_id == vintage.id)
        unit_ids_old = select(GeoUnit.id).where(GeoUnit.vintage_id == vintage.id)
        session.execute(delete(GeoUnitDemographics).where(GeoUnitDemographics.geo_unit_id.in_(unit_ids_old)))
        session.execute(
            delete(MunicipalityDemographics).where(MunicipalityDemographics.municipality_id.in_(muni_ids_old))
        )
        # stale units must go before stale municipalities (FK)
        new_unit_codes = set(units["code"].astype(str))
        stale_units = [
            uid
            for code, uid in session.execute(
                select(GeoUnit.cbs_code, GeoUnit.id).where(GeoUnit.vintage_id == vintage.id)
            )
            if code not in new_unit_codes
        ]
        if stale_units:
            session.execute(delete(GeoUnit).where(GeoUnit.id.in_(stale_units)))
        muni_ids = _sync_rows(session, Municipality, vintage.id, muni_rows, delete_stale=False)

        # municipality demographics: own columns when present, else population-weighted unit means
        mdemo = munis.copy()
        for c in DB_DEMOGRAPHICS:
            if c not in mdemo.columns and c in units.columns:
                w = pop.astype(float).where(pop > 0, 0.0)
                num = (units[c].astype(float) * w).groupby(unit_muni).sum()
                den = w.groupby(unit_muni).sum()
                mdemo[c] = mdemo["code"].map((num / den.where(den > 0)).astype(float))
        m_ids = [muni_ids[str(c)] for c in mdemo["code"]]
        rows = _demographic_rows(mdemo, "municipality_id", m_ids, years)
        if rows:
            session.execute(insert(MunicipalityDemographics), rows)

        # ---------------------------------------------------------- units
        uprov = unit_prov.map(province_ids)
        unit_rows = []
        cols = {
            c: units[c].to_numpy()
            for c in (
                "code",
                "name",
                "population",
                "eligible_voters_est",
                "area_km2",
                "land_area_km2",
                "density",
                "centroid_lon",
                "centroid_lat",
                "centroid_x",
                "centroid_y",
            )
        }
        wijk = _col(units, "wijk_code").to_numpy()
        urb = _col(units, "urbanity_class").to_numpy()
        addr = _col(units, "address_density").to_numpy()
        mids = unit_muni.map(muni_ids).to_numpy()
        pids = uprov.to_numpy()
        for i in range(len(units)):
            unit_rows.append(
                {
                    "vintage_id": vintage.id,
                    "cbs_code": str(cols["code"][i]),
                    "name": str(cols["name"][i])[:120],
                    "unit_kind": "buurt",
                    "wijk_code": None if wijk[i] is None else str(wijk[i])[:8],
                    "municipality_id": int(mids[i]),
                    "province_id": int(pids[i]),
                    "population": int(cols["population"][i]),
                    "eligible_voters_est": int(cols["eligible_voters_est"][i]),
                    "area_km2": float(cols["area_km2"][i]),
                    "land_area_km2": float(cols["land_area_km2"][i]),
                    "density": float(cols["density"][i]),
                    "urbanity_class": _int(urb[i]),
                    "address_density": _num(addr[i]),
                    "centroid_lon": float(cols["centroid_lon"][i]),
                    "centroid_lat": float(cols["centroid_lat"][i]),
                    "centroid_x": float(cols["centroid_x"][i]),
                    "centroid_y": float(cols["centroid_y"][i]),
                }
            )
        unit_ids = _sync_rows(session, GeoUnit, vintage.id, unit_rows)
        stale_munis = [
            mid
            for code, mid in session.execute(
                select(Municipality.cbs_code, Municipality.id).where(Municipality.vintage_id == vintage.id)
            )
            if code not in muni_ids
        ]
        if stale_munis:
            session.execute(delete(Municipality).where(Municipality.id.in_(stale_munis)))
        u_ids = [unit_ids[str(c)] for c in units["code"]]
        rows = _demographic_rows(units, "geo_unit_id", u_ids, years)
        if rows:
            session.execute(insert(GeoUnitDemographics), rows)
        session.flush()
        if fingerprint is not None:
            _set_meta(session, f"{FINGERPRINT_KEY_PREFIX}{year}", fingerprint)
        _activate(session, vintage)
    log.info(
        "geography %d loaded: %d municipalities, %d units",
        year,
        len(muni_rows),
        len(unit_rows),
        extra=log_ctx(vintage_id=vintage.id),
    )
    return vintage


def _vintage_complete(
    session: Session, vintage: GeoVintage, n_munis: int, n_units: int, population: int | None = None
) -> bool:
    got_munis = session.scalar(
        select(func.count()).select_from(Municipality).where(Municipality.vintage_id == vintage.id)
    )
    got_units = session.scalar(
        select(func.count()).select_from(GeoUnit).where(GeoUnit.vintage_id == vintage.id)
    )
    got_stats = session.scalar(
        select(func.count()).select_from(ProvinceStats).where(ProvinceStats.vintage_id == vintage.id)
    )
    return (
        got_munis == n_munis == vintage.municipality_count
        and got_units == n_units == vintage.unit_count
        and got_stats == vintage.province_count
        and (population is None or population == vintage.population_total)
    )


def _stored_fingerprint(session: Session, year: int) -> str | None:
    meta = session.get(AppMeta, f"{FINGERPRINT_KEY_PREFIX}{year}")
    return None if meta is None else meta.value


def _set_meta(session: Session, key: str, value: str) -> None:
    meta = session.get(AppMeta, key)
    if meta is None:
        session.add(AppMeta(key=key, value=value))
    else:
        meta.value = value


def _activate(session: Session, vintage: GeoVintage) -> None:
    session.execute(update(GeoVintage).where(GeoVintage.id != vintage.id).values(is_active=False))
    vintage.is_active = True
    _set_meta(session, ACTIVE_VINTAGE_KEY, str(vintage.year))
    session.flush()


def load_into_db(session: Session, year: int | None = None, force: bool = False) -> GeoVintage:
    """Load the REAL processed store of ``year`` (default: settings) into the database.

    Uses the attribute tables (no full-resolution geometry) and the already simplified EPSG:4326
    web layers for province/municipality geometry.  Raises ``DataNotPreparedError`` when the store
    has not been built.
    """
    from app.geography import store

    manifest = store.manifest(year)
    year = int(manifest["year"])
    base = store.store_dir(year)
    prov_attrs = pd.read_parquet(
        base / "provinces.parquet",
        columns=[c for c in _parquet_columns(base / "provinces.parquet") if c != "geometry"],
    )
    muni_attrs = pd.read_parquet(
        base / "municipalities.parquet",
        columns=[c for c in _parquet_columns(base / "municipalities.parquet") if c != "geometry"],
    )
    units = store.load_units_attrs(year)
    prov_web = pyogrio.read_dataframe(store.web_geojson_path(year, "provinces"), columns=["code"])
    muni_web = pyogrio.read_dataframe(store.web_geojson_path(year, "municipalities"), columns=["code"])
    provinces = gpd.GeoDataFrame(
        prov_attrs.merge(prov_web[["code", "geometry"]], on="code", how="left"),
        geometry="geometry",
        crs=WEB_CRS,
    )
    municipalities = gpd.GeoDataFrame(
        muni_attrs.merge(muni_web[["code", "geometry"]], on="code", how="left"),
        geometry="geometry",
        crs=WEB_CRS,
    )
    return load_tables_into_db(
        session,
        year,
        provinces,
        municipalities,
        units,
        sources=manifest.get("sources"),
        force=force,
        source_years=manifest.get("source_years"),
        processed_path=str(base),
        fingerprint=manifest.get("fingerprint"),
    )


def _parquet_columns(path: Any) -> list[str]:
    import pyarrow.parquet as pq

    return list(pq.read_schema(path).names)


def save_lineage(
    session: Session, from_year: int, to_year: int, lineage: pd.DataFrame, effective_date: str | None = None
) -> int:
    """Persist a :func:`app.geography.lineage.compute_lineage` table as ``MunicipalityLineage`` rows
    between two loaded vintages (replacing earlier rows for that pair).  Returns the row count."""
    from app.models import MunicipalityLineage

    from_v = session.scalar(select(GeoVintage).where(GeoVintage.year == from_year))
    to_v = session.scalar(select(GeoVintage).where(GeoVintage.year == to_year))
    if from_v is None or to_v is None:
        raise ValueError(f"both vintages must be loaded first ({from_year}, {to_year})")
    session.execute(
        delete(MunicipalityLineage).where(
            MunicipalityLineage.from_vintage_id == from_v.id, MunicipalityLineage.to_vintage_id == to_v.id
        )
    )
    rows = [
        {
            "from_vintage_id": from_v.id,
            "to_vintage_id": to_v.id,
            "from_code": str(r.from_code),
            "to_code": str(r.to_code),
            "population_weight": float(r.population_weight),
            "event": str(r.event),
            "effective_date": effective_date,
            "notes": f"{int(getattr(r, 'units', 0))} neighbourhoods, population {int(getattr(r, 'population', 0))}",
        }
        for r in lineage.itertuples(index=False)
    ]
    if rows:
        session.execute(insert(MunicipalityLineage), rows)
    session.flush()
    return len(rows)
