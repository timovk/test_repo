"""Generation of FICTIONAL polls from a SIMULATED "true" opinion.

Model (docs/POLLING.md):

* **True opinion path.** Election-day truth per poll group is given.  Going back in time, opinion
  drifts away from it as a seeded random-walk *bridge* in multinomial-logit space: pinned to the
  truth on election day and to a random offset at campaign start.  One walk per key is shared by
  every group (national movement), plus a smaller group-specific walk.
* **Industry-wide polling error.** One logit shift per key shared by *all* polls of a run
  (SD ``PollingSpec.true_polling_error_sd`` in share points at a 50 % share), plus a smaller
  province-level error shared by all polls in the same province.
* **House effects.** Each pollster's true lean = configured prior + seeded jitter (pp).
* **Sampling noise.** Multinomial draw of the decided respondents (effective sample size =
  sample / design effect); an undecided share that declines toward election day.
* **Fieldwork.** 2–5 day windows spread over the campaign, denser near election day; more
  province/district polls where races are competitive.

Deterministic: identical inputs and seed ⇒ identical polls (order-independent seed streams).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, overload

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.core.rng import make_rng
from app.polling.config import GenerationConfig, PollingConfig, load_polling_config
from app.polling.types import (
    FICTIONAL_POLL_SOURCE,
    NATIONAL_GEO,
    NATIONAL_POLL_TYPES,
    POLL_COLUMNS,
    POLL_DATA_CATEGORY,
    POLL_TYPES,
    RESULT_COLUMNS,
    PollsterLike,
    lookup_method,
    pollster_attr,
    pollster_names,
    province_of_geo,
    race_code_for,
    spec_poll_counts,
)

log = get_logger(__name__)

GroupKey = tuple[str, str]  # (poll_type, geo_code)

#: Logit-to-share slope at a 50 % share (d share / d logit = p (1 − p) = 0.25).
_LOGIT_SLOPE_AT_HALF = 0.25
_MIN_SHARE = 1e-4


@dataclass(frozen=True)
class GeneratedPoll:
    """One FICTIONAL poll.  ``results`` are percent of all respondents (sum = 100 − undecided)."""

    poll_id: int
    pollster: str
    poll_type: str
    geo_code: str
    race_code: str | None
    start_date: date
    end_date: date
    sample_size: int
    population: str
    method: str
    margin_of_error: float  # ± pp, 95 %, at a 50 % share, including the design effect
    undecided_pct: float
    results: dict[str, float]
    #: Audit: simulated true opinion (shares 0–1) at mid-fieldwork, before any polling error.
    true_shares: dict[str, float] = field(default_factory=dict)
    quality_rating: float | None = None
    is_fictional: bool = True
    data_category: str = POLL_DATA_CATEGORY
    source: str = FICTIONAL_POLL_SOURCE

    @property
    def decided_shares(self) -> dict[str, float]:
        """Results rescaled to the decided respondents (shares 0–1)."""
        total = sum(self.results.values())
        return {k: v / total for k, v in self.results.items()} if total > 0 else dict(self.results)


@dataclass
class GeneratedPolls(Sequence[GeneratedPoll]):
    """A run of generated polls (a read-only sequence of :class:`GeneratedPoll`) plus audit data."""

    polls: list[GeneratedPoll]
    seed: int
    election_date: date
    start_date: date
    #: Industry-wide logit error per key, shared by every poll of the run.
    industry_error: dict[str, float]
    #: Province-level logit error per province code and key (shared by polls in that province).
    geo_error: dict[str, dict[str, float]]
    #: Realised true house effects per pollster and key (pp) used for generation.
    house_effects: dict[str, dict[str, float]]
    #: Poll counts per group.
    allocation: dict[GroupKey, int]
    data_category: str = POLL_DATA_CATEGORY

    @overload
    def __getitem__(self, index: int) -> GeneratedPoll: ...

    @overload
    def __getitem__(self, index: slice) -> list[GeneratedPoll]: ...

    def __getitem__(self, index: int | slice) -> GeneratedPoll | list[GeneratedPoll]:
        return self.polls[index]

    def __len__(self) -> int:
        return len(self.polls)

    def __iter__(self) -> Iterator[GeneratedPoll]:
        return iter(self.polls)

    def frames(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """``(polls_df, results_df)`` in the :func:`app.polling.aggregate.aggregate_polls` schema."""
        return polls_to_frames(self.polls)

    def true_industry_error_pp(
        self, poll_type: str, geo_code: str, truth: Mapping[str, float]
    ) -> dict[str, float]:
        """Share-point effect (pp) of the industry + province error on a group with ``truth``."""
        keys = list(truth)
        base = np.log(np.clip(np.array([truth[k] for k in keys], float), _MIN_SHARE, None))
        prov = _error_province(poll_type, geo_code)
        shift = np.array(
            [
                self.industry_error.get(k, 0.0) + (self.geo_error.get(prov, {}).get(k, 0.0) if prov else 0.0)
                for k in keys
            ]
        )
        p0, p1 = _softmax(base), _softmax(base + shift)
        return {k: float(100.0 * (b - a)) for k, a, b in zip(keys, p0, p1, strict=True)}


def polls_to_frames(polls: Sequence[GeneratedPoll]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Convert generated polls into ``(polls_df, results_df)`` for aggregation."""
    poll_rows = [
        {
            "poll_id": p.poll_id,
            "pollster": p.pollster,
            "poll_type": p.poll_type,
            "geo_code": p.geo_code,
            "start_date": pd.Timestamp(p.start_date),
            "end_date": pd.Timestamp(p.end_date),
            "sample_size": p.sample_size,
            "population": p.population,
            "method": p.method,
            "undecided_pct": p.undecided_pct,
            "quality_rating": p.quality_rating,
            "margin_of_error": p.margin_of_error,
            "race_code": p.race_code,
        }
        for p in polls
    ]
    result_rows = [
        {"poll_id": p.poll_id, "key": k, "value_pct": v} for p in polls for k, v in p.results.items()
    ]
    polls_df = pd.DataFrame(poll_rows, columns=[*POLL_COLUMNS, "margin_of_error", "race_code"])
    polls_df["quality_rating"] = polls_df["quality_rating"].astype(float)
    results_df = pd.DataFrame(result_rows, columns=list(RESULT_COLUMNS))
    return polls_df, results_df


