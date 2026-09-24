"""Poll aggregation: weighted averages, house effects, trend lines and uncertainty.

Pure pandas/NumPy.  Input frames (see :mod:`app.polling.types`):

* ``polls``: ``poll_id, pollster, poll_type, geo_code, start_date, end_date, sample_size,
  population, method, undecided_pct, quality_rating`` (optional extra column
  ``pollster_rating``, e.g. from the database);
* ``results``: ``poll_id, key, value_pct`` (percent of all respondents, 0–100).

Per group (default ``(poll_type, geo_code)``) the aggregate computes

1. undecided handling (proportional reallocation by default);
2. per-poll weights = recency × sample size × pollster rating × population × method ×
   pollster-volume discount (all configurable; the transparency table lists every factor);
3. house effects per pollster × key, estimated iteratively as the pollster's mean residual from
   the house-adjusted trend line, shrunk toward the configured prior;
4. the weighted mean of house-adjusted values with a standard error that combines binomial
   sampling error (effective sample size) and the excess between-poll dispersion, and a central
   interval (90 % by default);
5. a daily trend line (Gaussian-kernel local-linear regression on fieldwork end dates).

Everything here is SIMULATED data about FICTIONAL pollsters (docs/POLLING.md).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm

from app.core.logging import get_logger
from app.polling.config import AggregationConfig, HouseEffectSettings, PollingConfig, load_polling_config
from app.polling.types import NATIONAL_GEO, POLL_DATA_CATEGORY, PollsterLike, lookup_method

log = get_logger(__name__)

_REQUIRED_POLL_COLUMNS: tuple[str, ...] = ("poll_id", "pollster", "poll_type", "end_date", "sample_size")
_REQUIRED_RESULT_COLUMNS: tuple[str, ...] = ("poll_id", "key", "value_pct")
_P_CLIP = (0.005, 0.995)


# --------------------------------------------------------------------------- result type
@dataclass
class PollAverage:
    """Aggregate of one poll group (values in percentage points, 0–100).

    ``weights`` is the per-poll transparency table, ``house_effects`` the long-format table
    ``pollster, key, prior_pp, estimated_pp, n_polls``, ``poll_values`` the undecided- and
    house-adjusted value of every poll, ``trend``/``trend_se`` daily series (index = date).
    """

    group: tuple[Any, ...]
    poll_type: str
    geo_code: str
    as_of: date
    keys: list[str]
    mean: dict[str, float]
    se: dict[str, float]
    lower: dict[str, float]
    upper: dict[str, float]
    total_se: dict[str, float]
    interval_level: float
    n_polls: int
    effective_n: float
    effective_polls: float
    latest_poll_date: date
    undecided_mean: float | None
    undecided_mode: str
    weights: pd.DataFrame
    poll_values: pd.DataFrame
    house_effects: pd.DataFrame
    trend: pd.DataFrame
    trend_se: pd.DataFrame
    trend_latest: dict[str, float]
    data_category: str = POLL_DATA_CATEGORY

    # ------------------------------------------------------------------ convenience
    def shares(self, *, use_trend: bool = False, normalize: bool = True) -> dict[str, float]:
        """Average (or latest trend value) as shares 0–1, optionally normalised to sum 1."""
        src = self.trend_latest if use_trend else self.mean
        vals = {k: max(float(src[k]), 0.0) / 100.0 for k in self.keys}
        total = sum(vals.values())
        if normalize and total > 0:
            vals = {k: v / total for k, v in vals.items()}
        return vals

    def share_se(self, *, use_trend: bool = False, include_systematic: bool = False) -> dict[str, float]:
        """Standard errors in share units (0–1).

        ``include_systematic`` adds the configured industry-wide error (``total_se``) — use it when
        the average is treated as evidence about the *true* vote (e.g. when blending with a model).
        """
        if use_trend and not self.trend_se.empty:
            last = self.trend_se.iloc[-1]
            random = {k: float(last[k]) for k in self.keys}
        else:
            random = dict(self.se)
        out = {}
        for k in self.keys:
            sys2 = max(self.total_se[k] ** 2 - self.se[k] ** 2, 0.0) if include_systematic else 0.0
            out[k] = float(np.sqrt(random[k] ** 2 + sys2)) / 100.0
        return out

    def leader(self, *, use_trend: bool = False) -> str | None:
        src = self.trend_latest if use_trend else self.mean
        return max(self.keys, key=lambda k: src[k]) if self.keys else None

    def margin(self, a: str, b: str, *, use_trend: bool = False) -> float:
        """``a`` minus ``b`` in percentage points."""
        src = self.trend_latest if use_trend else self.mean
        return float(src[a] - src[b])

    def house_effect_matrix(self, column: str = "estimated_pp") -> pd.DataFrame:
        """Pollster × key matrix of house effects (``estimated_pp`` or ``prior_pp``)."""
        if self.house_effects.empty:
            return pd.DataFrame()
        return self.house_effects.pivot(index="pollster", columns="key", values=column)

    def to_dict(self, *, include_polls: bool = True, include_trend: bool = True) -> dict[str, Any]:
        """JSON-serialisable representation (API / export)."""
        out: dict[str, Any] = {
            "poll_type": self.poll_type,
            "geo_code": self.geo_code,
            "as_of": self.as_of.isoformat(),
            "keys": list(self.keys),
            "mean": _round_dict(self.mean),
            "se": _round_dict(self.se),
            "lower": _round_dict(self.lower),
            "upper": _round_dict(self.upper),
            "total_se": _round_dict(self.total_se),
            "interval_level": self.interval_level,
            "trend_latest": _round_dict(self.trend_latest),
            "n_polls": self.n_polls,
            "effective_n": round(self.effective_n, 1),
            "effective_polls": round(self.effective_polls, 2),
            "latest_poll_date": self.latest_poll_date.isoformat(),
            "undecided_mean": None if self.undecided_mean is None else round(self.undecided_mean, 2),
            "undecided_mode": self.undecided_mode,
            "house_effects": _records(self.house_effects),
            "data_category": self.data_category,
        }
        if include_polls:
            out["weights"] = _records(self.weights)
        if include_trend and not self.trend.empty:
            out["trend"] = {
                "dates": [d.date().isoformat() for d in self.trend.index],
                "series": {k: [round(float(v), 3) for v in self.trend[k].to_numpy()] for k in self.keys},
                "se": {k: [round(float(v), 3) for v in self.trend_se[k].to_numpy()] for k in self.keys},
            }
        return out


def _round_dict(d: Mapping[str, float], nd: int = 3) -> dict[str, float]:
    return {k: round(float(v), nd) for k, v in d.items()}


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%d")
    out = out.astype(object).where(out.notna(), None)
    return out.to_dict(orient="records")


# --------------------------------------------------------------------------- public API
def aggregate_polls(
    polls: pd.DataFrame,
    results: pd.DataFrame,
    config: PollingConfig | AggregationConfig | None,
    as_of: date | datetime | str,
    group_by: Sequence[str] = ("poll_type", "geo_code"),
    *,
    pollsters: Sequence[PollsterLike] | None = None,
) -> dict[tuple[Any, ...], PollAverage]:
    """Aggregate polls completed on or before ``as_of`` per group.

    Parameters
    ----------
    polls, results:
        Frames in the schema above (``results.value_pct`` in percent).
    config:
        A :class:`PollingConfig` (pollster ratings / prior house effects come from its pollsters),
        an :class:`AggregationConfig` (ratings/priors from ``pollsters`` only) or ``None`` for
        ``config/polling.yaml``.
    as_of:
        Date of the average; polls whose fieldwork ends later are ignored.
    group_by:
        Poll columns defining a group; the returned dict is keyed by tuples of their values.
    pollsters:
        Optional pollster descriptions overriding the configured ratings and prior house effects
        (e.g. the scenario's pollsters or rows loaded from the database).

    ``poll_id`` must be unique (``ValueError`` otherwise).  Polls with a configured weight of 0
    (pollster / per-poll rating 0, population or method weight 0) or without any positive reported
    value are excluded; options without weighted information are dropped rather than averaged to
    NaN.  Groups with fewer than ``min_polls`` remaining polls are omitted; an empty input yields
    ``{}``.  Nothing that ends after ``as_of`` influences any output (election-night safe).
    """
    agg, ratings, priors = _resolve_config(config, pollsters)
    as_of_ts = pd.Timestamp(as_of).normalize()
    group_cols = list(group_by)
    if not group_cols:
        raise ValueError("group_by must name at least one column")
    if polls is None or results is None or len(polls) == 0 or len(results) == 0:
        return {}
    df = _prepare_polls(polls, as_of_ts, agg)
    missing_group = [c for c in group_cols if c not in df.columns]
    if missing_group:
        raise ValueError(f"group_by columns not in polls frame: {missing_group}")
    wide = _results_matrix(results, df["poll_id"])
    df = df[df["poll_id"].isin(wide.index)]
    if df.empty:
        return {}

    out: dict[tuple[Any, ...], PollAverage] = {}
    for gkey, sub in df.groupby(group_cols, sort=True, dropna=False):
        key = gkey if isinstance(gkey, tuple) else (gkey,)
        if len(sub) < agg.min_polls:
            log.debug("poll group %s has %d polls (< min_polls=%d); skipped", key, len(sub), agg.min_polls)
            continue
        avg = _aggregate_group(sub, wide, agg, ratings, priors, as_of_ts, key, group_cols)
        if avg is not None:
            out[key] = avg
    return out


def generic_ballot_average(
    polls: pd.DataFrame,
    results: pd.DataFrame,
    config: PollingConfig | AggregationConfig | None,
    as_of: date | datetime | str,
    *,
    pollsters: Sequence[PollsterLike] | None = None,
    poll_type: str = "generic_house",
) -> PollAverage | None:
    """National generic-ballot (House) average by party, or ``None`` without polls."""
    if polls is None or len(polls) == 0:
        return None
    sub = polls[polls["poll_type"] == poll_type]
    res = aggregate_polls(sub, results, config, as_of, group_by=("poll_type",), pollsters=pollsters)
    return res.get((poll_type,))


def averages_frame(averages: Mapping[tuple[Any, ...], PollAverage]) -> pd.DataFrame:
    """Tidy table (one row per group × key) of a set of averages (API / export)."""
    rows: list[dict[str, Any]] = []
    for avg in averages.values():
        for k in avg.keys:
            rows.append(
                {
                    "poll_type": avg.poll_type,
                    "geo_code": avg.geo_code,
                    "as_of": avg.as_of,
                    "key": k,
                    "mean_pct": avg.mean[k],
                    "se_pct": avg.se[k],
                    "lower_pct": avg.lower[k],
                    "upper_pct": avg.upper[k],
                    "total_se_pct": avg.total_se[k],
                    "trend_pct": avg.trend_latest[k],
                    "n_polls": avg.n_polls,
                    "effective_n": avg.effective_n,
                    "data_category": avg.data_category,
                }
            )
    cols = [
        "poll_type",
        "geo_code",
        "as_of",
        "key",
        "mean_pct",
        "se_pct",
        "lower_pct",
        "upper_pct",
        "total_se_pct",
        "trend_pct",
        "n_polls",
        "effective_n",
        "data_category",
    ]
    return pd.DataFrame(rows, columns=cols)


# --------------------------------------------------------------------------- preparation
def _resolve_config(
    config: PollingConfig | AggregationConfig | None, pollsters: Sequence[PollsterLike] | None
) -> tuple[AggregationConfig, dict[str, float], dict[str, dict[str, float]]]:
    if config is None:
        config = load_polling_config()
    if isinstance(config, PollingConfig):
        agg = config.aggregation
        specs: Sequence[PollsterLike] = pollsters if pollsters is not None else config.pollsters
    elif isinstance(config, AggregationConfig):
        agg = config
        specs = pollsters or []
    else:
        raise TypeError(
            f"config must be PollingConfig, AggregationConfig or None, not {type(config).__name__}"
        )
    ratings = {p.name: float(p.rating) for p in specs}
    priors = {p.name: {str(k): float(v) for k, v in dict(p.house_effects).items()} for p in specs}
    return agg, ratings, priors


def _prepare_polls(polls: pd.DataFrame, as_of: pd.Timestamp, agg: AggregationConfig) -> pd.DataFrame:
    missing = [c for c in _REQUIRED_POLL_COLUMNS if c not in polls.columns]
    if missing:
        raise ValueError(f"polls frame is missing columns {missing}")
    df = polls.copy()
    df["end_date"] = pd.to_datetime(df["end_date"]).dt.normalize()
    if "start_date" in df.columns:
        df["start_date"] = pd.to_datetime(df["start_date"]).dt.normalize().fillna(df["end_date"])
    else:
        df["start_date"] = df["end_date"]
    defaults: dict[str, Any] = {
        "geo_code": NATIONAL_GEO,
        "population": "LV",
        "method": "unknown",
        "undecided_pct": np.nan,
        "quality_rating": np.nan,
        "pollster_rating": np.nan,
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default
        elif isinstance(default, str):
            df[col] = df[col].where(df[col].notna(), default).astype(str)
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    dup = df["poll_id"].duplicated(keep=False)
    if dup.any():
        ids = sorted({str(v) for v in df.loc[dup, "poll_id"]})[:5]
        raise ValueError(f"polls frame has duplicate poll_id values (e.g. {ids}); poll_id must be unique")
    df["pollster"] = df["pollster"].astype(str)
    df["population"] = df["population"].str.strip().str.upper()
    df["sample_size"] = pd.to_numeric(df["sample_size"], errors="coerce")
    bad = df["sample_size"].isna() | (df["sample_size"] <= 0) | df["end_date"].isna()
    if bad.any():
        log.warning("ignoring %d polls without a valid sample size / end date", int(bad.sum()))
        df = df[~bad]
    df = df[df["end_date"] <= as_of]
    if agg.max_age_days is not None:
        age = (as_of - df["end_date"]).dt.days
        df = df[age <= agg.max_age_days]
    return df


def _results_matrix(results: pd.DataFrame, poll_ids: pd.Series) -> pd.DataFrame:
    missing = [c for c in _REQUIRED_RESULT_COLUMNS if c not in results.columns]
    if missing:
        raise ValueError(f"results frame is missing columns {missing}")
    res = results.loc[:, list(_REQUIRED_RESULT_COLUMNS)].copy()
    res["value_pct"] = pd.to_numeric(res["value_pct"], errors="coerce")
    res = res.dropna(subset=["value_pct", "key"])
    n_candidates = len(res)
    res = res[res["poll_id"].isin(poll_ids)]
    if n_candidates and len(poll_ids) and res.empty:
        log.warning(
            "no poll result matches a poll_id of the polls frame (poll_id dtypes %s vs %s?)",
            results["poll_id"].dtype,
            poll_ids.dtype,
        )
    res["key"] = res["key"].astype(str)
    if res.empty:
        return pd.DataFrame()
    return res.groupby(["poll_id", "key"], sort=True)["value_pct"].mean().unstack("key")


# --------------------------------------------------------------------------- smoother
def local_linear_weights(
    t_eval: np.ndarray, t_obs: np.ndarray, w_obs: np.ndarray, bandwidth: float, ridge: float
) -> np.ndarray:
    """Equivalent-kernel matrix ``L`` (E × N) of a Gaussian-kernel local-linear smoother.

    ``fit(t_eval) = L @ y``.  Observation weights ``w_obs`` multiply the kernel; a relative ridge
    penalty on the local slope keeps edge extrapolation stable.  Rows sum to 1 (constants are
    reproduced); rows without any positive-weight observation are all zero.
    """
    t_eval = np.asarray(t_eval, dtype=float)
    t_obs = np.asarray(t_obs, dtype=float)
    w_obs = np.asarray(w_obs, dtype=float)
    E, N = len(t_eval), len(t_obs)
    L = np.zeros((E, N))
    valid = w_obs > 0
    if E == 0 or not valid.any():
        return L
    x = t_obs[None, :] - t_eval[:, None]
    logk = -0.5 * (x / bandwidth) ** 2
    row_max = np.max(np.where(valid[None, :], logk, -np.inf), axis=1, keepdims=True)
    k = np.exp(logk - row_max) * np.where(valid, w_obs, 0.0)[None, :]
    s0 = k.sum(axis=1)
    s1 = (k * x).sum(axis=1)
    s2 = (k * x * x).sum(axis=1)
    s2r = s2 + ridge * s0 * bandwidth**2
    den = s0 * s2r - s1**2
    ok = den > 1e-12 * np.maximum(s0, 1e-300) ** 2 * bandwidth**2
    safe_den = np.where(ok, den, 1.0)
    linear = k * (s2r[:, None] - s1[:, None] * x) / safe_den[:, None]
    constant = k / np.maximum(s0, 1e-300)[:, None]
    L = np.where(ok[:, None], linear, constant)
    return L


# --------------------------------------------------------------------------- group engine
def _aggregate_group(
    sub: pd.DataFrame,
    wide: pd.DataFrame,
    agg: AggregationConfig,
    ratings: Mapping[str, float],
    priors: Mapping[str, Mapping[str, float]],
    as_of: pd.Timestamp,
    group_key: tuple[Any, ...],
    group_cols: list[str],
) -> PollAverage | None:
    sub = sub.sort_values(["end_date", "poll_id"], kind="mergesort").reset_index(drop=True)
    Yd = wide.reindex(sub["poll_id"].to_numpy())

    # ---- static weight factors; polls weighted 0 by configuration are excluded -------
    n_all = sub["sample_size"].to_numpy(dtype=float)
    w_samp = (np.minimum(n_all, agg.sample_size_cap) / agg.sample_size_cap) ** agg.sample_size_exponent
    rating = _poll_ratings(sub, ratings, agg.default_rating)
    w_rat = rating**agg.rating_exponent if agg.rating_weighting else np.ones(len(sub))
    w_pop = (
        sub["population"].map(agg.population_weights).fillna(agg.default_population_weight).to_numpy(float)
    )
    method_weight = {
        m: lookup_method(m, agg.method_weights, agg.default_method_weight) for m in sub["method"].unique()
    }
    w_meth = sub["method"].map(method_weight).to_numpy(dtype=float)
    base = w_samp * w_rat * w_pop * w_meth
    # A poll must report at least one positive value (an all-zero / all-undecided row carries no
    # information) and have a positive configured weight (rating / population / method weight 0 =
    # excluded on purpose).  Dropping them avoids 0/0 averages.
    informative = (np.nan_to_num(Yd.to_numpy(dtype=float), nan=0.0) > 0).any(axis=1)
    keep = informative & np.isfinite(base) & (base > 0)
    if not keep.all():
        log.debug(
            "poll group %s: %d uninformative or zero-weight polls excluded", group_key, int((~keep).sum())
        )
        sub = sub[keep].reset_index(drop=True)
        Yd = Yd[keep]
        w_samp, rating, w_rat, w_pop, w_meth, base = (
            a[keep] for a in (w_samp, rating, w_rat, w_pop, w_meth, base)
        )
    if len(sub) < agg.min_polls or len(sub) == 0:
        return None
    Yd = Yd.loc[:, Yd.notna().any(axis=0)]
    if Yd.shape[1] == 0:
        return None
    keys = [str(c) for c in Yd.columns]
    Y = Yd.to_numpy(dtype=float)
    mask = np.isfinite(Y)
    N, K = Y.shape

    # ---- undecided ---------------------------------------------------------------
    und = sub["undecided_pct"].to_numpy(dtype=float)
    und0 = np.where(np.isfinite(und), np.clip(und, 0.0, 95.0), 0.0)
    applies = ~sub["poll_type"].isin(agg.undecided_exempt_poll_types).to_numpy()
    mode = agg.undecided
    if mode == "proportional":
        scale = 100.0 / (100.0 - und0)
    elif mode == "normalize":
        rowsum = np.nansum(Y, axis=1)
        scale = np.where(rowsum > 0, 100.0 / np.where(rowsum > 0, rowsum, 1.0), 1.0)
    else:
        scale = np.ones(N)
    scale = np.where(applies, scale, 1.0)
    Y = Y * scale[:, None]
    n = sub["sample_size"].to_numpy(dtype=float)
    decided_frac = np.where(applies & (mode != "keep"), 1.0 - und0 / 100.0, 1.0)
    n_dec = np.maximum(n * decided_frac, 1.0)

    # ---- dynamic weights ---------------------------------------------------------
    age = (as_of - sub["end_date"]).dt.days.to_numpy(dtype=float)
    age = np.maximum(age, 0.0)
    w_rec = 0.5 ** (age / agg.recency_half_life_days)
    pidx, pollster_names = pd.factorize(sub["pollster"], sort=True)
    P = len(pollster_names)
    rec_count = np.bincount(pidx, weights=w_rec, minlength=P)
    w_vol = np.maximum(rec_count[pidx], 1.0) ** (-agg.pollster_volume_exponent)
    w = base * w_rec * w_vol
    if not np.isfinite(w).all() or w.sum() <= 0:
        w = base.copy()
    # Keys asked only in polls whose recency weight underflowed to 0 have no weighted information.
    has_weight = (w[:, None] * mask).sum(axis=0) > 0
    if not has_weight.all():
        keys = [k for k, ok in zip(keys, has_weight, strict=True) if ok]
        Y, mask = Y[:, has_weight], mask[:, has_weight]
        N, K = Y.shape
        if K == 0:
            return None

    t = (sub["end_date"] - as_of).dt.days.to_numpy(dtype=float)
    bw, ridge = agg.trend_bandwidth_days, agg.trend_slope_ridge

    # ---- house effects -----------------------------------------------------------
    pollster_weight = np.bincount(pidx, weights=w_rat, minlength=P) / np.maximum(
        np.bincount(pidx, minlength=P), 1
    )
    H, prior, counts = _house_effects(
        Y,
        mask,
        pidx,
        P,
        base,
        t,
        keys,
        list(pollster_names),
        priors,
        agg.house_effects,
        bw,
        ridge,
        pollster_weight,
    )
    Yadj = Y - H[pidx]  # NaN stays NaN where the poll did not ask about the key
    Yadj0 = np.where(mask, Yadj, 0.0)

    # ---- weighted mean and uncertainty -------------------------------------------
    Wk = w[:, None] * mask
    V1 = Wk.sum(axis=0)
    mean = (Wk * Yadj0).sum(axis=0) / V1
    p = np.clip(mean / 100.0, *_P_CLIP)
    sig2 = p[None, :] * (1.0 - p[None, :]) / n_dec[:, None] * 1e4 * agg.design_effect
    V2 = (Wk**2).sum(axis=0)
    dev2 = np.where(mask, (Yadj0 - mean[None, :]) ** 2, 0.0)
    denom = V1 - V2 / V1
    s2 = np.where(denom > 1e-9 * V1, (Wk * dev2).sum(axis=0) / np.where(denom > 0, denom, 1.0), 0.0)
    sbar = (Wk * sig2).sum(axis=0) / V1
    tau2 = np.maximum(s2 - sbar, agg.nonsampling_sd_pp**2)
    var = (Wk**2 * (sig2 + tau2[None, :])).sum(axis=0) / V1**2
    se = np.sqrt(var)
    z = float(norm.ppf(0.5 + agg.interval_level / 2.0))
    lower = np.clip(mean - z * se, 0.0, 100.0)
    upper = np.clip(mean + z * se, 0.0, 100.0)
    systematic = agg.systematic_error_sd_pp * 2.0 * np.sqrt(p * (1.0 - p))
    total_se = np.sqrt(se**2 + systematic**2)
    effective_n = float(w.sum() ** 2 / np.sum(w**2 / n_dec))
    effective_polls = float(w.sum() ** 2 / np.sum(w**2))

    # ---- trend -------------------------------------------------------------------
    start = max(sub["end_date"].min(), as_of - pd.Timedelta(days=agg.trend_max_days))
    dates = pd.date_range(start, as_of, freq="D")
    t_eval = (dates - as_of).days.to_numpy(dtype=float)
    trend = np.zeros((len(dates), K))
    trend_se = np.zeros((len(dates), K))
    for j in range(K):
        L = local_linear_weights(t_eval, t, base * mask[:, j], bw, ridge)
        trend[:, j] = L @ Yadj0[:, j]
        trend_se[:, j] = np.sqrt((L**2) @ (sig2[:, j] + tau2[j]))

    # ---- assemble ------------------------------------------------------------------
    order = np.argsort(-mean, kind="mergesort")
    keys_sorted = [keys[i] for i in order]
    trend_df = pd.DataFrame(trend[:, order], index=dates, columns=keys_sorted)
    trend_df.index.name = "date"
    trend_se_df = pd.DataFrame(trend_se[:, order], index=dates, columns=keys_sorted)
    trend_se_df.index.name = "date"
    w_norm = w / w.sum()
    weights_df = pd.DataFrame(
        {
            "poll_id": sub["poll_id"].to_numpy(),
            "pollster": sub["pollster"].to_numpy(),
            "start_date": sub["start_date"].to_numpy(),
            "end_date": sub["end_date"].to_numpy(),
            "sample_size": n.astype(int),
            "population": sub["population"].to_numpy(),
            "method": sub["method"].to_numpy(),
            "undecided_pct": np.where(np.isfinite(und), und, np.nan),
            "age_days": age.astype(int),
            "rating": rating,
            "w_recency": w_rec,
            "w_sample": w_samp,
            "w_rating": w_rat,
            "w_population": w_pop,
            "w_method": w_meth,
            "w_volume": w_vol,
            "weight_raw": w,
            "weight": w_norm,
        }
    )
    poll_values = pd.DataFrame(
        Yadj[:, order], index=pd.Index(sub["poll_id"].to_numpy(), name="poll_id"), columns=keys_sorted
    )
    he_rows = [
        {
            "pollster": pollster_names[pi],
            "key": keys[kj],
            "prior_pp": float(prior[pi, kj]),
            "estimated_pp": float(H[pi, kj]),
            "n_polls": int(counts[pi, kj]),
        }
        for pi in range(P)
        for kj in order
        if counts[pi, kj] > 0
    ]
    he_df = pd.DataFrame(he_rows, columns=["pollster", "key", "prior_pp", "estimated_pp", "n_polls"])
    und_known = np.isfinite(und)
    undecided_mean = (
        float(np.sum(w[und_known] * und[und_known]) / np.sum(w[und_known])) if und_known.any() else None
    )

    poll_type = _group_label(sub, group_key, group_cols, "poll_type", "mixed")
    geo_code = _group_label(sub, group_key, group_cols, "geo_code", NATIONAL_GEO)
    return PollAverage(
        group=group_key,
        poll_type=poll_type,
        geo_code=geo_code,
        as_of=as_of.date(),
        keys=keys_sorted,
        mean={keys[i]: float(mean[i]) for i in order},
        se={keys[i]: float(se[i]) for i in order},
        lower={keys[i]: float(lower[i]) for i in order},
        upper={keys[i]: float(upper[i]) for i in order},
        total_se={keys[i]: float(total_se[i]) for i in order},
        interval_level=agg.interval_level,
        n_polls=N,
        effective_n=effective_n,
        effective_polls=effective_polls,
        latest_poll_date=sub["end_date"].max().date(),
        undecided_mean=undecided_mean,
        undecided_mode=mode,
        weights=weights_df,
        poll_values=poll_values,
        house_effects=he_df,
        trend=trend_df,
        trend_se=trend_se_df,
        trend_latest={keys[i]: float(trend[-1, i]) for i in order},
    )


def _poll_ratings(sub: pd.DataFrame, ratings: Mapping[str, float], default: float) -> np.ndarray:
    """Per-poll rating: manual ``quality_rating`` > ``pollster_rating`` column > configured > default.

    A rating of 0 is a deliberate exclusion (the poll gets no weight); negative or missing values
    fall through to the next source.
    """
    configured = sub["pollster"].map(ratings).astype(float).fillna(default).to_numpy(float)
    from_db = sub["pollster_rating"].to_numpy(dtype=float)
    manual = sub["quality_rating"].to_numpy(dtype=float)
    rating = np.where(np.isfinite(from_db) & (from_db >= 0), from_db, configured)
    rating = np.where(np.isfinite(manual) & (manual >= 0), manual, rating)
    return rating


def _group_label(sub: pd.DataFrame, key: tuple[Any, ...], cols: list[str], col: str, fallback: str) -> str:
    if col in cols:
        value = key[cols.index(col)]
        return fallback if value is None or (isinstance(value, float) and np.isnan(value)) else str(value)
    values = sorted({str(v) for v in sub[col].dropna().unique()}) if col in sub.columns else []
    if len(values) == 1:
        return values[0]
    return fallback if not values else ",".join(values)


def _house_effects(
    Y: np.ndarray,
    mask: np.ndarray,
    pidx: np.ndarray,
    P: int,
    base: np.ndarray,
    t: np.ndarray,
    keys: list[str],
    pollster_names: list[str],
    priors: Mapping[str, Mapping[str, float]],
    settings: HouseEffectSettings,
    bandwidth: float,
    ridge: float,
    pollster_weight: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Estimate house effects (P × K, percentage points).

    Iterates: trend of house-adjusted values evaluated at each poll's end date → residual of the
    raw value from that trend → pollster's weighted mean residual → shrink toward the prior with
    weight ``n / (n + shrinkage)``.  Polls only identify effects *relative* to each other, so per
    key the ``pollster_weight``-weighted mean effect over pollsters (the "average pollster", not the
    average poll — a flooding pollster cannot define the level) is pinned to the same mean of the
    priors.  Returns ``(estimated, prior, counts)``.
    """
    N, K = Y.shape
    prior = np.array([[float(priors.get(name, {}).get(k, 0.0)) for k in keys] for name in pollster_names])
    prior = prior.reshape(P, K)
    counts = np.zeros((P, K))
    np.add.at(counts, pidx, mask.astype(float))
    if not settings.enabled:
        return np.zeros((P, K)), prior, counts
    prior_c = np.clip(prior, -settings.max_abs_pp, settings.max_abs_pp)
    if not settings.estimate:
        return prior_c.copy(), prior, counts

    # Smoother evaluated at the unique poll dates (keeps memory O(dates × polls)).
    t_unique, inv = np.unique(t, return_inverse=True)
    L_u = np.stack([local_linear_weights(t_unique, t, base * mask[:, j], bandwidth, ridge) for j in range(K)])
    G = np.zeros((N, P))
    G[np.arange(N), pidx] = 1.0
    v = base[:, None] * mask
    denom = G.T @ v
    if settings.shrinkage > 0:
        lam = counts / (counts + settings.shrinkage)
    else:
        lam = np.ones_like(counts)
    lam = np.where(counts >= settings.min_pollster_polls, lam, 0.0)
    has_data = denom > 0
    # Identifiability anchor; only pollsters that are actually estimated (lam > 0) absorb the
    # re-centring, pollsters kept at their prior stay there.
    estimated = has_data & (lam > 0)
    omega = np.where(has_data, pollster_weight[:, None], 0.0)
    tot = omega.sum(axis=0)
    est_tot = np.where(estimated, omega, 0.0).sum(axis=0)
    safe_tot = np.where(tot > 0, tot, 1.0)
    anchor = (omega * prior_c).sum(axis=0) / safe_tot
    Y0 = np.where(mask, Y, 0.0)
    H = prior_c.copy()
    delta = np.inf
    for _ in range(settings.max_iterations):
        adj = Y0 - H[pidx] * mask
        fit = np.einsum("kuj,jk->uk", L_u, adj)[inv]
        resid = np.where(mask, Y0 - fit, 0.0)
        raw = (G.T @ (v * resid)) / np.where(has_data, denom, 1.0)
        H_new = np.where(has_data, lam * raw + (1.0 - lam) * prior_c, prior_c)
        level = (omega * H_new).sum(axis=0) / safe_tot
        correction = np.where(est_tot > 0, (level - anchor) * tot / np.where(est_tot > 0, est_tot, 1.0), 0.0)
        H_new = np.where(estimated, H_new - correction[None, :], H_new)
        H_new = np.clip(H_new, -settings.max_abs_pp, settings.max_abs_pp)
        delta = float(np.max(np.abs(H_new - H))) if H.size else 0.0
        H = H_new
        if delta < settings.tolerance:
            break
    if delta >= settings.tolerance:
        log.debug(
            "house-effect iteration stopped after %d iterations (delta=%.2e)", settings.max_iterations, delta
        )
    return H, prior, counts
