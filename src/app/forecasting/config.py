"""Configuration of the Monte Carlo forecasting engine (``config/forecast.yaml``).

Everything here is a FICTIONAL modelling choice about how uncertain the fictional political model
is; the numbers describe the *simulation*, never real-world polling error.

The error structure defaults to the simulation model's own: every ``errors.*`` value left at
``null`` is taken from the scenario's ``environment.shocks`` (:class:`~app.scenarios.schema.ShockSpec`)
or from ``config/model.yaml`` (ideological swing share, party turnout shock, race-line shock), so a
forecast describes the same distribution that :func:`app.simulation.voting.simulate_election`
samples from.  ``errors.scale`` inflates (or shrinks) every party-utility error at once.

``block_size`` is the unit of randomness: draw block ``b`` is seeded with
``derive_seed(seed, "forecast", b)`` and always computed as one vectorised array, so the results
do not depend on ``chunk_size`` (how many draws a worker process handles per task) or on the
number of workers.  ``block_size`` therefore enters the configuration hash; ``chunk_size``,
``workers`` and ``parallel_min_simulations`` do not.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import load_config
from app.core.rng import config_hash

#: Default location of the forecast configuration (relative to ``config/``).
FORECAST_CONFIG_FILE = "forecast.yaml"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorConfig(_Model):
    """Standard deviations of the forecast's random components (logit points).

    ``None`` means "use the simulation model's value" (scenario ``environment.shocks`` or
    ``config/model.yaml``).  See docs/FORECASTING.md §3 for the correspondence.
    """

    #: Multiplier applied to every party-utility error SD (national, province, municipality,
    #: spatial, cell, race line).  1.0 = the simulation model's own uncertainty.
    scale: float = Field(1.0, ge=0.0, le=10.0)
    national_sd: float | None = Field(None, ge=0.0)
    tail_df: float | None = Field(None, gt=2.0)
    ideological_swing_share: float | None = Field(None, ge=0.0, le=1.0)
    province_sd: float | None = Field(None, ge=0.0)
    municipality_sd: float | None = Field(None, ge=0.0)
    spatial_sd: float | None = Field(None, ge=0.0)
    spatial_length_km: float | None = Field(None, gt=0.0)
    #: Neighbourhood (unit) noise; a cell of ``n_eff`` effective units gets ``unit_sd / √n_eff``.
    unit_sd: float | None = Field(None, ge=0.0)
    turnout_national_sd: float | None = Field(None, ge=0.0)
    turnout_local_sd: float | None = Field(None, ge=0.0)
    #: Unit turnout noise as a share of ``turnout_local_sd`` (model.yaml ``turnout.unit_shock_share``).
    turnout_unit_share: float | None = Field(None, ge=0.0)
    party_turnout_sd: float | None = Field(None, ge=0.0)
    race_line_sd: float | None = Field(None, ge=0.0)
    #: Draw the occurrence of probabilistic scenario events (``probability < 1``) in every draw.
    events: bool = True


class PollConfig(_Model):
    """How a poll-informed national shift (``polling.blend``) enters the forecast."""

    #: Multiplier of the poll shift (the blend is already precision-weighted; 1.0 applies it as is).
    weight: float = Field(1.0, ge=0.0, le=2.0)
    #: Multiplier of the national error when a poll shift is supplied (1.0 = polls do not shrink
    #: the national uncertainty, because they share a systematic industry-wide error).
    national_sd_scale: float = Field(1.0, ge=0.0, le=2.0)


class OutputConfig(_Model):
    """Size and resolution of the stored distributions."""

    top_combinations: int = Field(20, ge=0, le=500)
    top_compositions: int = Field(10, ge=0, le=500)
    #: Histogram bins over [0, 1] for race-line and municipality share quantiles (0.25 pp at 400).
    share_bins: int = Field(400, ge=20, le=5000)
    #: Bin width of the stored popular-vote share histograms (share units; 0.005 = 0.5 pp).
    pv_bin_width: float = Field(0.005, gt=0.0, le=0.1)
    municipality_distributions: bool = True


class ForecastConfig(_Model):
    """Validated ``config/forecast.yaml``."""

    version: int = 1
    description: str | None = None
    #: Default number of simulated elections.
    simulations: int = Field(10_000, ge=1)
    max_simulations: int = Field(1_000_000, ge=1)
    #: Draws per RNG block (the unit of randomness and of vectorisation; enters the config hash).
    block_size: int = Field(500, ge=1, le=20_000)
    #: Draws per worker task (rounded to whole blocks; scheduling only).
    chunk_size: int = Field(2_000, ge=1)
    #: Worker processes: 0 = auto (``NLFED_FORECAST_WORKERS`` or the available CPUs), 1 = in-process.
    workers: int = Field(0, ge=0, le=256)
    #: With ``workers = 0`` smaller runs stay in-process (process start-up would dominate).
    parallel_min_simulations: int = Field(20_000, ge=0)
    errors: ErrorConfig = Field(default_factory=ErrorConfig)
    polls: PollConfig = Field(default_factory=PollConfig)
    outputs: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _check(self) -> ForecastConfig:
        if self.simulations > self.max_simulations:
            raise ValueError("simulations exceeds max_simulations")
        return self

    def result_affecting(self) -> dict:
        """The part of the configuration that changes forecast numbers (hashed with every run)."""
        return self.model_dump(
            mode="json",
            exclude={"description", "simulations", "chunk_size", "workers", "parallel_min_simulations"},
        )

    @property
    def hash(self) -> str:
        """Stable 16-hex-digit hash of :meth:`result_affecting`."""
        return config_hash(json.dumps(self.result_affecting(), sort_keys=True))


def load_forecast_config(name: str | Path = FORECAST_CONFIG_FILE) -> ForecastConfig:
    """Load and validate ``config/forecast.yaml`` (schema defaults when the file is absent)."""
    return load_config(name, ForecastConfig, optional=True)
