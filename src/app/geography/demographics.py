"""Deterministic DERIVED quantities and gap imputation for the geography store.

All functions are pure and vectorised (NumPy in, NumPy out):

* :func:`distribute_missing_population` — spread each municipality's residual population
  (official total minus the known neighbourhood figures) over neighbourhoods whose figure is missing;
* :func:`fill_from_parents` — take a value from the containing wijk / gemeente when a neighbourhood's
  own value is suppressed;
* :func:`hierarchical_weighted_fill` — population-weighted means per municipality → province →
  nation for anything still missing;
* :func:`fill_composition` — the same fallback chain for compositional groups (age bands,
  migration background) that keeps the published parts and splits only the remainder, so a
  neighbourhood's bands still sum to ≈ 100 %;
* :func:`estimate_eligible_voters` — DERIVED eligible-voter estimate from the CBS age bands.

Every imputed cell is reported through a boolean mask so it can be flagged in ``imputed_fields``.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

from app.geography.cbs import urbanity_class_from_address_density


def group_weighted_mean(
    values: np.ndarray, weights: np.ndarray, groups: np.ndarray, n_groups: int
) -> np.ndarray:
    """Per-group weighted mean of the non-NaN ``values``; groups whose observed weights sum to zero
    fall back to the unweighted mean; groups without observations are NaN."""
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    groups = np.asarray(groups, dtype=np.int64)
    obs = ~np.isnan(values)
    g = groups[obs]
    x = values[obs]
    w = weights[obs]
    sw = np.bincount(g, weights=w, minlength=n_groups)
    swx = np.bincount(g, weights=w * x, minlength=n_groups)
    cnt = np.bincount(g, minlength=n_groups).astype(float)
    sx = np.bincount(g, weights=x, minlength=n_groups)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(
            sw > 0,
            swx / np.where(sw > 0, sw, 1.0),
            np.where(cnt > 0, sx / np.where(cnt > 0, cnt, 1.0), np.nan),
        )
    return mean


def fill_from_parents(values: np.ndarray, *parents: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Fill NaN entries of ``values`` from the first non-NaN parent array (same length).

    Returns ``(filled, imputed_mask)``.
    """
    out = np.asarray(values, dtype=float).copy()
    imputed = np.zeros(len(out), dtype=bool)
    for parent in parents:
        parent = np.asarray(parent, dtype=float)
        take = np.isnan(out) & ~np.isnan(parent)
        out[take] = parent[take]
        imputed |= take
    return out, imputed


