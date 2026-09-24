"""Full House plan on the REAL CBS geography (skipped unless the processed store is built)."""

from __future__ import annotations

import numpy as np
import pytest
import shapely

from app.core.constitution import ELECTORAL_VOTES, HOUSE_SEATS, PRESIDENTIAL_MAJORITY
from app.districts.apportionment import apportion
from app.districts.config import load_district_config
from app.districts.generator import generate_plan
from app.districts.stats import (
    district_adjacency,
    district_geometries,
    district_stats,
    geometries_wkb,
)
from app.districts.validation import validate_plan

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def real_inputs(real_frame):  # type: ignore[no-untyped-def]
    from app.geography.store import load_municipalities_gdf, load_unit_adjacency, load_units_attrs

    units = load_units_attrs()
    adjacency = load_unit_adjacency()
    munis = load_municipalities_gdf(columns=["code", "name"])
    return units, adjacency, dict(zip(munis["code"], munis["name"], strict=True)), real_frame


@pytest.fixture(scope="module")
def real_plan(real_inputs):  # type: ignore[no-untyped-def]
    units, adjacency, names, _ = real_inputs
    seats = apportion(
        units.groupby("province_code")["population"].sum().astype(int).to_dict(), HOUSE_SEATS
    ).seats
    plan = generate_plan(
        units, adjacency, seats, load_district_config(), seed=2028, municipality_names=names, workers=0
    )
    return plan, seats


def test_real_apportionment(real_inputs) -> None:  # type: ignore[no-untyped-def]
    _, _, _, frame = real_inputs
    r = apportion(
        dict(zip(frame.province_codes, frame.province_population().tolist(), strict=True)), HOUSE_SEATS
    )
    assert sum(r.seats.values()) == HOUSE_SEATS
    assert r.total_electoral_votes == ELECTORAL_VOTES
    assert PRESIDENTIAL_MAJORITY * 2 > ELECTORAL_VOTES
    # the largest province gets the most seats
    assert max(r.seats, key=r.seats.get) == frame.province_codes[int(np.argmax(frame.province_population()))]


def test_real_plan_is_valid(real_inputs, real_plan) -> None:  # type: ignore[no-untyped-def]
    units, _, _, frame = real_inputs
    plan, seats = real_plan
    assert plan.n_districts == HOUSE_SEATS
    v = validate_plan(plan, units, seats)
    assert v.ok, v.errors
    assert plan.noncontiguous_districts() == []
    assert np.abs(plan.deviation_pct()).max() <= plan.config.max_deviation_pct
    # every unit of the frame is assigned (unit and municipality counts derived from the data)
    assert set(plan.unit_codes.tolist()) == set(frame.unit_codes)
    assert len(set(plan.unit_municipality.tolist())) == frame.n_munis
    assert plan.timings["total"] < 180
    # far fewer split municipalities than districts
    assert len(plan.split_municipalities()) < plan.n_districts
    assert len(set(plan.district_names)) == plan.n_districts
    assert not any("GM" in n for n in plan.district_names)  # display names, not CBS codes
    assert plan.warnings == []


def test_real_plan_quality(real_plan) -> None:  # type: ignore[no-untyped-def]
    """Regression guards for the reported quality of the seed-2028 plan (docs/DISTRICTING.md §2.8)."""
    plan, seats = real_plan
    dev = np.abs(plan.deviation_pct())
    assert dev.max() <= plan.config.target_deviation_pct  # every district within ±2 %
    assert dev.mean() < 0.8
    for p, k in seats.items():  # per province too
        assert (np.asarray(plan.district_province) == p).sum() == k
    a = plan.assignment_frame()
    a["population"] = plan.unit_population
    target = dict(zip(plan.district_province, plan.district_target, strict=True))
    muni = a.groupby("municipality_code").agg(
        pop=("population", "sum"), prov=("province_code", "first"), parts=("district_code", "nunique")
    )
    ratio = muni["pop"] / muni["prov"].map(target)
    split = muni["parts"] > 1
    # every municipality too populous for one district is split into at least the necessary parts
    need = np.ceil(ratio / (1 + plan.config.target_deviation_pct / 100))
    assert (muni["parts"] >= need).all()
    assert split.sum() <= 62  # 60 with the default configuration (65 before recombination)
    assert ((ratio >= 1.02) & split).sum() == (ratio >= 1.02).sum() == 23
    # a municipality smaller than half a district is never cut into more than three pieces
    assert muni.loc[ratio < 0.5, "parts"].max() <= 3
    frags = (muni["parts"] - 1).sum()
    assert frags <= 100


