"""Topology-preserving simplification and compact web GeoJSON output.

:func:`coverage_simplify` wraps GEOS ``CoverageSimplifyVW`` (``shapely.coverage_simplify``): the
polygons of a coverage are simplified *together*, so a border shared by two neighbours is simplified
once and stays shared — no gaps or overlaps appear between neighbouring municipalities/units.

:func:`write_web_geojson` reprojects to EPSG:4326, snaps coordinates to 5 decimals (≈ 1 m) while
keeping every polygon valid, and writes a compact FeatureCollection for the Leaflet UI.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from app.core.logging import Timer, get_logger
from app.geography.topology import polygonal

try:  # orjson is a declared dependency; fall back to the stdlib for robustness
    import orjson

    def _dumps(obj: Any) -> bytes:
        return orjson.dumps(obj)

except ImportError:  # pragma: no cover
    import json

    def _dumps(obj: Any) -> bytes:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


log = get_logger(__name__)

WEB_CRS = "EPSG:4326"
WEB_DECIMALS = 5


def coverage_simplify(
    gdf: gpd.GeoDataFrame, tolerance_m: float, simplify_boundary: bool = True
) -> gpd.GeoDataFrame:
    """Copy of ``gdf`` with its polygon coverage simplified (shared borders stay shared).

    ``tolerance_m`` is in CRS units (metres for EPSG:28992); ``0`` returns an unmodified copy.
    Polygons that would collapse keep their original geometry.
    """
    out = gdf.copy()
    if tolerance_m <= 0 or len(gdf) == 0:
        return out
    geoms = np.asarray(gdf.geometry.values, dtype=object)
    simplified = shapely.coverage_simplify(geoms, tolerance_m, simplify_boundary=simplify_boundary)
    collapsed = shapely.is_empty(simplified) | (shapely.area(simplified) <= 0)
    if collapsed.any():
        simplified[collapsed] = geoms[collapsed]
    out.geometry = gpd.GeoSeries(simplified, index=gdf.index, crs=gdf.crs)
    return out


def round_coordinates(geoms: np.ndarray, decimals: int = WEB_DECIMALS) -> np.ndarray:
    """Round every coordinate to ``decimals`` and return *valid* geometries.

    Plain rounding of reprojected, simplified polygons can create self-intersections and collapsed
    rings (seen on the real data: 2 provinces, 6 municipalities and several units).  Coordinates
    are therefore snapped with the GEOS precision reducer (``shapely.set_precision``, which yields
    valid output and removes collapsed parts) and then rounded to the canonical decimal value.
    Identical input vertices stay identical, so shared borders stay shared.  A geometry that the
    precision reducer would erase entirely (or leave invalid) falls back to plain rounding followed
    by ``make_valid``, keeping whatever polygonal part survives at this precision.
    """
    arr = np.asarray(geoms, dtype=object)
    if len(arr) == 0:
        return arr.copy()

    def canon(xy: np.ndarray) -> np.ndarray:
        return np.round(xy, decimals)

    snapped = shapely.transform(shapely.set_precision(arr, 10.0**-decimals), canon)
    lost = shapely.is_empty(snapped) & ~shapely.is_empty(arr)
    lost |= ~shapely.is_valid(snapped) & ~shapely.is_missing(snapped)
    if lost.any():
        fallback = shapely.make_valid(shapely.transform(arr[lost], canon))
        was_polygonal = np.isin(shapely.get_type_id(arr[lost]), (3, 6))
        snapped[lost] = [polygonal(g) if poly else g for g, poly in zip(fallback, was_polygonal, strict=True)]
    return snapped


def to_web_geometries(
    gdf: gpd.GeoDataFrame, tolerance_m: float = 0.0, decimals: int = WEB_DECIMALS
) -> gpd.GeoDataFrame:
    """Coverage-simplify (in the source CRS), reproject to EPSG:4326 and round coordinates."""
    simplified = coverage_simplify(gdf, tolerance_m) if tolerance_m > 0 else gdf.copy()
    web = simplified.to_crs(WEB_CRS) if simplified.crs is not None else simplified.set_crs(WEB_CRS)
    web.geometry = gpd.GeoSeries(
        [polygonal(g) for g in round_coordinates(np.asarray(web.geometry.values, dtype=object), decimals)],
        index=web.index,
        crs=WEB_CRS,
    )
    return web


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating | float):
        return None if not np.isfinite(value) else round(float(value), 6)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def geojson_bytes(gdf: gpd.GeoDataFrame, props: Sequence[str]) -> bytes:
    """Serialise ``gdf`` (already in the target CRS) as a compact GeoJSON FeatureCollection."""
    geoms = np.asarray(gdf.geometry.values, dtype=object)
    features = []
    columns = {p: gdf[p].tolist() for p in props if p in gdf.columns}
    for i, geom in enumerate(geoms):
        properties = {p: _json_value(vals[i]) for p, vals in columns.items()}
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": None if geom is None or geom.is_empty else shapely.geometry.mapping(geom),
            }
        )
    return _dumps({"type": "FeatureCollection", "features": features})


def write_web_geojson(
    gdf: gpd.GeoDataFrame,
    path: str | Path,
    props: Sequence[str] = ("code", "name", "province_code", "population"),
    tolerance_m: float = 0.0,
    decimals: int = WEB_DECIMALS,
) -> Path:
    """Write ``gdf`` as coverage-simplified, EPSG:4326, 5-decimal GeoJSON with ``props`` properties.

    ``tolerance_m`` applies in the source (projected) CRS before reprojection.  Returns the path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with Timer(log, f"web geojson {path.name} ({len(gdf)} features)"):
        web = to_web_geometries(gdf, tolerance_m, decimals)
        payload = geojson_bytes(web, props)
        # unique temporary name: concurrent writers must not clobber each other's partial file
        tmp = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.part")
        try:
            tmp.write_bytes(payload)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
    return path
