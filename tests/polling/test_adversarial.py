"""Adversarial polling tests: invariants the aggregate, generator and blend must keep under hostile
or degenerate input (all polls FICTIONAL / SIMULATED)."""

from __future__ import annotations

import time
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.core.rng import make_rng
from app.polling.aggregate import PollAverage, aggregate_polls
from app.polling.blend import blend_polls_with_model, poll_environment_shift, shift_from_poll_average
from app.polling.config import AggregationConfig, GenerationConfig, HouseEffectSettings, PollsterConfig
from app.polling.generate import generate_polls
from app.scenarios.schema import PollingSpec

AS_OF = date(2028, 11, 6)
ELECTION = date(2028, 11, 7)
NAT = ("national_president", "NL")
PROVINCES = ["GR", "FR", "DR", "OV", "FL", "GE", "UT", "NH", "ZH", "ZE", "NB", "LI"]


def _assert_finite(avg: PollAverage) -> None:
    for field in ("mean", "se", "lower", "upper", "total_se", "trend_latest"):
        values = getattr(avg, field)
        assert set(values) == set(avg.keys), field
        assert all(np.isfinite(v) for v in values.values()), (field, values)
    assert np.isfinite(avg.trend.to_numpy()).all() and np.isfinite(avg.trend_se.to_numpy()).all()
    assert np.isfinite(avg.weights["weight"]).all() and avg.weights["weight"].sum() == pytest.approx(1.0)
    assert np.isfinite(avg.effective_n) and np.isfinite(avg.effective_polls)


def _realistic_truth() -> dict[tuple[str, str], dict[str, float]]:
    rng = make_rng(0, "test", "realistic-truth")
    parties = ["PA", "SAP", "VLP", "DM", "CVU", "NVB"]
    truth = {NAT: dict(zip(parties, rng.dirichlet(np.full(6, 6.0)), strict=True))}
    for pv in PROVINCES:
        truth[("province_president", pv)] = dict(zip(parties, rng.dirichlet(np.full(6, 6.0)), strict=True))
    for i in range(1, 25):
        truth[("house_district", f"{PROVINCES[i % 12]}-{i:02d}")] = dict(
            zip(parties[:3], rng.dirichlet(np.full(3, 6.0)), strict=True)
        )
    return truth


@pytest.fixture(scope="module")
def realistic(polling_config):
    spec = PollingSpec(
        national_polls=60,
        province_polls=120,
        district_polls=80,
        senate_polls=0,
        governor_polls=0,
        generic_ballot_polls=0,
    )
    return generate_polls(
        _realistic_truth(), polling_config.pollsters, spec, ELECTION, 404, config=polling_config
    )


# --------------------------------------------------------------------------- aggregation invariants
def test_duplicate_poll_ids_are_rejected(agg_config, make_frames) -> None:
    polls, results = make_frames(
        [
            {"poll_id": 7, "results": {"PA": 50, "SAP": 50}},
            {"poll_id": 7, "pollster": "Beta Toetsing", "results": {"PA": 40, "SAP": 60}},
        ]
    )
    with pytest.raises(ValueError, match="duplicate poll_id"):
        aggregate_polls(polls, results, agg_config, AS_OF)


def test_zero_weight_configuration_excludes_polls_without_nan(make_frames) -> None:
    rows = [
        {"population": "A", "results": {"PA": 40, "SAP": 40, "RV": 20}},
        {"population": "LV", "results": {"PA": 50, "SAP": 50}},
        {"population": "LV", "results": {"PA": 30, "SAP": 70}, "quality_rating": 0.0},
    ]
    polls, results = make_frames(rows)
    cfg = AggregationConfig(population_weights={"LV": 1.0, "RV": 1.0, "A": 0.0})
    avg = aggregate_polls(polls, results, cfg, AS_OF)[NAT]
    _assert_finite(avg)
    # the adult poll (weight 0) and the manually excluded poll (quality 0) do not count
    assert avg.n_polls == 1 and set(avg.keys) == {"PA", "SAP"}
    assert avg.mean == pytest.approx({"PA": 50.0, "SAP": 50.0})
    assert "RV" not in avg.mean
    # every poll excluded → group omitted (not a NaN average)
    only_adults, only_adult_results = make_frames([rows[0]])
    assert aggregate_polls(only_adults, only_adult_results, cfg, AS_OF) == {}


