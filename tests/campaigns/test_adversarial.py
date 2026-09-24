"""Adversarial campaign tests: effects stay modest and uncertain, budgets hold under random
inputs, invalid allocations and scenario overrides are rejected (FICTIONAL campaigns)."""

from __future__ import annotations

import numpy as np
import pytest

from app.campaigns.config import CampaignConfig, config_from_spec
from app.campaigns.engine import combine_effects, plan_campaign, plan_summary, realize_effects, run_campaigns
from app.campaigns.types import Allocation, Target
from app.core.errors import ConfigError
from app.core.rng import make_rng
from app.scenarios.schema import CampaignSpec

STRATEGIES = ("battleground", "balanced", "base", "expansion")


def _random_targets(seed: int, n: int, level: str = "district") -> list[Target]:
    rng = make_rng(seed, "test", "targets")
    margins = rng.normal(0, 12, n)
    return [
        Target(
            level,
            f"T{i:03d}",
            float(rng.integers(1, 30)),
            float(np.exp(-0.5 * (m / 8) ** 2)),
            float(m),
            float(rng.uniform(5e4, 4e6)),
        )
        for i, m in enumerate(margins)
    ]


# --------------------------------------------------------------------------- scenario overrides
def test_scenario_cannot_lift_the_effect_bounds(campaign_config: CampaignConfig) -> None:
    # The scenario schema itself rejects out-of-range bounds ...
    from pydantic import ValidationError as PydanticValidationError

    for bad in ({"effect_cap": 2.0}, {"effect_cap": -0.01}, {"effect_uncertainty": -1.0}):
        with pytest.raises(PydanticValidationError):
            CampaignSpec(**bad)
    # ... and the engine re-validates even if an unvalidated spec slips through.
    with pytest.raises(ConfigError):
        config_from_spec(CampaignSpec.model_construct(effect_cap=2.0), campaign_config)
    with pytest.raises(ConfigError):
        config_from_spec(CampaignSpec.model_construct(effect_cap=-0.01), campaign_config)
    with pytest.raises(ConfigError):
        config_from_spec(CampaignSpec.model_construct(effect_uncertainty=-1.0), campaign_config)
    with pytest.raises(ConfigError):
        run_campaigns(
            CampaignSpec.model_construct(effect_cap=1.0, budgets={"PA": 1e6}),
            {"PA": _random_targets(1, 5)},
            1,
            campaign_config,
        )


def test_national_scope_actions_cannot_be_targeted(campaign_config: CampaignConfig) -> None:
    data = campaign_config.model_dump()
    data["actions"]["advertising"]["scope"] = "national"
    with pytest.raises(ValueError, match="national-scope"):
        CampaignConfig.model_validate(data)
    with pytest.raises(ValueError, match="national-scope"):
        realize_effects([Allocation("province", "NB", "debate_prep", 5.0, 3, "PA")], campaign_config, 1)


@pytest.mark.parametrize(
    "alloc",
    [
        Allocation("province", "NB", "advertising", float("nan"), 3, "PA"),
        Allocation("province", "NB", "advertising", float("inf"), 3, "PA"),
        Allocation("province", "NB", "advertising", -1.0, 3, "PA"),
        Allocation("province", "NB", "advertising", 1.0, 0, "PA"),
        Allocation("province", "NB", "advertising", 1.0, 11, "PA"),
        Allocation("galaxy", "X", "advertising", 1.0, 3, "PA"),
        Allocation("province", "NB", "advertising", 1.0, 3, "PA", population=float("nan")),
    ],
)
def test_invalid_allocations_are_rejected(alloc: Allocation, campaign_config: CampaignConfig) -> None:
    with pytest.raises(ValueError):
        realize_effects([alloc], campaign_config, 1, weeks=10)


