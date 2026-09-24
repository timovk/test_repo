"""Fixtures for the political-model / simulation tests (synthetic toy country, offline)."""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from app.core.constitution import ELECTORAL_VOTES, HOUSE_SEATS, SENATORS_PER_PROVINCE
from app.elections.types import RaceSpec
from app.geography.frame import GeographyFrame
from app.scenarios.loader import load_scenario
from app.scenarios.schema import ScenarioDocument
from app.simulation.candidates import generate_down_ballot
from app.simulation.races import (
    governor_slots,
    house_slots,
    presidential_races,
    races_from_candidates,
    senate_slots,
    ticket_lines,
)
from app.simulation.regions import RegionsConfig, regions_config_from_dict
from app.simulation.structural import StructuralModel


def huntington_hill(pops: dict[str, int], seats: int) -> dict[str, int]:
    """Minimal Huntington–Hill apportionment (test helper; the real one lives in app.districts)."""
    alloc = dict.fromkeys(pops, 1)
    heap = [(-p / math.sqrt(2.0), c) for c, p in pops.items()]
    heapq.heapify(heap)
    for _ in range(seats - len(pops)):
        _, c = heapq.heappop(heap)
        alloc[c] += 1
        n = alloc[c]
        heapq.heappush(heap, (-pops[c] / math.sqrt(n * (n + 1)), c))
    return alloc


def striped_districts(frame: GeographyFrame, seats: dict[str, int]) -> np.ndarray:
    """Unit → district code by equal-eligible stripes within each province (test helper)."""
    out = np.empty(frame.n_units, dtype=object)
    for p, pv in enumerate(frame.province_codes):
        idx = frame.units_in_province(p)
        s = seats[pv]
        order = idx[np.argsort(frame.unit_xy[idx, 0] * 0.6 + frame.unit_xy[idx, 1], kind="stable")]
        d = (np.arange(len(order)) * s) // len(order)  # equal unit counts: every district non-empty
        for k in range(s):
            out[order[d == k]] = f"{pv}-{k + 1:02d}"
    return out


def small_doc_data(**overrides: Any) -> dict[str, Any]:
    """A compact 5-party scenario for unit tests (FICTIONAL)."""
    data: dict[str, Any] = {
        "scenario": {
            "slug": "unit-test",
            "name": "Unit test",
            "year": 2028,
            "seed": 11,
            "political_geography_seed": 5,
        },
        "parties": [
            {
                "code": "LEFT",
                "name": "Left",
                "abbreviation": "L",
                "color": "#ff0000",
                "base_share": 0.30,
                "ideology": {"economic": -0.7, "social": -0.5, "europe": 0.4},
                "demographics": {"pct_education_high": 0.3, "log_density": 0.2},
                "urbanity": {"1": 0.3, "5": -0.3},
            },
            {
                "code": "RIGHT",
                "name": "Right",
                "abbreviation": "R",
                "color": "#0000ff",
                "base_share": 0.30,
                "turnout_propensity": 0.3,
                "ideology": {"economic": 0.7, "social": 0.1, "europe": 0.2},
                "demographics": {"income_per_capita_keur": 0.3},
            },
            {
                "code": "CENTRE",
                "name": "Centre",
                "abbreviation": "C",
                "color": "#00ff00",
                "base_share": 0.20,
                "ideology": {"economic": 0.1, "social": -0.2, "europe": 0.8},
            },
            {
                "code": "RURAL",
                "name": "Rural",
                "abbreviation": "RU",
                "color": "#aaaa00",
                "base_share": 0.12,
                "ideology": {"economic": 0.4, "social": 0.5, "europe": -0.4},
                "urbanity": {"1": -0.8, "5": 0.8},
                "provinces": {"DR": 0.5},
            },
            {
                "code": "MINOR",
                "name": "Minor",
                "abbreviation": "M",
                "color": "#555555",
                "base_share": 0.08,
                "ideology": {"economic": -0.9, "social": 0.0, "europe": -0.6},
            },
        ],
        "candidates": [
            {
                "key": "left-pres",
                "first_name": "Anna",
                "last_name": "Links",
                "party": "LEFT",
                "home_municipality": "GM0101",
                "quality": 0.5,
            },
            {
                "key": "left-vp",
                "first_name": "Bert",
                "last_name": "Links",
                "party": "LEFT",
                "home_municipality": "GM1101",
            },
            {
                "key": "right-pres",
                "first_name": "Carla",
                "last_name": "Rechts",
                "party": "RIGHT",
                "home_municipality": "GM0801",
                "quality": 0.2,
            },
            {
                "key": "right-vp",
                "first_name": "Dirk",
                "last_name": "Rechts",
                "party": "RIGHT",
                "home_municipality": "GM0901",
            },
            {
                "key": "centre-pres",
                "first_name": "Eva",
                "last_name": "Midden",
                "party": "CENTRE",
                "home_municipality": "GM0701",
            },
            {
                "key": "centre-vp",
                "first_name": "Frits",
                "last_name": "Midden",
                "party": "CENTRE",
                "home_municipality": "GM0601",
            },
            {
                "key": "ind-cand",
                "first_name": "Gerda",
                "last_name": "Vrij",
                "party": None,
                "home_municipality": "GM0301",
                "quality": 1.0,
            },
        ],
        "president": {
            "tickets": [
                {"party": "LEFT", "president": "left-pres", "vice_president": "left-vp"},
                {"party": "RIGHT", "president": "right-pres", "vice_president": "right-vp"},
                {"party": "CENTRE", "president": "centre-pres", "vice_president": "centre-vp"},
            ]
        },
        "environment": {"turnout_base": 0.75, "national": {"LEFT": 0.02}},
    }
    for k, v in overrides.items():
        data[k] = v
    return data


