"""Bayesian poll/model blending in multinomial-logit space."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from app.polling.aggregate import aggregate_polls
from app.polling.blend import blend_polls_with_model, poll_environment_shift, shift_from_poll_average
from app.polling.config import AggregationConfig

MODEL = {"PA": 0.40, "SAP": 0.35, "VLP": 0.25}


def _softmax_shift(model: dict[str, float], shift: dict[str, float]) -> dict[str, float]:
    keys = list(model)
    u = np.log([model[k] for k in keys]) + np.array([shift.get(k, 0.0) for k in keys])
    e = np.exp(u - u.max())
    return dict(zip(keys, e / e.sum(), strict=True))


def test_no_shift_when_polls_match_model() -> None:
    shift = poll_environment_shift(MODEL, {k: 0.01 for k in MODEL}, MODEL, 0.1)
    assert shift == pytest.approx({k: 0.0 for k in MODEL})


def test_precision_weight_formula() -> None:
    polls = {"PA": 0.44, "SAP": 0.33, "VLP": 0.23}
    se = {"PA": 0.015, "SAP": 0.02, "VLP": 0.01}
    sigma = 0.08
    res = blend_polls_with_model(polls, se, MODEL, sigma)
    for k in MODEL:
        s = se[k] / polls[k]
        w = sigma**2 / (sigma**2 + s**2)
        assert res.weight[k] == pytest.approx(w)
        assert res.poll_log_ratio[k] == pytest.approx(math.log(polls[k] / MODEL[k]))
        assert res.shift[k] == pytest.approx(w * math.log(polls[k] / MODEL[k]))
        assert res.poll_logit_se[k] == pytest.approx(s)
        assert res.posterior_sd[k] == pytest.approx(math.sqrt(sigma**2 * s**2 / (sigma**2 + s**2)))
        assert 0 < res.weight[k] < 1
    # a more precise poll gets more weight
    assert res.weight["VLP"] > res.weight["SAP"]
    assert res.posterior_shares == pytest.approx(_softmax_shift(MODEL, res.shift))


def test_limits_of_the_blend() -> None:
    polls = {"PA": 0.30, "SAP": 0.45, "VLP": 0.25}
    se = {k: 1e-6 for k in polls}
    exact = blend_polls_with_model(polls, se, MODEL, 0.5)
    # precise polls (or a vague model) reproduce the poll shares exactly under the MNL
    assert exact.posterior_shares == pytest.approx(polls, abs=1e-4)
    # a certain model ignores the polls
    assert poll_environment_shift(polls, {k: 0.02 for k in polls}, MODEL, 0.0) == pytest.approx(
        {k: 0.0 for k in MODEL}
    )
    # infinitely uncertain polls are ignored
    assert poll_environment_shift(polls, {k: math.inf for k in polls}, MODEL, 0.1) == pytest.approx(
        {k: 0.0 for k in MODEL}
    )
    # larger model uncertainty → larger shift toward the polls
    small = poll_environment_shift(polls, {k: 0.02 for k in polls}, MODEL, 0.05)
    large = poll_environment_shift(polls, {k: 0.02 for k in polls}, MODEL, 0.30)
    assert abs(large["PA"]) > abs(small["PA"])
    with pytest.raises(ValueError):
        poll_environment_shift(polls, se, MODEL, -0.1)


def test_units_invariance_and_missing_keys() -> None:
    polls = {"PA": 0.42, "SAP": 0.38}
    se = {"PA": 0.02, "SAP": 0.02}
    as_share = poll_environment_shift(polls, se, MODEL, 0.1)
    as_pct = poll_environment_shift(
        {k: 100 * v for k, v in polls.items()}, {k: 100 * v for k, v in se.items()}, MODEL, 0.1
    )
    assert as_share == pytest.approx(as_pct)
    # VLP is not polled: no shift, and its posterior share stays (almost) put because the polled
    # options are rescaled to the model's total over the common keys
    assert "VLP" not in as_share
    post = blend_polls_with_model(polls, se, MODEL, 1.0).posterior_shares
    assert post["VLP"] == pytest.approx(MODEL["VLP"], abs=0.01)
    assert sum(post.values()) == pytest.approx(1.0)
    # keys only in the polls are ignored
    assert "RV" not in poll_environment_shift({**polls, "RV": 0.05}, {**se, "RV": 0.01}, MODEL, 0.1)


def test_shift_from_poll_average_includes_systematic_error(make_frames) -> None:
    rows = [
        {
            "poll_id": i,
            "pollster": f"Bureau {i}",
            "results": {"PA": 46, "SAP": 34, "VLP": 20},
            "end_date": date(2028, 11, 1),
        }
        for i in range(1, 30)
    ]
    avg = aggregate_polls(*make_frames(rows), AggregationConfig(), date(2028, 11, 1))[
        ("national_president", "NL")
    ]
    with_sys = shift_from_poll_average(avg, MODEL, 0.1)
    without = shift_from_poll_average(avg, MODEL, 0.1, include_systematic=False)
    full = math.log(0.46 / 0.40)
    assert 0 < with_sys["PA"] < without["PA"] < full
    assert shift_from_poll_average(avg, MODEL, 0.1, use_trend=True)["PA"] > 0
