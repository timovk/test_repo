"""Campaign effects: modest (capped), diminishing, uncertain, seeded; combination."""

from __future__ import annotations

import numpy as np
import pytest

from app.campaigns.config import CampaignConfig
from app.campaigns.engine import combine_effects, plan_campaign, realize_effects, run_campaigns
from app.campaigns.types import Allocation, Target
from app.scenarios.schema import CampaignSpec


def _alloc(
    amount: float,
    action: str = "advertising",
    week: int = 10,
    code: str = "NB",
    pop: float = 1.5e6,
    party: str = "PA",
):
    return Allocation("province", code, action, amount, week, party, population=pop)


def test_effects_bounded_even_for_huge_budgets(province_targets, campaign_config: CampaignConfig) -> None:
    cap, tcap = campaign_config.effect_cap, campaign_config.turnout_cap
    floor = campaign_config.backfire_floor
    for seed in range(20):
        allocs = plan_campaign("PA", 10_000.0, "balanced", province_targets, 10, campaign_config, seed)
        eff = realize_effects(allocs, campaign_config, seed, 10)
        for (level, _), v in eff.persuasion.items():
            c = campaign_config.level_cap(level)
            assert floor * c - 1e-12 <= v <= c + 1e-12
        for v in eff.turnout.values():
            assert floor * tcap - 1e-12 <= v <= tcap + 1e-12
        assert max(eff.expected_persuasion.values()) <= cap
    # ≈ 1.5 percentage points at most near a 50 % share
    assert cap * 0.25 * 100 <= 1.6


def test_diminishing_returns(campaign_config: CampaignConfig) -> None:
    spends = [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 40.0]
    expected = [
        realize_effects([_alloc(s)], campaign_config, 0).expected_persuasion[("province", "NB")]
        for s in spends
    ]
    gains = np.diff([0.0, *expected[:-1]])  # equal 2-unit steps
    assert all(np.diff(expected) > 0)  # more money helps …
    assert all(gains[i + 1] < gains[i] for i in range(len(gains) - 1))  # … by less and less
    assert expected[1] < 2 * expected[0]
    assert expected[-1] < campaign_config.effect_cap
    # the saturating form cap × (1 − exp(−potency × spend / scale))
    prof = campaign_config.actions["advertising"]
    assert expected[0] == pytest.approx(
        campaign_config.effect_cap * (1 - np.exp(-prof.persuasion * spends[0] / prof.scale))
    )


def test_seeded_and_uncertain(campaign_config: CampaignConfig) -> None:
    allocs = [_alloc(6.0), _alloc(3.0, "field"), _alloc(0.6, "rally")]
    a = realize_effects(allocs, campaign_config, 5)
    b = realize_effects(allocs, campaign_config, 5)
    assert a.persuasion == b.persuasion and a.turnout == b.turnout
    draws = np.array(
        [realize_effects(allocs, campaign_config, s).persuasion[("province", "NB")] for s in range(400)]
    )
    exp = a.expected_persuasion[("province", "NB")]
    assert len(set(draws.round(12))) > 300  # no deterministic purchase of votes
    assert draws.mean() == pytest.approx(exp, rel=0.1)
    rel_sd = draws.std() / exp
    assert 0.25 < rel_sd < 0.8


def test_backfire_is_rare_and_slight(campaign_config: CampaignConfig) -> None:
    # debate preparation is the most uncertain action
    allocs = [Allocation("national", "NL", "debate_prep", 3.0, 10, "PA")]
    draws = np.array(
        [realize_effects(allocs, campaign_config, s).persuasion[("national", "NL")] for s in range(1000)]
    )
    cap = campaign_config.level_cap("national")
    backfires = (draws < 0).mean()
    assert 0.0 < backfires < 0.25
    assert draws.min() >= campaign_config.backfire_floor * cap - 1e-12
    assert (draws > 0).mean() > 0.75


def test_per_allocation_attribution_sums_to_target(province_targets, campaign_config: CampaignConfig) -> None:
    allocs = plan_campaign("PA", 100.0, "battleground", province_targets, 10, campaign_config, 8)
    eff = realize_effects(allocs, campaign_config, 8, 10)
    totals: dict = {}
    for rec in eff.allocations:
        k = rec.allocation.key
        totals.setdefault(k, [0.0, 0.0, 0.0, 0.0])
        totals[k][0] += rec.expected_persuasion
        totals[k][1] += rec.realized_persuasion
        totals[k][2] += rec.realized_turnout
        totals[k][3] += rec.expected_turnout
    for k, (ep, rp, rt, et) in totals.items():
        if k in eff.persuasion:
            assert ep == pytest.approx(eff.expected_persuasion[k])
            assert rp == pytest.approx(eff.persuasion[k])
            assert rt == pytest.approx(eff.turnout[k])
            assert et == pytest.approx(eff.expected_turnout[k])
    fund = [r for r in eff.allocations if r.allocation.action == "fundraising"]
    assert fund and all(r.expected_persuasion == 0 and r.expected_turnout == 0 for r in fund)
    records = eff.to_records()
    assert len(records) == len(allocs)
    assert {
        "target_level",
        "target_code",
        "action",
        "amount",
        "week",
        "expected_effect",
        "realized_effect",
        "turnout_effect",
    } <= set(records[0])
    assert eff.total_spend() == pytest.approx(sum(a.amount for a in allocs))
    assert eff.data_category == "SIMULATED"


