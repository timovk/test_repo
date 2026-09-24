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
  (a deviation beyond the *target* tolerance is a warning); the target is recomputed from the unit
  table (province population / apportioned seats) and must agree with the plan's own targets;
* district codes are ``<PV>-<NN>`` and every province's districts are numbered 1 … k.

The result is a :class:`PlanValidation`: a ``list[str]`` of errors (the ``validate_plan(...) ->
list[str]`` contract of docs/ARCHITECTURE.md — empty means valid, so ``if validate_plan(...):``
means *invalid*) that also carries ``warnings`` and the ``ok`` / ``errors`` / ``raise_if_invalid``
helpers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

import numpy as np
import pandas as pd

from app.core.config import get_constitution
from app.core.constitution import ConstitutionConfig
from app.core.errors import ValidationError
from app.districts.plan import GeneratedPlan, district_code

#: House district code: province code, hyphen, two-digit (or longer) number.
DISTRICT_CODE_RE = re.compile(r"^[A-Z]{2}-\d{2,}$")


class PlanValidation(list[str]):
    """Result of :func:`validate_plan`: the list of errors (empty = valid) plus ``warnings``.

    It *is* a ``list[str]`` of error messages (docs/ARCHITECTURE.md: ``validate_plan(...) ->
    list[str]``), so iteration, ``len``, indexing, comparison with a list and truthiness follow the
    errors — ``if validate_plan(...):`` means the plan is **invalid**; use :attr:`ok` for the
    positive check.
    """

    def __init__(self, errors: Iterable[str] = (), warnings: Iterable[str] = ()) -> None:
        super().__init__(errors)
        self.warnings: list[str] = list(warnings)

    @property
    def errors(self) -> list[str]:
        """The error messages (this list itself)."""
        return self

    @property
    def ok(self) -> bool:
        """True when the plan passed every check (warnings allowed)."""
        return len(self) == 0

    def __repr__(self) -> str:
        return f"PlanValidation(errors={list(self)!r}, warnings={self.warnings!r})"

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
    n_u = len(plan.unit_codes)
    lengths = {
        "unit_district": len(plan.unit_district),
        "unit_province": len(plan.unit_province),
        "unit_municipality": len(plan.unit_municipality),
        "unit_population": len(plan.unit_population),
    }
    bad_len = sorted(k for k, v in lengths.items() if v != n_u)
    d_lengths = {
        "district_names": len(plan.district_names),
        "district_province": len(plan.district_province),
        "district_numbers": len(plan.district_numbers),
        "district_target": len(plan.district_target),
    }
    bad_len += sorted(k for k, v in d_lengths.items() if v != n_d)
    if bad_len:
        err.append(f"plan arrays have inconsistent lengths: {', '.join(bad_len)}")
        return res
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
    bad_codes = [
        c
        for c, p, n in zip(plan.district_codes, plan.district_province, plan.district_numbers, strict=True)
        if not DISTRICT_CODE_RE.match(str(c)) or str(c) != district_code(str(p), int(n))
    ]
    if bad_codes:
        err.append(
            f"{len(bad_codes)} district codes do not match '<PV>-<NN>' of their province and number "
            f"(e.g. {', '.join(map(str, bad_codes[:5]))})"
        )
    numbers: dict[str, list[int]] = {}
    for p, n in zip(plan.district_province, plan.district_numbers, strict=True):
        numbers.setdefault(str(p), []).append(int(n))
    for p, nums in sorted(numbers.items()):
        if sorted(nums) != list(range(1, len(nums) + 1)):
            err.append(f"province {p}: districts are not numbered 1..{len(nums)}")
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
    # ---- populations and targets (recomputed from the unit table when it has populations) --
    unit_pop = plan.unit_population.astype(np.int64)
    if "population" in table.columns:
        pops = (
            pd.to_numeric(table["population"], errors="coerce")
            .reindex(codes)
            .fillna(0)
            .clip(lower=0)
            .round()
            .astype(np.int64)
            .to_numpy()
        )
        if not np.array_equal(pops[known], unit_pop[known]):
            warn.append("plan unit populations differ from the unit table; the table's populations are used")
            unit_pop = np.where(known, pops, unit_pop)
    pop = np.bincount(ud, weights=unit_pop.astype(float), minlength=n_d).round().astype(np.int64)
    unit_prov = np.where(known, prov_of_unit, plan.unit_province).astype(str)
    prov_pop = pd.Series(unit_pop, dtype="float64").groupby(unit_prov).sum().to_dict()
    target = np.asarray(
        [prov_pop.get(str(p), 0.0) / max(seats.get(str(p), 0), 1) for p in plan.district_province],
        dtype=float,
    )
    stated = np.asarray(plan.district_target, dtype=float)
    off = ~(np.abs(stated - target) <= np.maximum(0.5, 1e-9 * np.abs(target)))
    if off.any():
        bad_t = [plan.district_codes[i] for i in np.flatnonzero(off)]
        err.append(
            f"{len(bad_t)} districts have a target population that is not their province population / "
            f"seats (e.g. {', '.join(bad_t[:5])})"
        )
    # ---- district contents ---------------------------------------------------------------
    n_units = np.bincount(ud, minlength=n_d)
    for i in np.flatnonzero(n_units == 0):
        err.append(f"{plan.district_codes[i]}: district has no units")
    for i in np.flatnonzero((n_units > 0) & (pop <= 0)):
        err.append(f"{plan.district_codes[i]}: district has no population")
    comps = plan.district_components()
    for i in np.flatnonzero((comps > 1) & (n_units > 0)):
        err.append(f"{plan.district_codes[i]}: district is not contiguous ({int(comps[i])} pieces)")
    dev = (pop - target) / np.where(target > 0, target, 1.0) * 100.0
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
