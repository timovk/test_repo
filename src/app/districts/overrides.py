"""Manual district overrides (``config/districts/overrides.yaml``).

An override pins a unit (CBS buurt) or a whole municipality to a district after generation::

    overrides:
      - {unit: BU03630000, district: NH-03, note: "keep the Dam square with Centrum"}
      - {municipality: GM0363, district: NH-03}

Rules:

* municipality entries are applied first, unit entries afterwards (the more specific entry wins);
  within each kind, later entries override earlier ones;
* unknown unit / municipality / district codes and moves across a province boundary are hard
  errors (:class:`~app.core.errors.DistrictingError`), as is an override that empties a district;
* resulting non-contiguity or deviation beyond the tolerances are reported as warnings (the
  validation step decides whether the plan is acceptable).

Overridden units are persisted with ``DistrictAssignment.source = 'override'`` and the overrides
are part of the plan's configuration hash.
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import config_path, load_yaml, parse_config
from app.core.errors import DistrictingError
from app.core.logging import get_logger
from app.core.rng import config_hash
from app.districts.plan import GeneratedPlan

log = get_logger(__name__)

DEFAULT_OVERRIDES_FILE = "districts/overrides.yaml"


class OverrideEntry(BaseModel):
    """Pin one unit or all units of one municipality to a district."""

    model_config = ConfigDict(extra="forbid")

    unit: str | None = Field(None, description="CBS buurt code, e.g. BU03630000")
    municipality: str | None = Field(None, description="CBS municipality code, e.g. GM0363")
    district: str = Field(..., pattern=r"^[A-Z]{2}-\d{2,3}$", description="district code, e.g. NH-03")
    note: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> OverrideEntry:
        if (self.unit is None) == (self.municipality is None):
            raise ValueError("an override needs exactly one of 'unit' or 'municipality'")
        return self

    def canonical(self) -> dict[str, Any]:
        """Hash-relevant content (notes excluded)."""
        key = "unit" if self.unit is not None else "municipality"
        return {key: self.unit if self.unit is not None else self.municipality, "district": self.district}


class OverridesDocument(BaseModel):
    """Schema of ``config/districts/overrides.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    overrides: list[OverrideEntry] = Field(default_factory=list)


def load_overrides(path: str | Path | None = None) -> list[OverrideEntry]:
    """Load the overrides document (default ``config/districts/overrides.yaml``; missing → [])."""
    p = config_path(path or DEFAULT_OVERRIDES_FILE)
    if not p.exists():
        return []
    doc = parse_config(load_yaml(p), OverridesDocument, source=str(p))
    return list(doc.overrides)


def coerce_overrides(overrides: Sequence[Any] | None) -> list[OverrideEntry]:
    """Accept :class:`OverrideEntry` objects or plain dicts."""
    out: list[OverrideEntry] = []
    for o in overrides or []:
        out.append(o if isinstance(o, OverrideEntry) else OverrideEntry.model_validate(o))
    return out


def apply_overrides(plan: GeneratedPlan, overrides: Sequence[Any]) -> GeneratedPlan:
    """Return a copy of ``plan`` with ``overrides`` applied (see module docstring).

    Raises:
        DistrictingError: unknown codes, cross-province moves or an emptied district.
    """
    entries = coerce_overrides(overrides)
    if not entries:
        return plan
    new = copy.copy(plan)
    new.unit_district = plan.unit_district.copy()
    new.unit_overridden = plan.unit_overridden.copy()
    new.warnings = list(plan.warnings)
    # the copy must not share mutable containers with the original plan
    new.district_codes = list(plan.district_codes)
    new.district_names = list(plan.district_names)
    new.district_province = list(plan.district_province)
    new.district_numbers = list(plan.district_numbers)
    new.seats_by_province = dict(plan.seats_by_province)
    new.timings = dict(plan.timings)
    new.diagnostics = dict(plan.diagnostics)
    new.overrides = [*plan.overrides, *(e.canonical() for e in entries)]
    new.invalidate_caches()
    district_idx = {c: i for i, c in enumerate(plan.district_codes)}
    unit_idx = {c: i for i, c in enumerate(plan.unit_codes.tolist())}
    muni_units: dict[str, np.ndarray] = {}
    munis = plan.unit_municipality
    order = np.argsort(munis.astype(str), kind="stable")
    sorted_m = munis[order]
    bounds = np.flatnonzero(np.r_[True, sorted_m[1:] != sorted_m[:-1], True])
    for s, e in itertools.pairwise(bounds):
        muni_units[str(sorted_m[s])] = order[s:e]
    problems: list[str] = []
    ordered = [e for e in entries if e.municipality is not None] + [e for e in entries if e.unit is not None]
    for entry in ordered:
        d = district_idx.get(entry.district)
        if d is None:
            problems.append(f"override references unknown district {entry.district}")
            continue
        if entry.unit is not None:
            u = unit_idx.get(entry.unit)
            if u is None:
                problems.append(f"override references unknown unit {entry.unit}")
                continue
            idx = np.asarray([u])
            label = entry.unit
        else:
            idx = muni_units.get(str(entry.municipality))
            if idx is None:
                problems.append(f"override references unknown municipality {entry.municipality}")
                continue
            label = str(entry.municipality)
        provs = set(plan.unit_province[idx].tolist())
        target_prov = plan.district_province[d]
        if provs != {target_prov}:
            problems.append(
                f"override moves {label} ({', '.join(sorted(provs))}) into {entry.district} "
                f"of province {target_prov}: districts may not cross province boundaries"
            )
            continue
        new.unit_district[idx] = d
        new.unit_overridden[idx] = True
    if problems:
        raise DistrictingError("Invalid district overrides:\n  - " + "\n  - ".join(problems))
    counts = np.bincount(new.unit_district, minlength=new.n_districts)
    empty = [new.district_codes[i] for i in np.flatnonzero(counts == 0)]
    if empty:
        raise DistrictingError(f"overrides leave districts without units: {empty}")
    cfg = new.config
    dev = new.deviation_pct()
    touched = sorted(
        set(new.unit_district[new.unit_overridden].tolist())
        | set(plan.unit_district[new.unit_overridden].tolist())
    )
    comps = new.district_components()
    for i in touched:
        code = new.district_codes[i]
        if comps[i] != 1:
            new.warnings.append(f"{code}: not contiguous after overrides ({int(comps[i])} pieces)")
        if abs(dev[i]) > cfg.max_deviation_pct:
            new.warnings.append(f"{code}: deviation {dev[i]:+.2f}% after overrides exceeds the hard maximum")
        elif abs(dev[i]) > cfg.target_deviation_pct:
            new.warnings.append(
                f"{code}: deviation {dev[i]:+.2f}% after overrides exceeds the target tolerance"
            )
    new.config_json = cfg.canonical_json(new.overrides)
    new.config_hash = config_hash(new.config_json)
    log.info("applied %d override entries (%d units)", len(entries), int(new.unit_overridden.sum()))
    return new
