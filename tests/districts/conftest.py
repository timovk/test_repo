"""Fixtures for the districting tests.

``fine_geo`` is a synthetic country with the processed-store schema whose units are small
relative to a House district (≈ 3 % of the target population), like real CBS buurten.  The shared
``synthetic`` fixture (``tests/conftest.py``) is far coarser: its "city" cells hold more people
than a whole district, so population equality within ±2 % is impossible there — it is used for the
structural invariants only.

The fine geography deliberately contains the awkward real-world features the generator must
handle: a large city that needs several districts, zero-population units, an enclave
municipality, a non-contiguous (exclave) municipality, an island joined by a water link and
cross-province borders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from app.core.rng import make_rng
from app.districts.apportionment import apportion
from app.districts.config import DistrictConfig, MergeSplitConfig
from app.geography.frame import DEMOGRAPHIC_VARIABLES
from app.geography.synthetic import PROVINCES

#: Approximate REAL province populations (millions) used to size the synthetic provinces.
PROVINCE_POP_M = {
    "GR": 0.59, "FR": 0.66, "DR": 0.50, "OV": 1.18, "FL": 0.45, "GE": 2.15,
    "UT": 1.40, "NH": 2.95, "ZH": 3.90, "ZE": 0.39, "NB": 2.65, "LI": 1.12,
}  # fmt: skip
CELL_M = 1000.0


@dataclass
class FineGeo:
    units: gpd.GeoDataFrame
    adjacency: pd.DataFrame
    municipalities: pd.DataFrame
    provinces: pd.DataFrame
    seats: dict[str, int]
    island_units: list[str]
    enclave: str
    exclave: str
    city: dict[str, str]  # province → city municipality code


def build_fine_geography(seed: int = 5, unit_pop: float = 3500.0) -> FineGeo:
    rng = make_rng(seed, "tests", "fine-geography")
    rows: list[dict] = []
    edges: list[tuple[str, str, float, str]] = []
    x0 = 20_000.0
    prev: tuple[int, float, list[list[str]]] | None = None
    island_units: list[str] = []
    city: dict[str, str] = {}
    enclave = exclave = ""
    for p, (pcode, _cbs, pname) in enumerate(PROVINCES):
        pop_total = PROVINCE_POP_M[pcode] * 1e6
        n = max(8, round(math.sqrt(pop_total / unit_pop)))
        c = max(4, (n // 3) // 2 * 2)  # city: top-left c × c cells (even, aligned with wijken)
        w = rng.lognormal(0.0, 0.35, (n, n))
        w[:c, :c] *= 2.0
        w[rng.random((n, n)) < 0.02] = 0.0  # zero-population units (industrial estates, nature)
        pops = np.round(w / w.sum() * pop_total).astype(np.int64)
        nb = math.ceil(n / 4)
        muni = np.empty((n, n), dtype=object)
        for i in range(n):
            for j in range(n):
                muni[i, j] = f"GM{p + 1:02d}{(i // 4) * nb + (j // 4) + 1:02d}"
        city_code = f"GM{p + 1:02d}00"
        muni[:c, :c] = city_code
        city[pcode] = city_code
        if pcode == "UT":  # enclave municipality surrounded by the city
            enclave = f"GM{p + 1:02d}99"
            muni[2:4, 2:4] = enclave
        if pcode == "GE":  # exclave: the last block belongs to a municipality far away
            exclave = str(muni[n - 1, 0])
            muni[n - 4 :, n - 4 :] = exclave
        codes = [[f"BU{p + 1:02d}{i:03d}{j:03d}" for j in range(n)] for i in range(n)]
        y_top = 450_000.0
        for i in range(n):
            for j in range(n):
                geom = box(
                    x0 + j * CELL_M, y_top - (i + 1) * CELL_M, x0 + (j + 1) * CELL_M, y_top - i * CELL_M
                )
                rows.append(
                    _unit_row(
                        codes[i][j],
                        pcode,
                        str(muni[i, j]),
                        f"WK{p + 1:02d}{i // 2:02d}{j // 2:02d}",
                        int(pops[i, j]),
                        geom,
                        pname,
                    )
                )
                if j + 1 < n:
                    edges.append((codes[i][j], codes[i][j + 1], CELL_M, "border"))
                if i + 1 < n:
                    edges.append((codes[i][j], codes[i + 1][j], CELL_M, "border"))
        # cross-province border with the previous province (squares touch along x = x0)
        if prev is not None:
            pn, _, pcodes = prev
            for i in range(min(n, pn)):
                edges.append((pcodes[i][pn - 1], codes[i][0], CELL_M, "border"))
        if pcode in ("ZE", "FR"):  # an island 3 km off the coast, linked by a water link
            island_muni = f"GM{p + 1:02d}98"
            for i in range(3):
                for j in range(3):
                    code = f"BU{p + 1:02d}9{i:02d}{j:03d}"
                    gx = x0 + (n + 3 + j) * CELL_M
                    geom = box(gx, y_top - (i + 1) * CELL_M, gx + CELL_M, y_top - i * CELL_M)
                    rows.append(
                        _unit_row(
                            code,
                            pcode,
                            island_muni,
                            f"WK{p + 1:02d}9{i // 2}{j // 2}",
                            int(unit_pop * 0.6),
                            geom,
                            pname,
                        )
                    )
                    island_units.append(code)
                    if j + 1 < 3:
                        edges.append((code, f"BU{p + 1:02d}9{i:02d}{j + 1:03d}", CELL_M, "border"))
                    if i + 1 < 3:
                        edges.append((code, f"BU{p + 1:02d}9{i + 1:02d}{j:03d}", CELL_M, "border"))
            edges.append((codes[0][n - 1], f"BU{p + 1:02d}900000", 0.0, "water_link"))
            x0 += 7 * CELL_M
        prev = (n, x0, codes)
        x0 += n * CELL_M
    units = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:28992")
    adj = pd.DataFrame(edges, columns=["a", "b", "shared_border_m", "kind"])
    lo = np.minimum(adj["a"], adj["b"])
    hi = np.maximum(adj["a"], adj["b"])
    adj["a"], adj["b"] = lo, hi
    munis = (
        units.groupby("municipality_code")
        .agg(
            province_code=("province_code", "first"),
            population=("population", "sum"),
            name=("municipality_name", "first"),
        )
        .reset_index()
        .rename(columns={"municipality_code": "code"})
    )
    provinces = pd.DataFrame(PROVINCES, columns=["code", "cbs_code", "name"])
    pop = units.groupby("province_code")["population"].sum()
    seats = apportion({c: int(pop[c]) for c in provinces["code"]}, 150).seats
    return FineGeo(units, adj, munis, provinces, seats, island_units, enclave, exclave, city)


def _unit_row(code: str, pcode: str, mcode: str, wijk: str, pop: int, geom, pname: str) -> dict:  # type: ignore[no-untyped-def]
    c = geom.centroid
    area = geom.area / 1e6
    density = pop / area
    row = {
        "code": code,
        "name": f"{pname} buurt {code[-6:]}",
        "wijk_code": wijk,
        "municipality_code": mcode,
        "municipality_name": f"Gemeente {mcode[2:]}",
        "province_code": pcode,
        "population": pop,
        "eligible_voters_est": int(pop * 0.77),
        "area_km2": area,
        "land_area_km2": area,
        "density": density,
        "urbanity_class": int(np.clip(5 - np.digitize(density, [500, 1000, 1500, 2500]), 1, 5)),
        "address_density": density * 0.5,
        "centroid_x": c.x,
        "centroid_y": c.y,
        "centroid_lon": 3.3 + c.x / 70_000.0,
        "centroid_lat": 50.7 + c.y / 111_000.0,
        "imputed_fields": "",
        "geometry": geom,
    }
    row.update(dict.fromkeys(DEMOGRAPHIC_VARIABLES, 10.0))
    return row


@pytest.fixture(scope="session")
def fine_geo() -> FineGeo:
    return build_fine_geography()


@pytest.fixture(scope="session")
def fast_config() -> DistrictConfig:
    """Default parameters with fewer restarts (unit tests must be fast)."""
    return DistrictConfig(restarts=2, workers=1, merge_split=MergeSplitConfig(restarts=1))


@pytest.fixture(scope="session")
def fine_plan(fine_geo: FineGeo, fast_config: DistrictConfig):  # type: ignore[no-untyped-def]
    from app.districts.generator import generate_plan

    return generate_plan(fine_geo.units, fine_geo.adjacency, fine_geo.seats, fast_config, seed=11)
