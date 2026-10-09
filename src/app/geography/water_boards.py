"""REAL water authority areas (*waterschappen*): GML parsing and the assignment of units.

The Netherlands has 21 water boards.  Their areas cross municipal and provincial borders, so they
are a separate geography next to the province → municipality → buurt hierarchy.  The areas come
from Het Waterschapshuis (INSPIRE *Administrative Units*, PDOK, CC0 1.0) as a WFS 2.0 GML
``FeatureCollection`` of ``au:AdministrativeUnit`` features::

    <au:AdministrativeUnit>
      <au:geometry><gml:MultiSurface srsName="urn:ogc:def:crs:EPSG::4258"> … <gml:Polygon>
        <gml:exterior><gml:LinearRing><gml:posList srsDimension="3">lat lon h lat lon h …
        <gml:interior> … (holes)
      <au:nationalCode>33</au:nationalCode>
      <gml:name>Waterschap Hunze en Aa's</gml:name>

GDAL cannot read the 3-D multi-surfaces of this file, so :func:`read_water_boards_gml` parses it
with :mod:`xml.etree.ElementTree`.  Coordinates follow the axis order of the CRS authority (GML
3.2): for EPSG:4258 that is *latitude, longitude*; they are swapped to x/y and projected to
EPSG:28992 (RD New, the CRS of the geography store).

Every unit (CBS buurt) is then assigned to one board by :func:`assign_units`: the board whose area
contains the unit's representative point (``within``), else the nearest board within a maximum
distance (``nearest``; gaps between the published polygons, coastline slivers), else none
(``none``).  The areas are REAL; the assignment and the board totals are DERIVED.  Everything
*elected* in a water board is FICTIONAL and lives outside the geography store.

All functions are pure (no database, no network).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry

from app.core.errors import ValidationError
from app.core.logging import Timer, get_logger, log_ctx
from app.geography.topology import polygonal

log = get_logger(__name__)

#: CRS of the geography store (Amersfoort / RD New).
STORE_CRS = 28992
#: CRS assumed when a GML geometry carries no ``srsName`` (INSPIRE data are ETRS89).
DEFAULT_GML_SRS = "EPSG:4258"

#: Assignment methods (column ``water_board_method`` of :func:`assign_units`).
WITHIN = "within"
NEAREST = "nearest"
NONE = "none"
ASSIGNMENT_METHODS: tuple[str, ...] = (WITHIN, NEAREST, NONE)

#: Columns of the board table produced by :func:`read_water_boards_gml`.
SOURCE_COLUMNS: tuple[str, ...] = ("code", "name", "national_code")
#: Attribute columns of the store table ``water_boards.parquet`` (:func:`summarize_water_boards`).
BOARD_COLUMNS: tuple[str, ...] = (
    "code",
    "name",
    "national_code",
    "province_code",
    "provinces",
    "population",
    "eligible_voters_est",
    "unit_count",
    "municipality_count",
    "area_km2",
)

_GML_NAMESPACE_PREFIX = "http://www.opengis.net/gml"
_POLYGON_TAGS = frozenset({"Polygon", "PolygonPatch"})


# --------------------------------------------------------------------------- codes
def water_board_code(national_code: int | str) -> str:
    """Store code of a water board from its national code: ``33`` → ``'WS33'``."""
    return f"WS{int(str(national_code).strip()):02d}"


# --------------------------------------------------------------------------- GML parsing
def _local(tag: str) -> str:
    """Local part of a ``{namespace}name`` tag."""
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _is_gml(elem: ET.Element) -> bool:
    return _namespace(elem.tag).startswith(_GML_NAMESPACE_PREFIX)


@lru_cache(maxsize=16)
def _transformer(srs_name: str, target_epsg: int) -> tuple[Transformer | None, bool]:
    """``(transformer to target_epsg with always_xy=True or None when identical, swap_axes)``.

    ``swap_axes`` is True when the CRS authority lists northing/latitude first (EPSG:4258, 4326),
    the axis order GML 3.2 coordinates follow.
    """
    crs = CRS.from_user_input(srs_name)
    first = crs.axis_info[0].direction.lower() if crs.axis_info else "east"
    swap = first in ("north", "south")
    target = CRS.from_epsg(target_epsg)
    transformer = None if crs.equals(target) else Transformer.from_crs(crs, target, always_xy=True)
    return transformer, swap


def _ring_coordinates(ring: ET.Element, dim: int) -> np.ndarray:
    """``(n, 2)`` array of the first two axes of a ``LinearRing`` (``posList`` or ``pos``)."""
    values: list[float] = []
    ring_dim = dim
    for child in ring.iter():
        name = _local(child.tag)
        if name == "posList" and child.text:
            ring_dim = int(child.get("srsDimension") or child.get("dimension") or dim)
            values.extend(float(v) for v in child.text.split())
        elif name == "pos" and child.text:
            parts = [float(v) for v in child.text.split()]
            ring_dim = len(parts)
            values.extend(parts)
        elif name == "coordinates" and child.text:  # GML 2 style "x,y x,y"
            ring_dim = 2
            for tup in child.text.split():
                values.extend(float(v) for v in tup.split(",")[:2])
    if not values:
        return np.zeros((0, 2))
    if ring_dim < 2 or len(values) % ring_dim:
        raise ValidationError(f"water board GML: {len(values)} ordinates do not fit dimension {ring_dim}")
    return np.asarray(values, dtype=float).reshape(-1, ring_dim)[:, :2]


def _project(coords: np.ndarray, srs_name: str, target_epsg: int) -> np.ndarray:
    transformer, swap = _transformer(srs_name, target_epsg)
    xy = coords[:, ::-1] if swap else coords
    if transformer is None:
        return np.ascontiguousarray(xy)
    x, y = transformer.transform(xy[:, 0], xy[:, 1])
    out = np.column_stack([np.asarray(x, dtype=float), np.asarray(y, dtype=float)])
    if not np.isfinite(out).all():
        raise ValidationError(f"water board GML: coordinates outside the domain of {srs_name}")
    return out


def _polygons(elem: ET.Element, srs: str | None, dim: int) -> Iterator[tuple[ET.Element, str | None, int]]:
    """Every ``Polygon``/``PolygonPatch`` below ``elem`` with the inherited ``srsName``/dimension."""
    srs = elem.get("srsName") or srs
    dim = int(elem.get("srsDimension") or dim)
    if _local(elem.tag) in _POLYGON_TAGS:
        yield elem, srs, dim
        return
    for child in elem:
        yield from _polygons(child, srs, dim)


def _polygon(elem: ET.Element, srs: str, dim: int, target_epsg: int) -> shapely.Polygon | None:
    shell: np.ndarray | None = None
    holes: list[np.ndarray] = []
    for child in elem:
        role = _local(child.tag)
        if role not in ("exterior", "interior", "outerBoundaryIs", "innerBoundaryIs"):
            continue
        coords = _ring_coordinates(child, dim)
        if len(coords) < 4:
            continue
        xy = _project(coords, srs, target_epsg)
        if role in ("exterior", "outerBoundaryIs"):
            shell = xy
        else:
            holes.append(xy)
    if shell is None:
        return None
    return shapely.Polygon(shell, holes)


def _feature_name(feature: ET.Element) -> str:
    """``gml:name`` of the feature, else the INSPIRE ``au:name`` spelling (``gn:text``)."""
    for child in feature:
        if _local(child.tag) == "name" and _is_gml(child) and (child.text or "").strip():
            return " ".join(child.text.split())
    for child in feature:
        if _local(child.tag) == "name":
            for sub in child.iter():
                if _local(sub.tag) == "text" and (sub.text or "").strip():
                    return " ".join(sub.text.split())
    return ""


def _feature_geometry(feature: ET.Element, default_srs: str, target_epsg: int) -> BaseGeometry:
    polys: list[shapely.Polygon] = []
    for child in feature:
        if _local(child.tag) != "geometry":
            continue
        for elem, srs, dim in _polygons(child, None, 2):
            poly = _polygon(elem, srs or default_srs, dim, target_epsg)
            if poly is not None and not poly.is_empty:
                polys.append(poly)
    geom: BaseGeometry = shapely.MultiPolygon(polys) if polys else shapely.MultiPolygon()
    if not geom.is_valid:
        geom = polygonal(shapely.make_valid(geom))
    return geom


def read_water_boards_gml(
    path: str | Path, target_epsg: int = STORE_CRS, default_srs: str = DEFAULT_GML_SRS
) -> gpd.GeoDataFrame:
    """Parse the INSPIRE water board GML into a GeoDataFrame (EPSG ``target_epsg``).

    Columns: ``code`` (``'WS33'``), ``name``, ``national_code`` (int) and ``geometry`` (valid
    (Multi)Polygons with their holes), one row per board, sorted by code.  Features sharing a
    national code are merged.  Raises :class:`ValidationError` when the file holds no water board.
    """
    path = Path(path)
    rows: dict[str, dict[str, Any]] = {}
    with Timer(log, f"read water boards {path.name}"):
        for _, elem in ET.iterparse(path, events=("end",)):
            if _local(elem.tag) != "AdministrativeUnit":
                continue
            raw_code = next((c.text for c in elem if _local(c.tag) == "nationalCode"), None)
            if raw_code is None or not raw_code.strip():
                log.warning("water board feature without nationalCode skipped")
                elem.clear()
                continue
            national = int(raw_code.strip())
            code = water_board_code(national)
            geom = _feature_geometry(elem, default_srs, target_epsg)
            name = _feature_name(elem) or code
            if code in rows:
                log.warning("water board %s appears in several features — merged", code)
                merged = shapely.union_all([rows[code]["geometry"], geom])
                rows[code]["geometry"] = polygonal(shapely.make_valid(merged))
            else:
                rows[code] = {"code": code, "name": name, "national_code": national, "geometry": geom}
            elem.clear()
    if not rows:
        raise ValidationError(f"{path.name}: no au:AdministrativeUnit (water board) features found")
    records = [rows[c] for c in sorted(rows)]
    empty = [r["code"] for r in records if r["geometry"].is_empty]
    if empty:
        raise ValidationError(f"{path.name}: water boards without geometry", empty)
    gdf = gpd.GeoDataFrame(
        {
            "code": [r["code"] for r in records],
            "name": [r["name"] for r in records],
            "national_code": np.array([r["national_code"] for r in records], dtype=np.int64),
        },
        geometry=gpd.GeoSeries([r["geometry"] for r in records], crs=target_epsg),
        crs=target_epsg,
    )
    log.info("%d water boards read from %s", len(gdf), path.name, extra=log_ctx(boards=len(gdf)))
    return gdf


# --------------------------------------------------------------------------- assignment
def representative_points(geoms: Sequence[BaseGeometry] | np.ndarray) -> np.ndarray:
    """One point per geometry: the centroid when it lies inside the polygon, otherwise a point on
    its surface (``shapely.point_on_surface``, always inside).  Points are returned unchanged."""
    arr = np.asarray(geoms, dtype=object)
    if len(arr) == 0:
        return arr.copy()
    centroids = shapely.centroid(arr)
    inside = shapely.contains(arr, centroids) | (shapely.get_type_id(arr) == 0)
    out = centroids.copy()
    if (~inside).any():
        out[~inside] = shapely.point_on_surface(arr[~inside])
    return out


def _unit_geometries(units: Any) -> tuple[np.ndarray, pd.Index, Any]:
    """``(geometries, index, crs)`` of the accepted unit inputs (see :func:`assign_units`)."""
    if isinstance(units, gpd.GeoDataFrame):
        return np.asarray(units.geometry.values, dtype=object), units.index, units.crs
    if isinstance(units, gpd.GeoSeries):
        return np.asarray(units.values, dtype=object), units.index, units.crs
    arr = np.asarray(units)
    if arr.dtype != object and arr.ndim == 2 and arr.shape[1] == 2:
        return shapely.points(arr.astype(float)), pd.RangeIndex(len(arr)), None
    arr = np.asarray(units, dtype=object)
    return arr, pd.RangeIndex(len(arr)), None


def assign_units(
    units: gpd.GeoDataFrame | gpd.GeoSeries | np.ndarray | Sequence[BaseGeometry],
    boards: gpd.GeoDataFrame,
    max_distance_m: float | None = 2000.0,
) -> pd.DataFrame:
    """Water board of every unit, by the unit's representative point.

    ``units`` are unit polygons (a GeoDataFrame/GeoSeries or an array of geometries: their
    :func:`representative_points` are used), points, or an ``(N, 2)`` coordinate array.  ``boards``
    is a GeoDataFrame with ``code`` and the board polygons in a metric CRS (EPSG:28992); units
    carrying another CRS are reprojected to it, plain arrays are taken to be in it.

    * ``within`` — a board polygon contains (or touches) the point.  Where published polygons
      overlap and contain the same point, the board with the largest share of the unit polygon
      wins (for point input: the first board in ``code`` order).
    * ``nearest`` — no polygon contains the point; the nearest board is at most ``max_distance_m``
      away (``None`` = any distance, ``0`` = no fallback).  Ties go to the first code.
    * ``none`` — otherwise; ``water_board_code`` is ``None``.

    Returns a DataFrame aligned with the input (same index) with columns ``water_board_code``,
    ``water_board_method`` and ``water_board_distance_m`` (0 within, NaN for none).
    """
    geoms, index, crs = _unit_geometries(units)
    b = boards.sort_values("code", kind="mergesort").reset_index(drop=True)
    if crs is not None and b.crs is not None and not b.crs.equals(crs):
        geoms = np.asarray(gpd.GeoSeries(geoms, crs=crs).to_crs(b.crs).values, dtype=object)
    bgeoms = np.asarray(b.geometry.values, dtype=object)
    bcodes = b["code"].astype(str).to_numpy(dtype=object)
    n = len(geoms)
    board_of = np.full(n, -1, dtype=np.int64)
    method = np.full(n, NONE, dtype=object)
    distance = np.full(n, np.nan)
    if n == 0 or len(bgeoms) == 0:
        return _assignment_frame(index, board_of, bcodes, method, distance)

    points = representative_points(geoms)
    is_point = shapely.get_type_id(geoms) == 0
    # boards are the (prepared) query geometries: fast point-in-polygon for complex polygons
    shapely.prepare(bgeoms)
    board_idx, point_idx = shapely.STRtree(points).query(bgeoms, predicate="intersects")
    hits = pd.DataFrame({"p": point_idx, "b": board_idx}).sort_values(["p", "b"], kind="mergesort")
    counts = hits.groupby("p")["b"].size()
    single = hits[hits["p"].map(counts) == 1]
    board_of[single["p"].to_numpy()] = single["b"].to_numpy()
    multi = hits[hits["p"].map(counts) > 1]
    if len(multi):
        p_arr, b_arr = multi["p"].to_numpy(), multi["b"].to_numpy()
        polys = ~is_point[p_arr]
        share = np.zeros(len(p_arr))
        if polys.any():
            share[polys] = shapely.area(shapely.intersection(geoms[p_arr[polys]], bgeoms[b_arr[polys]]))
        best = (
            pd.DataFrame({"p": p_arr, "b": b_arr, "share": share})
            .sort_values(["p", "share", "b"], ascending=[True, False, True], kind="mergesort")
            .drop_duplicates("p")
        )
        board_of[best["p"].to_numpy()] = best["b"].to_numpy()
        log.info("%d units lie in overlapping board polygons — largest share used", len(best))
    within = board_of >= 0
    method[within] = WITHIN
    distance[within] = 0.0

    outside = np.flatnonzero(~within)
    if len(outside) and (max_distance_m is None or max_distance_m > 0):
        tree = shapely.STRtree(bgeoms)
        kwargs: dict[str, Any] = {"return_distance": True, "all_matches": True}
        if max_distance_m is not None:
            kwargs["max_distance"] = float(max_distance_m)
        (src, dst), dist = tree.query_nearest(points[outside], **kwargs)
        if len(src):
            near = (
                pd.DataFrame({"p": outside[src], "b": dst, "d": dist})
                .sort_values(["p", "d", "b"], kind="mergesort")
                .drop_duplicates("p")
            )
            rows = near["p"].to_numpy()
            board_of[rows] = near["b"].to_numpy()
            method[rows] = NEAREST
            distance[rows] = near["d"].to_numpy()
    return _assignment_frame(index, board_of, bcodes, method, distance)


def _assignment_frame(
    index: pd.Index, board_of: np.ndarray, bcodes: np.ndarray, method: np.ndarray, distance: np.ndarray
) -> pd.DataFrame:
    codes = np.full(len(board_of), None, dtype=object)
    ok = board_of >= 0
    codes[ok] = bcodes[board_of[ok]]
    return pd.DataFrame(
        {"water_board_code": codes, "water_board_method": method, "water_board_distance_m": distance},
        index=index,
    )


def assignment_counts(assignment: pd.DataFrame) -> dict[str, int]:
    """``{'within': n, 'nearest': n, 'none': n}`` of an :func:`assign_units` result."""
    values = assignment["water_board_method"].value_counts()
    return {m: int(values.get(m, 0)) for m in ASSIGNMENT_METHODS}


def boundary_overlap(
    unit_geoms: Sequence[BaseGeometry] | np.ndarray,
    boards: gpd.GeoDataFrame,
    unit_board_codes: Sequence[str | None] | np.ndarray,
) -> np.ndarray:
    """Share (0–1) of each unit polygon's area that lies outside its assigned board polygon.

    A diagnostic of the representative-point rule: a buurt crossed by a board boundary belongs
    to one board as a whole.  Units that intersect a single board (the vast majority) get 0;
    unassigned or zero-area units get NaN.
    """
    geoms = np.asarray(unit_geoms, dtype=object)
    codes = np.asarray(unit_board_codes, dtype=object)
    out = np.zeros(len(geoms))
    b = boards.reset_index(drop=True)
    pos = pd.Index(b["code"].astype(str)).get_indexer(pd.Series(codes, dtype=object).fillna(""))
    out[pos < 0] = np.nan
    if len(geoms) == 0:
        return out
    bgeoms = np.asarray(b.geometry.values, dtype=object)
    shapely.prepare(bgeoms)  # boards as (prepared) query geometries: fast for complex polygons
    _, unit_idx = shapely.STRtree(geoms).query(bgeoms, predicate="intersects")
    crossing = np.flatnonzero((np.bincount(unit_idx, minlength=len(geoms)) > 1) & (pos >= 0))
    if len(crossing):
        area = shapely.area(geoms[crossing])
        inside = shapely.area(shapely.intersection(geoms[crossing], bgeoms[pos[crossing]]))
        share = np.full(len(crossing), np.nan)
        np.divide(area - inside, area, out=share, where=area > 0)
        out[crossing] = np.clip(share, 0.0, 1.0)
    out[(pos >= 0) & (shapely.area(geoms) <= 0)] = np.nan
    return out


# --------------------------------------------------------------------------- board table
def summarize_water_boards(
    boards: gpd.GeoDataFrame, units: pd.DataFrame, province_codes: Sequence[str]
) -> gpd.GeoDataFrame:
    """The board table of the store: :data:`BOARD_COLUMNS` + ``geometry`` (the board polygons).

    ``units`` needs ``water_board_code``, ``province_code``, ``municipality_code``, ``population``
    and ``eligible_voters_est``.  Totals are sums over the assigned units (DERIVED); ``area_km2``
    is the area of the REAL board polygon.  ``province_code`` is the province holding most of the
    board's eligible voters (ties: population, then canonical order) — the province that will
    hold the board's (fictional) election; ``provinces`` lists every province with assigned units,
    by eligible voters (descending).  A board without units gets ``province_code`` None.
    """
    b = boards.sort_values("code", kind="mergesort").reset_index(drop=True)
    codes = b["code"].astype(str).tolist()
    order = {c: i for i, c in enumerate(province_codes)}
    u = pd.DataFrame(
        {
            "board": units["water_board_code"].to_numpy(dtype=object),
            "province": units["province_code"].astype(str).to_numpy(),
            "muni": units["municipality_code"].astype(str).to_numpy(),
            "pop": pd.to_numeric(units["population"]).to_numpy(dtype=np.int64),
            "elig": pd.to_numeric(units["eligible_voters_est"]).to_numpy(dtype=np.int64),
        }
    )
    u = u[u["board"].notna()]
    by_board = u.groupby("board").agg(
        population=("pop", "sum"), eligible=("elig", "sum"), units=("pop", "size"), munis=("muni", "nunique")
    )
    by_prov = u.groupby(["board", "province"], as_index=False)[["elig", "pop"]].sum()
    by_prov["rank"] = by_prov["province"].map(order).fillna(len(order))
    by_prov = by_prov.sort_values(
        ["board", "elig", "pop", "rank"], ascending=[True, False, False, True], kind="mergesort"
    )
    provinces = by_prov.groupby("board")["province"].agg(list)
    rows = []
    for code in codes:
        plist = provinces.get(code, [])
        stats = by_board.loc[code] if code in by_board.index else None
        rows.append(
            {
                "province_code": plist[0] if plist else None,
                "provinces": ",".join(plist),
                "population": int(stats["population"]) if stats is not None else 0,
                "eligible_voters_est": int(stats["eligible"]) if stats is not None else 0,
                "unit_count": int(stats["units"]) if stats is not None else 0,
                "municipality_count": int(stats["munis"]) if stats is not None else 0,
            }
        )
    extra = pd.DataFrame(rows)
    out = pd.DataFrame(
        {
            "code": codes,
            "name": b["name"].astype(str).to_numpy(),
            "national_code": pd.to_numeric(b["national_code"]).to_numpy(dtype=np.int64)
            if "national_code" in b.columns
            else np.arange(1, len(b) + 1, dtype=np.int64),
            **{c: extra[c].to_numpy() for c in extra.columns},
            "area_km2": shapely.area(np.asarray(b.geometry.values, dtype=object)) / 1e6,
        }
    )
    for col in ("population", "eligible_voters_est", "unit_count", "municipality_count"):
        out[col] = out[col].astype(np.int64)
    return gpd.GeoDataFrame(
        out[list(BOARD_COLUMNS)], geometry=gpd.GeoSeries(b.geometry.values, crs=b.crs), crs=b.crs
    )