@pytest.fixture(scope="session")
def frame(synthetic) -> GeographyFrame:  # type: ignore[no-untyped-def]
    return synthetic.frame


@pytest.fixture(scope="session")
def small_doc() -> ScenarioDocument:
    return ScenarioDocument.model_validate(small_doc_data())


@pytest.fixture(scope="session")
def make_doc() -> Callable[..., ScenarioDocument]:
    def _make(**overrides: Any) -> ScenarioDocument:
        return ScenarioDocument.model_validate(small_doc_data(**overrides))

    return _make


@pytest.fixture(scope="session")
def no_regions() -> RegionsConfig:
    return RegionsConfig()


@pytest.fixture(scope="session")
def synth_regions(frame: GeographyFrame) -> RegionsConfig:
    """Regions of the synthetic country, defined by CBS codes and provinces."""
    return regions_config_from_dict(
        {
            "regions": {
                "cities": {"label": "Cities", "codes": [c for c in frame.muni_codes if c.endswith("01")]},
                "south": {"label": "South", "provinces": ["NB", "LI"]},
                "north_east": {"label": "North-east", "provinces": ["GR", "DR"], "exclude": ["GM0101"]},
            }
        }
    )


@pytest.fixture(scope="session")
def small_model(
    frame: GeographyFrame, small_doc: ScenarioDocument, no_regions: RegionsConfig
) -> StructuralModel:
    return StructuralModel.build(frame, small_doc, regions=no_regions)


@pytest.fixture(scope="session")
def demo_doc() -> ScenarioDocument:
    return load_scenario("demo-2028")


@pytest.fixture(scope="session")
def demo_model(frame: GeographyFrame, demo_doc: ScenarioDocument) -> StructuralModel:
    return StructuralModel.build(frame, demo_doc)


@pytest.fixture(scope="session")
def seats(frame: GeographyFrame) -> dict[str, int]:
    pops = dict(zip(frame.province_codes, (int(x) for x in frame.province_population()), strict=True))
    return huntington_hill(pops, HOUSE_SEATS)


@pytest.fixture(scope="session")
def ev_by_province(seats: dict[str, int]) -> dict[str, int]:
    ev = {pv: s + SENATORS_PER_PROVINCE for pv, s in seats.items()}
    assert sum(ev.values()) == ELECTORAL_VOTES
    return ev


@pytest.fixture(scope="session")
def full_races(
    frame: GeographyFrame,
    demo_model: StructuralModel,
    demo_doc: ScenarioDocument,
    seats: dict[str, int],
    ev_by_province: dict[str, int],
) -> list[RaceSpec]:
    """A realistic 2028 race set on the synthetic country: 12 PRES + parent, 150 House, 8 Senate, 12 Governors."""
    ud = striped_districts(frame, seats)
    sen = [(pv, 1) for pv in ("NH", "UT", "FR", "FL")] + [(pv, 2) for pv in ("ZH", "OV", "LI", "ZE")]
    slots = house_slots(frame, ud) + senate_slots(frame, sen) + governor_slots(frame)
    fielded = generate_down_ballot(demo_model, slots, seed=demo_doc.scenario.seed)
    return presidential_races(frame, ticket_lines(demo_doc, frame), ev_by_province) + races_from_candidates(
        slots, fielded
    )
