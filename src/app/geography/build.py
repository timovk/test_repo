"""Build the processed geography store ``data/processed/geo_<year>/`` from the raw CBS sources.

Pipeline (each step timed and logged):

1. **read** — land neighbourhoods (``water == 'NEE'``) of the CBS Wijk- en Buurtkaart, the
   ``wijken``/``gemeenten`` attribute layers, the PDOK generalised province/municipality polygons and
   the StatLine supplement (education, income, housing);
2. **coverage repair** — sub-millimetre overlaps between a few neighbourhoods are snapped away so
   the units form a valid polygon coverage (see :mod:`app.geography.topology`);
3. **units** — population (missing figures: municipal residual distributed and flagged),
   areas, density, centroids, CBS urbanity, demographics (buurt → wijk → gemeente → population-
   weighted municipality/province/national means; every non-own value flagged in
   ``imputed_fields``) and the DERIVED eligible-voter estimate;
4. **municipalities / provinces** — dissolve of the units (coverage union); each municipality is
   assigned to the generalised province polygon it overlaps most; official CBS municipal figures
   are kept alongside the canonical sums;
5. **adjacency** — rook adjacency of units, water links making every province graph connected,
   municipality adjacency lifted from the unit graph;
6. **validation** — hard invariants (12 provinces, unique assignment, consistent hierarchy,
   connected province graphs, municipality set equals the generalised municipality layer);
7. **write** — GeoParquet tables, web GeoJSON (coverage-simplified, EPSG:4326) and ``manifest.json``.

The build writes into a temporary directory and swaps it into place atomically, so readers of an
existing store never observe a half-written directory.  It is deterministic and idempotent: a
rebuild is skipped when the manifest fingerprint (source hashes + parameters + schema version)
matches, unless ``force=True``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer

from app.core.config import get_constitution
from app.core.errors import DataNotPreparedError, DownloadError, ValidationError
from app.core.logging import Timer, get_logger, log_ctx
from app.core.settings import get_settings
from app.geography import cbs
from app.geography.adjacency import (
    WATER_LINK,
    aggregate_adjacency,
    component_counts,
    compute_adjacency,
    connect_components,
)
from app.geography.config import GeographyConfig, load_geography_config
from app.geography.demographics import (
    distribute_missing_population,
    estimate_eligible_voters,
    fill_composition,
    fill_from_parents,
    group_weighted_mean,
    hierarchical_weighted_fill,
    join_imputed_flags,
    resolve_urbanity_class,
)
from app.geography.download import DownloadRecord, download_all
from app.geography.frame import DEMOGRAPHIC_VARIABLES
from app.geography.simplify import coverage_simplify, write_web_geojson
from app.geography.topology import dissolve_coverage, repair_coverage

log = get_logger(__name__)

#: Bump whenever the store schema or an algorithm changes (forces a rebuild).
#: 2: joint (compositional) imputation of age bands / migration background; valid web geometry.
SCHEMA_VERSION = 2

#: Additional REAL indicators stored next to the model variables (used by the DB loader).
EXTRA_DEMOGRAPHICS: tuple[str, ...] = (
    "pct_age_0_15",
    "pct_age_45_65",
    "avg_household_size",
    "pct_origin_nl",
    "pct_education_mid",
)
ALL_DEMOGRAPHICS: tuple[str, ...] = (*DEMOGRAPHIC_VARIABLES, *EXTRA_DEMOGRAPHICS)
#: Order of names in ``imputed_fields``.
FLAG_ORDER: tuple[str, ...] = ("population", *ALL_DEMOGRAPHICS)

UNIT_BASE_COLUMNS: tuple[str, ...] = (
    "code",
    "name",
    "wijk_code",
    "municipality_code",
    "province_code",
    "population",
    "eligible_voters_est",
    "area_km2",
    "land_area_km2",
    "density",
    "urbanity_class",
    "address_density",
    "centroid_x",
    "centroid_y",
    "centroid_lon",
    "centroid_lat",
)
MUNI_BASE_COLUMNS: tuple[str, ...] = (
    "code",
    "name",
    "province_code",
    "population",
    "population_official",
    "eligible_voters_est",
    "area_km2",
    "land_area_km2",
    "density",
    "urbanity_class",
    "address_density",
    "unit_count",
    "centroid_x",
    "centroid_y",
    "centroid_lon",
    "centroid_lat",
)
PROVINCE_COLUMNS: tuple[str, ...] = (
    "code",
    "cbs_code",
    "name",
    "name_en",
    "capital",
    "population",
    "population_official",
    "eligible_voters_est",
    "area_km2",
    "land_area_km2",
    "density",
    "municipality_count",
    "unit_count",
    "centroid_x",
    "centroid_y",
    "centroid_lon",
    "centroid_lat",
)

_TO_WGS84 = Transformer.from_crs(28992, 4326, always_xy=True)


# --------------------------------------------------------------------------- data containers
@dataclass
class RawInputs:
    """Cleaned raw inputs (see :mod:`app.geography.cbs` for the column conventions)."""

    buurten: gpd.GeoDataFrame
    wijken: pd.DataFrame
    gemeenten: pd.DataFrame
    provinces_generalized: gpd.GeoDataFrame
    municipalities_generalized: gpd.GeoDataFrame | None
    supplement: pd.DataFrame
    supplement_year: int | None = None
    core_year: int | None = None


@dataclass
class GeoTables:
    """The assembled store tables (EPSG:28992) plus build diagnostics."""

    units: gpd.GeoDataFrame
    municipalities: gpd.GeoDataFrame
    provinces: gpd.GeoDataFrame
    unit_adjacency: pd.DataFrame
    municipality_adjacency: pd.DataFrame
    info: dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildReport:
    """Result of :func:`build_geography`."""

    year: int
    path: Path
    skipped: bool
    counts: dict[str, Any]
    timings: dict[str, float]
    water_links: list[dict[str, Any]]
    warnings: list[str]
    manifest: dict[str, Any]


@contextmanager
def _step(name: str, timings: dict[str, float]) -> Iterator[None]:
    timer = Timer(log, name)
    with timer:
        yield
    timings[name] = round(timer.elapsed, 3)


# --------------------------------------------------------------------------- helpers
def _lonlat(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lon, lat = _TO_WGS84.transform(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    return np.asarray(lon), np.asarray(lat)


def _reindex(frame: pd.DataFrame, column: str, keys: pd.Series | np.ndarray) -> np.ndarray:
    """Values of ``frame[column]`` (indexed by code) for ``keys`` (NaN where absent)."""
    if column not in frame.columns:
        return np.full(len(keys), np.nan)
    return frame[column].reindex(pd.Index(np.asarray(keys, dtype=object))).to_numpy(dtype=float)


def _safe_div(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    num = np.asarray(num, dtype=float)
    den = np.asarray(den, dtype=float)
    out = np.zeros_like(num)
    np.divide(num, den, out=out, where=den > 0)
    return out


def assign_provinces(
    muni_codes: np.ndarray,
    muni_geoms: np.ndarray,
    provinces_generalized: gpd.GeoDataFrame,
    cbs_to_code: dict[str, str],
) -> tuple[np.ndarray, np.ndarray]:
    """Province code of each municipality by largest area overlap with the generalised province
    polygons (nearest province when nothing overlaps).  Returns ``(codes, overlap_fraction)``."""
    pgeoms = np.asarray(provinces_generalized.geometry.values, dtype=object)
    pcodes = provinces_generalized["cbs_code"].map(cbs_to_code).to_numpy(dtype=object)
    if pd.isna(pcodes).any():
        unknown = provinces_generalized.loc[pd.isna(pcodes), "cbs_code"].tolist()
        raise ValidationError("generalised province layer has unknown province codes", unknown)
    tree = shapely.STRtree(pgeoms)
    mi, pi = tree.query(np.asarray(muni_geoms, dtype=object), predicate="intersects")
    simplified = shapely.simplify(np.asarray(muni_geoms, dtype=object), 10.0, preserve_topology=True)
    overlap = shapely.area(shapely.intersection(simplified[mi], pgeoms[pi])) if len(mi) else np.zeros(0)
    df = pd.DataFrame({"m": mi, "p": pi, "area": overlap})
    df = df[df["area"] > 0].sort_values(["m", "area", "p"], ascending=[True, False, True], kind="mergesort")
    best = df.drop_duplicates("m")
    result = np.full(len(muni_codes), None, dtype=object)
    frac = np.zeros(len(muni_codes))
    result[best["m"].to_numpy()] = pcodes[best["p"].to_numpy()]
    total = shapely.area(simplified)
    frac[best["m"].to_numpy()] = _safe_div(best["area"].to_numpy(), total[best["m"].to_numpy()])
    unassigned = np.flatnonzero(pd.isna(result))
    if len(unassigned):
        (src, dst), _ = tree.query_nearest(
            np.asarray(muni_geoms, dtype=object)[unassigned], return_distance=True
        )
        for s, d in zip(src, dst, strict=True):
            if pd.isna(result[unassigned[s]]):
                result[unassigned[s]] = pcodes[d]
        log.warning(
            "%d municipalities had no overlap with a province polygon — nearest used", len(unassigned)
        )
    return result.astype(str), frac


# --------------------------------------------------------------------------- assembly
def assemble_tables(
    raw: RawInputs, cfg: GeographyConfig, timings: dict[str, float] | None = None
) -> GeoTables:
    """Assemble units, municipalities, provinces and adjacency tables from cleaned raw inputs."""
    timings = timings if timings is not None else {}
    info: dict[str, Any] = {}
    b = raw.buurten.sort_values("code", kind="mergesort").reset_index(drop=True)
    if b["code"].duplicated().any():
        raise ValidationError(
            "duplicate neighbourhood codes in source", b.loc[b["code"].duplicated(), "code"].tolist()[:10]
        )
    n = len(b)

    with _step("coverage repair", timings):
        geoms, repaired = repair_coverage(np.asarray(b.geometry.values, dtype=object))
    info["coverage_repaired_units"] = [str(b["code"].iat[i]) for i in repaired]

    # ---------------------------------------------------------------- hierarchy indices
    muni_codes = np.array(sorted(b["municipality_code"].unique().tolist()), dtype=object)
    muni_idx = pd.Index(muni_codes).get_indexer(b["municipality_code"])
    gem = raw.gemeenten.set_index("code")
    wijk = raw.wijken.set_index("code")

    with _step("municipality dissolve", timings):
        keys, muni_geoms = dissolve_coverage(geoms, b["municipality_code"].to_numpy(dtype=object))
        if list(keys) != list(muni_codes):
            raise ValidationError("municipality dissolve produced an unexpected set of codes")

    with _step("province assignment", timings):
        muni_prov, overlap = assign_provinces(
            muni_codes, muni_geoms, raw.provinces_generalized, cfg.cbs_to_code
        )
    info["province_overlap_min"] = float(overlap.min()) if len(overlap) else 1.0
    info["province_overlap_lowest"] = {
        str(muni_codes[i]): round(float(overlap[i]), 4) for i in np.argsort(overlap, kind="stable")[:5]
    }
    unit_prov = muni_prov[muni_idx]
    prov_idx = pd.Index(cfg.province_codes).get_indexer(unit_prov).astype(np.int64)
    if (prov_idx < 0).any():
        raise ValidationError("municipalities assigned to provinces missing from the configuration")

    flags: dict[str, np.ndarray] = {}
    with _step("unit attributes", timings):
        area_km2 = shapely.area(geoms) / 1e6
        land_ha = b["land_area_ha"].to_numpy(dtype=float)
        land_km2 = np.where(np.nan_to_num(land_ha, nan=0.0) > 0, land_ha / 100.0, area_km2)
        official = _reindex(gem, "population_raw", muni_codes)
        population, pop_imputed = distribute_missing_population(
            b["population_raw"].to_numpy(dtype=float),
            muni_idx,
            official,
            land_km2,
            b["address_density"].to_numpy(dtype=float),
        )
        flags["population"] = pop_imputed
        density = _safe_div(population, land_km2)
        centroids = shapely.centroid(geoms)
        cx, cy = shapely.get_x(centroids), shapely.get_y(centroids)
        lon, lat = _lonlat(cx, cy)
        weights = population.astype(float)
        levels = [muni_idx, prov_idx]

        demo: dict[str, np.ndarray] = {}
        compositional: set[str] = set()
        for parts in cbs.CORE_COMPOSITIONS.values():
            own_parts = b[list(parts)].to_numpy(dtype=float)
            filled_parts, imputed_parts = fill_composition(
                own_parts,
                [
                    np.column_stack([_reindex(wijk, var, b["wijk_code"]) for var in parts]),
                    np.column_stack([_reindex(gem, var, b["municipality_code"]) for var in parts]),
                ],
                weights,
                levels,
            )
            for j, var in enumerate(parts):
                demo[var] = filled_parts[:, j]
                flags[var] = imputed_parts[:, j]
            compositional.update(parts)
        for var in cbs.CORE_VARIABLES:
            if var in compositional:
                continue
            own = b[var].to_numpy(dtype=float)
            filled, from_parent = fill_from_parents(
                own, _reindex(wijk, var, b["wijk_code"]), _reindex(gem, var, b["municipality_code"])
            )
            filled, from_mean = hierarchical_weighted_fill(filled, weights, levels)
            demo[var] = filled
            flags[var] = from_parent | from_mean
        sup = raw.supplement.set_index("code")
        for var in cbs.SUPPLEMENT_VARIABLES:
            own = _reindex(sup, var, b["code"])
            filled, from_parent = fill_from_parents(
                own, _reindex(sup, var, b["wijk_code"]), _reindex(sup, var, b["municipality_code"])
            )
            filled, from_mean = hierarchical_weighted_fill(filled, weights, levels)
            demo[var] = filled
            flags[var] = from_parent | from_mean
        urb_cls, urb_parent = resolve_urbanity_class(
            b["urbanity_class_raw"].to_numpy(dtype=float),
            b["address_density"].to_numpy(dtype=float),
            _reindex(wijk, "urbanity_class_raw", b["wijk_code"]),
            _reindex(gem, "urbanity_class_raw", b["municipality_code"]),
        )
        urb_cls, urb_mean = hierarchical_weighted_fill(urb_cls, weights, levels)
        urb_cls = np.clip(np.rint(np.nan_to_num(urb_cls, nan=5.0)), 1, 5).astype(np.int64)
        demo["urbanity"] = 6.0 - urb_cls
        flags["urbanity"] = urb_parent | urb_mean
        demo["log_density"] = np.log1p(density)
        flags["log_density"] = pop_imputed.copy()
        eligible = estimate_eligible_voters(
            population,
            demo["pct_age_0_15"],
            demo["pct_age_15_25"],
            cfg.eligible_voters.citizenship_factor,
            cfg.eligible_voters.adult_share_15_25,
        )

        units = gpd.GeoDataFrame(
            {
                "code": b["code"].astype(str).to_numpy(),
                "name": b["name"].astype(str).to_numpy(),
                "wijk_code": b["wijk_code"].astype(str).to_numpy(),
                "municipality_code": b["municipality_code"].astype(str).to_numpy(),
                "province_code": unit_prov.astype(str),
                "population": population.astype(np.int64),
                "eligible_voters_est": eligible,
                "area_km2": area_km2,
                "land_area_km2": land_km2,
                "density": density,
                "urbanity_class": urb_cls,
                "address_density": b["address_density"].to_numpy(dtype=float),
                "centroid_x": cx,
                "centroid_y": cy,
                "centroid_lon": lon,
                "centroid_lat": lat,
                **{var: demo[var] for var in ALL_DEMOGRAPHICS},
                "imputed_fields": join_imputed_flags(flags, n, FLAG_ORDER),
            },
            geometry=gpd.GeoSeries(geoms, crs=28992),
            crs=28992,
        )
    info["imputed_units"] = {k: int(np.count_nonzero(flags[k])) for k in FLAG_ORDER if k in flags}
    info["population_imputed_units"] = int(np.count_nonzero(pop_imputed))

    with _step("municipality attributes", timings):
        fallback_names = b.groupby("municipality_code")["municipality_name"].first()
        munis = _municipality_table(units, muni_codes, muni_geoms, muni_prov, gem, sup, fallback_names)
    with _step("province attributes", timings):
        provinces = _province_table(munis, cfg, units)

    adj_cfg = cfg.adjacency
    with _step("unit adjacency", timings):
        unit_adj = compute_adjacency(units, "code", adj_cfg.snap_buffer_m, adj_cfg.min_shared_border_m)
    with _step("unit water links", timings):
        unit_adj = connect_components(
            unit_adj, units, "province_code", touch_tolerance_m=adj_cfg.snap_buffer_m
        )
    with _step("municipality adjacency", timings):
        muni_adj = aggregate_adjacency(unit_adj, units.set_index("code")["municipality_code"])
        muni_adj = connect_components(
            muni_adj, munis, "province_code", touch_tolerance_m=adj_cfg.snap_buffer_m
        )
    info["unit_water_links"] = unit_adj[unit_adj["kind"] == WATER_LINK].to_dict("records")
    info["municipality_water_links"] = muni_adj[muni_adj["kind"] == WATER_LINK].to_dict("records")
    return GeoTables(units, munis, provinces, unit_adj, muni_adj, info)


def _municipality_table(
    units: gpd.GeoDataFrame,
    muni_codes: np.ndarray,
    muni_geoms: np.ndarray,
    muni_prov: np.ndarray,
    gem: pd.DataFrame,
    sup: pd.DataFrame,
    fallback_names: pd.Series,
) -> gpd.GeoDataFrame:
    m = len(muni_codes)
    idx = pd.Index(muni_codes).get_indexer(units["municipality_code"])
    pop_u = units["population"].to_numpy(dtype=np.int64)
    population = np.bincount(idx, weights=pop_u, minlength=m).astype(np.int64)
    eligible = np.bincount(
        idx, weights=units["eligible_voters_est"].to_numpy(dtype=float), minlength=m
    ).astype(np.int64)
    area = np.bincount(idx, weights=units["area_km2"].to_numpy(dtype=float), minlength=m)
    land = np.bincount(idx, weights=units["land_area_km2"].to_numpy(dtype=float), minlength=m)
    unit_count = np.bincount(idx, minlength=m).astype(np.int64)
    density = _safe_div(population, land)
    official_names = gem["name"] if "name" in gem.columns else pd.Series(dtype=object)
    names = [
        str(official_names.get(code) or fallback_names.get(code) or code) for code in muni_codes.tolist()
    ]
    official = _reindex(gem, "population_raw", muni_codes)
    weights = pop_u.astype(float)
    flags: dict[str, np.ndarray] = {}
    demo: dict[str, np.ndarray] = {}
    for var in (*cbs.CORE_VARIABLES, *cbs.SUPPLEMENT_VARIABLES):
        src = gem if var in cbs.CORE_VARIABLES else sup
        own = _reindex(src, var, muni_codes)
        unit_mean = group_weighted_mean(units[var].to_numpy(dtype=float), weights, idx, m)
        filled, imputed = fill_from_parents(own, unit_mean)
        demo[var] = filled
        flags[var] = imputed
    urb_own = _reindex(gem, "urbanity_class_raw", muni_codes)
    unit_cls_mean = group_weighted_mean(units["urbanity_class"].to_numpy(dtype=float), weights, idx, m)
    urb_cls, urb_imp = resolve_urbanity_class(
        urb_own, _reindex(gem, "address_density", muni_codes), unit_cls_mean
    )
    urb_cls = np.clip(np.rint(np.nan_to_num(urb_cls, nan=5.0)), 1, 5).astype(np.int64)
    flags["urbanity"] = urb_imp
    demo["urbanity"] = 6.0 - urb_cls
    demo["log_density"] = np.log1p(density)
    pop_flag = (
        np.bincount(
            idx,
            weights=units["imputed_fields"].str.contains("population", regex=False).to_numpy(dtype=float),
            minlength=m,
        )
        > 0
    )
    flags["population"] = pop_flag
    flags["log_density"] = pop_flag
    centroids = shapely.centroid(muni_geoms)
    cx, cy = shapely.get_x(centroids), shapely.get_y(centroids)
    lon, lat = _lonlat(cx, cy)
    return gpd.GeoDataFrame(
        {
            "code": muni_codes.astype(str),
            "name": names,
            "province_code": muni_prov.astype(str),
            "population": population,
            "population_official": pd.array(np.where(np.isnan(official), np.nan, official), dtype="Int64"),
            "eligible_voters_est": eligible,
            "area_km2": area,
            "land_area_km2": land,
            "density": density,
            "urbanity_class": urb_cls,
            "address_density": _reindex(gem, "address_density", muni_codes),
            "unit_count": unit_count,
            "centroid_x": cx,
            "centroid_y": cy,
            "centroid_lon": lon,
            "centroid_lat": lat,
            **{var: demo[var] for var in ALL_DEMOGRAPHICS},
            "imputed_fields": join_imputed_flags(flags, m, FLAG_ORDER),
        },
        geometry=gpd.GeoSeries(muni_geoms, crs=28992),
        crs=28992,
    )


def _province_table(
    munis: gpd.GeoDataFrame, cfg: GeographyConfig, units: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    rows = []
    geoms = []
    for spec in cfg.provinces:
        sel = munis[munis["province_code"] == spec.code]
        members = np.asarray(sel.geometry.values, dtype=object)
        if len(members):
            _, merged = dissolve_coverage(members, np.zeros(len(members), dtype=np.int64))
            geom = merged[0]
        else:
            geom = shapely.MultiPolygon()
        geoms.append(geom)
        pop = int(sel["population"].sum())
        land = float(sel["land_area_km2"].sum())
        official = sel["population_official"]
        rows.append(
            {
                "code": spec.code,
                "cbs_code": spec.cbs_code,
                "name": spec.name,
                "name_en": spec.name_en or spec.name,
                "capital": spec.capital or "",
                "population": pop,
                "population_official": int(official.sum()) if official.notna().any() else pd.NA,
                "eligible_voters_est": int(sel["eligible_voters_est"].sum()),
                "area_km2": float(sel["area_km2"].sum()),
                "land_area_km2": land,
                "density": pop / land if land > 0 else 0.0,
                "municipality_count": len(sel),
                "unit_count": int((units["province_code"] == spec.code).sum()),
            }
        )
    geom_arr = np.asarray(geoms, dtype=object)
    centroids = shapely.centroid(geom_arr)
    cx, cy = shapely.get_x(centroids), shapely.get_y(centroids)
    lon, lat = _lonlat(cx, cy)
    df = pd.DataFrame(rows)
    df["population_official"] = df["population_official"].astype("Int64")
    df["centroid_x"], df["centroid_y"], df["centroid_lon"], df["centroid_lat"] = cx, cy, lon, lat
    return gpd.GeoDataFrame(
        df[list(PROVINCE_COLUMNS)], geometry=gpd.GeoSeries(geom_arr, crs=28992), crs=28992
    )


# --------------------------------------------------------------------------- validation
def validate_tables(
    tables: GeoTables, cfg: GeographyConfig, expected_municipalities: set[str] | None = None
) -> list[str]:
    """Hard invariants of the store.  Returns a list of problems (empty = valid)."""
    problems: list[str] = []
    units, munis, provinces = tables.units, tables.municipalities, tables.provinces
    expected_provinces = get_constitution().province_count
    if len(provinces) != expected_provinces:
        problems.append(f"{len(provinces)} provinces, expected {expected_provinces}")
    if list(provinces["code"]) != cfg.province_codes:
        problems.append("province order differs from config")
    used = set(munis["province_code"])
    if used != set(cfg.province_codes):
        problems.append(f"municipalities cover provinces {sorted(used)}, expected all {expected_provinces}")
    if munis["code"].duplicated().any():
        problems.append("duplicate municipality codes")
    if units["code"].duplicated().any():
        problems.append("duplicate unit codes")
    if munis["province_code"].isna().any() or not munis["province_code"].isin(cfg.province_codes).all():
        problems.append("municipality without a valid province")
    muni_prov = munis.set_index("code")["province_code"]
    unit_mp = units["municipality_code"].map(muni_prov)
    if unit_mp.isna().any():
        problems.append(f"{int(unit_mp.isna().sum())} units reference unknown municipalities")
    elif not (unit_mp.to_numpy() == units["province_code"].to_numpy()).all():
        problems.append("unit province differs from its municipality's province")
    missing_units = set(munis["code"]) - set(units["municipality_code"])
    if missing_units:
        problems.append(f"{len(missing_units)} municipalities without units")
    sums = units.groupby("municipality_code")["population"].sum()
    if not (
        sums.reindex(munis["code"]).fillna(0).to_numpy(dtype=np.int64) == munis["population"].to_numpy()
    ).all():
        problems.append("municipality population differs from the sum of its units")
    if int(provinces["population"].sum()) != int(units["population"].sum()):
        problems.append("province populations do not add up to the unit total")
    if (units["eligible_voters_est"] > units["population"]).any() or (units["eligible_voters_est"] < 0).any():
        problems.append("eligible voters outside [0, population]")
    demo = units[list(ALL_DEMOGRAPHICS)].to_numpy(dtype=float)
    if np.isnan(demo).any():
        problems.append("demographic gaps remain after imputation")
    if "geometry" in units.columns:
        geoms = np.asarray(units.geometry.values, dtype=object)
        bad_geom = shapely.is_missing(geoms) | shapely.is_empty(geoms) | ~shapely.is_valid(geoms)
        if bad_geom.any():
            problems.append(
                f"{int(bad_geom.sum())} units with missing, empty or invalid geometry "
                f"(e.g. {units.loc[bad_geom, 'code'].tolist()[:5]})"
            )
    if expected_municipalities is not None and set(munis["code"]) != set(expected_municipalities):
        extra = sorted(set(munis["code"]) - set(expected_municipalities))[:10]
        lacking = sorted(set(expected_municipalities) - set(munis["code"]))[:10]
        problems.append(
            f"municipalities differ from the generalised layer ({len(munis)} vs {len(expected_municipalities)};"
            f" extra {extra}, missing {lacking})"
        )
    for level, adj, table in (
        ("unit", tables.unit_adjacency, units),
        ("municipality", tables.municipality_adjacency, munis),
    ):
        comps = component_counts(adj, table, "province_code")
        bad = {k: v for k, v in comps.items() if v != 1}
        if bad:
            problems.append(f"{level} adjacency not connected within provinces: {bad}")
        known = set(table["code"])
        if not (adj["a"].isin(known).all() and adj["b"].isin(known).all()):
            problems.append(f"{level} adjacency references unknown codes")
    return problems


# --------------------------------------------------------------------------- IO
def read_raw_inputs(year: int, cfg: GeographyConfig, records: dict[str, DownloadRecord]) -> RawInputs:
    """Read and clean the raw source files for ``year``."""

    def path_of(key: str) -> Path:
        rec = records.get(key)
        if rec is not None and rec.path:
            p = Path(rec.path)
            return p if p.is_absolute() else get_settings().project_root / p
        return get_settings().raw_dir / cfg.source(key).filename_for(year)

    gpkg = path_of("wijkenbuurten")
    supplement_key = "supplement"
    return RawInputs(
        buurten=cbs.read_buurten(gpkg),
        wijken=cbs.read_layer_attributes(gpkg, "wijken"),
        gemeenten=cbs.read_layer_attributes(gpkg, "gemeenten"),
        provinces_generalized=cbs.read_generalized(path_of("provinces"), "PV"),
        municipalities_generalized=(
            cbs.read_generalized(path_of("municipalities_generalized"), "GM")
            if "municipalities_generalized" in cfg.sources
            else None
        ),
        supplement=cbs.read_supplement(path_of(supplement_key)),
        supplement_year=cfg.source(supplement_key).vintage_for(year),
        core_year=cfg.source("wijkenbuurten").vintage_for(year),
    )


def _parameters(cfg: GeographyConfig) -> dict[str, Any]:
    return {
        "adjacency": cfg.adjacency.model_dump(),
        "simplify": cfg.simplify.model_dump(),
        "eligible_voters": cfg.eligible_voters.model_dump(),
        "provinces": [p.model_dump() for p in cfg.provinces],
        "units": "land CBS buurten (water == 'NEE'), including zero-population buurten",
        "coverage_repair_snap_m": 0.05,
        "cbs_missing_threshold": cbs.MISSING_THRESHOLD,
        "urbanity_thresholds": list(cbs.URBANITY_THRESHOLDS),
        "compositional_groups": {k: list(v) for k, v in cbs.CORE_COMPOSITIONS.items()},
    }


def _fingerprint(records: dict[str, DownloadRecord], params: dict[str, Any]) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "sources": {k: records[k].sha256 for k in sorted(records)},
        "parameters": params,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def store_files(cfg: GeographyConfig) -> list[str]:
    """Relative paths of every file of a complete store."""
    files = [
        "provinces.parquet",
        "municipalities.parquet",
        "units.parquet",
        "units_attrs.parquet",
        "unit_adjacency.parquet",
        "municipality_adjacency.parquet",
        "web/provinces.geojson",
        "web/municipalities.geojson",
    ]
    files.extend(f"web/units/{code}.geojson" for code in cfg.province_codes)
    return files


def _existing_build(out_dir: Path, fingerprint: str, cfg: GeographyConfig) -> dict[str, Any] | None:
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if manifest.get("fingerprint") != fingerprint:
        return None
    if not all((out_dir / f).exists() for f in store_files(cfg)):
        return None
    return manifest


def _file_digest(path: Path) -> dict[str, Any]:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 22):
            h.update(block)
    return {"bytes": path.stat().st_size, "sha256": h.hexdigest()}


def write_store(tables: GeoTables, out_dir: Path, cfg: GeographyConfig, timings: dict[str, float]) -> None:
    """Write the GeoParquet tables and the web GeoJSON layers into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with _step("write parquet", timings):
        tables.provinces.to_parquet(out_dir / "provinces.parquet", index=False, compression="zstd")
        tables.municipalities.to_parquet(out_dir / "municipalities.parquet", index=False, compression="zstd")
        tables.units.to_parquet(out_dir / "units.parquet", index=False, compression="zstd")
        pd.DataFrame(tables.units.drop(columns="geometry")).to_parquet(
            out_dir / "units_attrs.parquet", index=False, compression="zstd"
        )
        tables.unit_adjacency.to_parquet(out_dir / "unit_adjacency.parquet", index=False, compression="zstd")
        tables.municipality_adjacency.to_parquet(
            out_dir / "municipality_adjacency.parquet", index=False, compression="zstd"
        )
    web = out_dir / "web"
    props = ("code", "name", "province_code", "population")
    with _step("web provinces", timings):
        prov = tables.provinces.assign(province_code=tables.provinces["code"])
        write_web_geojson(prov, web / "provinces.geojson", props, cfg.simplify.provinces_m)
    with _step("web municipalities", timings):
        write_web_geojson(
            tables.municipalities, web / "municipalities.geojson", props, cfg.simplify.municipalities_m
        )
    with _step("web units", timings):
        simplified = coverage_simplify(tables.units[[*props, "geometry"]], cfg.simplify.units_m)
        for code in cfg.province_codes:
            write_web_geojson(
                simplified[simplified["province_code"] == code], web / "units" / f"{code}.geojson", props, 0.0
            )


