"""Campaign planning and (modest, uncertain) campaign effects.

* :func:`plan_campaign` turns a party's budget, strategy and targets into weekly
  :class:`Allocation` rows (rallies, advertising, field, candidate visits, debate prep,
  fundraising, GOTV).  Presidential campaigns target provinces weighted by electoral votes, so
  the Electoral College shapes where money goes; House campaigns target districts; Senate
  campaigns target provinces.
* :func:`realize_effects` converts allocations into logit effects with diminishing returns, a
  hard cap and seeded multiplicative noise (effects can occasionally backfire slightly).
* :func:`combine_effects` merges the effects of all parties for the simulation services.

Pure NumPy; all randomness from :func:`app.core.rng.make_rng`.  See docs/CAMPAIGNS.md.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.campaigns.config import (
    LEVELS,
    NATIONAL_CODE,
    CampaignConfig,
    StrategyConfig,
    config_from_spec,
    load_campaign_config,
)
from app.campaigns.types import (
    Allocation,
    AllocationEffect,
    CampaignEffects,
    CombinedCampaignEffects,
    Target,
    TargetKey,
)
from app.core.logging import get_logger
from app.core.rng import make_rng

log = get_logger(__name__)

_EPS = 1e-12


# --------------------------------------------------------------------------- targets
def competitiveness_from_margin(margin_pp: float | np.ndarray, scale_pp: float = 8.0) -> float | np.ndarray:
    """0 (safe) … 1 (toss-up): ``exp(−½ (margin / scale)²)``."""
    out = np.exp(-0.5 * (np.asarray(margin_pp, dtype=float) / scale_pp) ** 2)
    return float(out) if np.ndim(out) == 0 else out


def build_targets(
    level: str,
    codes: Sequence[str],
    values: Sequence[float],
    margins_pp: Sequence[float],
    populations: Sequence[float],
    competitiveness: Sequence[float] | None = None,
    config: CampaignConfig | None = None,
) -> list[Target]:
    """Targets from aligned sequences; competitiveness is derived from the margins when omitted.

    Typical use: presidential targets = provinces with ``values`` = electoral votes; House
    targets = districts with value 1; Senate targets = provinces with value = seats up.
    """
    n = len(codes)
    if not (len(values) == len(margins_pp) == len(populations) == n):
        raise ValueError("codes, values, margins_pp and populations must have equal length")
    if competitiveness is None:
        scale = (config or load_campaign_config()).closeness_scale_pp
        comp = np.atleast_1d(competitiveness_from_margin(np.asarray(margins_pp, float), scale))
    else:
        if len(competitiveness) != n:
            raise ValueError("competitiveness must align with codes")
        comp = np.clip(np.asarray(competitiveness, float), 0.0, 1.0)
    return [
        Target(level, str(c), float(v), float(k), float(m), float(p))
        for c, v, k, m, p in zip(codes, values, comp, margins_pp, populations, strict=True)
    ]


# --------------------------------------------------------------------------- planning
def plan_campaign(
    party: str,
    budget: float,
    strategy: str,
    targets: Sequence[Target],
    weeks: int,
    config: CampaignConfig | None = None,
    seed: int = 0,
) -> list[Allocation]:
    """Plan a party's campaign: weekly allocations of ``budget`` over ``targets``.

    Guarantees: ``sum(a.amount) <= budget + sum(a.proceeds)`` (fundraising proceeds are spent in
    later weeks), discrete actions respect their weekly unit limits, GOTV only runs in its final
    weeks, national-scope actions (debate prep, fundraising) are allocated at ``('national', 'NL')``.
    Deterministic for a given seed.
    """
    cfg = config or load_campaign_config()
    if strategy not in cfg.strategies:
        raise ValueError(f"unknown campaign strategy {strategy!r}; known: {sorted(cfg.strategies)}")
    if budget < 0 or not math.isfinite(budget):
        raise ValueError("budget must be a non-negative number")
    if weeks < 1:
        raise ValueError("weeks must be >= 1")
    targets = list(targets)
    keys = [t.key for t in targets]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate targets")
    if budget == 0:
        return []

    strat = cfg.strategies[strategy]
    shares = _target_shares(targets, strat, cfg, party, seed)
    # No target scores under this strategy: choose targets with the fallback chain but keep the
    # requested strategy's spending pattern (action mix, ramp, fundraising).
    seen = {strategy}
    fallback = strat.fallback
    while shares.sum() <= 0 and fallback is not None and fallback not in seen and targets:
        log.debug("strategy %s found no targets for %s; choosing targets with %s", strategy, party, fallback)
        seen.add(fallback)
        shares = _target_shares(targets, cfg.strategies[fallback], cfg, party, seed)
        fallback = cfg.strategies[fallback].fallback

    available, fund_spend, proceeds = _weekly_budgets(budget, weeks, strat, cfg, party, seed)
    allocations: list[Allocation] = []
    national = ("national", NATIONAL_CODE)
    for t in range(weeks):
        week = t + 1
        if fund_spend[t] > 0:
            allocations.append(
                Allocation(
                    *national, "fundraising", float(fund_spend[t]), week, party, proceeds=float(proceeds[t])
                )
            )
        avail = float(available[t])
        if avail <= 0:
            continue
        debate = avail * strat.debate_prep_share
        if debate > 0:
            allocations.append(Allocation(*national, "debate_prep", debate, week, party))
        rest = avail - debate
        if shares.sum() <= 0:
            allocations.append(Allocation(*national, "advertising", rest, week, party))
            continue
        allocations.extend(_week_allocations(targets, shares, rest, t, weeks, strat, cfg, party))

    allocations = [a for a in allocations if a.amount >= cfg.min_allocation]
    log.debug(
        "planned campaign",
        extra={
            "ctx": {"party": party, "strategy": strategy, "allocations": len(allocations), "budget": budget}
        },
    )
    return allocations


def _target_shares(
    targets: list[Target], strat: StrategyConfig, cfg: CampaignConfig, party: str, seed: int
) -> np.ndarray:
    """Normalised spending share per target (0 for targets the strategy ignores)."""
    n = len(targets)
    if n == 0:
        return np.zeros(0)
    value = np.array([t.value for t in targets], float)
    comp = np.array([t.competitiveness for t in targets], float)
    margin = np.array([t.expected_margin_pp for t in targets], float)
    closeness = strat.closeness_floor + (1.0 - strat.closeness_floor) * np.power(
        comp, strat.closeness_exponent
    )
    closeness = np.where(comp < strat.min_competitiveness, 0.0, closeness)
    if strat.margin_center_pp is not None:
        window = np.exp(-0.5 * ((margin - strat.margin_center_pp) / strat.margin_width_pp) ** 2)
    else:
        window = np.ones(n)
    if cfg.planning_noise_sd > 0:
        z = np.array(
            [
                make_rng(seed, "campaign-plan", party, "score", t.level, t.code).standard_normal()
                for t in targets
            ]
        )
        noise = np.exp(cfg.planning_noise_sd * z)
    else:
        noise = np.ones(n)
    score = np.where(value > 0, np.power(value, strat.value_exponent), 0.0) * closeness * window * noise
    score = np.where(score > 1e-9 * max(score.max(), _EPS), score, 0.0)
    return _normalise_shares(score, strat.max_target_share, strat.min_target_share)


def _normalise_shares(score: np.ndarray, max_share: float, min_share: float) -> np.ndarray:
    total = score.sum()
    if total <= 0:
        return np.zeros_like(score)
    s = score / total
    # drop negligible targets (focus), keeping at least the best one
    for _ in range(len(s)):
        small = (s > 0) & (s < min_share)
        if not small.any() or small.sum() == (s > 0).sum():
            break
        s = np.where(small, 0.0, s)
        s = s / s.sum()
    # cap the share of any single target (water-filling)
    positive = int((s > 0).sum())
    cap = max(max_share, 1.0 / positive)
    for _ in range(len(s)):
        over = s > cap + 1e-12
        if not over.any():
            break
        excess = float((s[over] - cap).sum())
        s[over] = cap
        free = (s > 0) & (s < cap - 1e-12)
        if not free.any():
            break
        s[free] += excess * s[free] / s[free].sum()
    return s / s.sum()


def _weekly_budgets(
    budget: float, weeks: int, strat: StrategyConfig, cfg: CampaignConfig, party: str, seed: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(available, fundraising_spend, proceeds)`` per week (index 0 = week 1)."""
    ramp = np.power(np.arange(1, weeks + 1, dtype=float), strat.spend_ramp)
    base = budget * ramp / ramp.sum()
    fr = cfg.actions["fundraising"]
    fund = np.zeros(weeks)
    proceeds = np.zeros(weeks)
    available = base.copy()
    for t in range(min(strat.fundraising_weeks, weeks)):
        if t + fr.lag_weeks >= weeks or strat.fundraising_share <= 0 or fr.return_rate <= 0:
            continue
        fund[t] = strat.fundraising_share * base[t]
        z = make_rng(seed, "campaign-plan", party, "fundraising", t + 1).standard_normal()
        rate = fr.return_rate * math.exp(fr.return_sd * z - 0.5 * fr.return_sd**2)
        proceeds[t] = fund[t] * rate
        available[t] -= fund[t]
        later = np.arange(t + fr.lag_weeks, weeks)
        available[later] += proceeds[t] * ramp[later] / ramp[later].sum()
    return available, fund, proceeds


