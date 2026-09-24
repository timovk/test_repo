"""Vectorised, DB-independent view of the geographic hierarchy (REAL data).

:class:`GeographyFrame` is the contract between the geography pipeline and every numerical
engine (districting, political model, tabulation, forecasting, election night).  All arrays
are aligned NumPy arrays; hierarchy links are integer indices (not DB ids), so aggregation is
a single ``np.bincount``/``np.add.at``.

Levels:  P provinces (canonical order, see ``config/geography.yaml``) → M municipalities →
U units (CBS buurten, the precinct substitute).

Frames are built by :func:`app.geography.store.load_frame` from the processed GeoParquet
store, or synthetically by :func:`app.geography.synthetic.synthetic_frame` for tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

#: Canonical demographic model variables (column order of ``unit_demo``).  All are REAL CBS
#: indicators or deterministic transforms of them (``log_density``, ``urbanity``).
DEMOGRAPHIC_VARIABLES: tuple[str, ...] = (
    "log_density",  # ln(1 + inhabitants per km² land)
    "urbanity",  # 6 − CBS stedelijkheid class → 1 (not urban) … 5 (very urban)
    "pct_age_15_25",
    "pct_age_25_45",
    "pct_age_65_plus",
    "pct_households_with_children",
    "pct_single_households",
    "pct_origin_europe",
    "pct_origin_non_europe",
    "pct_education_high",
    "pct_education_low",
    "income_per_capita_keur",
    "pct_owner_occupied",
)


@dataclass
class GeographyFrame:
    year: int
    # --- provinces (P) -------------------------------------------------------------
    province_codes: list[str]  # ['GR', 'FR', …] canonical order
    province_names: list[str]
    province_cbs_codes: list[str]
    # --- municipalities (M) --------------------------------------------------------
    muni_codes: list[str]  # CBS codes 'GM0855'
    muni_names: list[str]
    muni_province: np.ndarray  # (M,) int index into provinces
    muni_population: np.ndarray  # (M,) int64
    muni_xy: np.ndarray  # (M, 2) float, EPSG:28992 metres (centroid)
    muni_lonlat: np.ndarray  # (M, 2) float
    # --- units (U) ------------------------------------------------------------------
    unit_codes: list[str]  # CBS buurt codes 'BU08550101'
    unit_names: list[str]
    unit_muni: np.ndarray  # (U,) int index into municipalities
    unit_province: np.ndarray  # (U,) int index into provinces
    unit_population: np.ndarray  # (U,) int64
    unit_eligible: np.ndarray  # (U,) int64 — DERIVED estimate of eligible voters
    unit_xy: np.ndarray  # (U, 2) float metres
    unit_urbanity_class: np.ndarray  # (U,) int CBS class 1..5 (0 = unknown)
    unit_land_km2: np.ndarray  # (U,) float
    demo_names: tuple[str, ...] = DEMOGRAPHIC_VARIABLES
    unit_demo: np.ndarray = field(default_factory=lambda: np.zeros((0, 0)))  # (U, K) raw (imputed filled)
    unit_demo_imputed: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), dtype=bool))  # (U, K)
    # Optional DB ids (0 when the frame was not loaded from the database).
    province_ids: np.ndarray | None = None
    muni_ids: np.ndarray | None = None
    unit_ids: np.ndarray | None = None
    # Lazily computed caches
    _unit_demo_z: np.ndarray | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ sizes
    @property
    def n_provinces(self) -> int:
        return len(self.province_codes)

    @property
    def n_munis(self) -> int:
        return len(self.muni_codes)

    @property
    def n_units(self) -> int:
        return len(self.unit_codes)

    # ------------------------------------------------------------------ lookups
    def province_index(self, code: str) -> int:
        return self.province_codes.index(code)

    def muni_index(self, code: str) -> int:
        return self._muni_lookup[code]

    def unit_index(self, code: str) -> int:
        return self._unit_lookup[code]

    @property
    def _muni_lookup(self) -> dict[str, int]:
        cache = self.__dict__.get("_muni_lookup_cache")
        if cache is None:
            cache = {c: i for i, c in enumerate(self.muni_codes)}
            self.__dict__["_muni_lookup_cache"] = cache
        return cache

    @property
    def _unit_lookup(self) -> dict[str, int]:
        cache = self.__dict__.get("_unit_lookup_cache")
        if cache is None:
            cache = {c: i for i, c in enumerate(self.unit_codes)}
            self.__dict__["_unit_lookup_cache"] = cache
        return cache

    def units_in_province(self, p: int) -> np.ndarray:
        return np.flatnonzero(self.unit_province == p)

    def units_in_muni(self, m: int) -> np.ndarray:
        return np.flatnonzero(self.unit_muni == m)

    def munis_in_province(self, p: int) -> np.ndarray:
        return np.flatnonzero(self.muni_province == p)

    # ------------------------------------------------------------------ aggregation
    def to_munis(self, unit_values: np.ndarray) -> np.ndarray:
        """Sum unit-level values (U,) or (U, K) to municipalities."""
        return _group_sum(self.unit_muni, unit_values, self.n_munis)

    def to_provinces(self, unit_values: np.ndarray) -> np.ndarray:
        return _group_sum(self.unit_province, unit_values, self.n_provinces)

    def province_population(self) -> np.ndarray:
        return self.to_provinces(self.unit_population)

    # ------------------------------------------------------------------ demographics
    def demo_column(self, name: str) -> np.ndarray:
        return self.unit_demo[:, self.demo_names.index(name)]

    @property
    def unit_demo_z(self) -> np.ndarray:
        """Population-weighted standardised demographics (mean 0, sd 1 over the population)."""
        if self._unit_demo_z is None:
            self._unit_demo_z = standardize(self.unit_demo, self.unit_population)
        return self._unit_demo_z

    def validate(self) -> list[str]:
        """Structural integrity checks; returns a list of problems (empty = OK)."""
        problems: list[str] = []
        U, M, P = self.n_units, self.n_munis, self.n_provinces
        for name, arr, n in (
            ("unit_muni", self.unit_muni, U),
            ("unit_province", self.unit_province, U),
            ("unit_population", self.unit_population, U),
            ("unit_eligible", self.unit_eligible, U),
            ("muni_province", self.muni_province, M),
            ("muni_population", self.muni_population, M),
        ):
            if len(arr) != n:
                problems.append(f"{name} has length {len(arr)}, expected {n}")
        if problems:
            return problems
        if U and (self.unit_muni.min() < 0 or self.unit_muni.max() >= M):
            problems.append("unit_muni index out of range")
        if M and (self.muni_province.min() < 0 or self.muni_province.max() >= P):
            problems.append("muni_province index out of range")
        if U and not np.array_equal(self.muni_province[self.unit_muni], self.unit_province):
            problems.append("unit_province inconsistent with muni_province[unit_muni]")
        if not np.array_equal(self.to_munis(self.unit_population), self.muni_population):
            problems.append("municipality populations do not equal the sum of their units")
        if len(set(self.unit_codes)) != U:
            problems.append("duplicate unit codes")
        if len(set(self.muni_codes)) != M:
            problems.append("duplicate municipality codes")
        missing = set(range(M)) - set(np.unique(self.unit_muni).tolist())
        if missing:
            problems.append(f"{len(missing)} municipalities have no units")
        if (self.unit_eligible > self.unit_population).any():
            problems.append("eligible voters exceed population in some units")
        return problems


def _group_sum(index: np.ndarray, values: np.ndarray, n: int) -> np.ndarray:
    values = np.asarray(values)
    if values.ndim == 1:
        if np.issubdtype(values.dtype, np.integer):
            out = np.zeros(n, dtype=np.int64)
            np.add.at(out, index, values)
            return out
        return np.bincount(index, weights=values, minlength=n)
    out = np.zeros((n, *values.shape[1:]), dtype=values.dtype if np.issubdtype(values.dtype, np.integer) else float)
    np.add.at(out, index, values)
    return out


def group_sum(index: np.ndarray, values: np.ndarray, n: int) -> np.ndarray:
    """Public alias: sum ``values`` (N,) or (N, K) into ``n`` groups given by ``index`` (N,)."""
    return _group_sum(index, values, n)


def standardize(x: np.ndarray, weights: np.ndarray | Sequence[float]) -> np.ndarray:
    """Weighted z-scores per column; constant columns become 0."""
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x.copy()
    w = np.asarray(weights, dtype=float)
    if w.sum() <= 0:
        w = np.ones_like(w)
    w = w / w.sum()
    mean = (x * w[:, None]).sum(axis=0)
    var = (((x - mean) ** 2) * w[:, None]).sum(axis=0)
    sd = np.sqrt(var)
    sd[sd < 1e-12] = 1.0
    z = (x - mean) / sd
    z[:, np.sqrt(var) < 1e-12] = 0.0
    return z
