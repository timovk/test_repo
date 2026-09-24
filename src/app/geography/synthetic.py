"""Synthetic toy country with the *same schema* as the processed CBS store.

Used by unit tests (no network, no 220 MB download) and for quick experiments.  The toy
country uses the real province codes/names so constitutional logic is exercised with the
canonical 12 provinces, but every geometry and number here is synthetic.

Layout: provinces are a 4 × 3 grid of squares; each province is a grid of square units
(``cells`` × ``cells``); municipalities are rectangular blocks of units.  The first
municipality of each province is a dense "city" large enough to require splitting.
"""

from __future__ import annotations

from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box

from app.core.rng import make_rng
from app.geography.frame import DEMOGRAPHIC_VARIABLES, GeographyFrame

PROVINCES: list[tuple[str, str, str]] = [
    ("GR", "PV20", "Groningen"),
    ("FR", "PV21", "Fryslân"),
    ("DR", "PV22", "Drenthe"),
    ("OV", "PV23", "Overijssel"),
    ("FL", "PV24", "Flevoland"),
    ("GE", "PV25", "Gelderland"),
    ("UT", "PV26", "Utrecht"),
    ("NH", "PV27", "Noord-Holland"),
    ("ZH", "PV28", "Zuid-Holland"),
    ("ZE", "PV29", "Zeeland"),
    ("NB", "PV30", "Noord-Brabant"),
    ("LI", "PV31", "Limburg"),
]

