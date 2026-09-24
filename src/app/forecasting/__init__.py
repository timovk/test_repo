"""Monte Carlo forecasting: vectorised, block-seeded simulation of many elections.

Pure engines (NumPy in, dataclasses out) are re-exported here; the SQLAlchemy persistence helpers
live in :mod:`app.forecasting.service` and are intentionally *not* imported by this package.
All forecast outputs are SIMULATED estimates of a FICTIONAL model, never real-world predictions.
"""

from app.forecasting.config import ForecastConfig, load_forecast_config
from app.forecasting.engine import execute_plan, resolve_workers, run_forecast, run_plan, simulate_block
from app.forecasting.plan import ForecastPlan, prepare_forecast
from app.forecasting.result import (
    DISCLAIMER,
    ChamberForecast,
    EVCombination,
    ForecastDraws,
    ForecastResult,
    LineForecast,
    PresidentialForecast,
    ProvinceForecast,
    RaceForecast,
    SeatForecast,
    Summary,
    TicketForecast,
)

__all__ = [
    "DISCLAIMER",
    "ChamberForecast",
    "EVCombination",
    "ForecastConfig",
    "ForecastDraws",
    "ForecastPlan",
    "ForecastResult",
    "LineForecast",
    "PresidentialForecast",
    "ProvinceForecast",
    "RaceForecast",
    "SeatForecast",
    "Summary",
    "TicketForecast",
    "execute_plan",
    "load_forecast_config",
    "prepare_forecast",
    "resolve_workers",
    "run_forecast",
    "run_plan",
    "simulate_block",
]
