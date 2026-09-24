"""District statistics, geometries, adjacency and municipality fragments.

All functions take a :class:`~app.districts.plan.GeneratedPlan` plus the unit table / GeoDataFrame
(EPSG:28992) it was generated from.  Compactness metrics are computed on the full-resolution
dissolved district geometry:

* **Polsby-Popper** ``4πA / P²`` (1 = circle),
* **Reock** ``A / area(minimum bounding circle)`` (shapely ``minimum_bounding_radius``, exact π r²),
* **convex-hull ratio** ``A / area(convex hull)``.

Contiguity is graph contiguity over the unit adjacency including water links (islands linked by a
ferry/bridge count as contiguous), so ``n_components`` is the number of graph components, not
the number of polygons.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer

from app.core.errors import DistrictingError
from app.core.logging import Timer, get_logger
from app.districts.plan import GeneratedPlan

log = get_logger(__name__)

RD_NEW = "EPSG:28992"
WGS84 = "EPSG:4326"
_to_wgs84 = Transformer.from_crs(RD_NEW, WGS84, always_xy=True)


# =========================================================================== alignment helpers
def _positions(plan: GeneratedPlan, units: pd.DataFrame) -> np.ndarray:
    """Row position in ``units`` of every plan unit."""
    pos = pd.Index(units["code"].astype(str)).get_indexer(plan.unit_codes.astype(str))
    if (pos < 0).any():
        raise DistrictingError(f"{int((pos < 0).sum())} plan units are missing from the unit table")
    return pos


def _groups(labels: np.ndarray, n: int) -> list[np.ndarray]:
    order = np.argsort(labels, kind="stable")
    counts = np.bincount(labels, minlength=n)
    return np.split(order, np.cumsum(counts)[:-1])


# =========================================================================== geometries
def district_geometries(plan: GeneratedPlan, units_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Full-resolution district polygons (EPSG:28992), one row per district in plan order.

    Units are dissolved with a fast coverage union; any district whose coverage union is invalid
    or loses area (imperfect source coverage) is recomputed with a robust ``union_all``.
    """
    with Timer(log, "district geometries: dissolve"):
        pos = _positions(plan, units_gdf)
        geoms = np.asarray(units_gdf.geometry.values, dtype=object)[pos]
        crs = units_gdf.crs or RD_NEW
        out: list[Any] = []
        for idx in _groups(plan.unit_district, plan.n_districts):
            parts = np.asarray(geoms[idx], dtype=object)
            if len(parts) == 0:
                out.append(shapely.Polygon())
                continue
            g = shapely.coverage_union_all(parts)
            expected = float(shapely.area(parts).sum())
            if not shapely.is_valid(g) or abs(shapely.area(g) - expected) > 1e-6 * max(expected, 1.0):
                g = shapely.union_all(shapely.make_valid(parts))
            out.append(g)
    return gpd.GeoDataFrame(
        {
            "code": plan.district_codes,
            "name": plan.district_names,
            "province_code": plan.district_province,
        },
        geometry=out,
        crs=crs,
    )


def simplify_geometries(geometries: gpd.GeoDataFrame, tolerance_m: float = 80.0) -> gpd.GeoSeries:
    """Topology-preserving (coverage) simplification: shared borders stay shared (EPSG:28992)."""
    geoms = np.asarray(geometries.geometry.values, dtype=object)
    if tolerance_m <= 0 or len(geoms) == 0:
        return geometries.geometry.copy()
    try:
        simp = shapely.coverage_simplify(geoms, tolerance_m, simplify_boundary=True)
    except Exception as exc:  # GEOS rejects invalid coverages: fall back to per-polygon simplify
        log.warning("coverage simplification failed (%s); using per-polygon simplification", exc)
        simp = shapely.simplify(geoms, tolerance_m, preserve_topology=True)
    simp = np.where(shapely.is_valid(simp), simp, shapely.make_valid(simp))
    return gpd.GeoSeries(simp, index=geometries.index, crs=geometries.crs)


def _round_coords(geoms: np.ndarray, precision: int) -> np.ndarray:
    return shapely.transform(geoms, lambda c: np.round(c, precision))