def test_database_rating_zero_excludes_pollster(make_frames) -> None:
    polls, results = make_frames(
        [
            {"pollster": "Alpha Toetsing", "results": {"PA": 60, "SAP": 40}},
            {"pollster": "Beta Toetsing", "results": {"PA": 40, "SAP": 60}},
        ]
    )
    polls["pollster_rating"] = [0.0, 1.0]
    avg = aggregate_polls(polls, results, AggregationConfig(), AS_OF)[NAT]
    assert avg.n_polls == 1 and avg.mean["PA"] == pytest.approx(40.0)


def test_min_polls_applies_after_exclusions(make_frames) -> None:
    polls, results = make_frames(
        [{"results": {"PA": 50, "SAP": 50}}, {"results": {"PA": 45, "SAP": 55}, "quality_rating": 0.0}]
    )
    assert aggregate_polls(polls, results, AggregationConfig(min_polls=2), AS_OF) == {}


def test_recency_underflow_drops_keys_instead_of_nan(make_frames) -> None:
    polls, results = make_frames(
        [
            {"end_date": date(2020, 1, 1), "results": {"PA": 60, "SAP": 30, "RV": 10}},
            {"end_date": AS_OF, "results": {"PA": 50, "SAP": 50}},
        ]
    )
    cfg = AggregationConfig(recency_half_life_days=0.5, max_age_days=None)
    avg = aggregate_polls(polls, results, cfg, AS_OF)[NAT]
    _assert_finite(avg)
    assert set(avg.keys) == {"PA", "SAP"}
    assert avg.mean["PA"] == pytest.approx(50.0)


def test_uninformative_polls_are_ignored(make_frames) -> None:
    polls, results = make_frames(
        [
            {"results": {"PA": 0.0, "SAP": 0.0}, "undecided_pct": 100.0},
            {"results": {"PA": 45.0, "SAP": 45.0}, "undecided_pct": 10.0},
        ]
    )
    avg = aggregate_polls(polls, results, AggregationConfig(), AS_OF)[NAT]
    assert avg.n_polls == 1 and avg.mean == pytest.approx({"PA": 50.0, "SAP": 50.0})
    zeros, zero_results = make_frames([{"results": {"PA": 0.0, "SAP": 0.0}}])
    assert aggregate_polls(zeros, zero_results, AggregationConfig(), AS_OF) == {}


def test_population_labels_are_case_insensitive(make_frames) -> None:
    polls, results = make_frames([{"population": " rv ", "results": {"PA": 50, "SAP": 50}}])
    avg = aggregate_polls(polls, results, AggregationConfig(), AS_OF)[NAT]
    assert avg.weights.loc[0, "population"] == "RV"
    assert avg.weights.loc[0, "w_population"] == pytest.approx(AggregationConfig().population_weights["RV"])


def test_mismatched_poll_id_dtypes_warn(make_frames, caplog) -> None:
    polls, results = make_frames([{"results": {"PA": 50, "SAP": 50}}])
    results["poll_id"] = results["poll_id"].astype(str)
    with caplog.at_level("WARNING", logger="app.polling.aggregate"):
        assert aggregate_polls(polls, results, AggregationConfig(), AS_OF) == {}
    assert "poll_id" in caplog.text


def test_future_polls_never_leak_into_the_average(realistic, polling_config) -> None:
    """Election-night rule: polls whose fieldwork ends after ``as_of`` must not influence anything
    (mean, SE, trend, house effects)."""
    polls, results = realistic.frames()
    as_of = ELECTION - timedelta(days=30)
    past = polls[polls["end_date"] <= pd.Timestamp(as_of)]
    past_results = results[results["poll_id"].isin(past["poll_id"])]
    # scramble the future: absurd values from future polls must not matter
    future_ids = set(polls.loc[polls["end_date"] > pd.Timestamp(as_of), "poll_id"])
    assert future_ids
    poisoned = results.copy()
    poisoned.loc[poisoned["poll_id"].isin(future_ids), "value_pct"] = 99.0
    full = aggregate_polls(polls, poisoned, polling_config, as_of)
    only_past = aggregate_polls(past, past_results, polling_config, as_of)
    assert set(full) == set(only_past)
    for key, avg in only_past.items():
        other = full[key]
        assert other.mean == avg.mean and other.se == avg.se and other.trend_latest == avg.trend_latest
        pd.testing.assert_frame_equal(other.house_effects, avg.house_effects)
        assert other.trend.index.max() == pd.Timestamp(as_of)


