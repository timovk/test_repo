"""Campaign planning: budgets, strategies, timing and limits."""

from __future__ import annotations

from collections import defaultdict

import numpy as np
import pytest
from pydantic import ValidationError

from app.campaigns.config import ACTIONS, CampaignConfig, StrategyConfig, config_from_spec
from app.campaigns.engine import build_targets, competitiveness_from_margin, plan_campaign, plan_summary
from app.campaigns.types import Allocation, Target
from app.scenarios.schema import CampaignSpec

STRATEGIES = ("battleground", "balanced", "base", "expansion")


def test_config_file_is_valid(campaign_config: CampaignConfig) -> None:
    assert set(campaign_config.actions) == set(ACTIONS)
    assert set(STRATEGIES) <= set(campaign_config.strategies)
    assert campaign_config.effect_cap <= 0.08  # modest by construction
    assert campaign_config.backfire_floor <= 0
    assert campaign_config.race_levels["president"] == "province"
    assert campaign_config.race_levels["house"] == "district"
    assert campaign_config.race_levels["senate"] == "province"
    with pytest.raises(ValidationError):
        StrategyConfig(action_mix={"fundraising": 1.0})
    with pytest.raises(ValidationError):
        CampaignConfig(actions={}, strategies={})


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_budget_respected(strategy: str, province_targets, campaign_config) -> None:
    budget = 100.0
    allocs = plan_campaign("PA", budget, strategy, province_targets, 10, campaign_config, seed=3)
    summary = plan_summary(allocs)
    assert summary["spent"] <= budget + summary["raised"] + 1e-9
    assert summary["spent"] >= 0.98 * (budget + summary["raised"])  # (almost) everything is used
    assert all(a.amount > 0 for a in allocs)
    assert all(1 <= a.week <= 10 for a in allocs)
    assert all(a.party == "PA" for a in allocs)
    assert set(summary["by_action"]) <= set(ACTIONS)


def test_battleground_concentrates_on_close_high_value_targets(campaign_config) -> None:
    targets = [
        Target("province", "BIG_CLOSE", 30, 0.95, 0.5, 2e6),
        Target("province", "BIG_SAFE", 30, 0.05, 25.0, 2e6),
        Target("province", "SMALL_CLOSE", 5, 0.95, -0.5, 5e5),
        Target("province", "SMALL_SAFE", 5, 0.05, -25.0, 5e5),
    ]
    bg = plan_summary(plan_campaign("PA", 100.0, "battleground", targets, 8, campaign_config, 1))["by_target"]
    bal = plan_summary(plan_campaign("PA", 100.0, "balanced", targets, 8, campaign_config, 1))["by_target"]
    spend_bg = defaultdict(float, {k[1]: v for k, v in bg.items()})
    spend_bal = defaultdict(float, {k[1]: v for k, v in bal.items()})
    assert spend_bg["BIG_CLOSE"] > spend_bg["SMALL_CLOSE"] > spend_bg["BIG_SAFE"] >= spend_bg["SMALL_SAFE"]
    assert spend_bg["BIG_SAFE"] == 0.0  # below the competitiveness threshold
    assert spend_bal["BIG_SAFE"] > 0.0  # balanced still spreads resources
    share_bg = spend_bg["BIG_CLOSE"] / sum(spend_bg.values())
    share_bal = spend_bal["BIG_CLOSE"] / sum(spend_bal.values())
    assert share_bg > share_bal


def test_electoral_votes_drive_presidential_spending(province_targets, campaign_config) -> None:
    by_target = plan_summary(
        plan_campaign("PA", 100.0, "battleground", province_targets, 10, campaign_config, 7)
    )["by_target"]
    spend = {k[1]: v for k, v in by_target.items() if k[0] == "province"}
    # ZH (33 EV, +1.5) and GE (20 EV, +0.5) outrank FL (5 EV, −1) despite similar closeness
    assert spend["ZH"] > spend["FL"] and spend["GE"] > spend["FL"]
    # safe provinces (|margin| ≥ 15) get nothing
    assert all(code not in spend for code in ("FR", "DR", "ZE"))