def _week_allocations(
    targets: list[Target],
    shares: np.ndarray,
    rest: float,
    t: int,
    weeks: int,
    strat: StrategyConfig,
    cfg: CampaignConfig,
    party: str,
) -> list[Allocation]:
    """Split one week's target budget over actions and targets."""
    week = t + 1
    weeks_left = weeks - t  # including this week
    mix = {a: w for a, w in strat.action_mix.items() if w > 0} or {"advertising": 1.0}
    total_mix = sum(mix.values())
    allowed = {
        a: w
        for a, w in mix.items()
        if cfg.actions[a].final_weeks is None or weeks_left <= int(cfg.actions[a].final_weeks)  # type: ignore[arg-type]
    }
    discrete = {a: w for a, w in allowed.items() if cfg.actions[a].discrete}
    continuous = {a: w for a, w in allowed.items() if not cfg.actions[a].discrete}
    out: list[Allocation] = []
    leftover = rest * (total_mix - sum(allowed.values())) / total_mix  # disallowed this week
    for action in sorted(discrete):
        prof = cfg.actions[action]
        pool = rest * discrete[action] / total_mix
        unit_cost = float(prof.unit_cost)  # type: ignore[arg-type]
        max_units = int(prof.max_units_per_week)  # type: ignore[arg-type]
        units = min(max_units, math.floor(pool / unit_cost + 1e-9))
        assigned = _assign_units(shares, units, prof.max_units_per_target_week)
        spent = 0.0
        for i in np.flatnonzero(assigned):
            amount = float(assigned[i]) * unit_cost
            spent += amount
            tg = targets[i]
            out.append(
                Allocation(
                    tg.level, tg.code, action, amount, week, party, int(assigned[i]), 0.0, tg.population
                )
            )
        leftover += pool - spent
    cont_total = rest * sum(continuous.values()) / total_mix + leftover
    if not continuous:
        continuous = {"advertising": 1.0}
    cont_mix = sum(continuous.values())
    for action in sorted(continuous):
        pool = cont_total * continuous[action] / cont_mix
        amounts = pool * shares
        for i in np.flatnonzero(amounts > 0):
            tg = targets[i]
            out.append(
                Allocation(
                    tg.level, tg.code, action, float(amounts[i]), week, party, None, 0.0, tg.population
                )
            )
    return out


