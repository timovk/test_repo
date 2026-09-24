"""Manual overrides applied after generation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.core.errors import ConfigError, DistrictingError
from app.districts.generator import generate_plan
from app.districts.overrides import OverrideEntry, apply_overrides, load_overrides
from app.districts.validation import validate_plan


def _boundary_unit(plan):  # type: ignore[no-untyped-def]
    """A unit whose neighbour lies in another district of the same province."""
    e = plan.edges
    ud = plan.unit_district
    for a, b in e.tolist():
        if ud[a] != ud[b] and plan.unit_province[a] == plan.unit_province[b]:
            return a, int(ud[b])
    raise AssertionError("no boundary")


def test_shipped_overrides_file_is_empty_and_valid() -> None:
    assert load_overrides() == []
    assert load_overrides("does/not/exist.yaml") == []


def test_unit_override(fine_plan, fine_geo) -> None:  # type: ignore[no-untyped-def]
    u, target = _boundary_unit(fine_plan)
    code = str(fine_plan.unit_codes[u])
    dcode = fine_plan.district_codes[target]
    new = apply_overrides(fine_plan, [{"unit": code, "district": dcode, "note": "test"}])
    assert new.unit_district[u] == target
    assert new.unit_overridden.sum() == 1 and new.overrides_applied == 1
    assert new.assignment_frame().set_index("unit_code").loc[code, "source"] == "override"
    # the original plan is untouched and the config hash changes with the override
    assert fine_plan.unit_district[u] != target
    assert new.config_hash != fine_plan.config_hash
    assert new.overrides == [{"unit": code, "district": dcode}]
    errors = validate_plan(new, fine_geo.units, fine_geo.seats).errors
    assert all("contiguous" in e or "deviation" in e for e in errors)


def test_municipality_override_and_warnings(fine_plan) -> None:  # type: ignore[no-untyped-def]
    # move a whole municipality into a different district of its province (likely non-contiguous)
    munis = fine_plan.unit_municipality
    ud = fine_plan.unit_district

    def movable(x: str) -> bool:
        ds = set(ud[munis == x].tolist())
        return len(ds) == 1 and (ud == next(iter(ds))).sum() > (munis == x).sum() and (munis == x).sum() >= 4

    m = next(x for x in np.unique(munis) if str(x).startswith("GM08") and movable(str(x)))
    prov = str(fine_plan.unit_province[munis == m][0])
    own = set(ud[munis == m].tolist())
    target = next(i for i, p in enumerate(fine_plan.district_province) if p == prov and i not in own)
    new = apply_overrides(
        fine_plan, [OverrideEntry(municipality=str(m), district=fine_plan.district_codes[target])]
    )
    assert (new.unit_district[munis == m] == target).all()
    assert new.overrides_applied == int((munis == m).sum())
    assert any(fine_plan.district_codes[target] in w for w in new.warnings)


def test_unit_entries_win_over_municipality_entries(fine_plan) -> None:  # type: ignore[no-untyped-def]
    u, target = _boundary_unit(fine_plan)
    m = str(fine_plan.unit_municipality[u])
    own = int(fine_plan.unit_district[u])
    entries = [
        {"unit": str(fine_plan.unit_codes[u]), "district": fine_plan.district_codes[own]},
        {"municipality": m, "district": fine_plan.district_codes[target]},
    ]
    new = apply_overrides(fine_plan, entries)
    assert new.unit_district[u] == own


def test_override_errors(fine_plan) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(DistrictingError, match="unknown unit"):
        apply_overrides(fine_plan, [{"unit": "BU99999999", "district": fine_plan.district_codes[0]}])
    with pytest.raises(DistrictingError, match="unknown municipality"):
        apply_overrides(fine_plan, [{"municipality": "GM9999", "district": fine_plan.district_codes[0]}])
    with pytest.raises(DistrictingError, match="unknown district"):
        apply_overrides(fine_plan, [{"unit": str(fine_plan.unit_codes[0]), "district": "XX-99"}])
    # cross-province move
    u = 0
    prov = fine_plan.unit_province[u]
    other = next(
        c for c, p in zip(fine_plan.district_codes, fine_plan.district_province, strict=True) if p != prov
    )
    with pytest.raises(DistrictingError, match="province"):
        apply_overrides(fine_plan, [{"unit": str(fine_plan.unit_codes[u]), "district": other}])
    # emptying a district
    d0 = fine_plan.district_codes[0]
    d1 = next(
        c
        for c, p in zip(fine_plan.district_codes, fine_plan.district_province, strict=True)
        if p == fine_plan.district_province[0] and c != d0
    )
    units = fine_plan.unit_codes[fine_plan.unit_district == 0]
    with pytest.raises(DistrictingError, match="without units"):
        apply_overrides(fine_plan, [{"unit": str(c), "district": d1} for c in units])
    with pytest.raises(ValueError):
        OverrideEntry(unit="BU1", municipality="GM1", district="NH-01")
    with pytest.raises(ValueError):
        OverrideEntry(unit="BU1", district="NH1")


def test_overrides_through_generate_plan(fine_geo, fine_plan, fast_config) -> None:  # type: ignore[no-untyped-def]
    subset = ["ZE"]
    units = fine_geo.units[fine_geo.units.province_code.isin(subset)]
    seats = {"ZE": fine_geo.seats["ZE"]}
    base = generate_plan(units, fine_geo.adjacency, seats, fast_config, seed=11)
    u, target = _boundary_unit(base)
    entry = {"unit": str(base.unit_codes[u]), "district": base.district_codes[target]}
    plan = generate_plan(units, fine_geo.adjacency, seats, fast_config, seed=11, overrides=[entry])
    assert plan.unit_district[u] == target
    assert plan.overrides_applied == 1
    assert "overrides" in plan.timings


def test_load_overrides_file(tmp_path: Path) -> None:
    p = tmp_path / "ov.yaml"
    p.write_text(
        "version: 1\noverrides:\n  - {unit: BU00000001, district: NB-07}\n  - {municipality: GM0855, district: NB-01}\n"
    )
    entries = load_overrides(p)
    assert [e.canonical() for e in entries] == [
        {"unit": "BU00000001", "district": "NB-07"},
        {"municipality": "GM0855", "district": "NB-01"},
    ]
    bad = tmp_path / "bad.yaml"
    bad.write_text("overrides:\n  - {unit: BU1, municipality: GM1, district: NB-01}\n")
    with pytest.raises(ConfigError):
        load_overrides(bad)
