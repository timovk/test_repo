"""Alias module: docs/ARCHITECTURE.md §6 names ``forecasting.montecarlo.run_forecast``.

The implementation lives in :mod:`app.forecasting.engine`; this module re-exports it so both
import paths work.
"""

from app.forecasting.engine import run_forecast
from app.forecasting.result import ForecastResult

__all__ = ["ForecastResult", "run_forecast"]