def _assign_units(shares: np.ndarray, units: int, max_per_target: int) -> np.ndarray:
    """Greedy assignment of discrete units (priority share / (1 + already assigned))."""
    assigned = np.zeros(len(shares), dtype=int)
    for _ in range(max(units, 0)):
        eligible = (shares > 0) & (assigned < max_per_target)
        if not eligible.any():
            break
        priority = np.where(eligible, shares / (1.0 + assigned), -1.0)
        assigned[int(np.argmax(priority))] += 1
    return assigned


def plan_summary(allocations: Iterable[Allocation]) -> dict[str, Any]:
    """Totals of a plan: spent, raised, by action, by week and by target."""
    allocs = list(allocations)
    by_action: dict[str, float] = defaultdict(float)
    by_week: dict[int, float] = defaultdict(float)
    by_target: dict[TargetKey, float] = defaultdict(float)
    for a in allocs:
        by_action[a.action] += a.amount
        by_week[a.week] += a.amount
        by_target[a.key] += a.amount
    return {
        "spent": float(sum(a.amount for a in allocs)),
        "raised": float(sum(a.proceeds for a in allocs)),
        "by_action": dict(by_action),
        "by_week": dict(sorted(by_week.items())),
        "by_target": dict(by_target),
    }


# --------------------------------------------------------------------------- effects
def realize_effects(
    allocations: Iterable[Allocation],
    config: CampaignConfig | None = None,
    seed: int = 0,
    weeks: int | None = None,
) -> CampaignEffects:
    """Realise one party's campaign effects per target.

    Per target: ``input = Σ potency_a × spend × persistence_a^(weeks − week) / scale_a`` (scale
    grows with the target's population), ``expected = cap × (1 − exp(−input))`` and
    ``realised = clip(expected × clip(1 + u·z, floor, max), floor·cap, cap)`` with ``z ~ N(0, 1)``
    drawn from a stream keyed by (party, level, code) and ``u`` the spend-weighted uncertainty of
    the actions used.  Each allocation is credited with its share of the target's input.

    ``weeks`` is the campaign length (default: the latest allocation week).  Allocations without a
    (positive) ``population`` are scaled at the level's reference population.  Raises
    ``ValueError`` for non-finite or negative amounts, weeks outside ``1..weeks``, unknown levels
    or actions, and national-scope actions (debate prep, fundraising) at a non-national target.
    """
    cfg = config or load_campaign_config()
    allocs = list(allocations)
    parties = {a.party for a in allocs}
    if len(parties) > 1:
        raise ValueError("realize_effects expects the allocations of a single party; see combine_effects")
    party = parties.pop() if parties else ""
    if not allocs:
        return CampaignEffects(party, {}, {}, {}, {}, {}, [], seed)
    unknown = sorted({a.action for a in allocs if a.action not in cfg.actions})
    if unknown:
        raise ValueError(f"unknown campaign actions {unknown}")
    _validate_allocations(allocs, cfg, weeks)
    W = weeks if weeks is not None else max(a.week for a in allocs)

    target_keys = sorted({a.key for a in allocs})
    tindex = {k: i for i, k in enumerate(target_keys)}
    T = len(target_keys)
    tidx = np.array([tindex[a.key] for a in allocs])
    amount = np.array([max(a.amount, 0.0) for a in allocs])
    persistence = np.array([cfg.actions[a.action].persistence for a in allocs])
    age = np.maximum(W - np.array([a.week for a in allocs]), 0)
    eff = amount * persistence**age
    ratio = np.array(
        [
            (a.population / cfg.reference_population.get(a.level, a.population)) if a.population else 1.0
            for a in allocs
        ]
    )
    elasticity = np.array([cfg.actions[a.action].population_elasticity for a in allocs])
    scale = np.array([cfg.actions[a.action].scale for a in allocs]) * np.maximum(ratio, 1e-3) ** elasticity
    cp = np.array([cfg.actions[a.action].persuasion for a in allocs]) * eff / scale
    ct = np.array([cfg.actions[a.action].turnout for a in allocs]) * eff / scale
    unc = np.array([cfg.action_uncertainty(a.action) for a in allocs])

    CP = np.bincount(tidx, weights=cp, minlength=T)
    CT = np.bincount(tidx, weights=ct, minlength=T)
    UP = np.bincount(tidx, weights=cp * unc, minlength=T) / np.maximum(CP, _EPS)
    UT = np.bincount(tidx, weights=ct * unc, minlength=T) / np.maximum(CT, _EPS)
    spend = np.bincount(tidx, weights=amount, minlength=T)
    cap_p = np.array([cfg.level_cap(k[0], "persuasion") for k in target_keys])
    cap_t = np.array([cfg.level_cap(k[0], "turnout") for k in target_keys])
    exp_p = cap_p * -np.expm1(-CP)
    exp_t = cap_t * -np.expm1(-CT)
    z_p = np.array(
        [
            make_rng(seed, "campaign-effects", party, lv, cd, "persuasion").standard_normal()
            for lv, cd in target_keys
        ]
    )
    z_t = np.array(
        [
            make_rng(seed, "campaign-effects", party, lv, cd, "turnout").standard_normal()
            for lv, cd in target_keys
        ]
    )
    real_p = _realise(exp_p, UP, z_p, cap_p, cfg)
    real_t = _realise(exp_t, UT, z_t, cap_t, cfg)

    share_p = np.where(CP[tidx] > 0, cp / np.maximum(CP[tidx], _EPS), 0.0)
    share_t = np.where(CT[tidx] > 0, ct / np.maximum(CT[tidx], _EPS), 0.0)
    records = [
        AllocationEffect(
            allocation=a,
            effective_spend=float(eff[i]),
            expected_persuasion=float(exp_p[tidx[i]] * share_p[i]),
            realized_persuasion=float(real_p[tidx[i]] * share_p[i]),
            expected_turnout=float(exp_t[tidx[i]] * share_t[i]),
            realized_turnout=float(real_t[tidx[i]] * share_t[i]),
        )
        for i, a in enumerate(allocs)
    ]
    active = [j for j in range(T) if CP[j] > 0 or CT[j] > 0]
    return CampaignEffects(
        party=party,
        persuasion={target_keys[j]: float(real_p[j]) for j in active},
        turnout={target_keys[j]: float(real_t[j]) for j in active},
        expected_persuasion={target_keys[j]: float(exp_p[j]) for j in active},
        expected_turnout={target_keys[j]: float(exp_t[j]) for j in active},
        spend={target_keys[j]: float(spend[j]) for j in range(T)},
        allocations=records,
        seed=seed,
    )


