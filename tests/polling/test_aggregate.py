"""Poll aggregation: weights, house effects, trend, undecided handling and edge cases."""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from app.core.rng import make_rng
from app.polling.aggregate import (
    aggregate_polls,
    averages_frame,
    generic_ballot_average,
    local_linear_weights,
)
from app.polling.config import AggregationConfig, HouseEffectSettings, PollingConfig, PollsterConfig
from app.polling.generate import generate_polls

AS_OF = date(2028, 11, 6)
NAT = ("national_president", "NL")


def _no_he(**kw) -> AggregationConfig:
    return AggregationConfig(house_effects=HouseEffectSettings(enabled=False), **kw)


# --------------------------------------------------------------------------- edge cases
def test_empty_inputs_return_empty(agg_config, make_frames) -> None:
    empty = pd.DataFrame(columns=["poll_id", "pollster", "poll_type", "end_date", "sample_size"])
    assert (
        aggregate_polls(empty, pd.DataFrame(columns=["poll_id", "key", "value_pct"]), agg_config, AS_OF) == {}
    )
    polls, results = make_frames([{"results": {"PA": 50, "SAP": 50}, "end_date": AS_OF + timedelta(days=3)}])
    assert aggregate_polls(polls, results, agg_config, AS_OF) == {}


def test_missing_columns_raise(agg_config, make_frames) -> None:
    polls, results = make_frames([{"results": {"PA": 50, "SAP": 50}}])
    with pytest.raises(ValueError):
        aggregate_polls(polls.drop(columns=["sample_size"]), results, agg_config, AS_OF)
    with pytest.raises(ValueError):
        aggregate_polls(polls, results.drop(columns=["value_pct"]), agg_config, AS_OF)
    with pytest.raises(ValueError):
        aggregate_polls(polls, results, agg_config, AS_OF, group_by=("region",))


def test_single_poll(agg_config, make_frames) -> None:
    polls, results = make_frames(
        [
            {
                "results": {"PA": 45.0, "SAP": 35.0},
                "undecided_pct": 20.0,
                "sample_size": 1000,
                "end_date": AS_OF,
            }
        ]
    )
    avg = aggregate_polls(polls, results, agg_config, AS_OF)[NAT]
    assert avg.n_polls == 1
    assert avg.keys == ["PA", "SAP"]
    assert avg.mean["PA"] == pytest.approx(56.25)
    assert avg.mean["SAP"] == pytest.approx(43.75)
    assert avg.trend_latest["PA"] == pytest.approx(56.25)
    # se: binomial on the 800 decided respondents plus the 1 pp non-sampling floor
    p = 0.5625
    expected = np.sqrt(p * (1 - p) / 800 * 1e4 + 1.0)
    assert avg.se["PA"] == pytest.approx(expected, rel=1e-6)
    assert avg.lower["PA"] < avg.mean["PA"] < avg.upper["PA"]
    assert avg.total_se["PA"] > avg.se["PA"]
    assert avg.effective_n == pytest.approx(800)
    assert avg.data_category == "SIMULATED"
    assert avg.leader() == "PA"
    assert avg.margin("PA", "SAP") == pytest.approx(12.5)


def test_polls_after_as_of_and_too_old_are_ignored(make_frames) -> None:
    polls, results = make_frames(
        [
            {"results": {"PA": 40, "SAP": 60}, "end_date": AS_OF - timedelta(days=2)},
            {"results": {"PA": 90, "SAP": 10}, "end_date": AS_OF + timedelta(days=1)},
            {"results": {"PA": 10, "SAP": 90}, "end_date": AS_OF - timedelta(days=400)},
        ]
    )
    avg = aggregate_polls(polls, results, _no_he(max_age_days=150), AS_OF)[NAT]
    assert avg.n_polls == 1
    assert avg.mean["PA"] == pytest.approx(40)


