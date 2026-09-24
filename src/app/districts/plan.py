"""The engine-level representation of a House district plan (FICTIONAL electoral geography).

:class:`GeneratedPlan` is produced by :func:`app.districts.generator.generate_plan`, modified by
:mod:`app.districts.overrides`, checked by :mod:`app.districts.validation`, summarised by
:mod:`app.districts.stats` and persisted by :mod:`app.districts.service`.  It is DB-independent:
units are identified by CBS codes and districts by codes such as ``NB-07``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from app.districts.config import DistrictConfig

#: ``DistrictAssignment.source`` values.
SOURCE_GENERATED = "generated"
SOURCE_OVERRIDE = "override"


def district_code(province_code: str, number: int) -> str:
    """House district code: ``district_code('NB', 7) == 'NB-07'``."""
    return f"{province_code}-{int(number):02d}"


@dataclass
class GeneratedPlan:
    """A complete assignment of every geographic unit to exactly one House district.

    Unit arrays are aligned with the ``units_gdf`` rows the plan was generated from; district arrays
    are ordered by province (``seats_by_province`` order) and then by district number.
    """

    # --- units (U) -------------------------------------------------------------------------
    unit_codes: np.ndarray  #: (U,) object — CBS buurt codes
    unit_province: np.ndarray  #: (U,) object — province code of each unit
    unit_municipality: np.ndarray  #: (U,) object — CBS municipality code of each unit
    unit_population: np.ndarray  #: (U,) int64
    unit_district: np.ndarray  #: (U,) int64 — index into the district arrays
    unit_overridden: np.ndarray  #: (U,) bool — assignment set by a manual override
    # --- districts (D) ---------------------------------------------------------------------
    district_codes: list[str]
    district_names: list[str]
    district_province: list[str]
    district_numbers: list[int]
    district_target: np.ndarray  #: (D,) float — provincial target population (province pop / seats)
    # --- provenance ------------------------------------------------------------------------
    seats_by_province: dict[str, int]
    seed: int
    config: DistrictConfig
    config_json: str
    config_hash: str
    method: str
    #: Unit adjacency used for contiguity: (E, 2) unit-index pairs and a water-link flag.
    edges: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), dtype=np.int64))
    edge_water: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    edge_border_m: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=float))
    overrides: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Per-province generation diagnostics (cuts, refinements, local-search moves …).
    diagnostics: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ------------------------------------------------------------------ sizes / lookups
    @property
    def n_units(self) -> int:
        return len(self.unit_codes)

    @property
    def n_districts(self) -> int:
        return len(self.district_codes)

    @property
    def overrides_applied(self) -> int:
        """Number of units whose assignment was set by a manual override."""
        return int(self.unit_overridden.sum())

    def district_index(self, code: str) -> int:
        lookup = self.__dict__.get("_district_lookup")
        if lookup is None:
            lookup = {c: i for i, c in enumerate(self.district_codes)}
            self.__dict__["_district_lookup"] = lookup
        return lookup[code]

    def unit_index(self, code: str) -> int:
        lookup = self.__dict__.get("_unit_lookup")
        if lookup is None:
            lookup = {c: i for i, c in enumerate(self.unit_codes.tolist())}
            self.__dict__["_unit_lookup"] = lookup
        return lookup[code]

    def invalidate_caches(self) -> None:
        """Drop cached lookups (call after mutating district codes or unit arrays)."""
        self.__dict__.pop("_district_lookup", None)
        self.__dict__.pop("_unit_lookup", None)

    # ------------------------------------------------------------------ derived quantities
    def district_population(self) -> np.ndarray:
        """(D,) int64 population per district."""
        return (
            np.bincount(
                self.unit_district, weights=self.unit_population.astype(float), minlength=self.n_districts
            )
            .round()
            .astype(np.int64)
        )

    def deviation_pct(self) -> np.ndarray:
        """(D,) float — (population − target) / target × 100."""
        tgt = np.where(self.district_target > 0, self.district_target, 1.0)
        return (self.district_population() - self.district_target) / tgt * 100.0

    @property
    def max_abs_deviation_pct(self) -> float:
        return float(np.abs(self.deviation_pct()).max()) if self.n_districts else 0.0

    @property
    def mean_abs_deviation_pct(self) -> float:
        return float(np.abs(self.deviation_pct()).mean()) if self.n_districts else 0.0

    def split_municipalities(self) -> list[str]:
        """Municipality codes whose units lie in more than one district (sorted)."""
        df = pd.DataFrame({"m": self.unit_municipality, "d": self.unit_district})
        n = df.drop_duplicates().groupby("m", sort=True).size()
        return [str(m) for m in n.index[n.to_numpy() > 1]]

    def district_components(self) -> np.ndarray:
        """(D,) int — number of graph-connected components of every district (1 = contiguous)."""
        return district_component_counts(self.unit_district, self.edges, self.n_districts)

    def noncontiguous_districts(self) -> list[str]:
        comps = self.district_components()
        return [self.district_codes[i] for i in np.flatnonzero(comps != 1)]

    def unit_district_codes(self) -> np.ndarray:
        """(U,) object — district code of every unit."""
        return np.asarray(self.district_codes, dtype=object)[self.unit_district]

    def assignment_frame(self) -> pd.DataFrame:
        """``unit_code, municipality_code, province_code, district_code, source`` (one row per unit)."""
        return pd.DataFrame(
            {
                "unit_code": self.unit_codes,
                "municipality_code": self.unit_municipality,
                "province_code": self.unit_province,
                "district_code": self.unit_district_codes(),
                "source": np.where(self.unit_overridden, SOURCE_OVERRIDE, SOURCE_GENERATED),
            }
        )

    def districts_frame(self) -> pd.DataFrame:
        """``code, number, name, province_code, population, target, deviation_pct, n_units``."""
        return pd.DataFrame(
            {
                "code": self.district_codes,
                "number": self.district_numbers,
                "name": self.district_names,
                "province_code": self.district_province,
                "population": self.district_population(),
                "target": self.district_target,
                "deviation_pct": self.deviation_pct(),
                "n_units": np.bincount(self.unit_district, minlength=self.n_districts),
            }
        )

    def summary(self) -> dict[str, Any]:
        """Compact plan-level metrics (JSON-serialisable)."""
        return {
            "method": self.method,
            "seed": int(self.seed),
            "config_hash": self.config_hash,
            "total_districts": self.n_districts,
            "max_abs_deviation_pct": round(self.max_abs_deviation_pct, 4),
            "mean_abs_deviation_pct": round(self.mean_abs_deviation_pct, 4),
            "split_municipalities": len(self.split_municipalities()),
            "noncontiguous_districts": len(self.noncontiguous_districts()),
            "overrides_applied": self.overrides_applied,
            "warnings": len(self.warnings),
            "generation_seconds": round(float(self.timings.get("total", 0.0)), 3),
        }


def district_component_counts(unit_district: np.ndarray, edges: np.ndarray, n_districts: int) -> np.ndarray:
    """(D,) number of connected components of each district in the unit graph ``edges``."""
    n = len(unit_district)
    if n == 0:
        return np.zeros(n_districts, dtype=np.int64)
    if len(edges):
        a, b = edges[:, 0], edges[:, 1]
        same = unit_district[a] == unit_district[b]
        graph = coo_matrix((np.ones(int(same.sum())), (a[same], b[same])), shape=(n, n))
    else:
        graph = coo_matrix((n, n))
    _, labels = connected_components(graph, directed=False)
    pairs = np.unique(np.column_stack([unit_district, labels]), axis=0)
    return np.bincount(pairs[:, 0], minlength=n_districts)