def test_real_plan_is_reproducible_and_row_order_independent(real_inputs, real_plan) -> None:  # type: ignore[no-untyped-def]
    """Re-generating a few provinces serially from shuffled rows reproduces the parallel plan
    exactly — assignment, numbers and names (the full plan was generated with 4 workers)."""
    units, adjacency, names, _ = real_inputs
    plan, seats = real_plan
    subset = ["ZE", "FL", "DR", "GR"]
    part_units = units[units["province_code"].isin(subset)].sample(frac=1.0, random_state=7)
    again = generate_plan(
        part_units,
        adjacency,
        {p: seats[p] for p in subset},
        load_district_config(),
        seed=2028,
        municipality_names=names,
        workers=1,
    )
    full = dict(zip(plan.unit_codes, plan.unit_district_codes(), strict=True))
    assert all(full[c] == d for c, d in zip(again.unit_codes, again.unit_district_codes(), strict=True))
    by_code = dict(zip(plan.district_codes, plan.district_names, strict=True))
    assert all(by_code[c] == n for c, n in zip(again.district_codes, again.district_names, strict=True))
    # without explicit names the processed store's municipality names are used
    ze = units[units["province_code"] == "ZE"]
    plain = generate_plan(ze, adjacency, {"ZE": seats["ZE"]}, load_district_config(), seed=2028, workers=1)
    assert plain.district_names == [by_code[c] for c in plain.district_codes]


def test_real_plan_persistence_round_trip(real_plan, db_session) -> None:  # type: ignore[no-untyped-def]
    """store_plan + load_plan_mapping on the full CBS plan (≈14.7k bulk-inserted assignments)."""
    import time

    from sqlalchemy import func, select

    from app.districts.service import load_plan_mapping, store_plan
    from app.geography.loader_db import load_into_db
    from app.geography.store import load_frame, load_units_gdf
    from app.models import DistrictAssignment, HouseDistrict

    plan, _ = real_plan
    vintage = load_into_db(db_session)
    units = load_units_gdf(
        columns=["code", "population", "eligible_voters_est", "land_area_km2", "urbanity_class",
                 "centroid_x", "centroid_y"]
    )  # fmt: skip
    t0 = time.perf_counter()
    row = store_plan(db_session, plan, vintage.id, None, 2028, units)
    t_store = time.perf_counter() - t0
    frame = load_frame()
    t0 = time.perf_counter()
    mapping = load_plan_mapping(db_session, row.id, frame)
    t_load = time.perf_counter() - t0
    assert db_session.scalar(select(func.count()).select_from(DistrictAssignment)) == plan.n_units
    assert db_session.scalar(select(func.count()).select_from(HouseDistrict)) == HOUSE_SEATS
    assert (mapping.unit_district >= 0).all()
    want = dict(zip(plan.unit_codes, plan.unit_district_codes(), strict=True))
    got = np.asarray(mapping.district_codes, dtype=object)[mapping.unit_district]
    assert all(got[i] == want[c] for i, c in enumerate(frame.unit_codes))
    assert row.split_municipalities == len(plan.split_municipalities())
    assert t_store < 60 and t_load < 5


def test_real_stats(real_plan) -> None:  # type: ignore[no-untyped-def]
    from app.geography.store import load_units_gdf

    plan, _ = real_plan
    units = load_units_gdf(
        columns=[
            "code",
            "population",
            "eligible_voters_est",
            "land_area_km2",
            "urbanity_class",
            "centroid_x",
            "centroid_y",
        ]
    )
    geoms = district_geometries(plan, units)
    stats = district_stats(plan, units, geoms)
    assert stats["population"].sum() == int(units["population"].sum())
    assert stats["polsby_popper"].between(0, 1).all()
    assert stats["is_contiguous"].all()
    adj = district_adjacency(plan)
    assert set(adj["district_a"]) | set(adj["district_b"]) == set(plan.district_codes)
    wkb = geometries_wkb(geoms, tolerance_m=80.0)
    assert len(wkb) == plan.n_districts
    assert all(shapely.from_wkb(b).is_valid for b in wkb.values())
