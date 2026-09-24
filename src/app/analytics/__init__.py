"""Election analytics over the standard results frame (docs/ANALYTICS.md).

* :mod:`app.analytics.results` — the results-frame contract, party keys, race families, contest
  selection and party aggregation;
* :mod:`app.analytics.metrics` — margins, two-party share, swing, flips, turnout, lean,
  elasticity, uniform-swing projection, competitiveness, wasted votes / efficiency gap,
  seat–vote analysis, Electoral College efficiency, tipping point and EC bias;
* :mod:`app.analytics.history` — records, EC/PV divergence, series across elections,
  lineage-aware comparisons;
* :mod:`app.analytics.filters` — composable row filters.

Everything here is pure (pandas / NumPy in, DataFrames or frozen dataclasses out) and operates on
SIMULATED results.
"""

from app.analytics.results import (
    INDEPENDENT_KEY,
    LEVELS,
    RESULTS_COLUMNS,
    ResultsFrameError,
    build_results_frame,
    validate_results_frame,
)

__all__ = [
    "INDEPENDENT_KEY",
    "LEVELS",
    "RESULTS_COLUMNS",
    "ResultsFrameError",
    "build_results_frame",
    "validate_results_frame",
]