def test_min_polls(make_frames) -> None:
    polls, results = make_frames([{"results": {"PA": 40, "SAP": 60}}])
    assert aggregate_polls(polls, results, _no_he(min_polls=2), AS_OF) == {}


# --------------------------------------------------------------------------- weights
def test_recency_weight_halves_per_half_life(make_frames) -> None:
    polls, results = make_frames(
        [
            {
                "poll_id": 1,
                "pollster": "Alpha Toetsing",
                "results": {"PA": 30, "SAP": 70},
                "end_date": AS_OF - timedelta(days=28),
            },
            {"poll_id": 2, "pollster": "Beta Toetsing", "results": {"PA": 50, "SAP": 50}, "end_date": AS_OF},
        ]
    )
    cfg = _no_he(recency_half_life_days=14)
    avg = aggregate_polls(polls, results, cfg, AS_OF)[NAT]
    w = avg.weights.set_index("poll_id")
    assert w.loc[1, "w_recency"] == pytest.approx(0.25)
    assert w.loc[2, "w_recency"] == pytest.approx(1.0)
    assert w.loc[2, "weight"] == pytest.approx(0.8)
    assert avg.mean["PA"] == pytest.approx(0.2 * 30 + 0.8 * 50)
    assert avg.weights["weight"].sum() == pytest.approx(1.0)


def test_sample_size_weight_is_capped(make_frames) -> None:
    polls, results = make_frames(
        [
            {
                "poll_id": 1,
                "pollster": "A One",
                "sample_size": 750,
                "results": {"PA": 40, "SAP": 60},
                "end_date": AS_OF,
            },
            {
                "poll_id": 2,
                "pollster": "B Two",
                "sample_size": 3000,
                "results": {"PA": 40, "SAP": 60},
                "end_date": AS_OF,
            },
            {
                "poll_id": 3,
                "pollster": "C Three",
                "sample_size": 12000,
                "results": {"PA": 40, "SAP": 60},
                "end_date": AS_OF,
            },
        ]
    )
    avg = aggregate_polls(polls, results, _no_he(sample_size_cap=3000, sample_size_exponent=0.5), AS_OF)[NAT]
    w = avg.weights.set_index("poll_id")["w_sample"]
    assert w[1] == pytest.approx(0.5)
    assert w[2] == pytest.approx(1.0)
    assert w[3] == pytest.approx(1.0)


def test_rating_weights_and_per_poll_override(make_frames) -> None:
    pollsters = [
        PollsterConfig(name="Hoog Kwaliteit", rating=1.5),
        PollsterConfig(name="Laag Kwaliteit", rating=0.5),
    ]
    cfg = PollingConfig(
        pollsters=pollsters, aggregation=AggregationConfig(house_effects=HouseEffectSettings(enabled=False))
    )
    rows = [
        {"poll_id": 1, "pollster": "Hoog Kwaliteit", "results": {"PA": 60, "SAP": 40}, "end_date": AS_OF},
        {"poll_id": 2, "pollster": "Laag Kwaliteit", "results": {"PA": 40, "SAP": 60}, "end_date": AS_OF},
        {"poll_id": 3, "pollster": "Onbekend Bureau", "results": {"PA": 50, "SAP": 50}, "end_date": AS_OF},
    ]
    polls, results = make_frames(rows)
    avg = aggregate_polls(polls, results, cfg, AS_OF)[NAT]
    w = avg.weights.set_index("poll_id")
    assert w.loc[1, "w_rating"] == pytest.approx(1.5)
    assert w.loc[2, "w_rating"] == pytest.approx(0.5)
    assert w.loc[3, "w_rating"] == pytest.approx(cfg.aggregation.default_rating)
    assert avg.mean["PA"] > 50  # the better-rated pollster dominates
    # per-poll manual override wins over the pollster rating
    rows[1]["quality_rating"] = 3.0
    polls, results = make_frames(rows)
    avg2 = aggregate_polls(polls, results, cfg, AS_OF)[NAT]
    assert avg2.weights.set_index("poll_id").loc[2, "w_rating"] == pytest.approx(3.0)
    assert avg2.mean["PA"] < 50
    # rating weighting can be switched off
    cfg_off = cfg.model_copy(
        update={"aggregation": cfg.aggregation.model_copy(update={"rating_weighting": False})}
    )
    avg3 = aggregate_polls(polls, results, cfg_off, AS_OF)[NAT]
    assert avg3.mean["PA"] == pytest.approx(50)


