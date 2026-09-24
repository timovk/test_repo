"""Bayesian blending of a poll average with the structural model's expectation.

The vote model is a multinomial logit: a party's share is ``softmax(utility)``.  Adding
``log(y_k / m_k)`` to every party's utility turns model shares ``m`` into ``y`` exactly, so the
natural "logit space" for conditioning the model on polls is the log-share (multinomial-logit)
scale.  For each key present in both the polls and the model:

* model prior on the true log-share: ``θ_k ~ N(log m_k, σ²)`` with ``σ = model_prior_sd``;
* poll likelihood: ``log y_k ~ N(θ_k, s_k²)`` with ``s_k = se_k / y_k`` (delta method);
* posterior mean ``log m_k + w_k (log y_k − log m_k)`` with precision weight
  ``w_k = σ² / (σ² + s_k²)``.

The returned shift ``w_k (log y_k − log m_k)`` is the utility (logit) shift the forecasting
engine adds to party ``k``.  Before taking log ratios the poll shares are rescaled so that, over
the common keys, they sum to the model's total — options missing from the polls keep their model
share and the result does not depend on whether shares are given in 0–1 or percent (as long as
``poll_average`` and ``poll_se`` use the same unit).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from app.polling.aggregate import PollAverage

_MIN_SHARE = 1e-4


@dataclass(frozen=True)
class BlendResult:
    """Details of a poll/model blend (all dicts keyed by option key)."""

    shift: dict[str, float]  # logit (utility) shift per key present in polls and model
    weight: dict[str, float]  # poll weight w_k ∈ [0, 1]
    poll_log_ratio: dict[str, float]  # log(y_k / m_k) after rescaling
    poll_logit_se: dict[str, float]  # s_k
    posterior_sd: dict[str, float]  # sqrt(σ² s_k² / (σ² + s_k²))
    posterior_shares: dict[str, float]  # softmax(log m + shift) over all model keys


def blend_polls_with_model(
    poll_average: Mapping[str, float],
    poll_se: Mapping[str, float],
    model_expected: Mapping[str, float],
    model_prior_sd: float,
) -> BlendResult:
    """Precision-weighted blend of poll shares with model shares in multinomial-logit space."""
    if not math.isfinite(float(model_prior_sd)) or model_prior_sd < 0:
        raise ValueError("model_prior_sd must be a finite, non-negative number")
    model = {str(k): float(v) for k, v in model_expected.items() if v is not None and math.isfinite(float(v))}
    # A key is informative only with a finite positive poll share and a known (non-NaN) standard
    # error; +inf SE is allowed (the poll gets zero weight), NaN SE is treated as "not polled".
    common = [
        k
        for k in model
        if k in poll_average
        and k in poll_se
        and poll_average[k] is not None
        and poll_se[k] is not None
        and math.isfinite(float(poll_average[k]))
        and not math.isnan(float(poll_se[k]))
        and float(poll_average[k]) > 0
        and model[k] > 0
    ]
    shift: dict[str, float] = {}
    weight: dict[str, float] = {}
    ratio: dict[str, float] = {}
    logit_se: dict[str, float] = {}
    post_sd: dict[str, float] = {}
    if common:
        y = np.array([float(poll_average[k]) for k in common])
        se = np.array([float(poll_se[k]) for k in common])
        m = np.array([model[k] for k in common])
        scale = m.sum() / y.sum()
        y_s = np.clip(y * scale, _MIN_SHARE * m.sum(), None)
        se_s = np.abs(se) * scale
        r = np.log(y_s) - np.log(np.clip(m, _MIN_SHARE * m.sum(), None))
        s = se_s / y_s
        sig2 = model_prior_sd**2
        with np.errstate(divide="ignore", invalid="ignore"):
            w = np.where(np.isinf(s), 0.0, np.where(s <= 0, 1.0 if sig2 > 0 else 0.0, sig2 / (sig2 + s**2)))
            psd = np.where(
                np.isinf(s),
                math.sqrt(sig2),
                np.where(s <= 0, 0.0, np.sqrt(sig2 * s**2 / np.maximum(sig2 + s**2, 1e-300))),
            )
        for i, k in enumerate(common):
            shift[k] = float(w[i] * r[i])
            weight[k] = float(w[i])
            ratio[k] = float(r[i])
            logit_se[k] = float(s[i])
            post_sd[k] = float(psd[i])
    keys = list(model)
    base = np.log(np.clip(np.array([model[k] for k in keys]), _MIN_SHARE, None)) if keys else np.zeros(0)
    post = base + np.array([shift.get(k, 0.0) for k in keys])
    if keys:
        e = np.exp(post - post.max())
        post_shares = e / e.sum() * sum(model.values())
    else:
        post_shares = np.zeros(0)
    return BlendResult(
        shift=shift,
        weight=weight,
        poll_log_ratio=ratio,
        poll_logit_se=logit_se,
        posterior_sd=post_sd,
        posterior_shares={k: float(v) for k, v in zip(keys, post_shares, strict=True)},
    )


def poll_environment_shift(
    poll_average: Mapping[str, float],
    poll_se: Mapping[str, float],
    model_expected: Mapping[str, float],
    model_prior_sd: float,
) -> dict[str, float]:
    """Logit (utility) shift per key that conditions the model on the polls.

    ``poll_average``/``poll_se`` (same unit) and ``model_expected`` (shares) are keyed by party /
    ticket key.  Keys missing from either side get no entry (i.e. no shift).
    """
    return blend_polls_with_model(poll_average, poll_se, model_expected, model_prior_sd).shift


def shift_from_poll_average(
    average: PollAverage,
    model_expected: Mapping[str, float],
    model_prior_sd: float,
    *,
    use_trend: bool = False,
    include_systematic: bool = True,
) -> dict[str, float]:
    """:func:`poll_environment_shift` for a :class:`~app.polling.aggregate.PollAverage`.

    By default the poll uncertainty includes the industry-wide systematic error
    (``PollAverage.total_se``), so a large pile of polls never fully overrides the model.
    """
    shares = average.shares(use_trend=use_trend, normalize=False)
    se = average.share_se(use_trend=use_trend, include_systematic=include_systematic)
    return poll_environment_shift(shares, se, model_expected, model_prior_sd)
