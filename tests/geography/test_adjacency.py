"""Adjacency: rook contiguity, point touches, water links, aggregation."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from app.geography.adjacency import (
    aggregate_adjacency,
    component_counts,
    compute_adjacency,
    connect_components,
)
from app.geography.synthetic import SyntheticGeography


def _gdf(rows: list[tuple[str, str, object]]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"code": [r[0] for r in rows], "province_code": [r[1] for r in rows]},
        geometry=[r[2] for r in rows],
        crs=28992,
    )


def test_rook_adjacency_excludes_point_touches() -> None:
    gdf = _gdf(
        [
            ("A", "P", box(0, 0, 1000, 1000)),
            ("B", "P", box(1000, 0, 2000, 1000)),  # shares a 1 km edge with A
            ("C", "P", box(2000, 1000, 3000, 2000)),  # touches B only at a corner
            ("D", "P", box(1000.5, 1000.5, 2000, 2000)),  # 0.5 m gap to A/B: snapped neighbour of B
        ]
    )
    adj = compute_adjacency(gdf, "code", snap_buffer_m=2.0, min_shared_border_m=20.0)
    pairs = set(zip(adj["a"], adj["b"], strict=True))
    assert ("A", "B") in pairs
    assert ("B", "C") not in pairs  # point touch excluded
    assert ("B", "D") in pairs and ("C", "D") in pairs
    ab = adj[(adj["a"] == "A") & (adj["b"] == "B")]["shared_border_m"].iloc[0]
    assert ab == pytest.approx(1000.0, abs=5.0)
    assert (adj["kind"] == "border").all() and (adj["a"] < adj["b"]).all()


def test_matches_synthetic_grid(synthetic: SyntheticGeography) -> None:
    units = synthetic.units[synthetic.units["province_code"].isin(["GR", "FR"])]
    adj = compute_adjacency(units, "code", 2.0, 20.0)
    ref = synthetic.unit_adjacency
    ref = ref[ref["a"].isin(set(units["code"])) & ref["b"].isin(set(units["code"]))]
    assert set(zip(adj["a"], adj["b"], strict=True)) == set(zip(ref["a"], ref["b"], strict=True))
    assert adj["shared_border_m"].between(3990, 4010).all()


def test_island_joined_by_water_link() -> None:
    gdf = _gdf(
        [
            ("A", "P", box(0, 0, 1000, 1000)),
            ("B", "P", box(1000, 0, 2000, 1000)),
            ("I", "P", box(0, 3500, 500, 4000)),  # island 2.5 km north of A
            ("J", "P", box(600, 3500, 1000, 4000)),  # second island 100 m east of I
            ("X", "Q", box(0, 1200, 1000, 2000)),  # other province, closer — must not be used
        ]
    )
    adj = compute_adjacency(gdf, "code", 2.0, 20.0)
    assert component_counts(adj, gdf, "province_code")["P"] == 3
    linked = connect_components(adj, gdf, "province_code")
    assert component_counts(linked, gdf, "province_code") == {"P": 1, "Q": 1}
    links = linked[linked["kind"] == "water_link"]
    assert set(zip(links["a"], links["b"], strict=True)) == {("I", "J"), ("A", "I")}
    gaps = dict(zip(zip(links["a"], links["b"], strict=True), links["gap_m"], strict=True))
    assert gaps[("I", "J")] == pytest.approx(100.0)
    assert gaps[("A", "I")] == pytest.approx(2500.0)
    assert "X" not in set(links["a"]) | set(links["b"])
    # idempotent: a connected graph gets no further links
    assert connect_components(linked, gdf, "province_code").equals(linked)


def test_short_gap_link_is_labelled_border() -> None:
    gdf = _gdf([("A", "P", box(0, 0, 1000, 1000)), ("B", "P", box(1000, 999.99, 2000, 2000))])
    # corner touch only: dropped as adjacency, restored as a (short) border link
    adj = compute_adjacency(gdf, "code", 2.0, 20.0)
    assert adj.empty
    linked = connect_components(adj, gdf, "province_code", touch_tolerance_m=2.0)
    assert list(linked["kind"]) == ["border"]


def test_aggregate_to_municipalities() -> None:
    unit_adj = pd.DataFrame(
        {
            "a": ["u1", "u1", "u2", "u3"],
            "b": ["u2", "u3", "u4", "u5"],
            "shared_border_m": [100.0, 50.0, 25.0, 0.0],
            "kind": ["border", "border", "border", "water_link"],
            "gap_m": [0.0, 0.0, 0.0, 800.0],
        }
    )
    mapping = {"u1": "M1", "u2": "M1", "u3": "M2", "u4": "M2", "u5": "M3"}
    agg = aggregate_adjacency(unit_adj, mapping)
    rows = {(r.a, r.b): (r.shared_border_m, r.kind) for r in agg.itertuples()}
    assert rows == {("M1", "M2"): (75.0, "border"), ("M2", "M3"): (0.0, "water_link")}


# --------------------------------------------------------------------------- review regressions
def test_enclave_and_exclave_adjacency() -> None:
    from shapely.geometry import MultiPolygon, Polygon

    outer = Polygon(
        [(0, 0), (3000, 0), (3000, 3000), (0, 3000)],
        holes=[[(1000, 1000), (2000, 1000), (2000, 2000), (1000, 2000)]],
    )
    exclave_owner = MultiPolygon([box(3000, 0, 4000, 1000), box(6000, 0, 7000, 1000)])  # 2nd part far east
    gdf = _gdf(
        [
            ("A", "P", outer),
            ("E", "P", box(1000, 1000, 2000, 2000)),  # enclave filling A's hole
            ("M", "P", exclave_owner),
            ("N", "P", box(7000, 0, 8000, 1000)),  # touches only M's exclave
        ]
    )
    adj = compute_adjacency(gdf, "code", 2.0, 20.0)
    rows = {(r.a, r.b): r.shared_border_m for r in adj.itertuples()}
    assert rows[("A", "E")] == pytest.approx(4000.0, abs=10.0)  # the hole's perimeter
    assert rows[("A", "M")] == pytest.approx(1000.0, abs=10.0)
    assert rows[("M", "N")] == pytest.approx(1000.0, abs=10.0)  # via the exclave
    assert component_counts(adj, gdf, "province_code") == {"P": 1}


def test_adjacency_and_links_independent_of_row_order(synthetic: SyntheticGeography) -> None:
    units = synthetic.units[synthetic.units["province_code"].isin(["GR", "FR"])].copy()
    # island 1 straddles the border of two mainland cells (4 km wide): both are exactly 1 km
    # away, so the link choice is a genuine tie that must not depend on row order
    gr = units[units["province_code"] == "GR"].total_bounds
    extra = _gdf(
        [
            ("BU01999901", "GR", box(gr[0] + 3750, gr[3] + 1000, gr[0] + 4250, gr[3] + 1500)),
            ("BU01999902", "GR", box(gr[2] - 500, gr[3] + 1000, gr[2], gr[3] + 1500)),
        ]
    )
    units = pd.concat([units[["code", "province_code", "geometry"]], extra], ignore_index=True)
    units = gpd.GeoDataFrame(units, geometry="geometry", crs=28992)
    results = []
    for seed in (0, 1, 2):
        shuffled = units.sample(frac=1.0, random_state=seed).reset_index(drop=True)
        adj = compute_adjacency(shuffled, "code", 2.0, 20.0)
        results.append(connect_components(adj, shuffled, "province_code"))
    assert results[0].equals(results[1]) and results[0].equals(results[2])
    links = results[0][results[0]["kind"] == "water_link"]
    assert len(links) == 2 and (links["gap_m"] == 1000.0).all()
    # tie broken by the smallest code pair: cell (0, 0) = BU01000000 rather than (0, 1)
    assert ("BU01000000", "BU01999901") in set(zip(links["a"], links["b"], strict=True))
    assert component_counts(results[0], units, "province_code") == {"FR": 1, "GR": 1}


def test_compute_adjacency_rejects_duplicate_codes() -> None:
    gdf = _gdf([("A", "P", box(0, 0, 1, 1)), ("A", "P", box(1, 0, 2, 1))])
    with pytest.raises(ValueError):
        compute_adjacency(gdf, "code", 2.0, 20.0)
    assert compute_adjacency(gdf.iloc[:0], "code", 2.0, 20.0).empty