def test_database_rating_column_is_used(make_frames) -> None:
    polls, results = make_frames(
        [
            {"poll_id": 1, "pollster": "Hoog Kwaliteit", "results": {"PA": 60, "SAP": 40}, "end_date": AS_OF},
            {"poll_id": 2, "pollster": "Laag Kwaliteit", "results": {"PA": 40, "SAP": 60}, "end_date": AS_OF},
        ]
    )
    polls["pollster_rating"] = [2.0, 1.0]
    avg = aggregate_polls(polls, results, _no_he(), AS_OF)[NAT]
    assert avg.weights.set_index("poll_id")["w_rating"].tolist() == [2.0, 1.0]


def test_population_and_method_weights(make_frames) -> None:
    rows = [
        {
            "poll_id": 1,
            "pollster": "A One",
            "population": "LV",
            "method": "phone",
            "results": {"PA": 50, "SAP": 50},
            "end_date": AS_OF,
        },
        {
            "poll_id": 2,
            "pollster": "B Two",
            "population": "A",
            "method": "ivr",
            "results": {"PA": 50, "SAP": 50},
            "end_date": AS_OF,
        },
        {
            "poll_id": 3,
            "pollster": "C Three",
            "population": "RV",
            "method": "smoke-signals",
            "results": {"PA": 50, "SAP": 50},
            "end_date": AS_OF,
        },
    ]
    polls, results = make_frames(rows)
    cfg = _no_he()
    w = aggregate_polls(polls, results, cfg, AS_OF)[NAT].weights.set_index("poll_id")
    assert (
        w.loc[1, "w_population"] == 1.0
        and w.loc[2, "w_population"] == 0.7
        and w.loc[3, "w_population"] == 0.85
    )
    assert w.loc[1, "w_method"] == 1.0 and w.loc[2, "w_method"] == 0.85
    assert w.loc[3, "w_method"] == cfg.default_method_weight


def test_pollster_volume_discount(make_frames) -> None:
    rows = [
        {"poll_id": i, "pollster": "Veel Peilingen", "results": {"PA": 60, "SAP": 40}, "end_date": AS_OF}
        for i in range(1, 5)
    ]
    rows.append(
        {"poll_id": 9, "pollster": "Eenmalig Bureau", "results": {"PA": 40, "SAP": 60}, "end_date": AS_OF}
    )
    polls, results = make_frames(rows)
    avg = aggregate_polls(polls, results, _no_he(pollster_volume_exponent=0.5), AS_OF)[NAT]
    w = avg.weights.set_index("poll_id")
    assert w.loc[1, "w_volume"] == pytest.approx(0.5)
    assert w.loc[9, "w_volume"] == pytest.approx(1.0)
    # 4 polls × 0.5 vs 1 poll × 1 → flooding pollster has 2/3 of the weight, not 4/5
    assert avg.mean["PA"] == pytest.approx(60 * 2 / 3 + 40 / 3)


def test_interval_uses_configured_level(make_frames) -> None:
    polls, results = make_frames([{"results": {"PA": 40, "SAP": 60}, "end_date": AS_OF}])
    for level in (0.8, 0.9, 0.95):
        avg = aggregate_polls(polls, results, _no_he(interval_level=level), AS_OF)[NAT]
        z = norm.ppf(0.5 + level / 2)
        assert avg.upper["PA"] - avg.mean["PA"] == pytest.approx(z * avg.se["PA"])
        assert avg.interval_level == level