def test_aggregate_is_invariant_to_row_order(realistic, polling_config) -> None:
    polls, results = realistic.frames()
    base = aggregate_polls(polls, results, polling_config, AS_OF)
    shuffled = aggregate_polls(
        polls.sample(frac=1.0, random_state=3),
        results.sample(frac=1.0, random_state=4),
        polling_config,
        AS_OF,
    )
    assert set(base) == set(shuffled)
    for key, avg in base.items():
        assert shuffled[key].keys == avg.keys
        for k in avg.keys:
            assert shuffled[key].mean[k] == pytest.approx(avg.mean[k], abs=1e-9)
            assert shuffled[key].trend_latest[k] == pytest.approx(avg.trend_latest[k], abs=1e-9)


def test_realistic_run_is_finite_and_sane(realistic, polling_config) -> None:
    averages = aggregate_polls(*realistic.frames(), polling_config, AS_OF)
    assert len(averages) == len(realistic.allocation) - sum(
        1 for v in realistic.allocation.values() if v == 0
    )
    for avg in averages.values():
        _assert_finite(avg)
        assert all(avg.lower[k] <= avg.mean[k] <= avg.upper[k] for k in avg.keys)
        assert all(avg.total_se[k] >= avg.se[k] for k in avg.keys)
        assert sum(avg.mean.values()) == pytest.approx(100.0, abs=3.0)  # undecided reallocated


def test_interval_is_calibrated_on_generated_polls(polling_config) -> None:
    """The 90 % interval of the mean covers truth + industry error about 90 % of the time."""
    gen = polling_config.generation.model_copy(
        update={"drift_sd_per_day": 0.0, "local_drift_sd_per_day": 0.0, "start_offset_sd": 0.0}
    )
    truth = {NAT: {"PA": 0.36, "SAP": 0.30, "VLP": 0.16, "DM": 0.10, "CVU": 0.08}}
    hits = []
    for seed in range(40):
        polls = generate_polls(
            truth, polling_config.pollsters, {"national_president": 40}, ELECTION, seed, config=gen
        )
        target = polls.true_industry_error_pp(*NAT, truth[NAT])
        avg = aggregate_polls(*polls.frames(), polling_config, AS_OF)[NAT]
        for k, share in truth[NAT].items():
            hits.append(avg.lower[k] <= 100 * share + target[k] <= avg.upper[k])
    assert 0.8 <= np.mean(hits) <= 0.99


def test_aggregate_scales_to_thousands_of_polls(polling_config) -> None:
    truth = _realistic_truth()
    counts = {"national_president": 1500, "province_president": 1500, "house_district": 1500}
    polls = generate_polls(truth, polling_config.pollsters, counts, ELECTION, 5, config=polling_config)
    frames = polls.frames()
    t0 = time.perf_counter()
    averages = aggregate_polls(*frames, polling_config, AS_OF)
    assert time.perf_counter() - t0 < 20.0
    assert sum(a.n_polls for a in averages.values()) == len(polls)


def test_house_effects_with_single_pollster_fall_back_to_prior(make_frames) -> None:
    pollsters = [PollsterConfig(name="Alpha Toetsing", house_effects={"PA": 2.0})]
    rows = [
        {"results": {"PA": 52.0 + d % 3 - 1, "SAP": 48.0}, "end_date": AS_OF - timedelta(days=d)}
        for d in range(12)
    ]
    polls, results = make_frames(rows)
    cfg = AggregationConfig(house_effects=HouseEffectSettings(shrinkage=0.0))
    avg = aggregate_polls(polls, results, cfg, AS_OF, pollsters=pollsters)[NAT]
    # a lone pollster's lean is not identified by the data: the prior is the anchor
    assert avg.house_effect_matrix().loc["Alpha Toetsing", "PA"] == pytest.approx(2.0, abs=1e-6)
    _assert_finite(avg)


# --------------------------------------------------------------------------- blend edge cases
def test_blend_ignores_nan_standard_errors() -> None:
    shift = poll_environment_shift(
        {"A": 0.5, "B": 0.3, "C": 0.2},
        {"A": float("nan"), "B": 0.01, "C": 0.01},
        {"A": 0.4, "B": 0.4, "C": 0.2},
        0.1,
    )
    assert "A" not in shift and all(np.isfinite(v) for v in shift.values())
    res = blend_polls_with_model(
        {"A": 0.5, "B": 0.5}, {"A": float("inf"), "B": float("inf")}, {"A": 0.4, "B": 0.6}, 0.1
    )
    assert res.shift == {"A": 0.0, "B": 0.0}
    assert sum(res.posterior_shares.values()) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        poll_environment_shift({"A": 0.5}, {"A": 0.01}, {"A": 0.5}, float("nan"))


