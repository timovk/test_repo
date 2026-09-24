"""Fixtures for the forecasting tests: the REAL demo scenario on the synthetic toy country.

Everything here is FICTIONAL / SIMULATED: parties, candidates and results come from the invented
demo scenario; the synthetic geography only borrows the real province codes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from app.core.constitution import HOUSE_SEATS, SENATORS_PER_PROVINCE
from app.districts.apportionment import apportion
from app.elections.types import RaceSpec
from app.forecasting import ForecastConfig, ForecastResult, load_forecast_config, run_forecast
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
from app.simulation.structural import StructuralModel
from app.simulation.voting import ElectionContext

#: Senate class 2 of the demo (8 seats up); the other 16 seats are held over.
SENATE_UP = [(pv, 1) for pv in ("NH", "UT", "FR", "FL")] + [(pv, 2) for pv in ("ZH", "OV", "LI", "ZE")]
HOLDOVER = {"VLP": 5, "PA": 4, "CVU": 4, "NVB": 3}


def striped_districts(frame: GeographyFrame, seats: dict[str, int]) -> np.ndarray:
    """Unit → district code by equal-unit stripes within each province (test helper)."""
    out = np.empty(frame.n_units, dtype=object)
    for p, pv in enumerate(frame.province_codes):
        idx = frame.units_in_province(p)
        order = idx[np.argsort(frame.unit_xy[idx, 0] * 0.6 + frame.unit_xy[idx, 1], kind="stable")]
        d = (np.arange(len(order)) * seats[pv]) // len(order)
        for k in range(seats[pv]):
            out[order[d == k]] = f"{pv}-{k + 1:02d}"
    return out


@pytest.fixture(scope="session")
def frame(synthetic) -> GeographyFrame:  # type: ignore[no-untyped-def]
    return synthetic.frame


@pytest.fixture(scope="session")
def demo_doc() -> ScenarioDocument:
    return load_scenario("demo-2028")


@pytest.fixture(scope="session")
def demo_model(frame: GeographyFrame, demo_doc: ScenarioDocument) -> StructuralModel:
    return StructuralModel.build(frame, demo_doc, strict=False)


@pytest.fixture(scope="session")
def context(demo_doc: ScenarioDocument) -> ElectionContext:
    return ElectionContext.from_scenario(demo_doc)


@pytest.fixture(scope="session")
def seats(frame: GeographyFrame) -> dict[str, int]:
    pops = {pv: int(x) for pv, x in zip(frame.province_codes, frame.province_population(), strict=True)}
    return apportion(pops, HOUSE_SEATS).seats


@pytest.fixture(scope="session")
def ev_by_province(seats: dict[str, int]) -> dict[str, int]:
    return {pv: s + SENATORS_PER_PROVINCE for pv, s in seats.items()}


@pytest.fixture(scope="session")
def unit_district(frame: GeographyFrame, seats: dict[str, int]) -> np.ndarray:
    return striped_districts(frame, seats)


@pytest.fixture(scope="session")
def pres_races(
    frame: GeographyFrame, demo_doc: ScenarioDocument, ev_by_province: dict[str, int]
) -> list[RaceSpec]:
    return presidential_races(frame, ticket_lines(demo_doc, frame), ev_by_province)


@pytest.fixture(scope="session")
def down_ballot(
    frame: GeographyFrame, demo_model: StructuralModel, demo_doc: ScenarioDocument, unit_district: np.ndarray
) -> dict[str, list[RaceSpec]]:
    slots = house_slots(frame, unit_district) + senate_slots(frame, SENATE_UP) + governor_slots(frame)
    fielded = generate_down_ballot(demo_model, slots, seed=demo_doc.scenario.seed)
    races = races_from_candidates(slots, fielded)
    out: dict[str, list[RaceSpec]] = {"HOUSE": [], "SENATE": [], "GOVERNOR": []}
    for r in races:
        out[str(r.race_type)].append(r)
    return out


@pytest.fixture(scope="session")
def full_races(pres_races: list[RaceSpec], down_ballot: dict[str, list[RaceSpec]]) -> list[RaceSpec]:
    """President (12 + parent), 150 House, 8 Senate (class 2) and 12 governors."""
    return pres_races + down_ballot["HOUSE"] + down_ballot["SENATE"] + down_ballot["GOVERNOR"]


@pytest.fixture(scope="session")
def test_config() -> ForecastConfig:
    """Default configuration with small blocks, so a few thousand draws span many blocks."""
    return load_forecast_config().model_copy(update={"block_size": 250, "chunk_size": 1000})


@pytest.fixture(scope="session")
def run(
    demo_model: StructuralModel,
    context: ElectionContext,
    ev_by_province: dict[str, int],
    test_config: ForecastConfig,
) -> Callable[..., ForecastResult]:
    """``run(races, n, seed, **kwargs)`` → forecast with the demo model and test configuration."""

    def _run(races: list[RaceSpec], n: int, seed: int, **kwargs: Any) -> ForecastResult:
        kwargs.setdefault("ev_by_province", ev_by_province)
        kwargs.setdefault("config", test_config)
        kwargs.setdefault("workers", 1)
        return run_forecast(demo_model, races, kwargs.pop("context", context), n, seed, **kwargs)

    return _run


@pytest.fixture(scope="session")
def full_forecast(run: Callable[..., ForecastResult], full_races: list[RaceSpec]) -> ForecastResult:
    """A complete general-election forecast (2,000 draws) shared by the property tests."""
    return run(full_races, 2000, 11, holdover_senate=HOLDOVER)