def test_between_poll_dispersion_widens_se(make_frames) -> None:
    tight = [
        {"poll_id": i, "pollster": f"Bureau {i}", "results": {"PA": 50, "SAP": 50}, "end_date": AS_OF}
        for i in range(1, 7)
    ]
    wide = [
        {
            "poll_id": i,
            "pollster": f"Bureau {i}",
            "results": {"PA": 50 + (8 if i % 2 else -8), "SAP": 50 - (8 if i % 2 else -8)},
            "end_date": AS_OF,
        }
        for i in range(1, 7)
    ]
    se_tight = aggregate_polls(*make_frames(tight), _no_he(), AS_OF)[NAT].se["PA"]
    se_wide = aggregate_polls(*make_frames(wide), _no_he(), AS_OF)[NAT].se["PA"]
    assert se_wide > 2 * se_tight


# --------------------------------------------------------------------------- undecided
def test_undecided_modes(make_frames) -> None:
    rows = [{"results": {"PA": 36.0, "SAP": 44.0}, "undecided_pct": 20.0, "end_date": AS_OF}]
    polls, results = make_frames(rows)
    prop = aggregate_polls(polls, results, _no_he(undecided="proportional"), AS_OF)[NAT]
    keep = aggregate_polls(polls, results, _no_he(undecided="keep"), AS_OF)[NAT]
    norm_ = aggregate_polls(polls, results, _no_he(undecided="normalize"), AS_OF)[NAT]
    assert prop.mean == pytest.approx({"SAP": 55.0, "PA": 45.0})
    assert keep.mean == pytest.approx({"SAP": 44.0, "PA": 36.0})
    assert norm_.mean == pytest.approx({"SAP": 55.0, "PA": 45.0})
    assert prop.undecided_mean == pytest.approx(20.0)
    assert prop.undecided_mode == "proportional"
    # proportional reallocation keeps the ratio between options
    assert prop.mean["SAP"] / prop.mean["PA"] == pytest.approx(44 / 36)


def test_favorability_is_exempt_from_undecided_reallocation(make_frames) -> None:
    rows = [
        {
            "poll_type": "favorability",
            "results": {"favorable": 42.0, "unfavorable": 38.0},
            "undecided_pct": 20.0,
            "end_date": AS_OF,
        }
    ]
    avg = aggregate_polls(*make_frames(rows), _no_he(), AS_OF)[("favorability", "NL")]
    assert avg.mean["favorable"] == pytest.approx(42.0)


def test_keys_missing_from_some_polls(make_frames) -> None:
    rows = [
        {"poll_id": 1, "pollster": "A One", "results": {"PA": 40, "SAP": 50, "RV": 10}, "end_date": AS_OF},
        {"poll_id": 2, "pollster": "B Two", "results": {"PA": 44, "SAP": 56}, "end_date": AS_OF},
    ]
    avg = aggregate_polls(*make_frames(rows), _no_he(), AS_OF)[NAT]
    assert set(avg.keys) == {"PA", "SAP", "RV"}
    assert avg.mean["RV"] == pytest.approx(10.0)
    assert avg.mean["PA"] == pytest.approx(42.0)
    assert np.isnan(avg.poll_values.loc[2, "RV"])


