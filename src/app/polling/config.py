"""Schema of ``config/polling.yaml``: fictional pollsters, aggregation and generation settings.

Pollster ratings are *subjective* judgements, therefore they are DATA (editable YAML / database
rows), never code.  Every pollster is invented; the schema rejects names of real polling firms.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import load_config
from app.polling.types import POLL_TYPES, POPULATIONS

#: Real polling organisations (Dutch and international).  Matched as whole words, case-insensitive.
REAL_POLLSTER_DENYLIST: tuple[str, ...] = (
    "peil.nl",
    "maurice de hond",
    "de hond",
    "ipsos",
    "i&o",
    "i&o research",
    "verian",
    "kantar",
    "tns nipo",
    "nipo",
    "eenvandaag",
    "motivaction",
    "synovate",
    "gallup",
    "yougov",
    "nielsen",
    "rasmussen",
    "quinnipiac",
    "marist",
    "monmouth",
    "emerson",
    "siena",
    "survation",
    "opinium",
    "infratest",
    "forsa",
    "ifop",
    "elabe",
)

_DENY_PATTERNS = [
    re.compile(r"(?<![\w&.])" + re.escape(n) + r"(?![\w&])", re.IGNORECASE) for n in REAL_POLLSTER_DENYLIST
]


def looks_like_real_pollster(name: str) -> bool:
    """True when ``name`` contains the name of a real polling organisation."""
    return any(p.search(name) for p in _DENY_PATTERNS)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PollsterConfig(_Model):
    """A FICTIONAL pollster.  Compatible with :class:`app.scenarios.schema.PollsterSpec`."""

    name: str = Field(..., min_length=2, max_length=120)
    #: Aggregation weight multiplier (subjective; 1.0 = average; 0 = excluded from averages).
    rating: float = Field(1.0, ge=0, le=5)
    rating_label: str | None = Field(None, max_length=8)
    #: online | phone | mixed | panel | ivr
    method: str = "online"
    typical_sample: int = Field(1500, ge=50, le=100_000)
    #: Prior house effect per party code, in percentage points (+ = overstates the party).
    house_effects: dict[str, float] = Field(default_factory=dict)
    #: Relative share of all polls fielded by this pollster (generation only).
    activity: float = Field(1.0, ge=0)
    #: Poll types this pollster fields (None = all types).
    poll_types: list[str] | None = None
    #: Population the pollster usually screens for.
    population: str = "LV"
    #: Systematic offset of the reported undecided share (percentage points).
    undecided_offset_pp: float = 0.0
    description: str | None = None
    fictional: Literal[True] = True

    @field_validator("name")
    @classmethod
    def _fictional_name(cls, v: str) -> str:
        if looks_like_real_pollster(v):
            raise ValueError(
                f"pollster name {v!r} resembles a real polling organisation; use an invented name"
            )
        return v

    @field_validator("population")
    @classmethod
    def _population(cls, v: str) -> str:
        if v not in POPULATIONS:
            raise ValueError(f"population must be one of {POPULATIONS}")
        return v

    @field_validator("poll_types")
    @classmethod
    def _poll_types(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            bad = [t for t in v if t not in POLL_TYPES]
            if bad:
                raise ValueError(f"unknown poll types {bad}")
        return v


class HouseEffectSettings(_Model):
    """House-effect adjustment of the aggregate."""

    #: Adjust polls for house effects at all.
    enabled: bool = True
    #: Estimate effects from the data (iterated residuals shrunk toward the prior); when False the
    #: configured prior effects are applied as fixed adjustments.
    estimate: bool = True
    #: Prior strength in "polls": estimate = (n·raw + shrinkage·prior) / (n + shrinkage).
    shrinkage: float = Field(4.0, ge=0)
    #: Pollsters with fewer polls (for a key within a group) keep their prior.
    min_pollster_polls: int = Field(2, ge=1)
    max_iterations: int = Field(300, ge=1)
    tolerance: float = Field(1e-6, gt=0)
    #: Estimated effects are clamped to ± this many percentage points.
    max_abs_pp: float = Field(8.0, gt=0)


class AggregationConfig(_Model):
    """Poll-average settings (see docs/POLLING.md)."""

    #: Recency weight = 0.5 ** (age_days / half_life) with age measured from the fieldwork end date.
    recency_half_life_days: float = Field(14.0, gt=0)
    #: Polls whose fieldwork ended more than this many days before ``as_of`` are ignored (None = keep all).
    max_age_days: float | None = Field(150.0, gt=0)
    #: Sample-size weight = (min(n, cap) / cap) ** exponent.
    sample_size_cap: int = Field(3000, ge=1)
    sample_size_exponent: float = Field(0.5, ge=0, le=1)
    #: Weight polls by pollster rating (per-poll ``quality_rating`` overrides the pollster rating).
    rating_weighting: bool = True
    rating_exponent: float = Field(1.0, ge=0)
    #: Rating of pollsters that are not configured.
    default_rating: float = Field(0.8, gt=0)
    population_weights: dict[str, float] = Field(default_factory=lambda: {"LV": 1.0, "RV": 0.85, "A": 0.7})
    default_population_weight: float = Field(0.7, gt=0)
    method_weights: dict[str, float] = Field(
        default_factory=lambda: {"phone": 1.0, "mixed": 1.0, "panel": 0.95, "online": 0.95, "ivr": 0.85}
    )
    default_method_weight: float = Field(0.9, gt=0)
    #: Discount for pollsters flooding the average: weight / (recency-weighted poll count) ** exponent.
    pollster_volume_exponent: float = Field(0.5, ge=0, le=1)
    house_effects: HouseEffectSettings = Field(default_factory=HouseEffectSettings)
    #: Gaussian-kernel bandwidth (days) of the local-linear trend line.
    trend_bandwidth_days: float = Field(10.0, gt=0)
    #: Ridge penalty on the local slope (relative), stabilises the trend at the edges.
    trend_slope_ridge: float = Field(0.1, ge=0)
    #: Longest trend series (days before ``as_of``).
    trend_max_days: int = Field(365, ge=1)
    #: proportional: rescale options by 100 / (100 − undecided); normalize: rescale to sum 100;
    #: keep: use reported values as they are.
    undecided: Literal["proportional", "normalize", "keep"] = "proportional"
    undecided_exempt_poll_types: list[str] = Field(default_factory=lambda: ["favorability"])
    #: Groups with fewer polls are omitted from the output.
    min_polls: int = Field(1, ge=1)
    #: Central probability of the reported interval (0.90 → 5th–95th percentile).
    interval_level: float = Field(0.90, gt=0, lt=1)
    #: Multiplier on the binomial sampling variance (weighting inflates variance).
    design_effect: float = Field(1.0, ge=1.0)
    #: Floor of the per-poll non-sampling SD (percentage points).
    nonsampling_sd_pp: float = Field(1.0, ge=0)
    #: Industry-wide (correlated) polling error that averaging cannot remove, SD in percentage
    #: points at a 50 % share (scaled by 2·sqrt(p(1−p))).  Reported as ``PollAverage.total_se``.
    systematic_error_sd_pp: float = Field(1.5, ge=0)


class GenerationConfig(_Model):
    """Fictional poll generation settings."""

    #: Campaign length when the scenario sets no ``polling.start_date``.
    default_campaign_days: int = Field(120, ge=14)
    fieldwork_days_min: int = Field(2, ge=1)
    fieldwork_days_max: int = Field(5, ge=1)
    #: End dates are election_date − 1 − Beta(1, late_bias) × span: > 1 concentrates polls late.
    late_bias: float = Field(1.8, gt=0)
    #: Daily SD (logit) of the per-key opinion random walk shared by every poll group.
    drift_sd_per_day: float = Field(0.007, ge=0)
    #: Daily SD (logit) of the group-specific random walk.
    local_drift_sd_per_day: float = Field(0.003, ge=0)
    #: SD (logit) of the opinion offset at campaign start (the far end of the bridge).
    start_offset_sd: float = Field(0.08, ge=0)
    #: Province-level polling error SD as a fraction of ``true_polling_error_sd``.
    geo_error_fraction: float = Field(0.5, ge=0)
    #: SD (pp) of the realised true house effect around the configured prior.
    house_effect_jitter_pp: float = Field(0.4, ge=0)
    undecided_start_pct: float = Field(14.0, ge=0, lt=90)
    undecided_end_pct: float = Field(5.0, ge=0, lt=90)
    undecided_curve: float = Field(1.0, gt=0)
    undecided_sd_pct: float = Field(1.5, ge=0)
    undecided_population_offset: dict[str, float] = Field(
        default_factory=lambda: {"LV": -1.0, "RV": 0.5, "A": 3.0}
    )
    #: Probability mix of screened populations; blended 50/50 with the pollster default.
    population_mix: dict[str, float] = Field(default_factory=lambda: {"LV": 0.65, "RV": 0.25, "A": 0.10})
    #: Sample size factor per poll type relative to the pollster's typical sample.
    sample_factor: dict[str, float] = Field(
        default_factory=lambda: {
            "national_president": 1.0,
            "generic_house": 1.0,
            "favorability": 1.0,
            "province_president": 0.55,
            "senate": 0.5,
            "governor": 0.5,
            "house_district": 0.35,
        }
    )
    sample_sd: float = Field(0.25, ge=0)
    min_sample: int = Field(300, ge=30)
    #: Design effect per method (inflates sampling noise and the margin of error).
    design_effect: dict[str, float] = Field(
        default_factory=lambda: {"phone": 1.2, "mixed": 1.25, "panel": 1.3, "online": 1.4, "ivr": 1.5}
    )
    default_design_effect: float = Field(1.3, ge=1.0)
    #: Geo selection weight = 1 + boost × competitiveness ** power.
    competitiveness_boost: float = Field(8.0, ge=0)
    competitiveness_power: float = Field(2.0, gt=0)
    #: Top-two margin (share) at which derived competitiveness reaches 0.
    tossup_margin_scale: float = Field(0.20, gt=0)
    report_decimals: int = Field(1, ge=0, le=3)

    @model_validator(mode="after")
    def _check(self) -> GenerationConfig:
        if self.fieldwork_days_max < self.fieldwork_days_min:
            raise ValueError("fieldwork_days_max must be >= fieldwork_days_min")
        if any(k not in POPULATIONS for k in self.population_mix):
            raise ValueError(f"population_mix keys must be in {POPULATIONS}")
        if sum(self.population_mix.values()) <= 0:
            raise ValueError("population_mix must have positive mass")
        return self


class PollingConfig(_Model):
    """Root of ``config/polling.yaml``."""

    pollsters: list[PollsterConfig] = Field(default_factory=list)
    aggregation: AggregationConfig = Field(default_factory=AggregationConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)

    @model_validator(mode="after")
    def _unique(self) -> PollingConfig:
        names = [p.name for p in self.pollsters]
        if len(set(names)) != len(names):
            raise ValueError("duplicate pollster names")
        return self

    def pollster(self, name: str) -> PollsterConfig:
        for p in self.pollsters:
            if p.name == name:
                return p
        raise KeyError(name)


def load_polling_config(name: str | Path = "polling.yaml") -> PollingConfig:
    """Load and validate ``config/polling.yaml`` (defaults when the file is absent)."""
    return load_config(name, PollingConfig, optional=True)


def resolve_pollsters(scenario_pollsters: list | None, config: PollingConfig | None = None) -> list:
    """Scenario pollsters when the scenario lists any, else the configured pollsters.

    Scenario pollsters (``PollingSpec.pollsters``) must be fictional too.
    """
    if scenario_pollsters:
        for p in scenario_pollsters:
            if looks_like_real_pollster(p.name):
                raise ValueError(f"pollster name {p.name!r} resembles a real polling organisation")
        return list(scenario_pollsters)
    cfg = config or load_polling_config()
    return list(cfg.pollsters)