def competitiveness_from_shares(shares: Mapping[str, float], tossup_margin_scale: float = 0.20) -> float:
    """0 (safe) … 1 (toss-up) from the top-two margin of ``shares`` (0–1)."""
    vals = sorted((float(v) for v in shares.values()), reverse=True)
    if len(vals) < 2:
        return 0.0
    total = sum(vals)
    margin = (vals[0] - vals[1]) / total if total > 0 else 0.0
    return float(np.clip(1.0 - margin / tossup_margin_scale, 0.0, 1.0))


def margin_of_error(sample_size: int, design_effect: float = 1.0, p: float = 0.5, z: float = 1.96) -> float:
    """95 % margin of error (± percentage points) of a share ``p`` with the given design effect."""
    return float(z * np.sqrt(p * (1.0 - p) * design_effect / max(sample_size, 1)) * 100.0)


# --------------------------------------------------------------------------- generation
def generate_polls(
    truth: Mapping[GroupKey, Mapping[str, float]],
    pollsters: Sequence[PollsterLike],
    spec: Any,
    election_date: date,
    seed: int,
    competitiveness: Mapping[GroupKey, float] | None = None,
    *,
    config: PollingConfig | GenerationConfig | None = None,
) -> GeneratedPolls:
    """Generate FICTIONAL polls.

    Parameters
    ----------
    truth:
        ``{(poll_type, geo_code): {key: share}}`` — the election-day true opinion (shares 0–1,
        normalised internally).  National poll types use ``geo_code = "NL"``.
    pollsters:
        Pollster descriptions (:class:`~app.polling.config.PollsterConfig` or the scenario's
        ``PollsterSpec``); an empty list uses ``config/polling.yaml``.
    spec:
        :class:`app.scenarios.schema.PollingSpec` (counts per type, ``start_date``,
        ``true_polling_error_sd``) or a mapping ``{poll_type: count}``.
    election_date, seed:
        Election day and root seed.
    competitiveness:
        Optional ``{(poll_type, geo_code): 0..1}``; derived from the truth's top-two margin when
        omitted.  Competitive groups receive more polls.
    """
    if isinstance(config, PollingConfig):
        gen, default_pollsters = config.generation, config.pollsters
    elif isinstance(config, GenerationConfig):
        gen, default_pollsters = config, []
    else:
        loaded = load_polling_config()
        gen, default_pollsters = loaded.generation, loaded.pollsters
    pollster_list: list[PollsterLike] = list(pollsters) if pollsters else list(default_pollsters)
    if not pollster_list:
        raise ValueError("no pollsters available for poll generation")
    pollster_names(pollster_list)  # validates uniqueness
    # canonical order: the polls do not depend on the order in which pollsters are listed
    pollster_list = sorted(pollster_list, key=lambda p: p.name)

    counts_by_type = spec_poll_counts(spec)
    error_sd = float(getattr(spec, "true_polling_error_sd", 0.02) or 0.0)
    start_date = getattr(spec, "start_date", None) or (
        election_date - timedelta(days=gen.default_campaign_days)
    )
    span = (election_date - start_date).days
    if span < gen.fieldwork_days_max + 1:
        raise ValueError(f"campaign too short for polling: {span} days")

    groups = _normalise_truth(truth)
    all_keys = sorted({k for shares in groups.values() for k in shares})

    # ---- run-wide random components (order-independent streams) -------------------
    industry = {
        k: float(make_rng(seed, "polling", "industry-error", k).standard_normal())
        * error_sd
        / _LOGIT_SLOPE_AT_HALF
        for k in all_keys
    }
    provinces = sorted({pv for (pt, geo) in groups if (pv := _error_province(pt, geo))})
    geo_error = {
        pv: {
            k: float(make_rng(seed, "polling", "geo-error", pv, k).standard_normal())
            * error_sd
            * gen.geo_error_fraction
            / _LOGIT_SLOPE_AT_HALF
            for k in all_keys
        }
        for pv in provinces
    }
    shared_walk = {
        k: _bridge(make_rng(seed, "polling", "walk", k), span, gen.drift_sd_per_day, gen.start_offset_sd)
        for k in all_keys
    }
    house = {
        p.name: {
            k: float(dict(p.house_effects).get(k, 0.0))
            + gen.house_effect_jitter_pp
            * float(make_rng(seed, "polling", "house-effect", p.name, k).standard_normal())
            for k in all_keys
        }
        for p in pollster_list
    }

    # ---- allocate poll counts to groups -------------------------------------------
    allocation = _allocate(groups, counts_by_type, competitiveness, gen, seed)

    polls: list[tuple[tuple, GeneratedPoll]] = []
    for (poll_type, geo), n_polls in sorted(allocation.items()):
        if n_polls <= 0:
            continue
        eligible = [p for p in pollster_list if _fields_type(p, poll_type)]
        if not eligible:
            log.warning("no pollster fields %s polls; %d polls skipped", poll_type, n_polls)
            continue
        activity = np.array([float(pollster_attr(p, "activity", 1.0)) for p in eligible])
        if activity.sum() <= 0:
            activity = np.ones(len(eligible))
        activity = activity / activity.sum()
        keys = list(groups[(poll_type, geo)])
        base_logit = np.log(np.clip(np.array([groups[(poll_type, geo)][k] for k in keys]), _MIN_SHARE, None))
        local = np.stack(
            [
                _bridge(
                    make_rng(seed, "polling", "walk-local", poll_type, geo, k),
                    span,
                    gen.local_drift_sd_per_day,
                    gen.start_offset_sd * 0.5,
                )
                for k in keys
            ]
        )  # (K, span + 1)
        walk = np.stack([shared_walk[k] for k in keys]) + local
        prov = _error_province(poll_type, geo)
        err = np.array([industry[k] + (geo_error[prov][k] if prov else 0.0) for k in keys])
        for j in range(n_polls):
            rng = make_rng(seed, "polling", "poll", poll_type, geo, j)
            poll = _one_poll(
                rng,
                eligible,
                activity,
                poll_type,
                geo,
                keys,
                base_logit,
                walk,
                err,
                house,
                gen,
                election_date,
                span,
            )
            polls.append(((poll.end_date, POLL_TYPES.index(poll_type), geo, poll.pollster, j), poll))

    polls.sort(key=lambda item: item[0])
    numbered = [_with_id(p, i + 1) for i, (_, p) in enumerate(polls)]
    log.info(
        "generated %d fictional polls",
        len(numbered),
        extra={"ctx": {"seed": seed, "groups": sum(1 for v in allocation.values() if v > 0)}},
    )
    return GeneratedPolls(
        polls=numbered,
        seed=seed,
        election_date=election_date,
        start_date=start_date,
        industry_error=industry,
        geo_error=geo_error,
        house_effects=house,
        allocation=allocation,
    )