def _library_versions() -> dict[str, str]:
    import sys

    import pyarrow
    import pyogrio
    import pyproj
    import scipy

    return {
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "geopandas": gpd.__version__,
        "shapely": shapely.__version__,
        "geos": shapely.geos_version_string,
        "pyproj": pyproj.__version__,
        "pyogrio": pyogrio.__version__,
        "gdal": pyogrio.__gdal_version_string__,
        "pyarrow": pyarrow.__version__,
        "scipy": scipy.__version__,
    }


def _report_from_manifest(manifest: dict[str, Any], out_dir: Path, skipped: bool) -> BuildReport:
    return BuildReport(
        year=int(manifest["year"]),
        path=out_dir,
        skipped=skipped,
        counts=dict(manifest.get("counts", {})),
        timings=dict(manifest.get("timings", {})),
        water_links=list(manifest.get("water_links", {}).get("units", [])),
        warnings=list(manifest.get("warnings", [])),
        manifest=manifest,
    )


def _swap_into_place(tmp_dir: Path, final: Path) -> None:
    """Replace ``final`` by ``tmp_dir`` with two renames (the old store is absent only between
    them); if the second rename fails the previous store is put back."""
    if final.exists():
        old = final.with_name(f".{final.name}.old-{os.getpid()}")
        if old.exists():
            shutil.rmtree(old)
        final.rename(old)
        try:
            tmp_dir.rename(final)
        except OSError:
            try:
                old.rename(final)
            except OSError:  # e.g. a concurrent build already put its store in place
                log.error("could not restore the previous store; it is kept at %s", old)
            raise
        shutil.rmtree(old, ignore_errors=True)
    else:
        tmp_dir.rename(final)


