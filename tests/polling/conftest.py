"""Fixtures for the polling tests (all polls are FICTIONAL / SIMULATED)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
from typing import Any

import pandas as pd
import pytest

from app.polling.config import AggregationConfig, PollingConfig, PollsterConfig, load_polling_config

FramesFactory = Callable[[Iterable[Mapping[str, Any]]], tuple[pd.DataFrame, pd.DataFrame]]


@pytest.fixture(scope="session")
def polling_config() -> PollingConfig:
    return load_polling_config()


@pytest.fixture()
def agg_config() -> AggregationConfig:
    """Aggregation defaults without the non-sampling floor (makes weight arithmetic exact)."""
    return AggregationConfig()


@pytest.fixture()
def test_pollsters() -> list[PollsterConfig]:
    return [
        PollsterConfig(name="Alpha Toetsing", rating=1.0, method="phone", typical_sample=1500),
        PollsterConfig(name="Beta Toetsing", rating=1.0, method="phone", typical_sample=1500),
        PollsterConfig(name="Gamma Toetsing", rating=1.0, method="phone", typical_sample=1500),
    ]


@pytest.fixture()
def make_frames() -> FramesFactory:
    """Build ``(polls_df, results_df)`` from dicts with poll fields and a ``results`` mapping."""

    def _make(rows: Iterable[Mapping[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame]:
        poll_rows, result_rows = [], []
        for i, row in enumerate(rows, start=1):
            end = row.get("end_date", date(2028, 11, 1))
            poll_rows.append(
                {
                    "poll_id": row.get("poll_id", i),
                    "pollster": row.get("pollster", "Alpha Toetsing"),
                    "poll_type": row.get("poll_type", "national_president"),
                    "geo_code": row.get("geo_code", "NL"),
                    "start_date": row.get("start_date", end - timedelta(days=2)),
                    "end_date": end,
                    "sample_size": row.get("sample_size", 1000),
                    "population": row.get("population", "LV"),
                    "method": row.get("method", "phone"),
                    "undecided_pct": row.get("undecided_pct"),
                    "quality_rating": row.get("quality_rating"),
                }
            )
            for key, value in row["results"].items():
                result_rows.append({"poll_id": row.get("poll_id", i), "key": key, "value_pct": value})
        return pd.DataFrame(poll_rows), pd.DataFrame(result_rows)

    return _make