def _validate_allocations(allocs: Sequence[Allocation], cfg: CampaignConfig, weeks: int | None) -> None:
    """Reject allocations that would silently corrupt the effects (NaN / negative money, weeks
    outside the campaign, national-scope actions at a local target, unknown levels)."""
    problems: list[str] = []
    for a in allocs:
        if not math.isfinite(a.amount) or a.amount < 0:
            problems.append(f"{a.action}@{a.level}:{a.code} week {a.week}: amount {a.amount!r}")
        if a.level not in LEVELS:
            problems.append(f"{a.action}@{a.level}:{a.code}: unknown level")
        if not isinstance(a.week, int | np.integer) or a.week < 1 or (weeks is not None and a.week > weeks):
            problems.append(f"{a.action}@{a.level}:{a.code}: week {a.week!r} outside 1..{weeks or '∞'}")
        if cfg.actions[a.action].scope == "national" and a.key != ("national", NATIONAL_CODE):
            problems.append(f"{a.action} is a national-scope action but targets {a.level}:{a.code}")
        if a.population is not None and (not math.isfinite(a.population) or a.population < 0):
            problems.append(f"{a.action}@{a.level}:{a.code}: population {a.population!r}")
    if problems:
        raise ValueError("invalid campaign allocations: " + "; ".join(problems[:5]))