# --------------------------------------------------------------------------- helpers
def _normalise_truth(truth: Mapping[GroupKey, Mapping[str, float]]) -> dict[GroupKey, dict[str, float]]:
    """Validated, normalised truth.

    Options with a zero share (withdrawn lines, parties not on the ballot) are not polled.  Groups
    with fewer than two remaining options (uncontested races) are skipped with a warning — nobody
    polls a race with one candidate.  Negative or non-finite shares are rejected.
    """
    groups: dict[GroupKey, dict[str, float]] = {}
    for gkey, shares in truth.items():
        poll_type, geo = str(gkey[0]), str(gkey[1])
        if poll_type not in POLL_TYPES:
            raise ValueError(f"unknown poll type {poll_type!r}")
        vals = {str(k): float(v) for k, v in shares.items()}
        bad = sorted(k for k, v in vals.items() if not np.isfinite(v) or v < 0)
        if bad:
            raise ValueError(f"truth for {gkey}: shares must be finite and non-negative ({bad})")
        vals = {k: v for k, v in vals.items() if v > 0}
        if len(vals) < 2:
            log.warning("truth for %s has fewer than two options with a positive share; not polled", gkey)
            continue
        total = sum(vals.values())
        groups[(poll_type, geo)] = {k: v / total for k, v in sorted(vals.items())}
    return groups


