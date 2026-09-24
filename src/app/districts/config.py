"""Configuration schema of the House district generator (``config/districts.yaml``).

Everything that influences a generated plan lives in :class:`DistrictConfig`; together with the
root seed, the apportionment and the manual overrides it makes a plan exactly reproducible.  The
canonical JSON of the configuration (see :meth:`DistrictConfig.canonical_json`) is hashed with
:func:`app.core.rng.config_hash` and stored with every plan.

Parameters are documented in ``docs/DISTRICTING.md``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import load_config


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CutWeights(_Model):
    """Score weights of a candidate bisection cut (lower score = better cut)."""

    #: Per unit of normalised population deviation (deviation / allowed tolerance of each side).
    deviation: float = Field(1.0, ge=0)
    #: Per municipality that is split for the first time by the cut.
    new_split: float = Field(3.0, ge=0)
    #: Extra penalty for newly splitting a *small* municipality: ``small_split × (1 − pop/target)``.
    small_split: float = Field(2.0, ge=0)
    #: Per already-split municipality (e.g. a large city) that the cut passes through again.
    existing_split: float = Field(0.3, ge=0)
    #: Per unit of cut length / sqrt(region area) — the compactness term.
    cut_length: float = Field(0.8, ge=0)
    #: Per district of difference between the two sides' district counts (0 for halves).
    unbalanced: float = Field(0.15, ge=0)


class LocalSearchConfig(_Model):
    """Seeded boundary refinement after the bisection (moves between adjacent districts)."""

    enabled: bool = True
    #: Maximum number of passes over all candidate moves (a pass without improvement stops early).
    max_passes: int = Field(40, ge=0)
    #: Move whole municipality fragments / whole municipalities between districts.
    fragment_moves: bool = True
    #: Move CBS wijk fragments (groups of buurten) between districts.
    wijk_moves: bool = True
    #: Move individual boundary units (buurten).
    unit_moves: bool = True
    #: Largest group (in units) moved in one fragment move.
    max_fragment_units: int = Field(400, ge=1)
    #: Ejection chains: move a municipal fragment into another district holding the rest of the
    #: municipality (un-splitting it) followed by up to ``chain_length`` greedy rebalancing moves;
    #: the chain is kept only if the objective improves overall.
    chain_moves: bool = True
    chain_length: int = Field(8, ge=1, le=64)
    #: Only fragments with at most this share of the provincial target population start a chain.
    chain_max_share: float = Field(0.25, gt=0, le=1)
    # Objective weights (see docs/DISTRICTING.md §Local search).
    weight_hard: float = Field(1000.0, ge=0, description="per %-point beyond the hard maximum")
    weight_target: float = Field(100.0, ge=0, description="per %-point beyond the target tolerance")
    weight_target_district: float = Field(50.0, ge=0, description="per district beyond the target tolerance")
    weight_split: float = Field(10.0, ge=0, description="per split municipality")
    weight_fragment: float = Field(2.0, ge=0, description="per extra municipal fragment")
    weight_deviation: float = Field(1.0, ge=0, description="per (deviation in %)²")
    weight_cut_km: float = Field(0.2, ge=0, description="per km of district boundary inside the province")


class MergeSplitConfig(_Model):
    """Recombination ("merge-split") after the local search of the winning restart.

    Every pair of adjacent districts of a province — and the districts sharing a municipality that
    is split into more parts than its population requires — is merged and re-drawn by the
    multi-resolution bisection (which prefers whole municipalities, runs the exact whole-municipality
    search and minimises the cut length), polished by the local search, and kept when the province
    objective (see :class:`LocalSearchConfig`) improves.  Passes repeat until one brings no
    improvement.  This removes municipal splits and C-shaped boundaries that the top-down
    bisection had to commit to early.
    """

    enabled: bool = True
    #: Maximum number of passes (each over all adjacent pairs and over-split municipalities).
    max_passes: int = Field(2, ge=0, le=50)
    #: Largest region (in districts) re-drawn around a municipality split into more districts than
    #: its population requires (the districts holding it, plus one neighbour when that fits);
    #: values below 2 disable this phase.
    max_region_districts: int = Field(4, ge=0, le=8)
    #: Number of best restarts (by objective) that are recombined; the best result wins.
    restarts: int = Field(3, ge=1, le=256)


class NamingConfig(_Model):
    """Descriptive district names."""

    #: Named regions (FICTIONAL naming aid over REAL municipality codes): region → municipality codes.
    regions: dict[str, list[str]] = Field(default_factory=dict)
    #: A multi-municipality district is named after a region holding at least this population share
    #: (the most specific such region wins) …
    region_min_share: float = Field(0.6, gt=0, le=1)
    #: … unless its largest municipality alone holds at least this share.
    region_max_main_share: float = Field(0.5, gt=0, le=1)
    #: A second municipality is added to the name when it holds at least this population share.
    second_min_share: float = Field(0.2, gt=0, le=1)
    #: Share of a district's population in one municipality to count as a single-municipality district.
    single_min_share: float = Field(0.95, gt=0, le=1)
    #: Parts of a split municipality below this population share get no reserved compass label.
    minor_part_share: float = Field(0.05, ge=0, lt=1)
    #: A split municipality with at least this many (significant) parts may get a "-Centrum" district.
    centre_min_parts: int = Field(4, ge=2)
    #: "-Centrum" when the part's centroid lies within this fraction of the municipality's radius.
    centre_radius_fraction: float = Field(0.4, gt=0, le=1)
    #: Suffix for a district dominated by one municipality plus small neighbours.
    surroundings_suffix: str = " e.o."


class DistrictConfig(_Model):
    """Generation parameters of a House district plan."""

    #: Algorithm identifier stored with the plan.
    method: Literal["multires_bisection"] = "multires_bisection"
    #: Target tolerance: every district should be within ± this % of its provincial target.
    target_deviation_pct: float = Field(2.0, gt=0, le=50)
    #: Hard maximum: a plan with a district beyond ± this % fails validation.
    max_deviation_pct: float = Field(5.0, gt=0, le=100)
    #: Tolerance funnel of the bisection: a side that will still be split into ``k`` districts must
    #: have an average deviation within ``target / k**tolerance_exponent`` (0 = loose, 1 = absolute).
    tolerance_exponent: float = Field(0.5, ge=0, le=1)
    #: Number of sweep-line directions per cut (over [0°, 180°), seeded rotation).
    sweep_angles: int = Field(18, ge=1, le=180)
    #: Number of geodesic (graph-distance) orderings grown from extreme atoms.
    geodesic_orderings: int = Field(8, ge=0, le=64)
    #: Geodesic orderings grown from the most populous atoms (carve out cities and their suburbs).
    centre_orderings: int = Field(3, ge=0, le=32)
    #: Also use the spectral (Fiedler vector) ordering of the atom graph.
    spectral_ordering: bool = True
    #: Exhaustive search over connected whole-atom subsets when the orderings find no balanced cut
    #: through whole municipalities, for regions of at most this many districts …
    exact_search_max_k: int = Field(4, ge=0, le=16)
    #: … and at most this many atoms (the search is abandoned after ``exact_search_max_subsets``).
    exact_search_max_atoms: int = Field(48, ge=2, le=60)
    exact_search_max_subsets: int = Field(6000, ge=1)
    #: Independent restarts per province (different seeds and jittered weights); the plan with
    #: the best local-search objective is kept.
    restarts: int = Field(12, ge=1, le=256)
    #: Relative jitter of the cut weights / tolerance exponent in restarts after the first.
    restart_jitter: float = Field(0.25, ge=0, le=1)
    #: Candidate cut positions per ordering and orientation that are checked exactly.
    candidates_per_ordering: int = Field(3, ge=1, le=50)
    #: Maximum refinement rounds (municipality → wijk → buurt) per cut.
    max_refine_rounds: int = Field(6, ge=0, le=20)
    #: Use CBS wijken as an intermediate resolution between municipality and buurt.
    use_wijk_level: bool = True
    #: Extra district-count splits tried around k/2 (0 = halves only).
    split_slack: int = Field(0, ge=0, le=8)
    #: Nominal shared-border length (m) of a water link (ferry / bridge / island connection).
    water_link_border_m: float = Field(250.0, ge=0)
    cut: CutWeights = Field(default_factory=CutWeights)
    local_search: LocalSearchConfig = Field(default_factory=LocalSearchConfig)
    merge_split: MergeSplitConfig = Field(default_factory=MergeSplitConfig)
    naming: NamingConfig = Field(default_factory=NamingConfig)
    #: Worker processes (0 = auto, 1 = serial).  Results are identical for every value.  Worker
    #: processes are started with ``spawn``: scripts must guard their entry point with
    #: ``if __name__ == "__main__":``.
    workers: int = Field(1, ge=0, le=64)
    #: Overrides document (relative to ``config/``); ``null`` disables file-based overrides.
    overrides_file: str | None = "districts/overrides.yaml"

    @model_validator(mode="after")
    def _check(self) -> DistrictConfig:
        if self.max_deviation_pct < self.target_deviation_pct:
            raise ValueError("max_deviation_pct must be >= target_deviation_pct")
        return self

    # ------------------------------------------------------------------ hashing
    def hash_payload(self) -> dict[str, Any]:
        """Configuration fields that influence the plan (``workers`` is excluded: results are
        identical for any worker count)."""
        return self.model_dump(mode="json", exclude={"workers"})

    def canonical_json(self, overrides: list[dict[str, Any]] | None = None) -> str:
        """Canonical (sorted keys, compact) JSON of the configuration plus manual overrides."""
        doc = {"config": self.hash_payload(), "overrides": overrides or []}
        return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def side_tolerance(self, k_side: int) -> float:
        """Allowed |average deviation| (fraction) of a bisection side holding ``k_side`` districts."""
        tol = self.target_deviation_pct / 100.0
        if k_side <= 1:
            return tol
        return tol / float(k_side) ** self.tolerance_exponent


def load_district_config(name: str = "districts.yaml") -> DistrictConfig:
    """Load ``config/districts.yaml`` (defaults when the file is absent)."""
    return load_config(name, DistrictConfig, optional=True)