# --------------------------------------------------------------------------- public API
def build_geography(year: int | None = None, force: bool = False) -> BuildReport:
    """Build (or reuse) ``data/processed/geo_<year>/`` from the raw CBS/PDOK sources.

    Missing raw sources are downloaded when network access is allowed, otherwise
    :class:`DataNotPreparedError` is raised.  Raises :class:`ValidationError` when an invariant fails
    (the existing store is then left untouched).
    """
    settings = get_settings()
    cfg = load_geography_config()
    year = int(year or settings.geography_year)
    out_dir = settings.processed_geo_dir(year)
    timings: dict[str, float] = {}
    t_start = time.perf_counter()
    total = Timer(log, f"build_geography({year})")
    with total:
        try:
            records = {r.key: r for r in download_all(year, config=cfg)}
        except DownloadError as exc:
            raise DataNotPreparedError(f"Raw sources for {year} are not available: {exc}") from exc
        params = _parameters(cfg)
        fingerprint = _fingerprint(records, params)
        existing = None if force else _existing_build(out_dir, fingerprint, cfg)
        if existing is not None:
            log.info("geography store %s is up to date (fingerprint %s) — skipped", out_dir, fingerprint[:12])
            return _report_from_manifest(existing, out_dir, skipped=True)

        with _step("read sources", timings):
            raw = read_raw_inputs(year, cfg, records)
        tables = assemble_tables(raw, cfg, timings)
        expected = (
            set(raw.municipalities_generalized["cbs_code"])
            if raw.municipalities_generalized is not None
            else None
        )
        with _step("validate", timings):
            problems = validate_tables(tables, cfg, expected)
        if problems:
            raise ValidationError(f"geography {year} failed validation", problems)

        settings.processed_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = settings.processed_dir / f".geo_{year}.build-{os.getpid()}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        try:
            write_store(tables, tmp_dir, cfg, timings)
            warnings: list[str] = []
            if tables.info.get("coverage_repaired_units"):
                warnings.append(
                    f"coverage repair adjusted {len(tables.info['coverage_repaired_units'])} unit polygons "
                    "(sub-millimetre overlaps removed)"
                )
            units = tables.units
            counts = {
                "provinces": len(tables.provinces),
                "municipalities": len(tables.municipalities),
                "municipalities_generalized_layer": len(expected) if expected is not None else None,
                "units": len(units),
                "units_zero_population": int((units["population"] == 0).sum()),
                "population": int(units["population"].sum()),
                "population_official": int(tables.municipalities["population_official"].fillna(0).sum()),
                "eligible_voters_est": int(units["eligible_voters_est"].sum()),
                "unit_edges": len(tables.unit_adjacency),
                "unit_water_links": int((tables.unit_adjacency["kind"] == WATER_LINK).sum()),
                "municipality_edges": len(tables.municipality_adjacency),
                "municipality_water_links": int((tables.municipality_adjacency["kind"] == WATER_LINK).sum()),
                "water_links": int((tables.unit_adjacency["kind"] == WATER_LINK).sum()),
                "units_with_imputed_fields": int((units["imputed_fields"] != "").sum()),
                "units_population_imputed": tables.info.get("population_imputed_units", 0),
            }
            manifest: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "year": year,
                "built_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                "fingerprint": fingerprint,
                "crs": "EPSG:28992",
                "web_crs": "EPSG:4326",
                "data_category": {
                    "REAL": "geometry, codes, names, population, CBS indicators",
                    "DERIVED": "eligible_voters_est, log_density, urbanity, imputed fields, water links",
                },
                "sources": {k: records[k].to_json() for k in sorted(records)},
                "source_years": {"core": raw.core_year, "supplement": raw.supplement_year},
                "counts": counts,
                "parameters": params,
                "imputation": {
                    "units_by_field": tables.info.get("imputed_units", {}),
                    "municipalities_by_field": _flag_counts(tables.municipalities["imputed_fields"]),
                },
                "province_assignment": {
                    "method": "largest area overlap with CBS generalised province polygons",
                    "min_overlap_fraction": round(tables.info.get("province_overlap_min", 1.0), 4),
                    "lowest_overlap": tables.info.get("province_overlap_lowest", {}),
                },
                "coverage_repaired_units": tables.info.get("coverage_repaired_units", []),
                "water_links": {
                    "units": _records(tables.info.get("unit_water_links", [])),
                    "municipalities": _records(tables.info.get("municipality_water_links", [])),
                },
                "warnings": warnings,
                "library_versions": _library_versions(),
            }
            files = {f: _file_digest(tmp_dir / f) for f in store_files(cfg)}
            manifest["files"] = files
            timings["total"] = round(time.perf_counter() - t_start, 3)
            manifest["timings"] = timings
            (tmp_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            _swap_into_place(tmp_dir, out_dir)
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
    from app.geography import store

    store.clear_cache()
    log.info(
        "geography %d built: %d provinces, %d municipalities, %d units, population %d, %d water links",
        year,
        counts["provinces"],
        counts["municipalities"],
        counts["units"],
        counts["population"],
        counts["water_links"],
        extra=log_ctx(seconds=round(total.elapsed, 1)),
    )
    return _report_from_manifest(manifest, out_dir, skipped=False)


def _flag_counts(flags: pd.Series) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in flags.fillna(""):
        if value:
            for name in str(value).split(","):
                counts[name] = counts.get(name, 0) + 1
    return {k: counts[k] for k in FLAG_ORDER if k in counts}


def _records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        out.append({k: (float(v) if isinstance(v, np.floating) else v) for k, v in r.items()})
    return out