def _error_province(poll_type: str, geo: str) -> str | None:
    """Province whose regional polling error applies to a group (None for national polls)."""
    if geo == NATIONAL_GEO or poll_type in NATIONAL_POLL_TYPES:
        return None
    return province_of_geo(geo)


def _fields_type(p: PollsterLike, poll_type: str) -> bool:
    types = pollster_attr(p, "poll_types", None)
    return types is None or poll_type in types  # type: ignore[operator]


def _bridge(rng: np.random.Generator, span: int, step_sd: float, end_sd: float) -> np.ndarray:
    """Random-walk bridge indexed by days before election (0 … span): 0 at election day,
    ``N(0, end_sd)`` at campaign start, Brownian in between."""
    steps = rng.normal(0.0, step_sd, span) if step_sd > 0 else np.zeros(span)
    walk = np.concatenate([[0.0], np.cumsum(steps)])
    end = float(rng.normal(0.0, end_sd)) if end_sd > 0 else 0.0
    frac = np.arange(span + 1) / span
    return walk - frac * walk[-1] + frac * end


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x))
    return e / e.sum()


def _allocate(
    groups: Mapping[GroupKey, Mapping[str, float]],
    counts_by_type: Mapping[str, int],
    competitiveness: Mapping[GroupKey, float] | None,
    gen: GenerationConfig,
    seed: int,
) -> dict[GroupKey, int]:
    """Distribute each poll type's count over its geos (weight 1 + boost × competitiveness^power)."""
    allocation: dict[GroupKey, int] = {g: 0 for g in groups}
    for poll_type in POLL_TYPES:
        n = int(counts_by_type.get(poll_type, 0))
        geos = sorted(geo for (pt, geo) in groups if pt == poll_type)
        if n <= 0:
            continue
        if not geos:
            log.debug("no truth for poll type %s; %d requested polls skipped", poll_type, n)
            continue
        if len(geos) == 1:
            allocation[(poll_type, geos[0])] = n
            continue
        derived = np.array(
            [competitiveness_from_shares(groups[(poll_type, g)], gen.tossup_margin_scale) for g in geos]
        )
        given = np.array(
            [
                float(competitiveness.get((poll_type, g), np.nan)) if competitiveness is not None else np.nan
                for g in geos
            ]
        )
        # missing or non-finite explicit values fall back to the truth-derived competitiveness
        comp = np.clip(np.where(np.isfinite(given), given, derived), 0.0, 1.0)
        w = 1.0 + gen.competitiveness_boost * comp**gen.competitiveness_power
        draws = make_rng(seed, "polling", "allocate", poll_type).multinomial(n, w / w.sum())
        for g, c in zip(geos, draws, strict=True):
            allocation[(poll_type, g)] = int(c)
    return allocation


