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
