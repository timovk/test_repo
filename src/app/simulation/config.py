"""Hyperparameters of the probabilistic political model (``config/model.yaml``).

Everything in this document is a FICTIONAL modelling assumption: it describes *how* the
simulated electorate behaves (how persistent local political character is distributed in space,
how turnout responds to REAL CBS demographics, how supporters of parties missing from a ballot
redistribute, how strongly candidates matter …).  Party-specific numbers live in the scenario
documents (``config/parties``, ``config/scenarios``); this file holds the model-wide ones.

    from app.simulation.config import load_model_config
    cfg = load_model_config()          # config/model.yaml (defaults when absent)

All utility-scale numbers are *logit points* of the multinomial-logit vote model
(docs/SIMULATION.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import load_config, parse_config
from app.core.constitution import RaceType
from app.geography.frame import DEMOGRAPHIC_VARIABLES

#: Default location of the model hyperparameters (relative to ``config/``).
MODEL_CONFIG_FILE = "model.yaml"

_RACE_TYPES = {rt.value for rt in RaceType}
_URBANITY_KEYS = {"0", "1", "2", "3", "4", "5"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_race_type_keys(v: dict[str, float]) -> dict[str, float]:
    bad = [k for k in v if k not in _RACE_TYPES]
    if bad:
        raise ValueError(f"unknown race types {bad}; expected keys from {sorted(_RACE_TYPES)}")
    return {str(k): float(x) for k, x in v.items()}


def _check_demo_keys(v: dict[str, float]) -> dict[str, float]:
    bad = [k for k in v if k not in DEMOGRAPHIC_VARIABLES]
    if bad:
        raise ValueError(
            f"unknown demographic variables {bad}; expected keys from {list(DEMOGRAPHIC_VARIABLES)}"
        )
    return {str(k): float(x) for k, x in v.items()}


def _check_urbanity_keys(v: dict[str, float]) -> dict[str, float]:
    bad = [k for k in v if str(k) not in _URBANITY_KEYS]
    if bad:
        raise ValueError(f"urbanity keys must be '0'..'5' (0 = unknown), got {bad}")
    return {str(k): float(x) for k, x in v.items()}


class LeanConfig(_Model):
    """Persistent (structural) local political character that demographics do not explain.

    Per party ``p`` and unit ``u``::

        lean[u,p] = spatial_sd·g_p(m(u)) + municipality_sd·ε_p(m(u)) + unit_sd·η_{u,p}

    ``g`` is a unit-variance Gaussian-process field over municipality centroids; a share
    ``ideological_share`` of its variance lies on the three shared ideology axes (so a place that
    leans left leans towards *all* left parties).  Drawn once from the scenario's
    ``political_geography_seed`` so municipalities keep their character across elections.
    """

    municipality_sd: float = Field(0.10, ge=0)
    unit_sd: float = Field(0.08, ge=0)
    spatial_sd: float = Field(0.12, ge=0)
    spatial_length_km: float = Field(35.0, gt=0)
    kernel: Literal["exponential", "matern32", "squared_exponential"] = "matern32"
    ideological_share: float = Field(0.5, ge=0, le=1)
    #: Imputed demographic values are shrunk towards the population mean by this factor (0–1)
    #: before entering the utility (they are DERIVED guesses, not observations).
    imputed_shrinkage: float = Field(0.5, ge=0, le=1)


class ElasticityConfig(_Model):
    """Persistent swinginess ``e_u`` (population-weighted mean 1) that scales national swings.

    ``log e_u ∝ entropy_weight·z(H_u) + suburban_weight·z(suburban_u) + noise_weight·ξ_m(u)``
    where ``H_u`` is the normalised entropy of the unit's baseline party shares (support is less
    entrenched where it is spread over many parties) and ``suburban_u`` scores CBS urbanity
    classes (mid-density suburbs swing most).  The result is rescaled to log-SD ``log_sd`` and
    raised to the scenario's ``environment.elasticity_strength``.
    """

    log_sd: float = Field(0.22, ge=0)
    entropy_weight: float = 0.6
    suburban_weight: float = 0.4
    noise_weight: float = 0.35
    suburban_by_class: dict[str, float] = Field(
        default_factory=lambda: {"0": 0.5, "1": 0.15, "2": 0.7, "3": 1.0, "4": 0.7, "5": 0.3}
    )
    min: float = Field(0.3, gt=0)
    max: float = Field(2.5, gt=0)

    _v_sub = field_validator("suburban_by_class")(_check_urbanity_keys)

    @model_validator(mode="after")
    def _bounds(self) -> ElasticityConfig:
        if self.min >= self.max:
            raise ValueError("elasticity.min must be < elasticity.max")
        return self


class TurnoutConfig(_Model):
    """Persistent turnout logit ``τ_u = τ0 + Σ_k γ_k z_uk + urbanity + local lean``.

    ``τ0`` is calibrated so that national expected turnout equals the scenario's
    ``environment.turnout_base``.  Supporters of party ``p`` turn out with probability
    ``σ(τ_u + turnout_propensity_p)``.
    """

    demographics: dict[str, float] = Field(
        default_factory=lambda: {
            "pct_age_65_plus": 0.10,
            "pct_age_15_25": -0.08,
            "pct_education_high": 0.22,
            "pct_education_low": -0.12,
            "income_per_capita_keur": 0.12,
            "pct_owner_occupied": 0.15,
            "pct_origin_non_europe": -0.25,
            "pct_single_households": -0.05,
        }
    )
    urbanity: dict[str, float] = Field(default_factory=dict)
    municipality_sd: float = Field(0.10, ge=0)
    unit_sd: float = Field(0.12, ge=0)
    #: Unit-level share of the election's local turnout shock SD (the rest is municipal).
    unit_shock_share: float = Field(0.6, ge=0, le=2)
    #: SD of the election-specific differential mobilisation of each party's supporters.
    party_turnout_shock_sd: float = Field(0.05, ge=0)
    min_probability: float = Field(0.05, gt=0, lt=1)
    max_probability: float = Field(0.985, gt=0, lt=1)
    #: Invalid-ballot rate multiplier per SD of low education (mean-normalised).
    invalid_low_education_effect: float = 0.15
    #: Blank-ballot rate multiplier per SD of the 65+ share (mean-normalised).
    blank_age_effect: float = 0.10

    _v_demo = field_validator("demographics")(_check_demo_keys)
    _v_urb = field_validator("urbanity")(_check_urbanity_keys)


class AffinityConfig(_Model):
    """Ideological affinity used for transfers between ballot lines.

    Support of a party that is absent from a ballot (or whose line was withdrawn) moves to the
    present lines with weights ``softmax(−d(p, ℓ)/temperature)``, where ``d`` is the weighted
    Euclidean distance in ideology space; ``absent_abstain_share`` of it abstains (undervote,
    counted as a blank ballot in that race — turnout is election-wide).
    """

    temperature: float = Field(0.35, gt=0)
    dimension_weights: dict[str, float] = Field(
        default_factory=lambda: {"economic": 1.0, "social": 1.0, "europe": 0.7}
    )
    absent_abstain_share: float = Field(0.12, ge=0, le=1)
    #: Extra distance for transfers towards an independent (no party brand).
    independent_extra_distance: float = Field(0.45, ge=0)

    @field_validator("dimension_weights")
    @classmethod
    def _dims(cls, v: dict[str, float]) -> dict[str, float]:
        bad = set(v) - {"economic", "social", "europe"}
        if bad:
            raise ValueError(f"unknown ideology dimensions {sorted(bad)}")
        return {k: float(v.get(k, 1.0)) for k in ("economic", "social", "europe")}


class CandidateEffectsConfig(_Model):
    """Race-specific candidate effects (logit points)."""

    quality_utility: dict[str, float] = Field(
        default_factory=lambda: {
            "PRESIDENT": 0.06,
            "PRESIDENT_PROVINCE": 0.06,
            "HOUSE": 0.08,
            "SENATE": 0.09,
            "GOVERNOR": 0.11,
            "MAYOR": 0.14,
            "PROVINCIAL_LEGISLATURE": 0.0,
            "MUNICIPAL_COUNCIL": 0.0,
        }
    )
    #: Weight of the running mate's quality relative to the top of the ticket.
    running_mate_quality_share: float = Field(0.25, ge=0, le=1)
    home_municipality_bonus: dict[str, float] = Field(
        default_factory=lambda: {
            "PRESIDENT": 0.20,
            "PRESIDENT_PROVINCE": 0.20,
            "HOUSE": 0.10,
            "SENATE": 0.12,
            "GOVERNOR": 0.12,
            "MAYOR": 0.0,
        }
    )
    #: Home-province bonus for non-presidential statewide/local races (presidential tickets use
    #: the scenario's ``president.home_province_bonus`` / ``vp_home_province_bonus``).
    home_province_bonus: dict[str, float] = Field(default_factory=lambda: {"SENATE": 0.0, "GOVERNOR": 0.0})
    running_mate_home_municipality_bonus: float = 0.08
    #: Baseline support mass of an independent line (relative to the party masses, which sum to 1).
    independent_base_share: float = Field(0.025, gt=0, lt=1)
    #: Per-race-type override of ``independent_base_share`` (local notables matter more locally).
    independent_base_share_by_race: dict[str, float] = Field(
        default_factory=lambda: {"MAYOR": 0.18, "MUNICIPAL_COUNCIL": 0.10}
    )
    independent_quality_utility: float = 0.35
    #: Share of a withdrawn line's support that still votes for it.
    withdrawn_residual_share: float = Field(0.06, ge=0, le=1)
    #: Small bonus for the party holding an open seat.
    open_seat_party_bonus: float = 0.02
    #: SD of the race-specific performance shock of each ballot line (drawn per race and line).
    race_line_sd: float = Field(0.04, ge=0)
    #: A House/Senate/Governor jurisdiction counts as lying in a region when at least this share
    #: of its eligible voters lives there (used by ContestRule.regions).
    region_overlap_threshold: float = Field(0.25, gt=0, le=1)
    #: Age ranges (years at election) of generated candidates by race type.
    age_range: dict[str, tuple[int, int]] = Field(
        default_factory=lambda: {
            "HOUSE": (27, 68),
            "SENATE": (38, 72),
            "GOVERNOR": (38, 70),
            "MAYOR": (32, 68),
            "PROVINCIAL_LEGISLATURE": (25, 70),
            "MUNICIPAL_COUNCIL": (22, 72),
        }
    )
    female_share: float = Field(0.46, ge=0, le=1)

    _v_q = field_validator("quality_utility")(_check_race_type_keys)
    _v_hm = field_validator("home_municipality_bonus")(_check_race_type_keys)
    _v_hp = field_validator("home_province_bonus")(_check_race_type_keys)
    _v_ib = field_validator("independent_base_share_by_race")(_check_race_type_keys)

    @field_validator("age_range")
    @classmethod
    def _ages(cls, v: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
        bad = [k for k in v if k not in _RACE_TYPES]
        if bad:
            raise ValueError(f"unknown race types {bad}")
        for k, (lo, hi) in v.items():
            if not 18 <= lo < hi <= 100:
                raise ValueError(f"invalid age range for {k}: {lo}..{hi}")
        return v


class StrategicConfig(_Model):
    """FPTP strategic voting (Duverger pressure).

    In single-winner plurality races, supporters of a line whose expected jurisdiction share is
    more than ``viability_gap_start`` behind the second-placed line start to defect, reaching the
    scenario's ``strategic_voting`` share at ``viability_gap_full``; they move to the lines ranked
    ahead of theirs by affinity (temperature ``temperature``) weighted by each target's perceived
    viability (1 within ``viability_gap_start`` of the second-placed line, falling to 0 over the
    same ramp), so expected shares stay continuous when lines swap places.
    """

    viability_gap_start: float = Field(0.04, ge=0)
    viability_gap_full: float = Field(0.20, gt=0)
    temperature: float = Field(0.30, gt=0)
    race_type_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "PRESIDENT": 1.0,
            "PRESIDENT_PROVINCE": 1.0,
            "HOUSE": 1.0,
            "SENATE": 1.0,
            "GOVERNOR": 1.0,
            "MAYOR": 0.8,
        }
    )
    viable_lines: int = Field(2, ge=1)

    _v_w = field_validator("race_type_weights")(_check_race_type_keys)

    @model_validator(mode="after")
    def _gaps(self) -> StrategicConfig:
        if self.viability_gap_full <= self.viability_gap_start:
            raise ValueError("viability_gap_full must exceed viability_gap_start")
        return self


class ShockModelConfig(_Model):
    """Structure of the election-specific shocks (their SDs come from the scenario)."""

    #: Share of the national shock variance carried by a common ideological swing (left/right,
    #: progressive/conservative, pro/anti-EU), so ideologically close parties co-move.
    ideological_swing_share: float = Field(0.35, ge=0, le=1)


class CalibrationConfig(_Model):
    """Numerical settings of the intercept / baseline calibration."""

    max_iterations: int = Field(400, ge=1)
    tolerance: float = Field(1e-7, gt=0)
    #: Skip the national intercept step when (almost) all eligible voters live in pinned areas.
    min_unpinned_share: float = Field(0.01, ge=0, le=1)


class ModelConfig(_Model):
    """Root of ``config/model.yaml``."""

    version: int = 1
    description: str | None = None
    lean: LeanConfig = Field(default_factory=LeanConfig)
    elasticity: ElasticityConfig = Field(default_factory=ElasticityConfig)
    turnout: TurnoutConfig = Field(default_factory=TurnoutConfig)
    affinity: AffinityConfig = Field(default_factory=AffinityConfig)
    candidates: CandidateEffectsConfig = Field(default_factory=CandidateEffectsConfig)
    strategic: StrategicConfig = Field(default_factory=StrategicConfig)
    shocks: ShockModelConfig = Field(default_factory=ShockModelConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)


def load_model_config(path: str | Path | None = None) -> ModelConfig:
    """Load and validate the model hyperparameters (defaults when ``config/model.yaml`` is absent)."""
    return load_config(path or MODEL_CONFIG_FILE, ModelConfig, optional=True)


def model_config_from_dict(data: dict) -> ModelConfig:
    """Validate an in-memory model configuration (raises :class:`~app.core.errors.ConfigError`)."""
    return parse_config(data, ModelConfig, source="<model config>")