# --------------------------------------------------------------------------- grouping
def test_grouping_and_generic_ballot(make_frames) -> None:
    rows = [
        {
            "poll_type": "province_president",
            "geo_code": "NB",
            "results": {"PA": 40, "SAP": 60},
            "end_date": AS_OF,
        },
        {
            "poll_type": "province_president",
            "geo_code": "ZH",
            "results": {"PA": 55, "SAP": 45},
            "end_date": AS_OF,
        },
        {"poll_type": "generic_house", "geo_code": None, "results": {"PA": 48, "SAP": 52}, "end_date": AS_OF},
        {"poll_type": "generic_house", "geo_code": None, "results": {"PA": 50, "SAP": 50}, "end_date": AS_OF},
    ]
    polls, results = make_frames(rows)
    avgs = aggregate_polls(polls, results, _no_he(), AS_OF)
    assert set(avgs) == {("province_president", "NB"), ("province_president", "ZH"), ("generic_house", "NL")}
    assert avgs[("province_president", "ZH")].mean["PA"] == pytest.approx(55)
    by_type = aggregate_polls(polls, results, _no_he(), AS_OF, group_by=("poll_type",))
    assert by_type[("province_president",)].n_polls == 2
    assert by_type[("province_president",)].geo_code == "NB,ZH"
    gb = generic_ballot_average(polls, results, _no_he(), AS_OF)
    assert gb is not None and gb.n_polls == 2 and gb.geo_code == "NL"
    assert gb.mean["SAP"] == pytest.approx(51)
    table = averages_frame(avgs)
    assert len(table) == 6
    assert set(table["data_category"]) == {"SIMULATED"}
    assert generic_ballot_average(polls[polls.poll_type != "generic_house"], results, _no_he(), AS_OF) is None


def test_to_dict_is_json_serialisable(make_frames) -> None:
    rows = [
        {"poll_id": i, "results": {"PA": 40 + i, "SAP": 60 - i}, "end_date": AS_OF - timedelta(days=i)}
        for i in range(5)
    ]
    avg = aggregate_polls(*make_frames(rows), AggregationConfig(), AS_OF)[NAT]
    payload = json.loads(json.dumps(avg.to_dict()))
    assert payload["data_category"] == "SIMULATED"
    assert len(payload["trend"]["dates"]) == len(avg.trend)
    assert len(payload["weights"]) == 5


# --------------------------------------------------------------------------- house effects
def _house_effect_run(n_polls: int = 240, seed: int = 7):
    truth = {NAT: {"PA": 0.34, "SAP": 0.31, "VLP": 0.15, "DM": 0.12, "CVU": 0.08}}
    true_effects = {
        "Alpha Toetsing": {"PA": 2.0, "SAP": -2.0},
        "Beta Toetsing": {"PA": -2.0, "SAP": 2.0},
        "Gamma Toetsing": {"VLP": 1.5, "DM": -1.5},
        "Delta Toetsing": {"VLP": -1.5, "DM": 1.5},
    }
    pollsters = [
        PollsterConfig(name=n, house_effects=h, typical_sample=1500, method="phone")
        for n, h in true_effects.items()
    ]
    cfg = PollingConfig(pollsters=pollsters)
    gen = cfg.generation.model_copy(update={"house_effect_jitter_pp": 0.0})
    polls = generate_polls(
        truth, pollsters, {"national_president": n_polls}, date(2028, 11, 7), seed, config=gen
    )
    return polls, true_effects, pollsters


def test_house_effect_recovery_on_generated_polls() -> None:
    polls, true_effects, pollsters = _house_effect_run()
    blank = [PollsterConfig(name=p.name) for p in pollsters]  # no prior knowledge of the leans
    agg = AggregationConfig(house_effects=HouseEffectSettings(shrinkage=1.0))
    avg = aggregate_polls(*polls.frames(), agg, AS_OF, pollsters=blank)[NAT]
    est = avg.house_effect_matrix()
    for name, effects in true_effects.items():
        for key in ("PA", "SAP", "VLP", "DM", "CVU"):
            assert est.loc[name, key] == pytest.approx(effects.get(key, 0.0), abs=0.6), (name, key)
    # identifiability: effects are anchored to the (zero) prior mean
    assert abs(avg.house_effects.groupby("key")["estimated_pp"].mean()).max() < 0.3