def test_base_and_expansion_pick_different_targets(province_targets, campaign_config) -> None:
    margin = {t.code: t.expected_margin_pp for t in province_targets}

    def weighted_margin(strategy: str) -> float:
        by_target = plan_summary(
            plan_campaign("PA", 100.0, strategy, province_targets, 10, campaign_config, 2)
        )["by_target"]
        spend = {k[1]: v for k, v in by_target.items() if k[0] == "province"}
        return sum(margin[c] * v for c, v in spend.items()) / sum(spend.values())

    assert weighted_margin("base") > 5.0  # home turf
    assert weighted_margin("expansion") < -2.0  # lean-opponent territory
    base = plan_summary(plan_campaign("PA", 100.0, "base", province_targets, 10, campaign_config, 2))[
        "by_action"
    ]
    battle = plan_summary(
        plan_campaign("PA", 100.0, "battleground", province_targets, 10, campaign_config, 2)
    )["by_action"]
    turnout_share = lambda s: (s.get("field", 0) + s.get("gotv", 0)) / sum(s.values())  # noqa: E731
    assert turnout_share(base) > turnout_share(battle)


def test_timing_and_limits(province_targets, campaign_config) -> None:
    weeks = 10
    allocs = plan_campaign("PA", 150.0, "battleground", province_targets, weeks, campaign_config, 5)
    gotv_final = campaign_config.actions["gotv"].final_weeks
    assert all(a.week > weeks - gotv_final for a in allocs if a.action == "gotv")
    assert any(a.action == "gotv" for a in allocs)
    for action in ("visit", "rally"):
        prof = campaign_config.actions[action]
        per_week: dict[int, int] = defaultdict(int)
        for a in allocs:
            if a.action == action:
                assert a.units is not None and 1 <= a.units <= prof.max_units_per_target_week
                assert a.amount == pytest.approx(a.units * prof.unit_cost)
                per_week[a.week] += a.units
        assert per_week and max(per_week.values()) <= prof.max_units_per_week
    for a in allocs:
        if a.action in ("debate_prep", "fundraising"):
            assert (a.level, a.code) == ("national", "NL")
    # spending ramps up toward election day
    by_week = plan_summary(allocs)["by_week"]
    assert by_week[weeks] > by_week[1]


def test_fundraising_increases_later_budget(province_targets, campaign_config) -> None:
    allocs = plan_campaign("PA", 100.0, "battleground", province_targets, 10, campaign_config, 9)
    fund = [a for a in allocs if a.action == "fundraising"]
    lag = campaign_config.actions["fundraising"].lag_weeks
    assert fund and all(a.proceeds > 0 for a in fund)
    assert all(a.week + lag <= 10 for a in fund)
    no_fund_strat = campaign_config.strategies["battleground"].model_copy(update={"fundraising_share": 0.0})
    cfg2 = campaign_config.model_copy(
        update={"strategies": {**campaign_config.strategies, "battleground": no_fund_strat}}
    )
    plain = plan_campaign("PA", 100.0, "battleground", province_targets, 10, cfg2, 9)
    with_fund = plan_summary(allocs)
    without = plan_summary(plain)
    assert with_fund["raised"] > sum(a.amount for a in fund)  # expected return > 1
    assert with_fund["by_week"][10] > without["by_week"][10]
    first = min(a.week for a in fund)
    non_fund_first = sum(a.amount for a in allocs if a.week == first and a.action != "fundraising")
    assert non_fund_first < without["by_week"][first]
    assert without["raised"] == 0.0