# --------------------------------------------------------------------------- properties over random inputs
@pytest.mark.parametrize("seed", range(6))
def test_budget_and_caps_hold_for_random_campaigns(seed: int, campaign_config: CampaignConfig) -> None:
    rng = make_rng(seed, "test", "campaign-shape")
    targets = _random_targets(seed, int(rng.integers(1, 40)))
    weeks = int(rng.integers(1, 16))
    budget = float(rng.choice([0.01, 1.0, 50.0, 1e4]))
    strategy = STRATEGIES[seed % len(STRATEGIES)]
    allocs = plan_campaign("PA", budget, strategy, targets, weeks, campaign_config, seed)
    summary = plan_summary(allocs)
    assert summary["spent"] <= budget + summary["raised"] + 1e-9 * max(budget, 1.0)
    assert all(np.isfinite(a.amount) and a.amount > 0 and 1 <= a.week <= weeks for a in allocs)
    rally, visit = campaign_config.actions["rally"], campaign_config.actions["visit"]
    for week in range(1, weeks + 1):
        assert (
            sum(a.units or 0 for a in allocs if a.week == week and a.action == "rally")
            <= rally.max_units_per_week
        )
        assert (
            sum(a.units or 0 for a in allocs if a.week == week and a.action == "visit")
            <= visit.max_units_per_week
        )
    gotv_final = campaign_config.actions["gotv"].final_weeks
    assert all(weeks - a.week < gotv_final for a in allocs if a.action == "gotv")
    eff = realize_effects(allocs, campaign_config, seed, weeks)
    floor = campaign_config.backfire_floor
    for key, value in eff.persuasion.items():
        cap = campaign_config.level_cap(key[0])
        assert floor * cap - 1e-12 <= value <= cap + 1e-12
        assert 0.0 <= eff.expected_persuasion[key] <= cap + 1e-12
    for key, value in eff.turnout.items():
        cap = campaign_config.level_cap(key[0], "turnout")
        assert floor * cap - 1e-12 <= value <= cap + 1e-12
    combined = combine_effects([eff], campaign_config)
    for t in targets:
        total = combined.effect(t.level, t.code, "PA")
        assert floor * campaign_config.effect_cap - 1e-12 <= total <= campaign_config.effect_cap + 1e-12


def test_money_cannot_buy_votes_deterministically(campaign_config: CampaignConfig) -> None:
    """Even an unlimited budget in one target yields at most the cap, and the realised effect
    varies across seeds (no deterministic purchase of votes)."""
    target = [Target("province", "NB", 22, 1.0, 0.0, 2.6e6)]
    realised = []
    for seed in range(30):
        allocs = plan_campaign("PA", 1e9, "battleground", target, 10, campaign_config, seed)
        eff = realize_effects(allocs, campaign_config, seed, 10)
        realised.append(eff.persuasion[("province", "NB")])
    realised = np.array(realised)
    assert realised.max() <= campaign_config.effect_cap + 1e-12
    assert realised.std() > 0.1 * campaign_config.effect_cap
    assert (realised < 0.9 * campaign_config.effect_cap).mean() > 0.2


def test_effects_do_not_depend_on_allocation_order(province_targets, campaign_config) -> None:
    allocs = plan_campaign("PA", 80.0, "balanced", province_targets, 8, campaign_config, 5)
    a = realize_effects(allocs, campaign_config, 5, 8)
    b = realize_effects(list(reversed(allocs)), campaign_config, 5, 8)
    assert a.persuasion.keys() == b.persuasion.keys()
    for key, value in a.persuasion.items():
        assert b.persuasion[key] == pytest.approx(value, rel=1e-12, abs=1e-15)


def test_zero_budget_party_and_unknown_strategy_in_scenario(province_targets, campaign_config) -> None:
    spec = CampaignSpec(budgets={"PA": 0.0, "SAP": 10.0}, strategies={"SAP": "base"})
    run = run_campaigns(spec, {"PA": province_targets, "SAP": province_targets}, 2, campaign_config)
    assert run.allocations["PA"] == [] and run.effects["PA"].persuasion == {}
    assert run.allocations["SAP"]
    with pytest.raises(ValueError):
        run_campaigns(
            CampaignSpec(budgets={"PA": 5.0}, strategies={"PA": "carpet-bombing"}),
            {"PA": province_targets},
            2,
            campaign_config,
        )
