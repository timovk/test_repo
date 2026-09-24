"""District statistics, geometries, adjacency, fragments and names."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest
import shapely

from app.districts.config import NamingConfig
from app.districts.naming import compass_label, name_districts, roman
from app.districts.stats import (
    compactness,
    district_adjacency,
    district_geometries,
    district_municipality_fragments,
    district_stats,
    geometries_wkb,
    write_districts_geojson,
)


@pytest.fixture(scope="module")
def geoms(fine_plan, fine_geo):  # type: ignore[no-untyped-def]
    return district_geometries(fine_plan, fine_geo.units)


@pytest.fixture(scope="module")
def stats(fine_plan, fine_geo, geoms):  # type: ignore[no-untyped-def]
    return district_stats(fine_plan, fine_geo.units, geoms)


def test_geometries_cover_units(fine_plan, fine_geo, geoms) -> None:  # type: ignore[no-untyped-def]
    assert len(geoms) == fine_plan.n_districts
    assert list(geoms["code"]) == fine_plan.district_codes
    assert shapely.is_valid(np.asarray(geoms.geometry.values)).all()
    assert math.isclose(geoms.geometry.area.sum(), fine_geo.units.geometry.area.sum(), rel_tol=1e-9)


def test_stats_columns_and_values(fine_plan, fine_geo, stats) -> None:  # type: ignore[no-untyped-def]
    assert len(stats) == fine_plan.n_districts
    assert stats["population"].sum() == fine_geo.units["population"].sum()
    assert stats["eligible_voters_est"].sum() == fine_geo.units["eligible_voters_est"].sum()
    assert stats["n_units"].sum() == len(fine_geo.units)
    assert np.allclose(stats["deviation_pct"], fine_plan.deviation_pct())
    assert stats["area_km2"].sum() == pytest.approx(fine_geo.units["land_area_km2"].sum())
    for col in ("polsby_popper", "reock", "convex_hull_ratio", "urban_share", "rural_share"):
        assert ((stats[col] >= 0) & (stats[col] <= 1 + 1e-9)).all(), col
    assert stats["is_contiguous"].all() and (stats["n_components"] == 1).all()
    assert (stats["n_split_municipalities"] <= stats["n_municipalities"]).all()
    assert stats["centroid_lon"].between(0, 10).all() and stats["centroid_lat"].between(49, 55).all()
    # districts containing the island are contiguous thanks to the water link but have 2 polygons
    island = fine_plan.unit_district[fine_plan.unit_index(fine_geo.island_units[0])]
    assert stats.loc[island, "is_contiguous"]


def test_compactness_of_simple_shapes() -> None:
    import geopandas as gpd

    square = shapely.box(0, 0, 1000, 1000)
    strip = shapely.box(0, 0, 10_000, 100)
    df = compactness(gpd.GeoDataFrame(geometry=[square, strip], crs="EPSG:28992"))
    assert df.loc[0, "polsby_popper"] == pytest.approx(math.pi / 4)
    assert df.loc[0, "reock"] == pytest.approx(2 / math.pi, rel=1e-3)
    assert df.loc[0, "convex_hull_ratio"] == pytest.approx(1.0)
    assert df.loc[1, "polsby_popper"] < 0.05


def test_stats_without_geometry(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    df = district_stats(fine_plan, pd.DataFrame(fine_geo.units.drop(columns="geometry")))
    assert df["polsby_popper"].isna().all()
    assert df["centroid_lon"].notna().all()


def test_wkb_and_geojson(fine_plan, geoms, stats, tmp_path) -> None:  # type: ignore[no-untyped-def]
    wkb = geometries_wkb(geoms, tolerance_m=200)
    assert set(wkb) == set(fine_plan.district_codes)
    g = shapely.from_wkb(wkb[fine_plan.district_codes[0]])
    assert g.is_valid and -1 < g.centroid.x < 12 and 45 < g.centroid.y < 56  # EPSG:4326
    path = write_districts_geojson(geoms, tmp_path / "d.geojson", tolerance_m=200, properties=stats)
    doc = json.loads(path.read_text())
    assert len(doc["features"]) == fine_plan.n_districts
    props = doc["features"][0]["properties"]
    assert {"code", "name", "province_code", "population", "deviation_pct"} <= set(props)
    coords = doc["features"][0]["geometry"]["coordinates"]
    flat = np.asarray(
        shapely.get_coordinates(shapely.from_geojson(json.dumps(doc["features"][0]["geometry"])))
    )
    assert coords and np.allclose(flat, np.round(flat, 5))


def test_district_adjacency(fine_plan) -> None:  # type: ignore[no-untyped-def]
    adj = district_adjacency(fine_plan)
    assert (adj["district_a"] < adj["district_b"]).all()
    assert (adj["shared_border_km"] >= 0).all()
    # every district touches at least one other district
    touched = set(adj["district_a"]) | set(adj["district_b"])
    assert touched == set(fine_plan.district_codes)
    # cross-province adjacencies exist (provinces share borders in the fine geography)
    assert (adj["district_a"].str[:2] != adj["district_b"].str[:2]).any()
    assert not adj.duplicated(["district_a", "district_b"]).any()


def test_fragments(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    fr = district_municipality_fragments(fine_plan)
    assert fr["population"].sum() == fine_geo.units["population"].sum()
    assert fr.groupby("municipality_code")["share_of_municipality"].sum().round(9).eq(1).all()
    assert fr.groupby("district_code")["share_of_district"].sum().round(9).eq(1).all()
    split = fr.groupby("municipality_code").size()
    assert sorted(split.index[split > 1]) == fine_plan.split_municipalities()


def test_names(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    names = fine_plan.district_names
    assert len(set(names)) == len(names)
    city = fine_geo.city["ZH"]
    city_name = f"Gemeente {city[2:]}"
    city_districts = [
        n
        for n, p in zip(names, fine_plan.district_province, strict=True)
        if p == "ZH" and n.startswith(city_name)
    ]
    assert len(city_districts) >= 3
    assert all("-" in n for n in city_districts)


def test_naming_rules() -> None:
    # two districts in one municipality (north / south halves) + one mixed district
    d = np.array([0, 0, 1, 1, 2, 2, 2, 2])
    m = np.array(["GM1", "GM1", "GM1", "GM1", "GM2", "GM3", "GM3", "GM4"], dtype=object)
    pop = np.array([10, 10, 10, 10, 20, 9, 9, 12])
    xy = np.array([[0, 10], [1, 10], [0, 0], [1, 0], [5, 5], [6, 5], [7, 5], [8, 5]], dtype=float)
    display = {"GM1": "Stad", "GM2": "Dorp", "GM3": "Gehucht", "GM4": "Buurtschap"}
    cfg = NamingConfig(regions={"Regio": ["GM2", "GM3"], "Grote Regio": ["GM2", "GM3", "GM4"]})
    names = name_districts(d, m, pop, xy, 3, display, cfg)
    assert names[:2] == ["Stad-Noord", "Stad-Zuid"]
    assert names[2] == "Regio – Dorp"  # the most specific region with >= 60 % wins
    names2 = name_districts(d, m, pop, xy, 3, display, NamingConfig())
    assert names2[2] == "Dorp – Gehucht"
    names3 = name_districts(d, m, pop, xy, 3, display, NamingConfig(second_min_share=0.5))
    assert names3[2] == "Dorp e.o."
    assert compass_label(1, 1) == "Noordoost" and compass_label(0, -1, eight=False) == "Zuid"
    assert roman(4) == "IV" and roman(9) == "IX"


def test_supplied_geometries_are_matched_by_code(fine_plan, fine_geo, geoms, stats) -> None:  # type: ignore[no-untyped-def]
    """Geometries in another order (e.g. sorted by name or read back from GeoJSON) must not be
    silently attached to the wrong districts."""
    from app.core.errors import DistrictingError

    shuffled = geoms.sample(frac=1.0, random_state=3)
    assert list(shuffled["code"]) != list(geoms["code"])
    again = district_stats(fine_plan, fine_geo.units, shuffled)
    pd.testing.assert_frame_equal(again, stats)
    with pytest.raises(DistrictingError, match="lack"):
        district_stats(fine_plan, fine_geo.units, geoms.iloc[1:])
    with pytest.raises(DistrictingError, match="no 'code' column"):
        district_stats(fine_plan, fine_geo.units, geoms.drop(columns="code").iloc[1:])


def test_second_municipality_keeps_its_compass_label() -> None:
    # GM1 is split north / south; district 2 = mostly GM2 plus the southern part of GM1 is named
    # with the part's label so it cannot be confused with the district holding GM1's north.
    d = np.array([0, 0, 0, 1, 1, 1, 1])
    m = np.array(["GM1", "GM1", "GM2", "GM1", "GM2", "GM2", "GM2"], dtype=object)
    pop = np.array([40, 40, 30, 30, 40, 20, 20])
    xy = np.array([[0, 10], [1, 10], [3, 10], [0, 0], [3, 0], [4, 0], [5, 0]], dtype=float)
    names = name_districts(d, m, pop, xy, 2, {"GM1": "Stad", "GM2": "Dorp"}, NamingConfig())
    assert names[0] == "Stad-Noord – Dorp-Noord"
    assert names[1] == "Dorp-Zuid – Stad-Zuid"


def test_many_part_municipality_labels_are_balanced() -> None:
    # 12 districts around one city: 9 labels (8 directions + Centrum) are each used at most twice
    from collections import Counter

    k = 12
    ang = np.arange(k) * 2 * np.pi / k
    xy = np.column_stack([np.cos(ang), np.sin(ang)]) * 1000.0
    xy[0] = [0.0, 0.0]  # one part in the centre
    names = name_districts(
        np.arange(k),
        np.array(["GM1"] * k, dtype=object),
        np.full(k, 100),
        xy,
        k,
        {"GM1": "Stad"},
        NamingConfig(),
    )
    assert len(set(names)) == k
    base = Counter(n.split(" ")[0] for n in names)
    assert max(base.values()) <= 2 and "Stad-Centrum" in base
    assert names[0] == "Stad-Centrum"


def test_dedupe_names_never_collides() -> None:
    from app.districts.naming import dedupe_names

    out = dedupe_names(["A", "A", "A I", "B", "B", "B II"])
    assert len(set(out)) == len(out)
    assert out == ["A II", "A III", "A I", "B I", "B III", "B II"]


def test_city_parts_are_named_from_the_dense_core() -> None:
    """With unit densities, a city split into ≥ 4 parts is named from its dense core: the part
    holding the core is "-Centrum" even when the population centroid lies elsewhere."""
    # a dense core at the origin (district 0) and three big low-density suburbs east / north-east /
    # south-east of it: the population centroid lies ~5 km east of the core
    xy = np.array(
        [[0, 0], [1000, 0], [8000, 0], [9000, 0], [6000, 6000], [6500, 6500], [6000, -6000], [6500, -6500]],
        dtype=float,
    )
    d = np.array([0, 0, 1, 1, 2, 2, 3, 3])
    m = np.array(["GM1"] * 8, dtype=object)
    pop = np.array([30, 30, 40, 40, 40, 40, 40, 40])
    dens = np.array([9000, 8000, 900, 900, 900, 900, 900, 900], dtype=float)
    cfg = NamingConfig()
    plain = name_districts(d, m, pop, xy, 4, {"GM1": "Stad"}, cfg)
    cored = name_districts(d, m, pop, xy, 4, {"GM1": "Stad"}, cfg, unit_density=dens)
    assert plain[0] == "Stad-West"  # measured from the population centroid
    assert cored == ["Stad-Centrum", "Stad-Oost", "Stad-Noordoost", "Stad-Zuidoost"]
    # two parts are always named by opposite directions (population centroid), density or not
    two = name_districts(
        np.array([0, 0, 1, 1]), m[:4], pop[:4], xy[:4], 2, {"GM1": "Stad"}, cfg, unit_density=dens[:4]
    )
    assert two == ["Stad-West", "Stad-Oost"]
