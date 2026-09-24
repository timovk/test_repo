"""Municipality lineage between two geography vintages (mergers, splits, renames).

Dutch municipalities merge regularly (e.g. Voorne aan Zee in 2023).  To compare results across
election cycles held on different vintages, :func:`compute_lineage` maps every *old* municipality
onto the *new* municipalities that received its population, using the neighbourhoods (units):

1. an old neighbourhood whose code still exists goes to that code's new municipality;
2. otherwise its centroid is located in the new vintage — inside a new unit polygon when
   ``new_units`` has geometry, else at the nearest new unit centroid (``centroid_x/centroid_y``);
3. otherwise, if its municipality code still exists, it stays in that municipality.

``population_weight`` is the share of the old municipality's population that moved to the new one
(unit-count shares for unpopulated municipalities).  :func:`remap_values` then re-expresses values
keyed by old municipality codes on the new codes.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd
import shapely

from app.core.logging import get_logger

log = get_logger(__name__)

LINEAGE_COLUMNS: tuple[str, ...] = (
    "from_code",
    "to_code",
    "population_weight",
    "event",
    "population",
    "units",
)
EVENTS: tuple[str, ...] = ("unchanged", "rename", "merger", "split", "boundary_change")


def _match_units(
    old_units: pd.DataFrame,
    new_units: pd.DataFrame,
    code_col: str,
    muni_col: str,
) -> tuple[np.ndarray, np.ndarray]:
    """New municipality code per old unit (None when unmatched) and the matching method."""
    new_by_code = new_units.set_index(code_col)[muni_col].astype(str)
    old_codes = old_units[code_col].astype(str)
    target = old_codes.map(new_by_code).to_numpy(dtype=object)
    method = np.where(pd.isna(target), "", "code").astype(object)
    todo = np.flatnonzero(pd.isna(target))
    has_xy = {"centroid_x", "centroid_y"} <= set(old_units.columns)
    if len(todo) and has_xy:
        pts_xy = old_units[["centroid_x", "centroid_y"]].to_numpy(dtype=float)[todo]
        new_geom = getattr(new_units, "geometry", None) if "geometry" in new_units.columns else None
        new_munis = new_units[muni_col].astype(str).to_numpy()
        if new_geom is not None:
            points = shapely.points(pts_xy)
            tree = shapely.STRtree(np.asarray(new_geom.values, dtype=object))
            src, dst = tree.query(points, predicate="intersects")
            first = pd.Series(dst).groupby(src).min()
            hit = todo[first.index.to_numpy()]
            target[hit] = new_munis[first.to_numpy()]
            method[hit] = "spatial"
            todo = np.flatnonzero(pd.isna(target))
            pts_xy = old_units[["centroid_x", "centroid_y"]].to_numpy(dtype=float)[todo]
        if len(todo) and {"centroid_x", "centroid_y"} <= set(new_units.columns) and len(new_units):
            from scipy.spatial import cKDTree

            tree_xy = cKDTree(new_units[["centroid_x", "centroid_y"]].to_numpy(dtype=float))
            _, nearest = tree_xy.query(pts_xy, k=1)
            target[todo] = new_munis[np.asarray(nearest, dtype=np.int64)]
            method[todo] = "nearest"
            todo = np.flatnonzero(pd.isna(target))
    if len(todo):
        new_munis_set = set(new_units[muni_col].astype(str))
        old_m = old_units[muni_col].astype(str).to_numpy()[todo]
        keep = np.array([m in new_munis_set for m in old_m], dtype=bool)
        target[todo[keep]] = old_m[keep]
        method[todo[keep]] = "municipality_code"
    return target, method


def compute_lineage(
    old_units: pd.DataFrame,
    new_units: pd.DataFrame,
    *,
    code_col: str = "code",
    muni_col: str = "municipality_code",
    pop_col: str = "population",
    boundary_share: float = 0.05,
) -> pd.DataFrame:
    """Map old municipalities to new ones via shared neighbourhoods / population overlap.

    Returns a DataFrame with columns ``from_code, to_code, population_weight, event, population,
    units`` sorted by ``(from_code, to_code)``.  ``event`` is one of :data:`EVENTS`: pieces carrying
    less than ``boundary_share`` of the old population are ``boundary_change``; an old municipality
    with several major destinations is a ``split``; a new municipality with several major sources is
    a ``merger``; otherwise ``unchanged`` (same code) or ``rename``.
    """
    target, _ = _match_units(old_units, new_units, code_col, muni_col)
    matched = ~pd.isna(target)
    if (~matched).any():
        log.warning("lineage: %d old units could not be located in the new vintage", int((~matched).sum()))
    pop = pd.to_numeric(old_units[pop_col], errors="coerce").fillna(0).to_numpy(dtype=float)
    df = pd.DataFrame(
        {
            "from_code": old_units[muni_col].astype(str).to_numpy()[matched],
            "to_code": target[matched].astype(str),
            "population": pop[matched],
            "units": 1,
        }
    )
    if df.empty:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in LINEAGE_COLUMNS})
    flows = df.groupby(["from_code", "to_code"], as_index=False).agg(
        population=("population", "sum"), units=("units", "sum")
    )
    old_pop = pd.Series(pop).groupby(old_units[muni_col].astype(str).to_numpy()).sum()
    old_cnt = old_units[muni_col].astype(str).value_counts()
    tot_pop = flows["from_code"].map(old_pop).to_numpy(dtype=float)
    tot_cnt = flows["from_code"].map(old_cnt).to_numpy(dtype=float)
    weight = np.where(
        tot_pop > 0,
        flows["population"].to_numpy(dtype=float) / np.where(tot_pop > 0, tot_pop, 1.0),
        flows["units"].to_numpy(dtype=float) / np.maximum(tot_cnt, 1.0),
    )
    flows["population_weight"] = np.round(weight, 6)
    major = flows["population_weight"] >= boundary_share
    n_major_to = flows[major].groupby("from_code")["to_code"].nunique()
    n_major_from = flows[major].groupby("to_code")["from_code"].nunique()
    events = []
    for f, t, w in zip(flows["from_code"], flows["to_code"], flows["population_weight"], strict=True):
        if w < boundary_share:
            events.append("boundary_change")
        elif n_major_to.get(f, 0) > 1:
            events.append("split")
        elif n_major_from.get(t, 0) > 1:
            events.append("merger")
        else:
            events.append("unchanged" if f == t else "rename")
    flows["event"] = events
    flows["population"] = flows["population"].round().astype(np.int64)
    flows["units"] = flows["units"].astype(np.int64)
    out = flows[list(LINEAGE_COLUMNS)].sort_values(["from_code", "to_code"]).reset_index(drop=True)
    changed = out[out["event"] != "unchanged"]
    if len(changed):
        log.info(
            "lineage: %d changed municipality links (%s)",
            len(changed),
            changed["event"].value_counts().to_dict(),
        )
    return out


def remap_values(
    values_by_old_code: Mapping[str, float] | pd.Series,
    lineage: pd.DataFrame,
    how: str = "sum",
) -> pd.Series:
    """Re-express values keyed by old municipality codes on the new codes.

    ``how='sum'`` (additive quantities: votes, population) distributes each old value by
    ``population_weight``; ``how='mean'`` (rates, shares) takes the mean weighted by the population
    that moved along each link.  Old codes absent from ``values_by_old_code`` are ignored.
    """
    values = pd.Series(values_by_old_code, dtype=float)
    lin = lineage[lineage["from_code"].isin(values.index)].copy()
    lin["value"] = lin["from_code"].map(values)
    lin = lin[lin["value"].notna()]
    if how == "sum":
        lin["part"] = lin["value"] * lin["population_weight"]
        return lin.groupby("to_code")["part"].sum().rename(None).sort_index()
    if how == "mean":
        # Weight by the population that moved along each link.  Population counts and
        # ``population_weight`` shares have different scales, so they are never mixed: a new
        # municipality falls back to share weights only when none of its links moved anyone.
        weight = lin["population_weight"].astype(float)
        moved = lin["population"].astype(float) if "population" in lin else pd.Series(0.0, index=lin.index)
        moved_total = moved.groupby(lin["to_code"]).transform("sum")
        use = moved.where(moved_total > 0, weight)
        num = (lin["value"] * use).groupby(lin["to_code"]).sum()
        den = use.groupby(lin["to_code"]).sum()
        return (num / den.where(den > 0)).rename(None).sort_index()
    raise ValueError("how must be 'sum' or 'mean'")