def _realise(
    expected: np.ndarray, u: np.ndarray, z: np.ndarray, cap: np.ndarray, cfg: CampaignConfig
) -> np.ndarray:
    mult = np.clip(1.0 + u * z, cfg.backfire_floor, cfg.max_multiplier)
    return np.clip(expected * mult, cfg.backfire_floor * cap, cap)


def combine_effects(
    effects: Iterable[CampaignEffects], config: CampaignConfig | None = None
) -> CombinedCampaignEffects:
    """Merge per-party effects.  Effects of the same party at the same target (e.g. several
    campaigns) are summed and clipped to the caps."""
    cfg = config or load_campaign_config()
    persuasion: dict[TargetKey, dict[str, float]] = defaultdict(dict)
    turnout: dict[TargetKey, dict[str, float]] = defaultdict(dict)
    by_party: dict[str, CampaignEffects] = {}
    parties: list[str] = []
    for eff in effects:
        if eff.party not in parties:
            parties.append(eff.party)
        by_party.setdefault(eff.party, eff)
        for table, src, kind in (
            (persuasion, eff.persuasion, "persuasion"),
            (turnout, eff.turnout, "turnout"),
        ):
            for key, value in src.items():
                cap = cfg.level_cap(key[0], kind)  # type: ignore[arg-type]
                total = table[key].get(eff.party, 0.0) + value
                table[key][eff.party] = float(np.clip(total, cfg.backfire_floor * cap, cap))
    return CombinedCampaignEffects(
        parties=parties,
        persuasion=dict(persuasion),
        turnout=dict(turnout),
        persuasion_cap=cfg.effect_cap,
        turnout_cap=cfg.turnout_cap,
        backfire_floor=cfg.backfire_floor,
        by_party=by_party,
    )


# --------------------------------------------------------------------------- orchestration helper
@dataclass
class CampaignRun:
    """All parties' plans and effects for one election (see :func:`run_campaigns`)."""

    allocations: dict[str, list[Allocation]] = field(default_factory=dict)
    effects: dict[str, CampaignEffects] = field(default_factory=dict)
    combined: CombinedCampaignEffects | None = None
    strategies: dict[str, str] = field(default_factory=dict)


def run_campaigns(
    spec: Any,
    targets: Mapping[str, Sequence[Target]],
    seed: int,
    config: CampaignConfig | None = None,
) -> CampaignRun:
    """Plan and realise every party's campaign of a scenario's :class:`CampaignSpec`.

    ``targets`` maps party code → its targets.  Parties without a budget do not campaign.
    """
    cfg = config_from_spec(spec, config)
    run = CampaignRun()
    if not getattr(spec, "enabled", True):
        run.combined = combine_effects([], cfg)
        return run
    weeks = int(getattr(spec, "weeks", 10))
    budgets: Mapping[str, float] = getattr(spec, "budgets", {}) or {}
    strategies: Mapping[str, str] = getattr(spec, "strategies", {}) or {}
    for party in sorted(budgets):
        strategy = strategies.get(party, cfg.default_strategy)
        allocs = plan_campaign(
            party, float(budgets[party]), strategy, list(targets.get(party, [])), weeks, cfg, seed
        )
        run.allocations[party] = allocs
        run.effects[party] = realize_effects(allocs, cfg, seed, weeks)
        run.strategies[party] = strategy
    run.combined = combine_effects(run.effects.values(), cfg)
    return run
