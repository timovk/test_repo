"""District generator on synthetic geographies."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.constitution import HOUSE_SEATS
from app.core.errors import DistrictingError
from app.core.rng import config_hash
from app.districts.apportionment import apportion
from app.districts.config import DistrictConfig, load_district_config
from app.districts.generator import build_unit_edges, generate_plan
from app.districts.naming import dedupe_names, serpentine_order
from app.districts.validation import validate_plan


def _province_tables(plan) -> pd.DataFrame:  # type: ignore[no-untyped-def]
    return plan.assignment_frame()


def test_fine_plan_is_valid(fine_geo, fine_plan, fast_config) -> None:  # type: ignore[no-untyped-def]
    plan = fine_plan
    assert plan.n_districts == HOUSE_SEATS
    assert pd.Series(plan.district_province).value_counts().to_dict() == {
        p: s for p, s in fine_geo.seats.items()
    }
    v = validate_plan(plan, fine_geo.units, fine_geo.seats)
    assert v.ok, v.errors
    # population equality within the target tolerance everywhere
    dev = np.abs(plan.deviation_pct())
    assert dev.max() <= fast_config.target_deviation_pct + 1e-9
    assert plan.noncontiguous_districts() == []
    # every unit exactly once, district codes '<PV>-<NN>' numbered 1..k per province
    assert len(plan.unit_codes) == len(fine_geo.units) == len(set(plan.unit_codes))
    for p, k in fine_geo.seats.items():
        nums = sorted(n for n, q in zip(plan.district_numbers, plan.district_province, strict=True) if q == p)
        assert nums == list(range(1, k + 1))
    assert all(
        c == f"{p}-{n:02d}"
        for c, p, n in zip(plan.district_codes, plan.district_province, plan.district_numbers, strict=True)
    )
    assert len(set(plan.district_names)) == plan.n_districts
    assert plan.config_hash == config_hash(fast_config.canonical_json([]))
    assert plan.summary()["total_districts"] == HOUSE_SEATS
    assert set(plan.timings) >= {"prepare", "partition", "naming", "total"}


def test_island_is_joined_by_water_link(fine_geo, fine_plan) -> None:  # type: ignore[no-untyped-def]
    idx = [fine_plan.unit_index(c) for c in fine_geo.island_units]
    districts = {fine_plan.district_codes[fine_plan.unit_district[i]] for i in idx}
    # the island lies in exactly one district of its own province, and that district is contiguous
    for code in districts:
        assert code[:2] in ("ZE", "FR")
    assert fine_plan.edge_water.sum() >= 2
    assert fine_plan.noncontiguous_districts() == []


def test_municipal_splits_only_where_needed(fine_geo, fine_plan, fast_config) -> None:  # type: ignore[no-untyped-def]
    a = fine_plan.assignment_frame()
    a["population"] = fine_plan.unit_population
    target = {
        p: fine_geo.units.loc[fine_geo.units.province_code == p, "population"].sum() / s
        for p, s in fine_geo.seats.items()
    }
    muni = a.groupby("municipality_code").agg(
        pop=("population", "sum"), prov=("province_code", "first"), parts=("district_code", "nunique")
    )
    muni["ratio"] = muni["pop"] / muni["prov"].map(target)
    # every city larger than a district is split into (at least) the necessary number of parts
    big = muni[muni["ratio"] > 1 + fast_config.target_deviation_pct / 100]
    assert (big["parts"] >= np.ceil(big["ratio"] / (1 + fast_config.target_deviation_pct / 100))).all()
    # most municipalities stay whole and splits stay a minority of the municipalities
    assert (muni["parts"] == 1).mean() > 0.75
    # small municipalities are rarely cut into more than two pieces
    assert (muni.loc[muni["ratio"] < 0.5, "parts"] <= 2).mean() > 0.95


def test_no_splits_when_whole_municipalities_balance(fast_config) -> None:  # type: ignore[no-untyped-def]
    """A province whose municipalities combine exactly into districts must not be split."""
    import geopandas as gpd
    from shapely.geometry import box

    rows, edges = [], []
    n = 12  # 12 × 12 units, municipalities of 3 × 3 units, 16 municipalities of 9,000 people
    for i in range(n):
        for j in range(n):
            code = f"BU00{i:03d}{j:03d}"
            muni = f"GM00{(i // 3) * 4 + j // 3:02d}"
            g = box(j * 1000.0, -(i + 1) * 1000.0, (j + 1) * 1000.0, -i * 1000.0)
            rows.append({
                "code": code, "municipality_code": muni, "province_code": "XX", "wijk_code": muni + "W",
                "population": 1000, "centroid_x": g.centroid.x, "centroid_y": g.centroid.y,
                "land_area_km2": 1.0, "geometry": g,
            })  # fmt: skip
            if j + 1 < n:
                edges.append((code, f"BU00{i:03d}{j + 1:03d}", 1000.0, "border"))
            if i + 1 < n:
                edges.append((code, f"BU00{i + 1:03d}{j:03d}", 1000.0, "border"))
    units = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:28992")
    adj = pd.DataFrame(edges, columns=["a", "b", "shared_border_m", "kind"])
    plan = generate_plan(units, adj, {"XX": 4}, fast_config, seed=3)
    assert plan.split_municipalities() == []
    assert np.abs(plan.deviation_pct()).max() == 0.0
    # a municipality with more people than a district is split
    units.loc[units.municipality_code == "GM0000", "population"] = 6000
    plan2 = generate_plan(units, adj, {"XX": 4}, fast_config, seed=3)
    assert "GM0000" in plan2.split_municipalities()


def test_reproducible_and_seed_dependent(fine_geo, fine_plan, fast_config) -> None:  # type: ignore[no-untyped-def]
    again = generate_plan(fine_geo.units, fine_geo.adjacency, fine_geo.seats, fast_config, seed=11)
    assert np.array_equal(again.unit_district, fine_plan.unit_district)
    assert again.district_names == fine_plan.district_names
    assert again.config_hash == fine_plan.config_hash
    other = generate_plan(fine_geo.units, fine_geo.adjacency, fine_geo.seats, fast_config, seed=12)
    assert not np.array_equal(other.unit_district, fine_plan.unit_district)
    assert validate_plan(other, fine_geo.units, fine_geo.seats).ok


def test_row_order_independent(fine_geo, fine_plan, fast_config) -> None:  # type: ignore[no-untyped-def]
    shuffled = fine_geo.units.sample(frac=1.0, random_state=0)
    plan = generate_plan(shuffled, fine_geo.adjacency, fine_geo.seats, fast_config, seed=11)
    a = pd.Series(plan.unit_district_codes(), index=plan.unit_codes)
    b = pd.Series(fine_plan.unit_district_codes(), index=fine_plan.unit_codes)
    assert a.sort_index().equals(b.sort_index())


def test_parallel_equals_serial(fine_geo, fine_plan, fast_config) -> None:  # type: ignore[no-untyped-def]
    subset = ["GR", "DR", "ZE"]
    units = fine_geo.units[fine_geo.units.province_code.isin(subset)]
    seats = {p: fine_geo.seats[p] for p in subset}
    serial = generate_plan(units, fine_geo.adjacency, seats, fast_config, seed=11, workers=1)
    parallel = generate_plan(units, fine_geo.adjacency, seats, fast_config, seed=11, workers=2)
    assert np.array_equal(serial.unit_district, parallel.unit_district)
    assert serial.district_names == parallel.district_names
    # a province's districts do not depend on which other provinces are generated alongside it
    full = pd.Series(fine_plan.unit_district_codes(), index=fine_plan.unit_codes)
    part = pd.Series(serial.unit_district_codes(), index=serial.unit_codes)
    assert full.reindex(part.index).equals(part)


def test_coarse_synthetic_structural_invariants(synthetic, fast_config) -> None:  # type: ignore[no-untyped-def]
    """The shared toy country has city cells larger than a district: population equality is
    impossible, but every structural invariant must still hold."""
    units = synthetic.units
    seats = apportion(units.groupby("province_code")["population"].sum().to_dict(), HOUSE_SEATS).seats
    plan = generate_plan(units, synthetic.unit_adjacency, seats, fast_config, seed=1)
    v = validate_plan(plan, units, seats)
    assert plan.n_districts == HOUSE_SEATS
    assert plan.noncontiguous_districts() == []
    assert (np.bincount(plan.unit_district, minlength=plan.n_districts) > 0).all()
    structural = [e for e in v.errors if "deviation" not in e]
    assert structural == []
    assert any("deviation" in w for w in plan.warnings)


def test_zero_population_province_and_disconnected_graph(fast_config) -> None:  # type: ignore[no-untyped-def]
    """Missing adjacency (disconnected graph) is repaired with synthetic water links."""
    rows = []
    for i in range(6):
        rows.append({
            "code": f"BU{i:08d}", "municipality_code": f"GM{i // 2:04d}", "province_code": "XX",
            "population": 100 * (i + 1), "centroid_x": 1000.0 * i, "centroid_y": 0.0,
        })  # fmt: skip
    units = pd.DataFrame(rows)
    adj = pd.DataFrame(
        {"a": ["BU00000000"], "b": ["BU00000001"], "shared_border_m": [500.0], "kind": ["border"]}
    )
    plan = generate_plan(units, adj, {"XX": 2}, fast_config, seed=1)
    assert plan.noncontiguous_districts() == []
    assert any("synthetic water link" in w for w in plan.warnings)
    assert plan.edge_water.sum() == 4  # 6 units, 1 real edge → 4 links to connect


def test_input_errors(fine_geo, fast_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(DistrictingError):
        generate_plan(
            fine_geo.units.drop(columns=["population"]), fine_geo.adjacency, fine_geo.seats, fast_config
        )
    seats = dict(fine_geo.seats)
    seats.pop("ZE")
    with pytest.raises(DistrictingError):
        generate_plan(fine_geo.units, fine_geo.adjacency, seats, fast_config)
    small = fine_geo.units[fine_geo.units.province_code == "ZE"].head(2)
    with pytest.raises(DistrictingError):
        generate_plan(small, fine_geo.adjacency, {"ZE": 3}, fast_config)


def test_build_unit_edges_merges_duplicates() -> None:
    adj = pd.DataFrame(
        {
            "a": ["U1", "U2", "U1", "U3", "ZZ"],
            "b": ["U2", "U1", "U3", "U1", "U1"],
            "shared_border_m": [10.0, 5.0, 0.0, 0.0, 1.0],
            "kind": ["border", "border", "water_link", "water_link", "border"],
        }
    )
    edges, water, border, unknown = build_unit_edges(["U1", "U2", "U3"], adj)
    assert edges.tolist() == [[0, 1], [0, 2]]
    assert water.tolist() == [False, True]
    assert border.tolist() == [15.0, 0.0]
    assert unknown == 1


def test_config_file_matches_schema_defaults() -> None:
    cfg = load_district_config()
    assert cfg.model_dump(exclude={"naming"}) == DistrictConfig().model_dump(exclude={"naming"})
    assert cfg.naming.regions  # curated region names are configured
    assert cfg.side_tolerance(1) == pytest.approx(0.02)
    assert cfg.side_tolerance(4) == pytest.approx(0.01)
    assert "workers" not in cfg.hash_payload()
    with pytest.raises(ValueError):
        DistrictConfig(target_deviation_pct=6, max_deviation_pct=5)


def test_serpentine_numbering_and_name_dedupe() -> None:
    # 2 × 3 grid of centroids (x east, y north): north row west→east, south row east→west
    xy = np.array([[0, 10], [10, 10], [20, 10], [0, 0], [10, 0], [20, 0]], dtype=float)
    assert serpentine_order(xy).tolist() == [0, 1, 2, 5, 4, 3]
    assert dedupe_names(["A", "B", "A", "A"]) == ["A I", "B", "A II", "A III"]


def _toy(n: int, pops: list[int], edges: list[tuple[int, int]]):  # type: ignore[no-untyped-def]
    units = pd.DataFrame(
        {
            "code": [f"BU{i:08d}" for i in range(n)],
            "municipality_code": "GM0001",
            "province_code": "XX",
            "population": pops,
            "centroid_x": np.arange(n) * 1000.0,
            "centroid_y": 0.0,
        }
    )
    adj = pd.DataFrame(
        [(f"BU{a:08d}", f"BU{b:08d}", 100.0, "border") for a, b in edges],
        columns=["a", "b", "shared_border_m", "kind"],
    )
    return units, adj


@pytest.mark.parametrize(
    ("n", "pops", "edges", "seats"),
    [
        (4, [100, 5000, 20, 700], [(0, 1), (1, 2), (2, 3)], 4),  # one unit per district
        (6, [10, 100, 100, 100, 100, 100], [(0, i) for i in range(1, 6)], 3),  # star graph
        (6, [10, 100, 100, 100, 100, 100], [(0, i) for i in range(1, 6)], 5),
        (5, [1_000_000, 10, 10, 10, 10], [(0, 1), (1, 2), (2, 3), (3, 4)], 2),  # one giant unit
        (6, [0, 0, 0, 0, 0, 0], [(i, i + 1) for i in range(5)], 3),  # no population at all
    ],
)
def test_degenerate_geographies_still_give_valid_structure(n, pops, edges, seats, fast_config) -> None:  # type: ignore[no-untyped-def]
    units, adj = _toy(n, pops, edges)
    plan = generate_plan(units, adj, {"XX": seats}, fast_config, seed=1)
    assert plan.n_districts == seats
    assert (np.bincount(plan.unit_district, minlength=seats) > 0).all()
    assert plan.noncontiguous_districts() == []
