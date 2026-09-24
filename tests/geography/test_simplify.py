"""Coverage simplification keeps shared borders; web GeoJSON output format."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import Polygon, box

from app.geography.simplify import coverage_simplify, write_web_geojson
from app.geography.topology import dissolve_coverage, repair_coverage


def _jagged_pair() -> gpd.GeoDataFrame:
    """Two squares sharing a wiggly border with many small zig-zags."""
    xs = np.linspace(0, 10_000, 401)
    wiggle = 5_000 + 15.0 * np.sin(xs / 40.0)
    border = list(zip(xs, wiggle, strict=True))
    south = Polygon([(0, 0), (10_000, 0), *reversed(border)])
    north = Polygon([*border, (10_000, 10_000), (0, 10_000)])
    shifted = [shapely.transform(g, lambda xy: xy + np.array([150_000.0, 450_000.0])) for g in (south, north)]
    return gpd.GeoDataFrame({"code": ["S", "N"], "name": ["south", "north"]}, geometry=shifted, crs=28992)


def test_coverage_simplify_keeps_shared_border() -> None:
    gdf = _jagged_pair()
    out = coverage_simplify(gdf, 50.0)
    before = shapely.get_num_coordinates(gdf.geometry.values).sum()
    after = shapely.get_num_coordinates(out.geometry.values).sum()
    assert after < before / 4
    s, n = out.geometry.iloc[0], out.geometry.iloc[1]
    assert shapely.is_valid(s) and shapely.is_valid(n)
    assert s.intersection(n).area < 1e-6  # no overlap
    assert abs(s.union(n).area - 100_000_000) < 1e-3  # no gap
    shared = s.boundary.intersection(n.boundary).length
    assert shared > 9_900  # the simplified border is still identical on both sides
    assert shapely.coverage_is_valid(out.geometry.values)
    # tolerance 0 → untouched copy
    same = coverage_simplify(gdf, 0.0)
    assert same.geometry.iloc[0].equals(gdf.geometry.iloc[0])


def test_write_web_geojson(tmp_path: Path) -> None:
    gdf = _jagged_pair()
    gdf["province_code"] = "UT"
    gdf["population"] = np.array([10, 20], dtype=np.int64)
    path = write_web_geojson(
        gdf, tmp_path / "web" / "x.geojson", ("code", "name", "province_code", "population"), 50.0
    )
    doc = json.loads(path.read_text())
    assert doc["type"] == "FeatureCollection" and len(doc["features"]) == 2
    props = doc["features"][1]["properties"]
    assert props == {"code": "N", "name": "north", "province_code": "UT", "population": 20}
    coords = np.array(doc["features"][0]["geometry"]["coordinates"][0], dtype=float)
    assert np.allclose(coords, np.round(coords, 5), atol=0)
    assert 3.0 < coords[:, 0].min() < 7.5 and 50.0 < coords[:, 1].min() < 54.0  # lon/lat of RD coordinates
    # shared border vertices are identical after rounding
    a = {tuple(c) for c in doc["features"][0]["geometry"]["coordinates"][0]}
    b = {tuple(c) for c in doc["features"][1]["geometry"]["coordinates"][0]}
    assert len(a & b) >= 2


def test_repair_and_dissolve_coverage() -> None:
    # B overlaps A by a sliver of 0.001 m and has an extra vertex on the shared edge
    a = box(0, 0, 100, 100)
    b = Polygon([(99.999, 0), (200, 0), (200, 100), (100, 100), (99.999, 50)])
    geoms = np.array([a, b, box(0, 100, 200, 200)], dtype=object)
    assert not shapely.coverage_is_valid(geoms)
    fixed, touched = repair_coverage(geoms, snap_tolerance_m=0.05)
    assert touched and shapely.coverage_is_valid(fixed)
    assert abs(sum(g.area for g in fixed) - 40_000) < 0.5
    keys, merged = dissolve_coverage(fixed, np.array(["X", "X", "Y"]))
    assert list(keys) == ["X", "Y"]
    assert abs(merged[0].area - 20_000) < 0.5
    assert shapely.is_valid(merged[0])


# --------------------------------------------------------------------------- review regressions
def test_round_coordinates_returns_valid_geometries() -> None:
    """Plain 5-decimal rounding turns these into invalid polygons (a ring touching itself, a
    collapsed sliver part); the precision-aware rounding keeps them valid and non-empty."""
    from app.geography.simplify import round_coordinates

    pinched = Polygon([(5.0, 52.0), (5.001, 52.0), (5.001, 52.001), (5.0005, 52.000004), (5.0, 52.001)])
    sliver = shapely.MultiPolygon(
        [box(5.0, 52.0, 5.001, 52.001), Polygon([(5.002, 52.0), (5.003, 52.0), (5.0025, 52.000004)])]
    )
    geoms = np.array([pinched, sliver, box(5.0, 52.0, 5.001, 52.001), None], dtype=object)
    naive = shapely.transform(geoms[:2], lambda xy: np.round(xy, 5))
    assert not shapely.is_valid(naive).any()  # the failure mode seen on the real data
    out = round_coordinates(geoms, 5)
    assert shapely.is_valid(out[:3]).all() and not shapely.is_empty(out[:3]).any() and out[3] is None
    coords = shapely.get_coordinates(out[:3])
    assert np.array_equal(coords, np.round(coords, 5))
    assert abs(out[1].area - box(5.0, 52.0, 5.001, 52.001).area) < 1e-9  # the collapsed part is dropped


def test_repair_coverage_leaves_valid_coverage_untouched() -> None:
    geoms = np.array([box(0, 0, 10, 10), box(10, 0, 20, 10)], dtype=object)
    fixed, touched = repair_coverage(geoms)
    assert touched == [] and all(a.equals(b) for a, b in zip(fixed, geoms, strict=True))
    empty, none = repair_coverage(np.array([], dtype=object))
    assert len(empty) == 0 and none == []
