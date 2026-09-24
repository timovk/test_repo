"""Constitutional and integrity validation of a House district plan.

Checks (errors unless noted):

* the plan has exactly ``constitution.house_seats`` districts;
* every province has exactly its apportioned number of districts;
* every geographic unit is assigned to exactly one district ("every voter/geographic unit is
  represented exactly once"; "every geographic unit assigned to a House district is accounted
  for") — no missing, unknown, duplicate or unassigned units;
* no district crosses a province boundary;
* every district is non-empty and has a positive population;
* every district is contiguous (graph contiguity over the unit adjacency incl. water links);
* every district's deviation from its provincial target is within the hard maximum
  (a deviation beyond the *target* tolerance is a warning).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.config import get_constitution
from app.core.constitution import ConstitutionConfig
from app.core.errors import ValidationError
from app.districts.plan import GeneratedPlan


@dataclass
class PlanValidation:
    """Result of :func:`validate_plan`."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok

    def raise_if_invalid(self) -> None:
        """Raise :class:`~app.core.errors.ValidationError` listing every error."""
        if self.errors:
            raise ValidationError("District plan failed validation", self.errors)


def validate_plan(
    plan: GeneratedPlan,
    units_df: pd.DataFrame,
    seats_by_province: Mapping[str, int],
    constitution: ConstitutionConfig | None = None,
) -> PlanValidation:
    """Validate ``plan`` against the unit table it must cover and the apportionment.

    Args:
        plan: the plan to check.
        units_df: the complete unit table (``code``, ``province_code``; ``population`` optional —
            the plan's populations are cross-checked when present).
        seats_by_province: the apportionment the plan must implement.
        constitution: defaults to the active constitution (``house_seats``).
    """
    cons = constitution or get_constitution()
    res = PlanValidation()
    err, warn = res.errors, res.warnings
    n_d = plan.n_districts
    # ---- district counts ---------------------------------------------------------------
    if n_d != cons.house_seats:
        err.append(f"plan has {n_d} districts; the constitution requires {cons.house_seats}")
    seats = {str(p): int(s) for p, s in seats_by_province.items()}
    if sum(seats.values()) != cons.house_seats:
        err.append(f"apportionment totals {sum(seats.values())} seats, not {cons.house_seats}")
    per_prov = pd.Series(plan.district_province).value_counts().to_dict()
    for p in sorted(set(seats) | set(per_prov)):
        have, want = int(per_prov.get(p, 0)), seats.get(p, 0)
        if have != want:
            err.append(f"province {p} has {have} districts, apportioned {want}")
    if len(set(plan.district_codes)) != n_d:
        err.append("duplicate district codes")
    # ---- unit coverage -----------------------------------------------------------------
    codes = plan.unit_codes.astype(str)
    uniq, counts = np.unique(codes, return_counts=True)
    dup = uniq[counts > 1]
    if len(dup):
        err.append(f"{len(dup)} units assigned more than once (e.g. {', '.join(dup[:5])})")
    expected = pd.Index(units_df["code"].astype(str).unique())
    have = pd.Index(uniq)
    missing = np.sort(expected.difference(have).to_numpy(dtype=str))
    extra = np.sort(have.difference(expected).to_numpy(dtype=str))
    if len(missing):
        err.append(f"{len(missing)} units not assigned to any district (e.g. {', '.join(missing[:5])})")
    if len(extra):
        err.append(f"{len(extra)} assigned units are not in the unit table (e.g. {', '.join(extra[:5])})")
    ud = plan.unit_district
    bad_idx = (ud < 0) | (ud >= n_d)
    if bad_idx.any():
        err.append(f"{int(bad_idx.sum())} units have no valid district index")
        return res
    # ---- province boundaries -------------------------------------------------------------
    table = units_df.drop_duplicates("code").copy()
    table.index = pd.Index(table["code"].astype(str))
    prov_of_unit = table["province_code"].astype(str).reindex(codes).to_numpy(dtype=object)
    d_prov = np.asarray(plan.district_province, dtype=object)[ud]
    known = pd.notna(prov_of_unit)
    crossing = known & (prov_of_unit != d_prov)
    if crossing.any():
        bad_d = sorted(set(np.asarray(plan.district_codes, dtype=object)[ud[crossing]].tolist()))
        err.append(f"{len(bad_d)} districts contain units of another province: {', '.join(bad_d[:10])}")
    if not np.array_equal(plan.unit_province.astype(str)[known], prov_of_unit[known].astype(str)):
        err.append("plan unit provinces disagree with the unit table")
    if "population" in table.columns:
        pops = (
            pd.to_numeric(table["population"], errors="coerce")
            .reindex(codes)
            .fillna(0)
            .round()
            .astype(np.int64)
        )
        if not np.array_equal(pops.to_numpy()[known], plan.unit_population[known]):
            warn.append("plan unit populations differ from the unit table")
    # ---- district contents ---------------------------------------------------------------
    n_units = np.bincount(ud, minlength=n_d)
    pop = plan.district_population()
    for i in np.flatnonzero(n_units == 0):
        err.append(f"{plan.district_codes[i]}: district has no units")
    for i in np.flatnonzero((n_units > 0) & (pop <= 0)):
        err.append(f"{plan.district_codes[i]}: district has no population")
    comps = plan.district_components()
    for i in np.flatnonzero((comps > 1) & (n_units > 0)):
        err.append(f"{plan.district_codes[i]}: district is not contiguous ({int(comps[i])} pieces)")
    dev = plan.deviation_pct()
    cfg = plan.config
    for i in range(n_d):
        if abs(dev[i]) > cfg.max_deviation_pct:
            err.append(
                f"{plan.district_codes[i]}: population deviation {dev[i]:+.2f}% exceeds the hard maximum "
                f"±{cfg.max_deviation_pct}%"
            )
        elif abs(dev[i]) > cfg.target_deviation_pct:
            warn.append(
                f"{plan.district_codes[i]}: population deviation {dev[i]:+.2f}% exceeds the target "
                f"±{cfg.target_deviation_pct}%"
            )
    if plan.overrides_applied:
        warn.append(f"{plan.overrides_applied} unit(s) assigned by manual override")
    return res