def test_house_effects_shrink_toward_prior() -> None:
    polls, _, pollsters = _house_effect_run(n_polls=40, seed=11)
    blank = [PollsterConfig(name=p.name) for p in pollsters]
    loose = aggregate_polls(
        *polls.frames(),
        AggregationConfig(house_effects=HouseEffectSettings(shrinkage=0.5)),
        AS_OF,
        pollsters=blank,
    )[NAT]
    tight = aggregate_polls(
        *polls.frames(),
        AggregationConfig(house_effects=HouseEffectSettings(shrinkage=500.0)),
        AS_OF,
        pollsters=blank,
    )[NAT]
    assert tight.house_effects["estimated_pp"].abs().max() < 0.2
    assert loose.house_effects["estimated_pp"].abs().max() > 1.0


def test_prior_only_house_effects_are_applied_exactly(make_frames) -> None:
    pollsters = [PollsterConfig(name="Alpha Toetsing", house_effects={"PA": 2.0, "SAP": -2.0})]
    cfg = PollingConfig(
        pollsters=pollsters, aggregation=AggregationConfig(house_effects=HouseEffectSettings(estimate=False))
    )
    polls, results = make_frames(
        [{"pollster": "Alpha Toetsing", "results": {"PA": 52, "SAP": 48}, "end_date": AS_OF}]
    )
    avg = aggregate_polls(polls, results, cfg, AS_OF)[NAT]
    assert avg.mean == pytest.approx({"PA": 50.0, "SAP": 50.0})
    he = avg.house_effects.set_index("key")
    assert he.loc["PA", "prior_pp"] == 2.0 and he.loc["PA", "estimated_pp"] == 2.0


def test_house_effect_adjustment_resists_flooding_pollster(make_frames) -> None:
    """A flooding pollster with a strong lean biases the raw average; house effects are measured
    against the average *pollster*, so the adjusted average moves to the pollsters' consensus."""
    rng = make_rng(3, "test", "flood")
    rows = []
    pid = 0
    for day in range(60):
        end = AS_OF - timedelta(days=day)
        for pollster, lean, every in (
            ("Luid Bureau", 4.0, 1),
            ("Stil Bureau A", -1.0, 4),
            ("Stil Bureau B", 1.0, 4),
        ):
            if day % every:
                continue
            pid += 1
            pa = 40.0 + lean + rng.normal(0, 1.0)
            rows.append(
                {
                    "poll_id": pid,
                    "pollster": pollster,
                    "results": {"PA": pa, "SAP": 100 - pa},
                    "end_date": end,
                }
            )
    polls, results = make_frames(rows)
    blank = [PollsterConfig(name=n) for n in ("Luid Bureau", "Stil Bureau A", "Stil Bureau B")]
    base = AggregationConfig(pollster_volume_exponent=0.0, recency_half_life_days=30)
    raw = aggregate_polls(
        polls,
        results,
        base.model_copy(update={"house_effects": HouseEffectSettings(enabled=False)}),
        AS_OF,
        pollsters=blank,
    )[NAT]
    adj = aggregate_polls(
        polls,
        results,
        base.model_copy(update={"house_effects": HouseEffectSettings(shrinkage=1.0)}),
        AS_OF,
        pollsters=blank,
    )[NAT]
    consensus = 40.0 + (4.0 - 1.0 + 1.0) / 3  # truth + the average pollster's lean
    assert abs(raw.mean["PA"] - consensus) > 1.0
    assert abs(adj.mean["PA"] - consensus) < 0.5
    est = adj.house_effect_matrix()["PA"]
    assert est["Luid Bureau"] - est["Stil Bureau A"] == pytest.approx(5.0, abs=0.7)
    assert est["Luid Bureau"] - est["Stil Bureau B"] == pytest.approx(3.0, abs=0.7)
    assert est.mean() == pytest.approx(0.0, abs=1e-6)
    loud = adj.poll_values.loc[[r["poll_id"] for r in rows if r["pollster"] == "Luid Bureau"], "PA"].mean()
    quiet = adj.poll_values.loc[[r["poll_id"] for r in rows if r["pollster"] != "Luid Bureau"], "PA"].mean()
    assert abs(loud - quiet) < 0.7


