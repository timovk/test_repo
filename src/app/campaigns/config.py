"""Schema of ``config/campaigns.yaml``: campaign actions, strategies and the effect model.

Campaign effects are FICTIONAL model assumptions and deliberately modest (docs/CAMPAIGNS.md):
every effect saturates at a small cap (≈ 0.06 logit ≈ 1.5 pp) and is realised with seeded noise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import load_config, parse_config

#: Action identifiers (also the ``campaign_allocation.action`` column values).
ACTIONS: tuple[str, ...] = ("rally", "advertising", "field", "visit", "debate_prep", "fundraising", "gotv")

#: Target levels (``campaign_allocation.target_level``).
LEVELS: tuple[str, ...] = ("national", "province", "district", "municipality")

NATIONAL_CODE = "NL"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionProfile(_Model):
    """Cost/effect profile of one campaign action."""

    description: str = ""
    #: Relative persuasion and turnout potency per unit of (decayed) spend / scale.
    persuasion: float = Field(0.0, ge=0)
    turnout: float = Field(0.0, ge=0)
    #: local: contact in the target itself; broad: media-style reach (both are target-level
    #: actions; the difference in reach is modelled by ``population_elasticity``); national: only
    #: allocated to, and only valid at, the national target ``('national', 'NL')``.
    scope: Literal["local", "broad", "national"] = "local"
    #: Diminishing-returns scale: spend (abstract units) at which a target at the reference
    #: population reaches 1 − 1/e ≈ 63 % of the cap from this action alone.
    scale: float = Field(1.0, gt=0)
    #: Scale grows with (population / reference_population) ** population_elasticity.
    population_elasticity: float = Field(1.0, ge=0, le=1.5)
    #: Relative SD of the realised effect (multiplied by the global ``effect_uncertainty``).
    uncertainty_multiplier: float = Field(1.0, ge=0)
    #: Weekly retention of the effect until election day (1 = no decay).
    persistence: float = Field(1.0, gt=0, le=1)
    #: Discrete actions (rallies, candidate visits): cost per unit and weekly limits.
    unit_cost: float | None = Field(None, gt=0)
    max_units_per_week: int | None = Field(None, ge=0)
    max_units_per_target_week: int = Field(1, ge=1)
    #: Only allowed in the last N weeks (e.g. GOTV); None = any week.
    final_weeks: int | None = Field(None, ge=1)
    #: Fundraising: expected return per unit spent, lognormal SD, and weeks until money arrives.
    return_rate: float = Field(0.0, ge=0)
    return_sd: float = Field(0.0, ge=0)
    lag_weeks: int = Field(2, ge=1)

    @property
    def discrete(self) -> bool:
        return self.unit_cost is not None and self.max_units_per_week is not None


class StrategyConfig(_Model):
    """Target scoring and spending pattern of a strategy.

    ``score = value ** value_exponent × closeness × margin_window × noise`` with
    ``closeness = floor + (1 − floor) × competitiveness ** closeness_exponent`` (0 when
    competitiveness < ``min_competitiveness``) and an optional Gaussian window on the party's
    expected margin (pp) centred at ``margin_center_pp``.
    """

    description: str = ""
    value_exponent: float = Field(1.0, ge=0)
    closeness_exponent: float = Field(1.0, ge=0)
    closeness_floor: float = Field(0.0, ge=0, le=1)
    min_competitiveness: float = Field(0.0, ge=0, le=1)
    margin_center_pp: float | None = None
    margin_width_pp: float = Field(8.0, gt=0)
    max_target_share: float = Field(0.35, gt=0, le=1)
    min_target_share: float = Field(0.01, ge=0, lt=1)
    #: Split of the weekly target budget over target-level actions.
    action_mix: dict[str, float] = Field(default_factory=dict)
    #: Share of early-week budget invested in fundraising, and how many weeks it runs.
    fundraising_share: float = Field(0.05, ge=0, lt=1)
    fundraising_weeks: int = Field(3, ge=0)
    #: Share of every week's budget spent on national debate preparation.
    debate_prep_share: float = Field(0.02, ge=0, lt=1)
    #: Weekly budget ∝ week ** spend_ramp (1 = linear ramp toward election day).
    spend_ramp: float = Field(1.0, ge=0)
    #: Strategy used when no target scores positively.
    fallback: str | None = "balanced"

    @model_validator(mode="after")
    def _check(self) -> StrategyConfig:
        bad = [a for a in self.action_mix if a not in ACTIONS or a in ("fundraising", "debate_prep")]
        if bad:
            raise ValueError(f"action_mix may only contain target-level actions, got {bad}")
        if self.action_mix and sum(self.action_mix.values()) <= 0:
            raise ValueError("action_mix must have positive mass")
        return self


class CampaignConfig(_Model):
    """Root of ``config/campaigns.yaml``."""

    #: Hard cap of the persuasion effect (logit) any party can obtain in one target.
    effect_cap: float = Field(0.06, gt=0, le=0.25)
    #: Hard cap of the turnout effect (logit shift of the party's supporters' turnout).
    turnout_cap: float = Field(0.05, gt=0, le=0.25)
    #: Caps at the national level are this fraction of the target caps.
    national_cap_fraction: float = Field(0.5, gt=0, le=1)
    #: Global relative SD of realised effects (scaled per action by ``uncertainty_multiplier``).
    effect_uncertainty: float = Field(0.5, ge=0, le=3)
    #: Realised multiplier lower bound (negative ⇒ a slight backfire is possible).
    backfire_floor: float = Field(-0.3, ge=-1, le=0)
    #: Realised multiplier upper bound (the hard caps still apply).
    max_multiplier: float = Field(2.0, ge=1)
    #: Reference population per target level (scales are defined at these sizes).
    reference_population: dict[str, float] = Field(
        default_factory=lambda: {
            "national": 17_900_000,
            "province": 1_500_000,
            "district": 120_000,
            "municipality": 50_000,
        }
    )
    #: Multiplicative lognormal noise on target scores (campaigns are not perfect optimisers).
    planning_noise_sd: float = Field(0.1, ge=0)
    #: Allocations below this amount are dropped (money left unspent).
    min_allocation: float = Field(0.001, ge=0)
    #: Closeness scale (pp) for deriving competitiveness from an expected margin.
    closeness_scale_pp: float = Field(8.0, gt=0)
    default_strategy: str = "balanced"
    #: Target level per race family.
    race_levels: dict[str, str] = Field(
        default_factory=lambda: {
            "president": "province",
            "house": "district",
            "senate": "province",
            "governor": "municipality",
        }
    )
    actions: dict[str, ActionProfile] = Field(default_factory=dict)
    strategies: dict[str, StrategyConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> CampaignConfig:
        unknown = [a for a in self.actions if a not in ACTIONS]
        if unknown:
            raise ValueError(f"unknown actions {unknown}")
        missing = [a for a in ACTIONS if a not in self.actions]
        if missing:
            raise ValueError(f"missing action profiles {missing}")
        for name, strat in self.strategies.items():
            if strat.fallback is not None and strat.fallback not in self.strategies:
                raise ValueError(f"strategy {name}: unknown fallback {strat.fallback}")
            national = [a for a in strat.action_mix if self.actions[a].scope == "national"]
            if national:
                raise ValueError(
                    f"strategy {name}: national-scope actions {national} cannot be in action_mix"
                )
        if self.default_strategy not in self.strategies:
            raise ValueError(f"default_strategy {self.default_strategy!r} is not defined")
        bad_levels = [v for v in self.race_levels.values() if v not in LEVELS]
        if bad_levels:
            raise ValueError(f"unknown levels {bad_levels}")
        return self

    def level_cap(self, level: str, kind: Literal["persuasion", "turnout"] = "persuasion") -> float:
        cap = self.effect_cap if kind == "persuasion" else self.turnout_cap
        return cap * (self.national_cap_fraction if level == "national" else 1.0)

    def action_uncertainty(self, action: str) -> float:
        return self.effect_uncertainty * self.actions[action].uncertainty_multiplier


def load_campaign_config(name: str | Path = "campaigns.yaml") -> CampaignConfig:
    """Load and validate ``config/campaigns.yaml``."""
    return load_config(name, CampaignConfig)


def config_from_spec(spec: object, base: CampaignConfig | None = None) -> CampaignConfig:
    """Apply a scenario's :class:`~app.scenarios.schema.CampaignSpec` overrides (``effect_cap``,
    ``effect_uncertainty``) to ``base`` (default: ``config/campaigns.yaml``).

    The result is re-validated against :class:`CampaignConfig`, so a scenario cannot lift the
    hard bounds that keep campaign effects modest (e.g. ``effect_cap ≤ 0.25`` logit); invalid
    overrides raise :class:`~app.core.errors.ConfigError`.
    """
    cfg = base or load_campaign_config()
    update: dict[str, float] = {}
    cap = getattr(spec, "effect_cap", None)
    if cap is not None:
        update["effect_cap"] = float(cap)
    unc = getattr(spec, "effect_uncertainty", None)
    if unc is not None:
        update["effect_uncertainty"] = float(unc)
    if not update:
        return cfg
    return parse_config({**cfg.model_dump(), **update}, CampaignConfig, source="scenario campaigns block")