UNIT_COLUMNS: tuple[str, ...] = (
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


@dataclass
class SyntheticGeography:
    frame: GeographyFrame
    units: gpd.GeoDataFrame  # UNIT_COLUMNS + DEMOGRAPHIC columns + 'imputed_fields' + geometry (EPSG:28992)
    municipalities: gpd.GeoDataFrame
    provinces: gpd.GeoDataFrame
    unit_adjacency: pd.DataFrame  # columns: a, b, shared_border_m, kind


def synthetic_geography(
    seed: int = 7,
    cells: int = 10,
    cell_m: float = 4000.0,
    muni_block: int = 2,
    population_scale: float = 1.0,
) -> SyntheticGeography:
    """Build a toy country.

    ``cells``² units per province, municipalities of ``muni_block``² units, and province
    populations roughly proportional to the real ones (scaled by ``population_scale``).
    """
    rng = make_rng(seed, "synthetic-geography")
    real_pop_m = [0.59, 0.66, 0.50, 1.18, 0.45, 2.15, 1.40, 2.95, 3.90, 0.39, 2.65, 1.12]
    rows = []
    prov_rows = []
    span = cells * cell_m
    for p, (pcode, pcbs, pname) in enumerate(PROVINCES):
        gx, gy = p % 4, p // 4
        x0, y0 = 50_000 + gx * (span + 5_000), 350_000 + (2 - gy) * (span + 5_000)
        prov_rows.append(
            {"code": pcode, "cbs_code": pcbs, "name": pname, "geometry": box(x0, y0, x0 + span, y0 + span)}
        )
        target_pop = real_pop_m[p] * 1_000_000 * population_scale
        weights = rng.lognormal(0.0, 0.8, size=(cells, cells))
        # dense city in the first municipality block (top-left 3x3 units)
        weights[:3, :3] *= 12.0
        weights = weights / weights.sum() * target_pop
        muni_counter = 0
        muni_of_cell: dict[tuple[int, int], str] = {}
        for bi in range(0, cells, muni_block):
            for bj in range(0, cells, muni_block):
                muni_counter += 1
                mcode = f"GM{p + 1:02d}{muni_counter:02d}"
                for i in range(bi, min(bi + muni_block, cells)):
                    for j in range(bj, min(bj + muni_block, cells)):
                        muni_of_cell[(i, j)] = mcode
        # merge the 3x3 city cells into the first municipality
        for i in range(3):
            for j in range(3):
                muni_of_cell[(i, j)] = f"GM{p + 1:02d}01"
        for i in range(cells):
            for j in range(cells):
                mcode = muni_of_cell[(i, j)]
                geom = box(
                    x0 + j * cell_m,
                    y0 + span - (i + 1) * cell_m,
                    x0 + (j + 1) * cell_m,
                    y0 + span - i * cell_m,
                )
                pop = round(float(weights[i, j]))
                area = geom.area / 1e6
                density = pop / area
                urb = int(np.clip(5 - np.digitize(density, [500, 1000, 1500, 2500]), 1, 5))
                c = geom.centroid
                demo = _synthetic_demo(rng, density)
                rows.append(
                    {
                        "code": f"BU{p + 1:02d}{i:02d}{j:02d}00",
                        "name": f"{pname} buurt {i}-{j}",
                        "wijk_code": f"WK{mcode[2:]}{i:02d}",
                        "municipality_code": mcode,
                        "province_code": pcode,
                        "population": pop,
                        "eligible_voters_est": int(pop * 0.78),
                        "area_km2": area,
                        "land_area_km2": area,
                        "density": density,
                        "urbanity_class": urb,
                        "address_density": density * 0.5,
                        "centroid_x": c.x,
                        "centroid_y": c.y,
                        "centroid_lon": 3.3 + c.x / 70_000.0 * 1.0,
                        "centroid_lat": 50.7 + c.y / 111_000.0,
                        **demo,
                        "imputed_fields": "",
                        "geometry": geom,
                    }
                )
    units = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:28992")
    munis = (
        units.dissolve(
            by="municipality_code",
            aggfunc={"population": "sum", "province_code": "first", "land_area_km2": "sum"},
        )
        .reset_index()
        .rename(columns={"municipality_code": "code"})
    )
    munis["name"] = [f"Gemeente {c}" for c in munis["code"]]
    provinces = gpd.GeoDataFrame(prov_rows, geometry="geometry", crs="EPSG:28992")
    adjacency = _grid_adjacency(units)
    frame = frame_from_tables(units, munis, provinces, year=0)
    return SyntheticGeography(
        frame=frame, units=units, municipalities=munis, provinces=provinces, unit_adjacency=adjacency
    )


def _synthetic_demo(rng: np.random.Generator, density: float) -> dict[str, float]:
    urban = min(1.0, np.log1p(density) / 9.0)
    return {
        "log_density": float(np.log1p(density)),
        "urbanity": float(1 + 4 * urban),
        "pct_age_15_25": float(10 + 8 * urban + rng.normal(0, 1.5)),
        "pct_age_25_45": float(22 + 10 * urban + rng.normal(0, 2)),
        "pct_age_65_plus": float(24 - 10 * urban + rng.normal(0, 2)),
        "pct_households_with_children": float(34 - 12 * urban + rng.normal(0, 3)),
        "pct_single_households": float(30 + 25 * urban + rng.normal(0, 3)),
        "pct_origin_europe": float(6 + 6 * urban + rng.normal(0, 1)),
        "pct_origin_non_europe": float(4 + 20 * urban + abs(rng.normal(0, 3))),
        "pct_education_high": float(25 + 20 * urban + rng.normal(0, 5)),
        "pct_education_low": float(30 - 10 * urban + rng.normal(0, 4)),
        "income_per_capita_keur": float(30 + rng.normal(0, 4)),
        "pct_owner_occupied": float(70 - 35 * urban + rng.normal(0, 5)),
    }


def _grid_adjacency(units: gpd.GeoDataFrame) -> pd.DataFrame:
    from shapely import STRtree

    geoms = units.geometry.values
    tree = STRtree(geoms)
    left, right = tree.query(geoms, predicate="touches")
    mask = left < right
    left, right = left[mask], right[mask]
    lengths = np.array(
        [geoms[a].boundary.intersection(geoms[b].boundary).length for a, b in zip(left, right, strict=True)]
    )
    keep = lengths > 1.0  # rook adjacency (shared edge, not just a corner)
    codes = units["code"].to_numpy()
    return pd.DataFrame(
        {"a": codes[left[keep]], "b": codes[right[keep]], "shared_border_m": lengths[keep], "kind": "border"}
    )


def frame_from_tables(
    units: pd.DataFrame, munis: pd.DataFrame, provinces: pd.DataFrame, year: int
) -> GeographyFrame:
    """Build a :class:`GeographyFrame` from unit/municipality/province tables with the store schema.

    ``provinces`` must be in canonical order (column ``code``); ``munis`` needs ``code``,
    ``name``, ``province_code``; ``units`` needs UNIT_COLUMNS + DEMOGRAPHIC_VARIABLES.
    """
    pcodes = list(provinces["code"])
    pidx = {c: i for i, c in enumerate(pcodes)}
    munis = munis.sort_values("code").reset_index(drop=True)
    mcodes = list(munis["code"])
    midx = {c: i for i, c in enumerate(mcodes)}
    units = units.sort_values("code").reset_index(drop=True)
    unit_muni = units["municipality_code"].map(midx).to_numpy(dtype=np.int64)
    unit_prov = units["province_code"].map(pidx).to_numpy(dtype=np.int64)
    unit_pop = units["population"].to_numpy(dtype=np.int64)
    muni_pop = np.zeros(len(mcodes), dtype=np.int64)
    np.add.at(muni_pop, unit_muni, unit_pop)
    muni_prov = munis["province_code"].map(pidx).to_numpy(dtype=np.int64)
    # population-weighted municipal centroids from units
    w = np.maximum(unit_pop, 1).astype(float)
    mx = np.bincount(unit_muni, weights=units["centroid_x"].to_numpy() * w, minlength=len(mcodes))
    my = np.bincount(unit_muni, weights=units["centroid_y"].to_numpy() * w, minlength=len(mcodes))
    mlon = np.bincount(unit_muni, weights=units["centroid_lon"].to_numpy() * w, minlength=len(mcodes))
    mlat = np.bincount(unit_muni, weights=units["centroid_lat"].to_numpy() * w, minlength=len(mcodes))
    mw = np.bincount(unit_muni, weights=w, minlength=len(mcodes))
    mw[mw == 0] = 1.0
    demo = units[list(DEMOGRAPHIC_VARIABLES)].to_numpy(dtype=float)
    imputed = np.zeros_like(demo, dtype=bool)
    if "imputed_fields" in units.columns:
        for i, fields in enumerate(units["imputed_fields"].fillna("")):
            if fields:
                for f in str(fields).split(","):
                    if f in DEMOGRAPHIC_VARIABLES:
                        imputed[i, DEMOGRAPHIC_VARIABLES.index(f)] = True
    return GeographyFrame(
        year=year,
        province_codes=pcodes,
        province_names=list(provinces["name"]),
        province_cbs_codes=list(provinces["cbs_code"]) if "cbs_code" in provinces else [""] * len(pcodes),
        muni_codes=mcodes,
        muni_names=list(munis["name"]),
        muni_province=muni_prov,
        muni_population=muni_pop,
        muni_xy=np.column_stack([mx / mw, my / mw]),
        muni_lonlat=np.column_stack([mlon / mw, mlat / mw]),
        unit_codes=list(units["code"]),
        unit_names=list(units["name"]),
        unit_muni=unit_muni,
        unit_province=unit_prov,
        unit_population=unit_pop,
        unit_eligible=units["eligible_voters_est"].to_numpy(dtype=np.int64),
        unit_xy=units[["centroid_x", "centroid_y"]].to_numpy(dtype=float),
        unit_urbanity_class=units["urbanity_class"].fillna(0).to_numpy(dtype=np.int64),
        unit_land_km2=units["land_area_km2"].to_numpy(dtype=float),
        demo_names=DEMOGRAPHIC_VARIABLES,
        unit_demo=demo,
        unit_demo_imputed=imputed,
    )