def _one_poll(
    rng: np.random.Generator,
    eligible: list[PollsterLike],
    activity: np.ndarray,
    poll_type: str,
    geo: str,
    keys: list[str],
    base_logit: np.ndarray,
    walk: np.ndarray,
    err: np.ndarray,
    house: Mapping[str, Mapping[str, float]],
    gen: GenerationConfig,
    election_date: date,
    span: int,
) -> GeneratedPoll:
    pollster = eligible[int(rng.choice(len(eligible), p=activity))]
    # ---- fieldwork window ----------------------------------------------------------
    length = int(rng.integers(gen.fieldwork_days_min, gen.fieldwork_days_max + 1))
    latest_end = span - length  # days before election of the latest-possible *earliest* end
    days_before_end = 1 + int(np.floor(rng.beta(1.0, gen.late_bias) * max(latest_end, 1)))
    days_before_end = min(days_before_end, span - length + 1)
    end = election_date - timedelta(days=days_before_end)
    start = end - timedelta(days=length - 1)
    mid = round(days_before_end + (length - 1) / 2.0)
    mid = min(max(mid, 0), span)
    # ---- population / method / sample ----------------------------------------------
    default_pop = str(pollster_attr(pollster, "population", "LV"))
    pops = list(gen.population_mix)
    mix = np.array([gen.population_mix[k] for k in pops], float)
    mix = 0.5 * mix / mix.sum() + 0.5 * np.array([1.0 if k == default_pop else 0.0 for k in pops])
    if mix.sum() <= 0:
        mix = np.ones(len(pops))
    population = pops[int(rng.choice(len(pops), p=mix / mix.sum()))]
    method = str(pollster.method)
    factor = float(gen.sample_factor.get(poll_type, 1.0))
    raw_n = float(pollster.typical_sample) * factor * float(np.exp(rng.normal(0.0, gen.sample_sd)))
    sample = int(max(gen.min_sample, round(raw_n / 10.0) * 10))
    deff = lookup_method(method, gen.design_effect, gen.default_design_effect)
    # ---- true opinion, polling error and house effect -------------------------------
    logits = base_logit + walk[:, mid]
    true = _softmax(logits)
    biased = _softmax(logits + err)
    h = np.array([house[pollster.name].get(k, 0.0) for k in keys]) / 100.0
    leaned = biased + h - h.sum() * biased
    leaned = np.clip(leaned, _MIN_SHARE, None)
    leaned = leaned / leaned.sum()
    # ---- undecided ---------------------------------------------------------------------
    progress = (mid / span) ** gen.undecided_curve
    und = (
        gen.undecided_end_pct
        + (gen.undecided_start_pct - gen.undecided_end_pct) * progress
        + float(pollster_attr(pollster, "undecided_offset_pp", 0.0))
        + float(gen.undecided_population_offset.get(population, 0.0))
        + float(rng.normal(0.0, gen.undecided_sd_pct))
    )
    und = float(np.clip(und, 0.5, 40.0))
    # ---- sampling noise (multinomial over the effective decided sample) ---------------
    n_decided = max(round(sample * (1.0 - und / 100.0) / deff), len(keys))
    counts = rng.multinomial(n_decided, leaned)
    decided = counts / n_decided
    dec = gen.report_decimals
    values = {k: round(float(v) * (100.0 - und), dec) for k, v in zip(keys, decided, strict=True)}
    und_reported = round(100.0 - sum(values.values()), dec)
    if und_reported < 0:
        # many options: rounding pushed the total above 100 — take the excess off the largest option
        top = max(values, key=lambda k: (values[k], k))
        values[top] = round(values[top] + und_reported, dec)
        und_reported = round(100.0 - sum(values.values()), dec)
    return GeneratedPoll(
        poll_id=0,
        pollster=pollster.name,
        poll_type=poll_type,
        geo_code=geo,
        race_code=race_code_for(poll_type, geo),
        start_date=start,
        end_date=end,
        sample_size=sample,
        population=population,
        method=method,
        margin_of_error=round(margin_of_error(sample, deff), 1),
        undecided_pct=max(und_reported, 0.0),
        results=values,
        true_shares={k: float(v) for k, v in zip(keys, true, strict=True)},
    )


def _with_id(p: GeneratedPoll, poll_id: int) -> GeneratedPoll:
    return replace(p, poll_id=poll_id)