def hierarchical_weighted_fill(
    values: np.ndarray,
    weights: np.ndarray,
    levels: Sequence[np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    """Fill NaNs with population-weighted means of the observed values in successively coarser
    groups (``levels``: integer group index arrays, e.g. municipality then province), finally the
    national weighted mean.  Returns ``(filled, imputed_mask)``.  Means are computed from the
    *originally observed* values only (imputed values never feed later levels).
    """
    original = np.asarray(values, dtype=float)
    out = original.copy()
    imputed = np.zeros(len(out), dtype=bool)
    w = np.asarray(weights, dtype=float)
    for groups in levels:
        groups = np.asarray(groups, dtype=np.int64)
        if not np.isnan(out).any():
            break
        n = int(groups.max()) + 1 if len(groups) else 0
        means = group_weighted_mean(original, w, groups, n)
        take = np.isnan(out) & ~np.isnan(means[groups])
        out[take] = means[groups][take]
        imputed |= take
    if np.isnan(out).any():
        national = group_weighted_mean(original, w, np.zeros(len(out), dtype=np.int64), 1)[0]
        take = np.isnan(out)
        if not np.isnan(national):
            out[take] = national
            imputed |= take
    return out, imputed


def fill_composition(
    values: np.ndarray,
    parents: Sequence[np.ndarray],
    weights: np.ndarray,
    levels: Sequence[np.ndarray],
    total: float = 100.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Fill the missing parts of *compositional* rows (shares that sum to ``total``, e.g. the five
    CBS age bands) so that every row stays a consistent composition.

    CBS suppresses individual cells, so a neighbourhood often has some bands published and others
    missing.  Filling each band independently from a parent area mixes two compositions (sums of
    50–180 % occur).  Instead, the known own parts are kept and the remainder
    ``max(0, total − Σ known)`` is split over the missing parts in proportion to a *reference
    composition*: the first parent row (e.g. wijk, then gemeente) that is complete, else the
    population-weighted mean composition of the complete own rows in successively coarser groups
    (``levels``), else the national mean, else equal shares.  A row with every part missing takes
    the reference composition rescaled to ``total``.

    ``values`` and each parent are ``(n, k)`` arrays (NaN = missing).  Returns ``(filled,
    imputed_mask)`` with the mask marking exactly the cells that were filled.
    """
    own = np.asarray(values, dtype=float)
    if own.ndim != 2:
        raise ValueError("values must be a 2-D (rows × parts) array")
    n, k = own.shape
    missing = np.isnan(own)
    todo = missing.any(axis=1)
    out = own.copy()
    if n == 0 or k == 0 or not todo.any():
        return out, missing
    w = np.asarray(weights, dtype=float)
    ref = np.full((n, k), np.nan)
    have = np.zeros(n, dtype=bool)
    for parent in parents:
        par = np.asarray(parent, dtype=float)
        if par.shape != own.shape:
            raise ValueError(f"parent shape {par.shape} differs from values shape {own.shape}")
        take = todo & ~have & ~np.isnan(par).any(axis=1)
        ref[take] = par[take]
        have |= take
    complete = ~todo
    national = np.zeros(n, dtype=np.int64)
    for groups in [*levels, national]:
        need = todo & ~have
        if not need.any():
            break
        groups = np.asarray(groups, dtype=np.int64)
        n_groups = int(groups.max()) + 1 if len(groups) else 0
        cols = []
        for j in range(k):
            column = np.where(complete, own[:, j], np.nan)
            cols.append(group_weighted_mean(column, w, groups, n_groups))
        means = np.column_stack(cols)[groups]
        take = need & ~np.isnan(means).any(axis=1)
        ref[take] = means[take]
        have |= take
    ref[todo & ~have] = 1.0  # no reference anywhere: equal shares
    ref = np.clip(ref, 0.0, None)

    known_sum = np.where(missing, 0.0, own).sum(axis=1)
    remainder = np.clip(total - known_sum, 0.0, total)
    ref_missing = np.where(missing, ref, 0.0)
    ref_sum = ref_missing.sum(axis=1)
    n_missing = missing.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where(
            ref_sum[:, None] > 0,
            ref_missing / np.where(ref_sum > 0, ref_sum, 1.0)[:, None],
            missing / np.maximum(n_missing, 1)[:, None],
        )
    fill = remainder[:, None] * share
    out[missing] = fill[missing]
    return out, missing


def largest_remainder_by_group(amounts: np.ndarray, shares: np.ndarray, groups: np.ndarray) -> np.ndarray:
    """Integer allocation of ``amounts[g]`` over members of each group proportional to ``shares``
    (shares sum to 1 within each group).  Ties broken by position (deterministic)."""
    groups = np.asarray(groups, dtype=np.int64)
    exact = np.asarray(amounts, dtype=float)[groups] * np.asarray(shares, dtype=float)
    base = np.floor(exact + 1e-9)
    remainder = exact - base
    n_groups = len(amounts)
    deficit = np.rint(
        np.asarray(amounts, dtype=float) - np.bincount(groups, weights=base, minlength=n_groups)
    ).astype(np.int64)
    order = np.lexsort((np.arange(len(groups)), -remainder, groups))
    sorted_groups = groups[order]
    first = np.searchsorted(sorted_groups, sorted_groups, side="left")
    rank = np.arange(len(order)) - first
    bonus = np.zeros(len(groups), dtype=np.int64)
    bonus[order] = (rank < deficit[sorted_groups]).astype(np.int64)
    return (base.astype(np.int64) + bonus).astype(np.int64)


def distribute_missing_population(
    population: np.ndarray,
    muni_index: np.ndarray,
    official_by_muni: np.ndarray,
    land_area_km2: np.ndarray,
    address_density: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Fill missing (NaN) neighbourhood populations.

    For every municipality the residual ``official − Σ known`` (if positive) is distributed over its
    neighbourhoods with a missing figure, proportionally to *address density × land area* (≈ number
    of addresses) when every missing neighbourhood has an address density, else to land area (equal
    split when all areas are zero).  Missing neighbourhoods get 0 when there is no positive residual.
    Returns ``(population int64, imputed_mask)``; the allocation is integral and exact per municipality.
    """
    pop = np.asarray(population, dtype=float)
    muni = np.asarray(muni_index, dtype=np.int64)
    official = np.asarray(official_by_muni, dtype=float)
    n_munis = len(official)
    missing = np.isnan(pop)
    out = np.where(missing, 0.0, pop)
    if not missing.any():
        return np.rint(out).astype(np.int64), missing
    known_sum = np.bincount(muni[~missing], weights=pop[~missing], minlength=n_munis)
    residual = np.where(np.isnan(official), 0.0, np.maximum(official - known_sum, 0.0))
    residual = np.floor(residual + 1e-9)
    area = np.nan_to_num(np.asarray(land_area_km2, dtype=float), nan=0.0).clip(min=0.0)
    if address_density is not None:
        ad = np.asarray(address_density, dtype=float)
        ad_known = ~np.isnan(ad)
        n_missing = np.bincount(muni[missing], minlength=n_munis)
        n_ad = np.bincount(muni[missing & ad_known], minlength=n_munis)
        use_ad = (n_missing > 0) & (n_ad == n_missing)
        weight = np.where(use_ad[muni] & ad_known, np.nan_to_num(ad, nan=0.0) * area, area)
    else:
        weight = area
    mi = muni[missing]
    w = weight[missing]
    wsum = np.bincount(mi, weights=w, minlength=n_munis)
    cnt = np.bincount(mi, minlength=n_munis).astype(float)
    shares = np.where(wsum[mi] > 0, w / np.where(wsum[mi] > 0, wsum[mi], 1.0), 1.0 / np.maximum(cnt[mi], 1.0))
    alloc = largest_remainder_by_group(residual, shares, mi) if len(mi) else np.zeros(0, dtype=np.int64)
    out[missing] = alloc
    return np.rint(out).astype(np.int64), missing


def estimate_eligible_voters(
    population: np.ndarray,
    pct_age_0_15: np.ndarray,
    pct_age_15_25: np.ndarray,
    citizenship_factor: float = 0.93,
    adult_share_15_25: float = 0.7,
) -> np.ndarray:
    """DERIVED eligible voters:
    ``round(pop × ((100 − p0_15 − p15_25)/100 + adult_share × p15_25/100) × citizenship_factor)``,
    clipped to ``[0, pop]``.  Percentages are 0–100."""
    pop = np.asarray(population, dtype=float)
    p0 = np.asarray(pct_age_0_15, dtype=float)
    p15 = np.asarray(pct_age_15_25, dtype=float)
    if np.isnan(p0).any() or np.isnan(p15).any():
        raise ValueError("age shares must be imputed before estimating eligible voters")
    frac = (100.0 - p0 - p15) / 100.0 + adult_share_15_25 * p15 / 100.0
    est = np.rint(pop * frac * citizenship_factor)
    return np.clip(est, 0, pop).astype(np.int64)


def resolve_urbanity_class(
    cbs_class: np.ndarray,
    address_density: np.ndarray,
    *parent_classes: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """CBS urbanity class (1–5): own class, else derived from own address density (CBS thresholds —
    not an imputation), else the first available parent class (flagged).  Returns
    ``(class float array, imputed_mask)``; remaining gaps stay NaN."""
    cls = np.asarray(cbs_class, dtype=float).copy()
    cls[(cls < 1) | (cls > 5)] = np.nan
    derived = urbanity_class_from_address_density(address_density)
    take = np.isnan(cls) & ~np.isnan(derived)
    cls[take] = derived[take]
    parents = []
    for p in parent_classes:
        p = np.asarray(p, dtype=float).copy()
        p[(p < 1) | (p > 5)] = np.nan
        parents.append(p)
    return fill_from_parents(cls, *parents)


def join_imputed_flags(
    flags: dict[str, np.ndarray], n: int, order: Sequence[str] | None = None
) -> np.ndarray:
    """Comma-separated variable names per row for the boolean masks in ``flags``."""
    names = list(order) if order is not None else list(flags)
    out = np.full(n, "", dtype=object)
    for name in names:
        mask = flags.get(name)
        if mask is None or not np.any(mask):
            continue
        sel = np.asarray(mask, dtype=bool)
        cur = out[sel]
        out[sel] = np.where(cur == "", name, cur + "," + name)
    return out


def codes_to_index(codes: pd.Series | np.ndarray, universe: Sequence[str]) -> np.ndarray:
    """Positions of ``codes`` in ``universe`` (−1 where absent)."""
    return pd.Index(list(universe)).get_indexer(pd.Index(np.asarray(codes, dtype=object)))
