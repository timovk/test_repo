# Campaigns

Campaigns are **FICTIONAL** model assumptions and their effects are **SIMULATED**. The campaign
system does two things: it decides where and how a party spends its resources (`plan_campaign`),
and it turns that spending into small, uncertain shifts of the vote model (`realize_effects`).
The engines are pure NumPy (`app.campaigns.engine`), all randomness comes from
`app.core.rng.make_rng`, and every number lives in `config/campaigns.yaml`.

## 1. Resources

* **Budget.** Abstract resource units. 100 is a typical major-party presidential campaign; see
  `CampaignSpec.budgets` in the scenario.
* **Weeks.** Weeks are numbered 1 … `weeks`, and the last week ends on election day
  (`CampaignSpec.weeks`). The weekly budget ramps up toward election day, in proportion to
  `week ** spend_ramp`.
* **Candidate time.** Rallies and candidate visits are discrete units, limited per week
  (`max_units_per_week`) and per target per week (`max_units_per_target_week`).
* **Fundraising.** In the first `fundraising_weeks` weeks, a strategy spends
  `fundraising_share` of the weekly budget on fundraising. It returns
  `amount × return_rate × lognormal(return_sd)` (seeded, mean `return_rate`), and the money
  becomes available from `week + lag_weeks` onward, spread over the remaining weeks. Fundraising
  therefore increases the later-week budget. It never runs in weeks whose proceeds would arrive
  after the election.

The planner guarantees that `sum(amount) ≤ budget + sum(proceeds)`. `plan_summary()` reports
`spent`, `raised` and totals by action, week and target.

## 2. Targets and levels

A `Target(level, code, value, competitiveness, expected_margin_pp, population)` is a place to
spend money. Its fields are:

* `value`: what the target is worth. For presidential targets this is the province's
  **electoral votes**, so spending follows the Electoral College: a close 33-EV province
  outranks an equally close 5-EV province. For House districts it is 1 seat; for Senate
  provinces it is the seats up.
* `competitiveness`: 0 (safe) … 1 (toss-up). `build_targets` derives it from the expected
  margin as `exp(−½ (margin / closeness_scale_pp)²)` when it is not given.
* `expected_margin_pp`: the party's expected margin, where + means the party is ahead.
* `population`: scales the cost of reaching voters.

The level for each race family is set in `race_levels`:

| Race family | Target level |
|---|---|
| president | province |
| house | district |
| senate | province |
| governor | municipality |

National actions (debate preparation and fundraising) are allocated at `("national", "NL")`.

## 3. Actions

| action | persuasion | turnout | notes |
|---|---|---|---|
| `rally` | 0.5 | 0.5 | discrete (unit cost 0.6, ≤ 3 per week), local, high variance |
| `advertising` | 1.0 | 0.1 | broad reach, cost scales with population, decays fast (persistence 0.75 per week) |
| `field` | 0.3 | 1.0 | canvassing, mostly turnout, durable (0.95) |
| `visit` | 0.8 | 0.3 | discrete candidate visits (unit cost 0.3, ≤ 4 per week), local news, very uncertain |
| `debate_prep` | 1.0 | 0 | national only; the highest variance, because debates can go badly |
| `fundraising` | 0 | 0 | no direct effect; returns money later |
| `gotv` | 0 | 1.0 | get-out-the-vote, only in the final `final_weeks` (2) weeks |

Each action profile also sets these parameters:

* `scope`: `local` (contact in the target) or `broad` (media reach) for target-level actions;
  the difference in reach is modelled by `population_elasticity`. `national` actions (debate
  prep, fundraising) are only allocated at, and only valid at, `("national", "NL")`. The schema
  rejects a strategy whose `action_mix` contains a national-scope action.
* `scale`: the diminishing-returns scale;
* `population_elasticity`: how steeply the scale grows with population;
* `uncertainty_multiplier`: the action's multiple of the global `effect_uncertainty`;
* `persistence`: the fraction of the effect kept each week until election day.

## 4. Strategies

For each target the score is:

```
score = value^value_exponent
        × (closeness_floor + (1 − closeness_floor) × competitiveness^closeness_exponent)   [0 if < min_competitiveness]
        × exp(−½ ((margin − margin_center_pp) / margin_width_pp)²)                        [if margin_center_pp is set]
        × lognormal(planning_noise_sd)                                                     [seeded per party × target]
```

Scores are normalised to spending shares. Targets below `min_target_share` are dropped, and no
target gets more than `max_target_share`; the excess is redistributed. If no target qualifies,
the planner picks targets with the `fallback` strategy.

| strategy | idea | target choice | action mix |
|---|---|---|---|
| `battleground` | value × closeness | competitiveness^2.5, nothing below 0.25 competitiveness | ads, field, GOTV |
| `balanced` | spread by value, tilted to close races | floor 0.35 + 0.65 × competitiveness | ads and field |
| `base` | shore up home turf, turnout | margin window around +12 pp, sub-linear in value | field and GOTV heavy |
| `expansion` | reach into lean-opponent territory | margin window around −6 pp | ads heavy |

Each week follows the same sequence:

1. Fundraising runs, if it is scheduled that week.
2. `debate_prep_share` of the budget goes to national debate preparation.
3. Discrete units (rallies, visits) are assigned greedily by `share / (1 + units already assigned)`
   within their limits. Unspent money from those pools flows to the continuous actions.
4. The continuous actions (advertising, field, GOTV) are split across targets by their shares.
   GOTV's share goes to the other actions until its final weeks.

## 5. Effect model

For one party and one target:

```
input      = Σ_allocations  potency_a × amount × persistence_a^(weeks − week) / scale_a(population)
expected   = cap × (1 − exp(−input))                      # diminishing returns, never above cap
multiplier = clip(1 + u × z, backfire_floor, max_multiplier),   z ~ N(0, 1) seeded by (party, level, code)
realised   = clip(expected × multiplier, backfire_floor × cap, cap)
```

* `scale_a(population) = scale_a × (population / reference_population[level]) ^ population_elasticity`.
  The same money moves a large province less than a small one. An allocation without a positive
  population is scaled at the reference population.
* `u` is the spend-weighted uncertainty of the actions used in that target. The global value is
  `effect_uncertainty` (0.5), and each action applies its multiplier to it.
* Persuasion is a **logit shift of the party's utility** in that target (see docs/SIMULATION.md).
  Turnout is a **logit shift of the party's supporters' turnout**. Each has its own cap:
  `effect_cap` = 0.06 (about 1.5 pp near a 50 % share) and `turnout_cap` = 0.05. National
  effects are capped at `national_cap_fraction` × the cap.
* Every allocation is credited with its share of the target's input. `AllocationEffect` records
  the expected and realised persuasion and turnout, which map onto the
  `campaign_allocation.expected_effect`, `realized_effect` and `turnout_effect` columns
  (`CampaignEffects.to_records()`).

`realize_effects` rejects allocations that would silently corrupt the effects. It raises
`ValueError` for any of these:

* a non-finite or negative amount or population;
* a week outside `1 … weeks`;
* an unknown level or action;
* a national-scope action at a local target.

`combine_effects(effects)` merges all parties' `CampaignEffects`. Effects of the same party at the
same target (for example from several campaigns) are summed and clipped to the cap.
`CombinedCampaignEffects.effect(level, code, party)` and `.matrix(level, codes, parties)` add the
national effect to each target, clip the total to the cap, and return arrays ready for vectorised
consumers. `run_campaigns(spec, targets_by_party, seed)` plans, realises and combines the effects
of every party with a budget in a scenario's `CampaignSpec`. The scenario's `effect_cap` and
`effect_uncertainty` override the configuration (`config_from_spec`). The overridden
configuration is **re-validated**, so a scenario cannot lift the schema bounds: for example,
`effect_cap` must be at most 0.25 logit. An invalid override raises `ConfigError`.

## 6. Why effects are modest and uncertain

Campaign research consistently finds that persuasion effects of campaign contact and advertising
in general elections are small, short-lived and hard to predict. Turnout operations have somewhat
more reliable, but still small, effects. The model builds in four features to match:

* **Hard caps.** Money cannot buy more than about 1.5 points in any target, however large the
  budget, and the `1 − exp(−x)` form means each extra unit buys less than the previous one.
* **Decay.** Advertising in week 1 has mostly faded by election day. Late spending matters more,
  but GOTV only exists at the end.
* **Seeded noise.** The same plan can over- or under-perform. With a small probability (a few
  percent for most actions, more for debate prep) an effort **backfires slightly**, bounded at
  `backfire_floor × cap`. There is no deterministic purchase of votes.
* **Competition.** Both parties campaign in the same places. In a multinomial-logit model their
  utility shifts largely cancel, as they do in reality.

A scenario can still change the magnitudes (`effect_cap`, `effect_uncertainty`), but the
defaults keep campaigns a second-order influence next to structural partisanship, the national
environment and candidate quality.

## 7. API summary

```python
plan_campaign(party, budget, strategy, targets, weeks, config=None, seed=0) -> list[Allocation]
realize_effects(allocations, config=None, seed=0, weeks=None) -> CampaignEffects
combine_effects(effects, config=None) -> CombinedCampaignEffects
run_campaigns(spec, targets_by_party, seed, config=None) -> CampaignRun
build_targets(level, codes, values, margins_pp, populations, competitiveness=None, config=None) -> list[Target]
plan_summary(allocations) -> dict
load_campaign_config() / config_from_spec(spec, base=None) -> CampaignConfig
```

`Allocation(level, code, action, amount, week, party, units, proceeds, population)` is a frozen
dataclass that maps one-to-one onto the `campaign_allocation` table.

## 8. Limitations

* Advertising has no spill-over between neighbouring targets. Media markets are approximated by
  the target's own population.
* The planner is a transparent heuristic, not an optimiser. It does not react to the opponent's
  plan or to polls during the campaign.
* Candidate travel is modelled as a weekly unit limit, not as geography-aware routing.
* The effect noise is keyed by (party, level, code). Two campaigns of the same party in the same
  target (for example presidential and Senate, both at province level) therefore share one draw.
  `CombinedCampaignEffects.by_party` keeps the first `CampaignEffects` of each party.
* `effect_uncertainty: 0` is allowed for what-if analysis and makes the (still capped) effects
  deterministic.