# --------------------------------------------------------------------------- trend
def test_trend_tracks_moving_truth(make_frames) -> None:
    rng = make_rng(5, "test", "trend")
    days = 100
    truth = {AS_OF - timedelta(days=d): 30.0 + 10.0 * (days - d) / days for d in range(days + 1)}
    rows = []
    for d in range(days, -1, -1):
        end = AS_OF - timedelta(days=d)
        for j, pollster in enumerate(("Alpha Toetsing", "Beta Toetsing", "Gamma Toetsing")):
            if (d + j) % 2:
                continue
            pa = truth[end] + rng.normal(0, 1.5)
            rows.append(
                {
                    "pollster": pollster,
                    "results": {"PA": pa, "SAP": 100 - pa},
                    "end_date": end,
                    "sample_size": 1500,
                }
            )
    polls, results = make_frames(rows)
    avg = aggregate_polls(
        polls, results, AggregationConfig(max_age_days=None, trend_bandwidth_days=7), AS_OF
    )[NAT]
    trend = avg.trend["PA"]
    assert trend.index[-1] == pd.Timestamp(AS_OF)
    errors = np.array([trend[pd.Timestamp(d)] - v for d, v in truth.items()])
    assert np.abs(errors).max() < 2.0
    assert np.abs(errors).mean() < 0.6
    # the local-linear trend follows the movement to the end; the recency-weighted mean lags
    assert abs(avg.trend_latest["PA"] - 40.0) < 1.2
    assert avg.mean["PA"] < avg.trend_latest["PA"]
    assert (avg.trend_se["PA"] > 0).all()


def test_local_linear_reproduces_lines_and_constants() -> None:
    t_obs = np.array([-30.0, -20.0, -12.0, -5.0, -1.0])
    w = np.array([1.0, 2.0, 1.0, 0.5, 1.0])
    t_eval = np.arange(-35.0, 1.0)
    L = local_linear_weights(t_eval, t_obs, w, bandwidth=6.0, ridge=0.0)
    assert np.allclose(L.sum(axis=1), 1.0)
    line = 3.0 + 0.2 * t_obs
    assert np.allclose(L @ line, 3.0 + 0.2 * t_eval)
    # a single observation falls back to a local constant
    L1 = local_linear_weights(t_eval, t_obs[:1], w[:1], bandwidth=6.0, ridge=0.0)
    assert np.allclose(L1, 1.0)
    # no usable observations → zeros
    assert not local_linear_weights(t_eval, t_obs, np.zeros(5), 6.0, 0.1).any()
    # far extrapolation does not underflow
    far = local_linear_weights(np.array([-2000.0]), t_obs, w, bandwidth=2.0, ridge=0.1)
    assert np.isfinite(far).all() and far.sum() == pytest.approx(1.0)


def test_aggregation_on_generated_polls_is_accurate(polling_config) -> None:
    truth = {NAT: {"PA": 0.36, "SAP": 0.30, "VLP": 0.16, "DM": 0.10, "CVU": 0.08}}
    gen = polling_config.generation.model_copy(update={"house_effect_jitter_pp": 0.3})
    spec = {"national_president": 80}
    polls = generate_polls(truth, polling_config.pollsters, spec, date(2028, 11, 7), 21, config=gen)
    avg = aggregate_polls(*polls.frames(), polling_config, AS_OF)[NAT]
    industry = polls.true_industry_error_pp(*NAT, truth[NAT])
    for key, share in truth[NAT].items():
        # average ≈ truth + industry error (which no aggregate can remove) within a few SEs
        target = 100 * share + industry[key]
        assert abs(avg.trend_latest[key] - target) < 2.0 + 3 * avg.se[key], key