def geometries_wgs84(
    geometries: gpd.GeoDataFrame, tolerance_m: float = 80.0, precision: int = 6
) -> np.ndarray:
    """Coverage-simplified district geometries in EPSG:4326 (object array)."""
    simp = simplify_geometries(geometries, tolerance_m).to_crs(WGS84)
    geoms = _round_coords(np.asarray(simp.values, dtype=object), precision)
    return np.where(shapely.is_valid(geoms), geoms, shapely.make_valid(geoms))


def geometries_wkb(geometries: gpd.GeoDataFrame, tolerance_m: float = 80.0) -> dict[str, bytes]:
    """District code → WKB (EPSG:4326, coverage-simplified) for ``HouseDistrict.geometry_wkb``."""
    geoms = geometries_wgs84(geometries, tolerance_m)
    wkb = shapely.to_wkb(geoms)
    return dict(zip(geometries["code"].tolist(), wkb.tolist(), strict=True))


def write_districts_geojson(
    geometries: gpd.GeoDataFrame,
    path: str | Path,
    tolerance_m: float = 80.0,
    precision: int = 5,
    properties: pd.DataFrame | None = None,
) -> Path:
    """Write a web GeoJSON (EPSG:4326, coverage-simplified, ``precision`` decimals).

    Properties are ``code, name, province_code`` plus any columns of ``properties`` (a frame
    indexed or keyed by ``code``, e.g. :func:`district_stats` output).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    geoms = geometries_wgs84(geometries, tolerance_m, precision)
    props = geometries[["code", "name", "province_code"]].copy()
    if properties is not None:
        extra = properties.set_index("code") if "code" in properties.columns else properties
        extra = extra.drop(columns=[c for c in ("name", "province_code", "geometry") if c in extra.columns])
        props = props.join(extra, on="code")
    features = []
    for rec, geom in zip(props.to_dict(orient="records"), geoms, strict=True):
        clean = {k: _json_value(v) for k, v in rec.items()}
        features.append(
            {"type": "Feature", "properties": clean, "geometry": json.loads(shapely.to_geojson(geom))}
        )
    doc = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return path


def _json_value(v: Any) -> Any:
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and not np.isfinite(v):
        return None
    return v


# =========================================================================== statistics
def district_stats(
    plan: GeneratedPlan,
    units_gdf: pd.DataFrame,
    geometries: gpd.GeoDataFrame | None = None,
) -> pd.DataFrame:
    """Per-district statistics (one row per district, plan order).

    Columns: ``code, number, name, province_code, population, eligible_voters_est,
    target_population, deviation_pct, area_km2, polsby_popper, reock, convex_hull_ratio, n_units,
    n_municipalities, n_split_municipalities, urban_share, rural_share, is_contiguous,
    n_components, centroid_lon, centroid_lat``.  Geometric metrics need a geometry column in
    ``units_gdf`` (or ``geometries``, matched to the plan's districts on their ``code`` column); without
    one they are NaN and the centroid is the population-weighted unit centroid.
    """
    n_d = plan.n_districts
    pos = _positions(plan, units_gdf)
    ud = plan.unit_district
    pop = plan.unit_population.astype(np.int64)
    units = units_gdf.iloc[pos]
    eligible = (
        pd.to_numeric(units["eligible_voters_est"], errors="coerce").fillna(0).to_numpy(dtype=float)
        if "eligible_voters_est" in units.columns
        else np.zeros(len(pos))
    )
    urb = (
        pd.to_numeric(units["urbanity_class"], errors="coerce").fillna(0).to_numpy(dtype=np.int64)
        if "urbanity_class" in units.columns
        else np.zeros(len(pos), dtype=np.int64)
    )
    d_pop = np.bincount(ud, weights=pop.astype(float), minlength=n_d)
    safe = np.where(d_pop > 0, d_pop, 1.0)
    urban = np.bincount(ud, weights=pop * ((urb >= 1) & (urb <= 2)), minlength=n_d) / safe
    rural = np.bincount(ud, weights=pop * ((urb >= 4) & (urb <= 5)), minlength=n_d) / safe
    frag = pd.DataFrame({"m": plan.unit_municipality, "d": ud}).drop_duplicates()
    parts_per_muni = frag.groupby("m").size()
    frag["split"] = frag["m"].map(parts_per_muni).to_numpy() > 1
    n_munis = np.bincount(frag["d"].to_numpy(), minlength=n_d)
    n_split = np.bincount(frag["d"].to_numpy(), weights=frag["split"].to_numpy(dtype=float), minlength=n_d)
    comps = plan.district_components()
    target = plan.district_target
    stats = pd.DataFrame(
        {
            "code": plan.district_codes,
            "number": plan.district_numbers,
            "name": plan.district_names,
            "province_code": plan.district_province,
            "population": d_pop.round().astype(np.int64),
            "eligible_voters_est": np.bincount(ud, weights=eligible, minlength=n_d).round().astype(np.int64),
            "target_population": target,
            "deviation_pct": (d_pop - target) / np.where(target > 0, target, 1.0) * 100.0,
            "n_units": np.bincount(ud, minlength=n_d),
            "n_municipalities": n_munis,
            "n_split_municipalities": n_split.astype(np.int64),
            "urban_share": urban,
            "rural_share": rural,
            "is_contiguous": comps == 1,
            "n_components": comps,
        }
    )
    has_geom = geometries is not None or has_geometry(units_gdf)
    if "land_area_km2" in units.columns:
        land = pd.to_numeric(units["land_area_km2"], errors="coerce").fillna(0).to_numpy(dtype=float)
        stats["area_km2"] = np.bincount(ud, weights=land, minlength=n_d)
    if has_geom:
        geo = (
            _align_geometries(plan, geometries)
            if geometries is not None
            else district_geometries(plan, units_gdf)  # type: ignore[arg-type]
        )
        stats = stats.join(compactness(geo).set_index(stats.index))
        if "area_km2" not in stats.columns:
            stats["area_km2"] = shapely.area(np.asarray(geo.geometry.values, dtype=object)) / 1e6
    else:
        if "area_km2" not in stats.columns:
            stats["area_km2"] = np.nan
        for c in ("polsby_popper", "reock", "convex_hull_ratio"):
            stats[c] = np.nan
        w = pop.astype(float) + 1e-3
        cx = np.bincount(ud, weights=w * units["centroid_x"].to_numpy(dtype=float), minlength=n_d)
        cy = np.bincount(ud, weights=w * units["centroid_y"].to_numpy(dtype=float), minlength=n_d)
        sw = np.bincount(ud, weights=w, minlength=n_d)
        lon, lat = _to_wgs84.transform(cx / sw, cy / sw)
        stats["centroid_lon"], stats["centroid_lat"] = lon, lat
    cols = [
        "code", "number", "name", "province_code", "population", "eligible_voters_est", "target_population",
        "deviation_pct", "area_km2", "polsby_popper", "reock", "convex_hull_ratio", "n_units", "n_municipalities",
        "n_split_municipalities", "urban_share", "rural_share", "is_contiguous", "n_components",
        "centroid_lon", "centroid_lat",
    ]  # fmt: skip
    return stats[cols]


def _align_geometries(plan: GeneratedPlan, geometries: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """``geometries`` in plan district order (matched on ``code`` when the column is present)."""
    if "code" in geometries.columns:
        codes = geometries["code"].astype(str)
        if not codes.is_unique:
            raise DistrictingError("district geometries contain duplicate codes")
        pos = pd.Index(codes).get_indexer(plan.district_codes)
        if (pos < 0).any():
            missing = [c for c, i in zip(plan.district_codes, pos, strict=True) if i < 0]
            raise DistrictingError(f"district geometries lack {len(missing)} districts (e.g. {missing[:5]})")
        return geometries.iloc[pos]
    if len(geometries) != plan.n_districts:
        raise DistrictingError(
            f"{len(geometries)} district geometries for {plan.n_districts} districts (and no 'code' column)"
        )
    return geometries


def has_geometry(df: pd.DataFrame) -> bool:
    """True when ``df`` is a GeoDataFrame with an active geometry column."""
    if not isinstance(df, gpd.GeoDataFrame):
        return False
    try:
        return df.geometry is not None
    except AttributeError:
        return False


def compactness(geometries: gpd.GeoDataFrame) -> pd.DataFrame:
    """Polsby-Popper, Reock, convex-hull ratio and label point (lon/lat) per geometry."""
    geoms = np.asarray(geometries.geometry.values, dtype=object)
    area = shapely.area(geoms)
    perim = shapely.length(geoms)
    with np.errstate(divide="ignore", invalid="ignore"):
        pp = np.where(perim > 0, 4.0 * np.pi * area / perim**2, np.nan)
        circle = np.pi * shapely.minimum_bounding_radius(geoms) ** 2  # exact circle, not a polygon
        reock = np.where(circle > 0, area / circle, np.nan)
        hull = shapely.area(shapely.convex_hull(geoms))
        chr_ = np.where(hull > 0, area / hull, np.nan)
    cent = shapely.centroid(geoms)
    inside = shapely.contains(geoms, cent) | shapely.is_empty(geoms)
    label = np.where(inside, cent, shapely.point_on_surface(geoms))
    x = shapely.get_x(label)
    y = shapely.get_y(label)
    lon, lat = _to_wgs84.transform(x, y)
    return pd.DataFrame(
        {
            "polsby_popper": pp,
            "reock": reock,
            "convex_hull_ratio": chr_,
            "centroid_lon": lon,
            "centroid_lat": lat,
        }
    )


# =========================================================================== adjacency / fragments
def district_adjacency(plan: GeneratedPlan) -> pd.DataFrame:
    """District adjacency from the unit graph: ``district_a, district_b, shared_border_km,
    via_water_link`` (``district_a < district_b`` by code; ``via_water_link`` = connected only by
    water links).  Includes adjacencies across province boundaries."""
    cols = ["district_a", "district_b", "shared_border_km", "via_water_link"]
    if len(plan.edges) == 0:
        return pd.DataFrame(columns=cols)
    ud = plan.unit_district
    a, b = ud[plan.edges[:, 0]], ud[plan.edges[:, 1]]
    cross = a != b
    if not cross.any():
        return pd.DataFrame(columns=cols)
    codes = np.asarray(plan.district_codes, dtype=object)
    ca, cb = codes[a[cross]], codes[b[cross]]
    lo = np.where(ca < cb, ca, cb)
    hi = np.where(ca < cb, cb, ca)
    border = (
        plan.edge_border_m[cross]
        if len(plan.edge_border_m) == len(plan.edges)
        else np.zeros(int(cross.sum()))
    )
    water = (
        plan.edge_water[cross]
        if len(plan.edge_water) == len(plan.edges)
        else np.zeros(int(cross.sum()), bool)
    )
    df = pd.DataFrame({"district_a": lo, "district_b": hi, "border_m": border, "land": ~water})
    out = df.groupby(["district_a", "district_b"], sort=True).agg(
        border_m=("border_m", "sum"), land=("land", "any")
    )
    out = out.reset_index()
    out["shared_border_km"] = out["border_m"] / 1000.0
    out["via_water_link"] = ~out["land"]
    return out[cols]


def district_municipality_fragments(plan: GeneratedPlan) -> pd.DataFrame:
    """District × municipality fragments: ``district_code, municipality_code, population, n_units,
    share_of_municipality, share_of_district`` (shares of population; of units when zero)."""
    df = pd.DataFrame(
        {
            "district_code": plan.unit_district_codes(),
            "municipality_code": plan.unit_municipality,
            "population": plan.unit_population.astype(np.int64),
        }
    )
    frag = (
        df.groupby(["district_code", "municipality_code"], sort=True)
        .agg(population=("population", "sum"), n_units=("population", "size"))
        .reset_index()
    )
    m_pop = frag.groupby("municipality_code")["population"].transform("sum")
    m_units = frag.groupby("municipality_code")["n_units"].transform("sum")
    d_pop = frag.groupby("district_code")["population"].transform("sum")
    d_units = frag.groupby("district_code")["n_units"].transform("sum")
    frag["share_of_municipality"] = np.where(
        m_pop > 0, frag["population"] / m_pop.where(m_pop > 0, 1), frag["n_units"] / m_units
    )
    frag["share_of_district"] = np.where(
        d_pop > 0, frag["population"] / d_pop.where(d_pop > 0, 1), frag["n_units"] / d_units
    )
    return frag