def test_blend_handles_empty_and_disjoint_inputs() -> None:
    assert poll_environment_shift({}, {}, {"A": 0.5, "B": 0.5}, 0.1) == {}
    assert poll_environment_shift({"X": 0.5}, {"X": 0.01}, {"A": 0.5, "B": 0.5}, 0.1) == {}
    assert blend_polls_with_model({}, {}, {}, 0.1).posterior_shares == {}


def test_shift_from_average_is_finite_on_realistic_run(realistic, polling_config) -> None:
    averages = aggregate_polls(*realistic.frames(), polling_config, AS_OF)
    for avg in averages.values():
        model = {k: 1.0 / len(avg.keys) for k in avg.keys}
        for use_trend in (False, True):
            shift = shift_from_poll_average(avg, model, 0.15, use_trend=use_trend)
            assert all(np.isfinite(v) and abs(v) < 3.0 for v in shift.values())


# --------------------------------------------------------------------------- generation edge cases
def test_uncontested_groups_and_withdrawn_lines(polling_config) -> None:
    truth = {
        ("house_district", "NB-01"): {"PA": 1.0},  # uncontested
        ("house_district", "NB-02"): {"PA": 0.55, "SAP": 0.45, "DM": 0.0},  # DM withdrew
        ("house_district", "NB-03"): {"PA": 0.5, "SAP": 0.5},
    }
    polls = generate_polls(
        truth, polling_config.pollsters, {"house_district": 30}, ELECTION, 9, config=polling_config
    )
    assert len(polls) == 30  # the uncontested district's share goes to the contested ones
    assert {p.geo_code for p in polls} <= {"NB-02", "NB-03"}
    assert all("DM" not in p.results for p in polls)
    assert ("house_district", "NB-01") not in polls.allocation


def test_generation_independent_of_pollster_order(polling_config) -> None:
    truth = {NAT: {"PA": 0.5, "SAP": 0.3, "VLP": 0.2}}
    a = generate_polls(
        truth, polling_config.pollsters, {"national_president": 25}, ELECTION, 3, config=polling_config
    )
    b = generate_polls(
        truth,
        list(reversed(polling_config.pollsters)),
        {"national_president": 25},
        ELECTION,
        3,
        config=polling_config,
    )
    pd.testing.assert_frame_equal(a.frames()[0], b.frames()[0])
    pd.testing.assert_frame_equal(a.frames()[1], b.frames()[1])


def test_non_finite_competitiveness_falls_back_to_truth(polling_config) -> None:
    truth = {
        ("province_president", "NB"): {"PA": 0.5, "SAP": 0.5},
        ("province_president", "LI"): {"PA": 0.85, "SAP": 0.15},
    }
    comp = {("province_president", "NB"): float("nan")}
    polls = generate_polls(
        truth, polling_config.pollsters, {"province_president": 100}, ELECTION, 2, comp, config=polling_config
    )
    assert polls.allocation[("province_president", "NB")] > polls.allocation[("province_president", "LI")]


def test_many_options_keep_results_plus_undecided_at_100(polling_config) -> None:
    keys = [f"P{i:02d}" for i in range(14)]
    truth = {("generic_house", "NL"): dict.fromkeys(keys, 1.0 / 14)}
    gen = GenerationConfig(
        undecided_start_pct=0.0, undecided_end_pct=0.0, undecided_sd_pct=0.0, undecided_population_offset={}
    )
    pollsters = [PollsterConfig(name="Toetsbureau Veel", method="phone")]
    polls = generate_polls(truth, pollsters, {"generic_house": 200}, ELECTION, 12, config=gen)
    for p in polls:
        assert p.undecided_pct >= 0.0
        assert sum(p.results.values()) + p.undecided_pct == pytest.approx(100.0, abs=1e-6)
        assert all(v >= 0 for v in p.results.values())


def test_no_poll_on_or_after_election_day_even_for_short_campaigns(polling_config) -> None:
    spec = PollingSpec(
        start_date=ELECTION - timedelta(days=7),
        national_polls=50,
        province_polls=0,
        district_polls=0,
        senate_polls=0,
        governor_polls=0,
        generic_ballot_polls=0,
    )
    polls = generate_polls(
        {NAT: {"PA": 0.5, "SAP": 0.5}}, polling_config.pollsters, spec, ELECTION, 1, config=polling_config
    )
    assert len(polls) == 50
    assert all(spec.start_date <= p.start_date <= p.end_date < ELECTION for p in polls)
