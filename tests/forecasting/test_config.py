"""Forecast configuration: the shipped YAML validates and hashes only result-affecting fields."""

from __future__ import annotations

import pytest

from app.core.config import parse_config
from app.core.errors import ConfigError
from app.forecasting.config import ForecastConfig, load_forecast_config


def test_shipped_config_is_valid_and_defaults_to_model_errors() -> None:
    cfg = load_forecast_config()
    assert cfg.simulations == 10_000
    assert cfg.workers == 0
    assert cfg.block_size > 0 and cfg.chunk_size >= cfg.block_size
    e = cfg.errors
    # every SD inherits the simulation model's value unless overridden
    assert e.scale == 1.0
    assert all(
        getattr(e, name) is None
        for name in (
            "national_sd",
            "tail_df",
            "province_sd",
            "municipality_sd",
            "spatial_sd",
            "unit_sd",
            "turnout_national_sd",
            "turnout_local_sd",
            "race_line_sd",
        )
    )
    assert cfg.outputs.top_combinations > 0 and cfg.outputs.share_bins >= 20
    assert 0 <= cfg.polls.weight <= 2


def test_hash_ignores_scheduling_but_not_randomness_layout() -> None:
    cfg = load_forecast_config()
    same = cfg.model_copy(update={"workers": 3, "chunk_size": 12_345, "simulations": 5})
    assert same.hash == cfg.hash
    assert cfg.model_copy(update={"block_size": cfg.block_size + 1}).hash != cfg.hash
    scaled = cfg.model_copy(update={"errors": cfg.errors.model_copy(update={"scale": 1.5})})
    assert scaled.hash != cfg.hash
    assert len(cfg.hash) == 16


@pytest.mark.parametrize(
    "data",
    [
        {"simulations": 0},
        {"simulations": 10, "max_simulations": 5},
        {"block_size": 0},
        {"errors": {"national_sd": -0.1}},
        {"errors": {"tail_df": 2.0}},
        {"errors": {"unknown": 1}},
        {"polls": {"weight": 5}},
        {"outputs": {"share_bins": 5}},
    ],
)
def test_invalid_configs_are_rejected(data: dict) -> None:
    with pytest.raises(ConfigError):
        parse_config(data, ForecastConfig, source="test")
