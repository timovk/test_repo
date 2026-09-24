"""Plan validation: constitutional and integrity invariants."""

from __future__ import annotations

import copy

import numpy as np
import pytest

from app.core.constitution import ConstitutionConfig
from app.core.errors import ValidationError
from app.districts.validation import validate_plan


def _mutable(plan):  # type: ignore[no-untyped-def]
    p = copy.copy(plan)
    p.unit_district = plan.unit_district.copy()
    p.unit_codes = plan.unit_codes.copy()
    p.district_province = list(plan.district_province)
    p.invalidate_caches()
    return p


def test_valid_plan(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    v = validate_plan(fine_plan, fine_geo.units, fine_geo.seats)
    # a PlanValidation *is* the list of errors (docs/ARCHITECTURE.md: validate_plan -> list[str])
    assert v.ok and v == [] and not v and len(v) == 0 and list(v) == v.errors == []
    assert isinstance(v, list) and isinstance(v.warnings, list)
    v.raise_if_invalid()


def test_wrong_totals(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    cons = ConstitutionConfig(house_seats=151)
    v = validate_plan(fine_plan, fine_geo.units, fine_geo.seats, constitution=cons)
    assert any("151" in e for e in v.errors)
    seats = dict(fine_geo.seats)
    seats["ZE"] += 1
    seats["ZH"] -= 1
    v2 = validate_plan(fine_plan, fine_geo.units, seats)
    assert any("province ZE" in e for e in v2.errors) and any("province ZH" in e for e in v2.errors)
    with pytest.raises(ValidationError):
        v2.raise_if_invalid()


def test_unit_coverage(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    extra = fine_geo.units.copy()
    extra = extra._append(extra.iloc[[0]].assign(code="BU99999999"))
    v = validate_plan(fine_plan, extra, fine_geo.seats)
    assert any("not assigned" in e for e in v.errors)
    p = _mutable(fine_plan)
    p.unit_codes[1] = p.unit_codes[0]
    v2 = validate_plan(p, fine_geo.units, fine_geo.seats)
    assert any("more than once" in e for e in v2.errors)
    p3 = _mutable(fine_plan)
    p3.unit_district[0] = -1
    assert any("no valid district" in e for e in validate_plan(p3, fine_geo.units, fine_geo.seats).errors)


def test_cross_province_and_contiguity_and_deviation(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    p = _mutable(fine_plan)
    # move one unit into a district of another province → crosses a boundary and is detached
    u = 0
    other = next(i for i, q in enumerate(p.district_province) if q != p.unit_province[u])
    p.unit_district[u] = other
    v = validate_plan(p, fine_geo.units, fine_geo.seats)
    assert any("another province" in e for e in v.errors)
    assert any("not contiguous" in e for e in v.errors)
    # empty a district → error; the neighbour becomes too populous → deviation error
    q = _mutable(fine_plan)
    q.unit_district[q.unit_district == 1] = 0
    v2 = validate_plan(q, fine_geo.units, fine_geo.seats)
    assert any("no units" in e for e in v2.errors)
    assert any("hard maximum" in e for e in v2.errors)


def test_zero_population_district(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    p = _mutable(fine_plan)
    p.unit_population = fine_plan.unit_population.copy()
    p.unit_population[p.unit_district == 3] = 0
    v = validate_plan(p, fine_geo.units.drop(columns=["population"]), fine_geo.seats)
    assert any("no population" in e for e in v.errors)
    assert np.isfinite(p.deviation_pct()).all()


def test_validation_is_a_list_of_errors(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    """docs/ARCHITECTURE.md: ``validate_plan(...) -> list[str]`` (empty = valid)."""
    p = _mutable(fine_plan)
    p.unit_district[p.unit_district == 1] = 0
    v = validate_plan(p, fine_geo.units, fine_geo.seats)
    assert v and not v.ok and len(v) == len(v.errors) > 0
    assert [e for e in v] == v.errors and all(isinstance(e, str) for e in v)
    assert "PlanValidation(errors=" in repr(v)


def test_stale_or_tampered_targets_are_detected(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    p = _mutable(fine_plan)
    # a plan whose targets were inflated would otherwise hide real deviations
    p.district_target = fine_plan.district_target * 1.10
    v = validate_plan(p, fine_geo.units, fine_geo.seats)
    assert any("target population" in e for e in v.errors)
    # deviations are computed against the recomputed target, not the stated one
    assert not any("hard maximum" in e for e in v.errors)
    # populations taken from the unit table: a stale plan population is reported and corrected
    units = fine_geo.units.copy()
    units.loc[units.index[0], "population"] += 1
    v2 = validate_plan(fine_plan, units, fine_geo.seats)
    assert any("populations differ" in w for w in v2.warnings)


def test_codes_and_numbering_are_checked(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    p = _mutable(fine_plan)
    p.district_codes = list(fine_plan.district_codes)
    p.district_numbers = list(fine_plan.district_numbers)
    p.district_codes[0] = "Groningen 1"
    v = validate_plan(p, fine_geo.units, fine_geo.seats)
    assert any("<PV>-<NN>" in e for e in v.errors)
    q = _mutable(fine_plan)
    q.district_numbers = list(fine_plan.district_numbers)
    q.district_codes = list(fine_plan.district_codes)
    q.district_numbers[1] = q.district_numbers[0]
    q.district_codes[1] = q.district_codes[0]
    v2 = validate_plan(q, fine_geo.units, fine_geo.seats)
    assert any("numbered 1.." in e for e in v2.errors) and any(
        "duplicate district codes" in e for e in v2.errors
    )


def test_inconsistent_arrays(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    p = _mutable(fine_plan)
    p.unit_district = fine_plan.unit_district[:-1]
    v = validate_plan(p, fine_geo.units, fine_geo.seats)
    assert v.errors == ["plan arrays have inconsistent lengths: unit_district"]