def test_timing_population_and_action_profiles(campaign_config: CampaignConfig) -> None:
    key = ("province", "NB")
    late = realize_effects([_alloc(4.0, week=10)], campaign_config, 0, weeks=10).expected_persuasion[key]
    early = realize_effects([_alloc(4.0, week=1)], campaign_config, 0, weeks=10).expected_persuasion[key]
    assert early < late  # advertising decays before election day
    small = realize_effects([_alloc(4.0, pop=4e5)], campaign_config, 0).expected_persuasion[key]
    big = realize_effects([_alloc(4.0, pop=4e6)], campaign_config, 0).expected_persuasion[key]
    assert big < small  # reaching a larger electorate costs more
    gotv = realize_effects([_alloc(4.0, "gotv")], campaign_config, 0)
    assert gotv.expected_persuasion[key] == 0.0 and gotv.expected_turnout[key] > 0.0
    field = realize_effects([_alloc(4.0, "field")], campaign_config, 0)
    ads = realize_effects([_alloc(4.0, "advertising")], campaign_config, 0)
    assert field.expected_turnout[key] > ads.expected_turnout[key]
    assert ads.expected_persuasion[key] > field.expected_persuasion[key]


def test_realize_validation(campaign_config: CampaignConfig) -> None:
    empty = realize_effects([], campaign_config, 0)
    assert empty.persuasion == {} and empty.allocations == []
    with pytest.raises(ValueError):
        realize_effects([_alloc(1.0), _alloc(1.0, party="SAP")], campaign_config, 0)
    with pytest.raises(ValueError):
        realize_effects([_alloc(1.0, action="bribery")], campaign_config, 0)


def test_combine_effects(province_targets, campaign_config: CampaignConfig) -> None:
    pa = realize_effects(
        plan_campaign("PA", 100.0, "battleground", province_targets, 10, campaign_config, 1),
        campaign_config,
        1,
    )
    sap = realize_effects(
        plan_campaign("SAP", 80.0, "expansion", province_targets, 10, campaign_config, 1), campaign_config, 1
    )
    combined = combine_effects([pa, sap], campaign_config)
    assert combined.parties == ["PA", "SAP"]
    codes = [t.code for t in province_targets]
    mat = combined.matrix("province", codes, kind="persuasion")
    assert mat.shape == (len(codes), 2)
    assert np.all(mat <= campaign_config.effect_cap + 1e-12)
    zh = codes.index("ZH")
    national = pa.persuasion.get(("national", "NL"), 0.0)
    local = pa.persuasion.get(("province", "ZH"), 0.0)
    assert mat[zh, 0] == pytest.approx(
        np.clip(
            local + national,
            campaign_config.backfire_floor * campaign_config.effect_cap,
            campaign_config.effect_cap,
        )
    )
    assert combined.effect("province", "ZH", "PA", include_national=False) == pytest.approx(local)
    assert combined.effect("province", "XX", "RV") == 0.0
    tmat = combined.matrix("province", codes, parties=["SAP"], kind="turnout", include_national=False)
    assert tmat.shape == (len(codes), 1)
    # the same party twice (two campaigns) is summed and clipped to the cap
    twice = combine_effects([pa, pa], campaign_config)
    for key, by_party in twice.persuasion.items():
        cap = campaign_config.level_cap(key[0])
        assert by_party["PA"] == pytest.approx(min(2 * pa.persuasion[key], cap)) or pa.persuasion[key] < 0


def test_run_campaigns_from_scenario_spec(province_targets, campaign_config: CampaignConfig) -> None:
    spec = CampaignSpec(
        weeks=8, budgets={"PA": 100.0, "SAP": 90.0}, strategies={"PA": "battleground"}, effect_cap=0.05
    )
    targets = {"PA": province_targets, "SAP": province_targets}
    run = run_campaigns(spec, targets, seed=42, config=campaign_config)
    assert set(run.allocations) == {"PA", "SAP"}
    assert run.strategies == {"PA": "battleground", "SAP": campaign_config.default_strategy}
    assert run.combined is not None and set(run.combined.parties) == {"PA", "SAP"}
    assert max(v for d in run.combined.persuasion.values() for v in d.values()) <= 0.05 + 1e-12
    again = run_campaigns(spec, targets, seed=42, config=campaign_config)
    assert again.effects["PA"].persuasion == run.effects["PA"].persuasion
    off = run_campaigns(
        CampaignSpec(enabled=False, budgets={"PA": 100.0}), targets, seed=42, config=campaign_config
    )
    assert off.allocations == {} and off.combined is not None and off.combined.persuasion == {}


def test_house_campaign_targets_districts(campaign_config: CampaignConfig) -> None:
    districts = [
        Target("district", f"NB-{i:02d}", 1, c, m, 118_000)
        for i, (c, m) in enumerate([(0.9, 1.0), (0.2, 18.0), (0.7, -3.0)], 1)
    ]
    allocs = plan_campaign("PA", 5.0, "battleground", districts, 6, campaign_config, 3)
    eff = realize_effects(allocs, campaign_config, 3)
    assert ("district", "NB-01") in eff.persuasion
    assert ("district", "NB-02") not in eff.spend  # safe seat ignored by a battleground campaign
    assert all(abs(v) <= campaign_config.effect_cap for v in eff.persuasion.values())
