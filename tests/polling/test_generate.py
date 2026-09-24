"""Fictional poll generation: determinism, sanity and the shared industry error."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.polling.config import GenerationConfig, PollingConfig, PollsterConfig
from app.polling.generate import (
    GeneratedPoll,
    competitiveness_from_shares,
    generate_polls,
    margin_of_error,
    polls_to_frames,
)
from app.polling.types import POLL_COLUMNS, RESULT_COLUMNS
from app.scenarios.schema import PollingSpec

ELECTION = date(2028, 11, 7)
PROVINCES = ["GR", "FR", "DR", "OV", "FL", "GE", "UT", "NH", "ZH", "ZE", "NB", "LI"]


def _truth() -> dict[tuple[str, str], dict[str, float]]:
    truth: dict[tuple[str, str], dict[str, float]] = {
        ("national_president", "NL"): {"PA": 0.35, "SAP": 0.32, "VLP": 0.15, "DM": 0.10, "CVU": 0.08},
        ("generic_house", "NL"): {"PA": 0.30, "SAP": 0.30, "VLP": 0.20, "DM": 0.20},
    }
    for i, pv in enumerate(PROVINCES):
        lead = -0.12 + 0.02 * i
        truth[("province_president", pv)] = {"PA": 0.35 + lead / 2, "SAP": 0.35 - lead / 2, "VLP": 0.3}
        truth[("governor", pv)] = {"PA": 0.5 + lead, "SAP": 0.5 - lead}
    for d in range(1, 9):
        truth[("house_district", f"NB-{d:02d}")] = {"PA": 0.42 + 0.01 * d, "SAP": 0.48 - 0.01 * d, "DM": 0.10}
    truth[("senate", "NB")] = {"PA": 0.5, "SAP": 0.5}
    return truth


@pytest.fixture(scope="module")
def run(polling_config: PollingConfig):
    return generate_polls(
        _truth(), polling_config.pollsters, PollingSpec(), ELECTION, 1234, config=polling_config
    )


def test_deterministic_for_seed(polling_config: PollingConfig, run) -> None:
    again = generate_polls(
        _truth(), polling_config.pollsters, PollingSpec(), ELECTION, 1234, config=polling_config
    )
    a_polls, a_res = run.frames()
    b_polls, b_res = again.frames()
    pd.testing.assert_frame_equal(a_polls, b_polls)
    pd.testing.assert_frame_equal(a_res, b_res)
    assert run.industry_error == again.industry_error
    other = generate_polls(
        _truth(), polling_config.pollsters, PollingSpec(), ELECTION, 99, config=polling_config
    )
    assert other.frames()[1]["value_pct"].tolist() != a_res["value_pct"].tolist()


def test_truth_insertion_order_does_not_matter(polling_config: PollingConfig, run) -> None:
    reordered = dict(reversed(list(_truth().items())))
    again = generate_polls(
        reordered, polling_config.pollsters, PollingSpec(), ELECTION, 1234, config=polling_config
    )
    pd.testing.assert_frame_equal(run.frames()[1], again.frames()[1])


def test_counts_follow_spec(run) -> None:
    spec = PollingSpec()
    polls, _ = run.frames()
    by_type = polls.groupby("poll_type").size().to_dict()
    assert by_type["national_president"] == spec.national_polls
    assert by_type["province_president"] == spec.province_polls
    assert by_type["governor"] == spec.governor_polls
    assert by_type["house_district"] == spec.district_polls
    assert by_type["senate"] == spec.senate_polls
    assert by_type["generic_house"] == spec.generic_ballot_polls
    assert sum(run.allocation.values()) == len(run)
    assert polls["poll_id"].tolist() == list(range(1, len(run) + 1))


def test_poll_fields_are_sane(run, polling_config: PollingConfig) -> None:
    gen = polling_config.generation
    start = ELECTION - timedelta(days=gen.default_campaign_days)
    for p in run:
        assert isinstance(p, GeneratedPoll)
        length = (p.end_date - p.start_date).days + 1
        assert gen.fieldwork_days_min <= length <= gen.fieldwork_days_max
        assert start <= p.start_date <= p.end_date < ELECTION
        assert p.sample_size >= gen.min_sample and p.sample_size % 10 == 0
        assert p.population in ("LV", "RV", "A")
        assert 0.0 < p.undecided_pct <= 40.0
        assert sum(p.results.values()) + p.undecided_pct == pytest.approx(100.0, abs=1e-6)
        assert all(v >= 0 for v in p.results.values())
        assert p.is_fictional and p.data_category == "SIMULATED" and "FICTIONAL" in p.source
        deff = gen.design_effect.get(p.method, gen.default_design_effect)
        assert p.margin_of_error == pytest.approx(margin_of_error(p.sample_size, deff), abs=0.051)
        assert set(p.results) == set(p.true_shares)
        assert sum(p.true_shares.values()) == pytest.approx(1.0)
    by_type = {p.poll_type: p for p in run}
    assert by_type["national_president"].race_code == "PRES"
    assert by_type["house_district"].race_code.startswith("HOUSE-NB-")
    assert by_type["senate"].race_code is None  # seat class unknown from a province code
    assert by_type["generic_house"].race_code is None


def test_frames_schema(run) -> None:
    polls, results = run.frames()
    assert list(polls.columns[: len(POLL_COLUMNS)]) == list(POLL_COLUMNS)
    assert list(results.columns) == list(RESULT_COLUMNS)
    assert len(results) == sum(len(p.results) for p in run)
    empty_polls, empty_results = polls_to_frames([])
    assert empty_polls.empty and empty_results.empty


def test_pollster_poll_types_respected(run, polling_config: PollingConfig) -> None:
    restricted = {p.name: set(p.poll_types) for p in polling_config.pollsters if p.poll_types is not None}
    assert restricted
    for p in run:
        if p.pollster in restricted:
            assert p.poll_type in restricted[p.pollster]


def test_polls_denser_near_election_and_undecided_declines(run) -> None:
    polls, _ = run.frames()
    days_before = (pd.Timestamp(ELECTION) - polls["end_date"]).dt.days
    assert (days_before <= 40).mean() > 0.4  # 1/3 of the window holds well over 1/3 of the polls
    late = polls.loc[days_before <= 21, "undecided_pct"].mean()
    early = polls.loc[days_before >= 80, "undecided_pct"].mean()
    assert late < early - 3.0


def test_truth_path_is_pinned_at_election_day(run) -> None:
    truth = _truth()
    last = [p for p in run if p.poll_type == "national_president" and (ELECTION - p.end_date).days <= 3]
    assert last
    for p in last:
        for k, v in truth[("national_president", "NL")].items():
            assert p.true_shares[k] == pytest.approx(v, abs=0.02)
    early = [p for p in run if p.poll_type == "national_president" and (ELECTION - p.end_date).days >= 60]
    drift = max(abs(p.true_shares["PA"] - 0.35) for p in early) if early else 0.0
    assert drift < 0.12  # opinion wanders, but stays plausible


def _clean_run(error_sd: float, seed: int, geo_fraction: float = 0.0):
    pollsters = [
        PollsterConfig(name=f"Neutraal Bureau {i}", typical_sample=2000, method="phone") for i in range(3)
    ]
    gen = GenerationConfig(house_effect_jitter_pp=0.0, geo_error_fraction=geo_fraction)
    truth = {
        ("national_president", "NL"): {"PA": 0.40, "SAP": 0.35, "VLP": 0.25},
        ("province_president", "NB"): {"PA": 0.45, "SAP": 0.30, "VLP": 0.25},
        ("province_president", "ZH"): {"PA": 0.35, "SAP": 0.40, "VLP": 0.25},
    }
    spec = PollingSpec(
        national_polls=150,
        province_polls=300,
        district_polls=0,
        senate_polls=0,
        governor_polls=0,
        generic_ballot_polls=0,
        true_polling_error_sd=error_sd,
    )
    competitiveness = {("province_president", "NB"): 1.0, ("province_president", "ZH"): 1.0}
    return generate_polls(truth, pollsters, spec, ELECTION, seed, competitiveness, config=gen)


def test_sampling_errors_within_expected_bounds() -> None:
    polls = _clean_run(error_sd=0.0, seed=5)
    gen = GenerationConfig()
    z = []
    for p in polls:
        deff = gen.design_effect[p.method]
        n_dec = p.sample_size * (1 - p.undecided_pct / 100) / deff
        for k, v in p.decided_shares.items():
            t = p.true_shares[k]
            z.append((v - t) / np.sqrt(t * (1 - t) / n_dec))
    z = np.array(z)
    assert abs(z.mean()) < 0.1
    assert 0.85 < z.std() < 1.15
    assert (np.abs(z) < 4.5).all()


def test_industry_error_is_shared_by_all_polls() -> None:
    polls = _clean_run(error_sd=0.05, seed=17)
    assert set(polls.industry_error) == {"PA", "SAP", "VLP"}
    raw: dict[tuple[str, str], list[float]] = {}
    shared: dict[tuple[str, str], list[float]] = {}
    residual: dict[tuple[str, str], list[float]] = {}
    for p in polls:
        group = (p.poll_type, p.geo_code)
        err = 100 * (p.decided_shares["PA"] - p.true_shares["PA"])
        # the shared logit error translated to share points at this poll's own true opinion
        expected = polls.true_industry_error_pp(p.poll_type, p.geo_code, p.true_shares)["PA"]
        raw.setdefault(group, []).append(err)
        shared.setdefault(group, []).append(expected)
        residual.setdefault(group, []).append(err - expected)
    assert len(raw) == 3
    signs = {np.sign(np.mean(v)) for v in shared.values()}
    assert len(signs) == 1  # one error for the whole industry
    for group in raw:
        # every group is shifted the same way, by a material amount …
        assert np.sign(np.mean(raw[group])) in signs
        assert abs(np.mean(raw[group])) > 1.0
        # … and once the shared error is removed only sampling noise remains
        assert np.mean(residual[group]) == pytest.approx(0.0, abs=0.5), group
    # regenerating with another seed draws another industry error
    assert _clean_run(error_sd=0.05, seed=18).industry_error != polls.industry_error


def test_province_error_shared_within_province() -> None:
    polls = _clean_run(error_sd=0.04, seed=23, geo_fraction=1.0)
    assert set(polls.geo_error) == {"NB", "ZH"}
    assert polls.geo_error["NB"] != polls.geo_error["ZH"]


def test_house_effects_enter_polls(polling_config: PollingConfig) -> None:
    pollsters = [
        PollsterConfig(name="Scheef Bureau", house_effects={"PA": 3.0, "SAP": -3.0}, typical_sample=3000)
    ]
    gen = GenerationConfig(house_effect_jitter_pp=0.0)
    truth = {("national_president", "NL"): {"PA": 0.45, "SAP": 0.45, "VLP": 0.10}}
    spec = PollingSpec(national_polls=200, true_polling_error_sd=0.0)
    polls = generate_polls(truth, pollsters, spec, ELECTION, 3, config=gen)
    bias = np.mean([100 * (p.decided_shares["PA"] - p.true_shares["PA"]) for p in polls])
    assert bias == pytest.approx(3.0, abs=0.4)
    assert polls.house_effects["Scheef Bureau"]["PA"] == 3.0


def test_competitive_geos_get_more_polls(polling_config: PollingConfig) -> None:
    truth = {
        ("province_president", "NB"): {"PA": 0.50, "SAP": 0.50},
        ("province_president", "LI"): {"PA": 0.80, "SAP": 0.20},
    }
    spec = PollingSpec(
        national_polls=0,
        province_polls=200,
        district_polls=0,
        senate_polls=0,
        governor_polls=0,
        generic_ballot_polls=0,
    )
    polls = generate_polls(truth, polling_config.pollsters, spec, ELECTION, 8, config=polling_config)
    counts = polls.allocation
    assert counts[("province_president", "NB")] > 3 * counts[("province_president", "LI")]
    # explicit competitiveness overrides the truth-derived value
    flipped = {("province_president", "NB"): 0.0, ("province_president", "LI"): 1.0}
    polls2 = generate_polls(
        truth, polling_config.pollsters, spec, ELECTION, 8, flipped, config=polling_config
    )
    assert (
        polls2.allocation[("province_president", "LI")] > 3 * polls2.allocation[("province_president", "NB")]
    )


def test_competitiveness_from_shares() -> None:
    assert competitiveness_from_shares({"A": 0.5, "B": 0.5}) == 1.0
    assert competitiveness_from_shares({"A": 0.8, "B": 0.2}) == 0.0
    assert competitiveness_from_shares({"A": 0.45, "B": 0.40, "C": 0.15}) == pytest.approx(0.75)
    assert competitiveness_from_shares({"A": 1.0}) == 0.0


def test_margin_of_error_formula() -> None:
    assert margin_of_error(1000) == pytest.approx(3.099, abs=0.001)
    assert margin_of_error(1000, design_effect=2.0) == pytest.approx(3.099 * np.sqrt(2), abs=0.002)


def test_input_validation(polling_config: PollingConfig) -> None:
    with pytest.raises(ValueError):
        generate_polls(
            {("exit_poll", "NL"): {"A": 0.5, "B": 0.5}},
            polling_config.pollsters,
            {},
            ELECTION,
            1,
            config=polling_config,
        )
    for bad in (-0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            generate_polls(
                {("governor", "NB"): {"A": 0.5, "B": bad}},
                polling_config.pollsters,
                {"governor": 3},
                ELECTION,
                1,
                config=polling_config,
            )
    with pytest.raises(ValueError):
        spec = PollingSpec(start_date=ELECTION - timedelta(days=3))
        generate_polls(_truth(), polling_config.pollsters, spec, ELECTION, 1, config=polling_config)


def test_uses_configured_pollsters_when_none_given(polling_config: PollingConfig) -> None:
    truth = {("national_president", "NL"): {"PA": 0.5, "SAP": 0.5}}
    polls = generate_polls(truth, [], {"national_president": 30}, ELECTION, 4, config=polling_config)
    assert {p.pollster for p in polls} <= {p.name for p in polling_config.pollsters}
    assert len(polls) == 30 and polls[0].poll_id == 1 and len(polls[:3]) == 3
