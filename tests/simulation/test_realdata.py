"""Checks against the REAL CBS geography (skipped when the processed store is not built).

These are regression guards for the demo scenarios' qualitative electoral geography — all
parties and numbers are FICTIONAL — and the full-country performance target.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from app.scenarios.loader import load_scenario, validate_scenario
from app.simulation.candidates import generate_down_ballot
from app.simulation.races import (
    governor_slots,
    house_slots,
    presidential_races,
    races_from_candidates,
    senate_slots,
    ticket_lines,
)
from app.simulation.regions import resolve_regions
from app.simulation.structural import StructuralModel
from app.simulation.voting import simulate_election

pytestmark = pytest.mark.realdata

SLUGS = ("founding-2024", "midterm-2026", "demo-2028")


@pytest.fixture(scope="module")
def real_model_2028(real_frame):  # type: ignore[no-untyped-def]
    return StructuralModel.build(real_frame, load_scenario("demo-2028"), strict=True)


def test_regions_resolve_on_real_frame(real_frame):
    r = resolve_regions(real_frame)
    assert r.resolution_rate >= 0.95, r.unresolved
    assert r.muni_mask("catholic_south").sum() > 50
    assert 15 <= r.muni_mask("bible_belt").sum() <= 40


@pytest.mark.parametrize("slug", SLUGS)
def test_demo_scenarios_fit_real_geography(real_frame, slug):
    doc = load_scenario(slug)
    assert validate_scenario(doc, real_frame) == []
    m = StructuralModel.build(real_frame, doc, strict=True)
    assert m.calibration.converged
    assert not m.warnings


def test_demo_geography_is_plausible(real_model_2028, real_frame):
    m = real_model_2028
    mun = m.municipality_shares(environment=False)
    prov = m.province_shares(environment=False)
    nat = m.national_shares(environment=False)

    def share(name: str, party: str) -> float:
        return float(mun.loc[real_frame.muni_codes[real_frame.muni_names.index(name)], party])

    assert share("Staphorst", "RV") > 0.3 and share("Urk", "RV") > 0.3
    assert share("Amsterdam", "PA") > 1.3 * nat["PA"]
    assert share("Wassenaar", "VLP") > 1.5 * nat["VLP"]
    assert share("Kerkrade", "NVB") > 1.5 * nat["NVB"]
    assert share("Tubbergen", "CVU") > 1.5 * nat["CVU"]
    assert prov.loc["DR", "PLB"] > 2 * nat["PLB"]
    assert prov.loc["LI", "RV"] < 0.005
    assert prov.loc["NB", "CVU"] > nat["CVU"] and prov.loc["LI", "NVB"] > nat["NVB"]
    t = m.expected_turnout_by("municipality", environment=False)
    assert t.min() > 0.5 and t.max() < 0.97


def test_full_country_performance(real_frame, real_model_2028):
    m = real_model_2028
    doc = m.scenario
    f = real_frame
    pops = f.province_population().astype(float)
    seats = np.maximum(1, np.round(pops / pops.sum() * 150)).astype(int)
    seats[int(np.argmax(pops))] += 150 - seats.sum()
    ud = np.empty(f.n_units, dtype=object)
    for p, pv in enumerate(f.province_codes):
        idx = f.units_in_province(p)
        order = idx[np.argsort(f.unit_xy[idx, 1], kind="stable")]
        d = (np.arange(len(order)) * seats[p]) // len(order)
        for k in range(seats[p]):
            ud[order[d == k]] = f"{pv}-{k + 1:02d}"
    sen = [(pv, 1) for pv in ("NH", "UT", "FR", "FL")] + [(pv, 2) for pv in ("ZH", "OV", "LI", "ZE")]
    slots = house_slots(f, ud) + senate_slots(f, sen) + governor_slots(f)
    fielded = generate_down_ballot(m, slots, seed=doc.scenario.seed)
    ev = {pv: int(s) + 2 for pv, s in zip(f.province_codes, seats, strict=True)}
    races = presidential_races(f, ticket_lines(doc, f), ev) + races_from_candidates(slots, fielded)
    assert len(races) == 183
    simulate_election(m, races, seed=1)  # warm-up (Cholesky cache)
    t0 = time.perf_counter()
    draw = simulate_election(m, races, seed=2)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"{elapsed:.2f}s"
    for rv in draw.races.values():
        rv.check()