def test_deterministic_and_seeded(province_targets, campaign_config) -> None:
    a = plan_campaign("PA", 100.0, "balanced", province_targets, 10, campaign_config, 11)
    b = plan_campaign("PA", 100.0, "balanced", province_targets, 10, campaign_config, 11)
    c = plan_campaign("PA", 100.0, "balanced", province_targets, 10, campaign_config, 12)
    assert a == b
    assert a != c
    # different parties draw different planning noise
    d = plan_campaign("SAP", 100.0, "balanced", province_targets, 10, campaign_config, 11)
    assert [x.amount for x in d] != [x.amount for x in a]


def test_edge_cases(province_targets, campaign_config) -> None:
    assert plan_campaign("PA", 0.0, "balanced", province_targets, 10, campaign_config, 1) == []
    with pytest.raises(ValueError):
        plan_campaign("PA", 10.0, "carpet-bombing", province_targets, 10, campaign_config, 1)
    with pytest.raises(ValueError):
        plan_campaign("PA", -1.0, "balanced", province_targets, 10, campaign_config, 1)
    with pytest.raises(ValueError):
        plan_campaign("PA", 10.0, "balanced", province_targets, 0, campaign_config, 1)
    with pytest.raises(ValueError):
        plan_campaign("PA", 10.0, "balanced", province_targets + province_targets[:1], 10, campaign_config, 1)
    # no targets: everything goes to national actions
    national = plan_campaign("PA", 10.0, "battleground", [], 4, campaign_config, 1)
    assert national and all((a.level, a.code) == ("national", "NL") for a in national)
    assert plan_summary(national)["spent"] <= 10.0 + plan_summary(national)["raised"] + 1e-9
    # one week: no fundraising (money would arrive after the election)
    one = plan_campaign("PA", 10.0, "battleground", province_targets, 1, campaign_config, 1)
    assert not any(a.action == "fundraising" for a in one)


def test_strategy_fallback_when_no_target_qualifies(campaign_config) -> None:
    safe = [Target("district", f"NB-{i:02d}", 1, 0.01, 30.0, 120e3) for i in range(1, 4)]
    allocs = plan_campaign("PA", 20.0, "battleground", safe, 6, campaign_config, 4)
    assert any(a.level == "district" for a in allocs)  # balanced fallback chooses targets


def test_targets_validation_and_helpers(campaign_config) -> None:
    with pytest.raises(ValueError):
        Target("galaxy", "X", 1, 0.5, 0.0, 1.0)
    with pytest.raises(ValueError):
        Target("province", "NB", 1, 1.5, 0.0, 1.0)
    with pytest.raises(ValueError):
        Target("province", "NB", -1, 0.5, 0.0, 1.0)
    assert competitiveness_from_margin(0.0) == 1.0
    assert competitiveness_from_margin(8.0, 8.0) == pytest.approx(np.exp(-0.5))
    tg = build_targets(
        "district", ["NB-01", "NB-02"], [1, 1], [0.0, 20.0], [1e5, 1e5], config=campaign_config
    )
    assert tg[0].competitiveness == 1.0 and tg[1].competitiveness < 0.1
    tg2 = build_targets("district", ["NB-01"], [1], [3.0], [1e5], competitiveness=[0.7])
    assert tg2[0].competitiveness == 0.7
    with pytest.raises(ValueError):
        build_targets("district", ["NB-01"], [1, 2], [3.0], [1e5], config=campaign_config)


def test_config_from_spec(campaign_config) -> None:
    spec = CampaignSpec(effect_cap=0.04, effect_uncertainty=0.3)
    cfg = config_from_spec(spec, campaign_config)
    assert cfg.effect_cap == 0.04 and cfg.effect_uncertainty == 0.3
    assert campaign_config.effect_cap == 0.06  # base untouched


def test_allocation_is_hashable_value_object() -> None:
    a = Allocation("province", "NB", "rally", 0.6, 3, "PA", 1)
    assert a == Allocation("province", "NB", "rally", 0.6, 3, "PA", 1)
    assert a.key == ("province", "NB")
    assert len({a, Allocation("province", "NB", "rally", 0.6, 3, "PA", 1)}) == 1
