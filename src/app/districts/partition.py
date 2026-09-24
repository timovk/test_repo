"""Per-province districting engine: multi-resolution recursive bisection + seeded local search.

This module is pure NumPy/SciPy (no geopandas, no SQLAlchemy) so provinces can be processed in
worker processes.  :func:`partition_province` turns a :class:`ProvinceProblem` (unit arrays of one
province plus its unit adjacency graph) into an assignment of every unit to one of ``seats``
districts.  The algorithm is documented in ``docs/DISTRICTING.md``; in short:

1. **Recursive bisection.**  A region that must hold ``k`` districts is cut into two connected
   sides holding ``⌊k/2⌋`` and ``⌈k/2⌉`` districts.  The cut is searched on a *multi-resolution*
   atom graph: atoms start as whole municipalities (connected pieces of a municipality inside the
   region) and are refined on demand — municipality → CBS wijken → buurten — only when no
   population-balanced, contiguous cut through whole atoms exists.  Candidate cuts are prefixes of
   many atom orderings (sweep lines at seeded angles, geodesic distance from extreme atoms, the
   spectral Fiedler ordering), evaluated vectorised for population balance, cut length and
   municipal splits; the best few per ordering are checked exactly for contiguity (with repair).
2. **Local search.**  Seeded passes move municipality fragments, wijk fragments and single
   boundary buurten between adjacent districts when this lowers an objective that ranks, in
   order: hard-limit violations, target-tolerance violations, split municipalities, squared
   deviations and boundary length.  Moves never break contiguity and never create a new
   municipal split unless that improves feasibility.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra

from app.core.logging import get_logger
from app.core.rng import derive_seed, make_rng
from app.districts.config import DistrictConfig

log = get_logger(__name__)

_EPS = 1e-9


@dataclass
class ProvinceProblem:
    """Input of :func:`partition_province` (all arrays are local to the province)."""

    code: str
    seats: int
    pop: np.ndarray  #: (n,) int64 population per unit
    xy: np.ndarray  #: (n, 2) float — unit centroids, metres (EPSG:28992)
    area: np.ndarray  #: (n,) float — land area km²
    muni: np.ndarray  #: (n,) int64 — local municipality index
    wijk: np.ndarray  #: (n,) int64 — local wijk index (every wijk lies inside one municipality)
    edges: np.ndarray  #: (E, 2) int64 — unique undirected unit pairs (i < j)
    edge_km: np.ndarray  #: (E,) float — boundary weight of each edge in km (water links: nominal)
    seed: int
    config: DistrictConfig


@dataclass
class ProvinceResult:
    """Output of :func:`partition_province`."""

    code: str
    assignment: np.ndarray  #: (n,) int64 — local district index 0 … seats−1
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)


# =========================================================================== graph helpers
def components(n: int, a: np.ndarray, b: np.ndarray) -> tuple[int, np.ndarray]:
    """Connected components of the undirected graph with ``n`` nodes and edges ``(a, b)``."""
    if n == 0:
        return 0, np.zeros(0, dtype=np.int64)
    g = csr_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(n, n))
    nc, lab = connected_components(g, directed=False)
    return int(nc), lab.astype(np.int64)


def _fiedler(n: int, ea: np.ndarray, eb: np.ndarray, w: np.ndarray) -> np.ndarray | None:
    """Fiedler vector of the weighted graph Laplacian (``None`` if undefined or tiny).

    Uses sparse shift-invert Lanczos (ARPACK) with a fixed start vector — deterministic and free
    of threaded dense BLAS, which degrades badly on busy machines.
    """
    if n < 8 or len(ea) == 0:
        return None
    nc, _ = components(n, ea, eb)
    if nc != 1:
        return None
    from scipy.sparse import diags
    from scipy.sparse.linalg import eigsh

    ww = w / max(float(w.mean()), 1e-12) + 1e-3
    A = coo_matrix((np.r_[ww, ww], (np.r_[ea, eb], np.r_[eb, ea])), shape=(n, n)).tocsr()
    deg = np.asarray(A.sum(axis=1)).ravel()
    L = (diags(deg) - A).tocsc()
    try:
        vals, vecs = eigsh(L, k=2, sigma=-1e-2, which="LM", v0=np.linspace(1.0, 2.0, n), tol=1e-8)
    except Exception as exc:  # numerical failure: the ordering is optional
        log.debug("Fiedler vector failed (%s)", exc)
        return None
    vec = vecs[:, int(np.argsort(vals)[1])]
    i = int(np.argmax(np.abs(vec)))
    if vec[i] < 0:
        vec = -vec
    return vec / max(float(np.abs(vec).max()), 1e-300)


# =========================================================================== atoms
@dataclass
class _Atoms:
    n: int
    lab: np.ndarray  # (nR,) atom of each region unit
    pop: np.ndarray
    units: np.ndarray
    xy: np.ndarray
    muni: np.ndarray
    wijk: np.ndarray
    level: np.ndarray  # 0 municipality piece, 1 wijk piece, 2 unit
    ea: np.ndarray
    eb: np.ndarray
    ew: np.ndarray


@dataclass
class _Cand:
    order: int
    mask: np.ndarray  # atoms on side A
    k_a: int
    k_b: int
    nd: float  # normalised deviation (≤ 1 = within the funnel tolerance)
    connected: bool
    units_ok: bool
    score: float
    new_splits: int
    cut_km: float

    @property
    def feasible(self) -> bool:
        return self.connected and self.units_ok and self.nd <= 1.0 + 1e-12


class _Bisector:
    """Recursive population-balanced bisection of one province."""

    def __init__(
        self, prob: ProvinceProblem, cfg: DistrictConfig | None = None, seed: int | None = None
    ) -> None:
        self.prob = prob
        self.cfg = cfg if cfg is not None else prob.config
        self.code = prob.code
        self.seed = int(prob.seed if seed is None else seed)
        n = len(prob.pop)
        self.n = n
        pop = prob.pop.astype(float)
        self.balance_by_units = pop.sum() <= 0
        self.weights = np.ones(n) if self.balance_by_units else pop
        self.T = float(self.weights.sum()) / prob.seats
        self.M = int(prob.muni.max()) + 1 if n else 0
        self.W = int(prob.wijk.max()) + 1 if n else 0
        self.muni_units = np.bincount(prob.muni, minlength=self.M)
        self.muni_pop = np.bincount(prob.muni, weights=self.weights, minlength=self.M)
        self.ei = prob.edges[:, 0].astype(np.int64)
        self.ej = prob.edges[:, 1].astype(np.int64)
        self.ew = prob.edge_km.astype(float)
        self.warnings: list[str] = []
        self.stats = {
            "cuts": 0,
            "refine_rounds": 0,
            "fallback_cuts": 0,
            "balanced_cuts": 0,
            "split_cuts": 0,
            "exact_cuts": 0,
        }

    # ------------------------------------------------------------------ driver
    def run(self) -> np.ndarray:
        assignment = np.full(self.n, -1, dtype=np.int64)
        stack: list[tuple[np.ndarray, int, str]] = [(np.arange(self.n), self.prob.seats, "r")]
        next_id = 0
        while stack:
            region, k, path = stack.pop()
            if k == 1 or len(region) <= 1:
                if k > 1:
                    self.warnings.append(f"{self.code}: region {path} has a single unit for {k} districts")
                assignment[region] = next_id
                next_id += 1
                continue
            (side_a, k_a), (side_b, k_b) = self._bisect(region, k, path)
            stack.append((side_b, k_b, path + "1"))
            stack.append((side_a, k_a, path + "0"))
        if next_id != self.prob.seats:
            self.warnings.append(f"{self.code}: produced {next_id} districts instead of {self.prob.seats}")
        return assignment

    # ------------------------------------------------------------------ one cut
    def _bisect(
        self, region: np.ndarray, k: int, path: str
    ) -> tuple[tuple[np.ndarray, int], tuple[np.ndarray, int]]:
        cfg = self.cfg
        rng = make_rng(self.seed, "districts", "bisect", self.code, path)
        off_sweep = float(rng.uniform(0.0, math.pi / cfg.sweep_angles))
        off_geo = float(rng.uniform(0.0, 2.0 * math.pi / max(cfg.geodesic_orderings, 1)))
        n_r = len(region)
        in_r = np.zeros(self.n, dtype=bool)
        in_r[region] = True
        pos = np.full(self.n, -1, dtype=np.int64)
        pos[region] = np.arange(n_r)
        em = in_r[self.ei] & in_r[self.ej]
        reg = _Region(
            n=n_r,
            ea=pos[self.ei[em]],
            eb=pos[self.ej[em]],
            ew=self.ew[em],
            w=self.weights[region],
            xy=self.prob.xy[region],
            muni=self.prob.muni[region],
            wijk=self.prob.wijk[region],
        )
        total = float(reg.w.sum())
        area_norm = math.sqrt(max(float(self.prob.area[region].sum()), 1e-6))
        muni_in_r = np.bincount(reg.muni, minlength=self.M)
        whole = (muni_in_r == self.muni_units) & (muni_in_r > 0)
        split_w = np.where(
            whole,
            cfg.cut.new_split
            + cfg.cut.small_split * np.clip(1.0 - self.muni_pop / max(self.T, 1e-9), 0.0, 1.0),
            cfg.cut.existing_split,
        )
        opts = self._side_options(k)
        muni_level = np.zeros(self.M, dtype=np.int8)
        wijk_ref = np.zeros(self.W, dtype=bool)
        best: _Cand | None = None
        last: list[_Cand] = []
        atoms: _Atoms | None = None
        max_rounds = cfg.max_refine_rounds + 2  # two extra rounds reserved for unit-count feasibility
        for rnd in range(max_rounds + 1):
            atoms = self._atoms(reg, muni_level, wijk_ref)
            cands, crossing = self._candidates(
                atoms, reg, k, opts, total, area_norm, whole, split_w, off_sweep, off_geo
            )
            feasible = [c for c in cands if c.feasible]
            if (
                not feasible
                and rnd == 0
                and k <= cfg.exact_search_max_k
                and 2 <= atoms.n <= cfg.exact_search_max_atoms
            ):
                exact = self._exact_candidates(atoms, k, opts, total, area_norm, whole, split_w, len(cands))
                feasible = [c for c in exact if c.feasible]
                if feasible:
                    self.stats["exact_cuts"] += 1
                cands = cands + exact
            if feasible:
                best = min(feasible, key=lambda c: (c.score, c.order))
                break
            last = cands
            any_units_ok = any(c.units_ok for c in cands)
            if rnd >= cfg.max_refine_rounds and any_units_ok:
                break
            if not any_units_ok or atoms.n < 2:
                refine = np.flatnonzero(atoms.level < 2)
            else:
                cap = max(opts) * self.T * (1.0 + cfg.side_tolerance(max(opts)))
                big = np.flatnonzero((atoms.pop > cap) & (atoms.level < 2))
                refine = np.union1d(np.asarray(sorted(crossing), dtype=np.int64), big)
                refine = refine[atoms.level[refine] < 2]
            if len(refine) == 0:
                break
            self.stats["refine_rounds"] += 1
            for a in refine.tolist():
                if atoms.level[a] == 0:
                    m = int(atoms.muni[a])
                    n_wijk = len(np.unique(reg.wijk[reg.muni == m]))
                    muni_level[m] = 1 if (cfg.use_wijk_level and n_wijk > 1) else 2
                else:
                    wijk_ref[int(atoms.wijk[a])] = True
        assert atoms is not None
        self.stats["cuts"] += 1
        if best is None:
            self.stats["fallback_cuts"] += 1
            pool = [c for c in last if c.units_ok]
            if not pool:
                # Degenerate region: take a geodesic prefix that leaves every side enough units.
                mask_u, k_a = self._count_safe_cut(reg, k, opts)
                self.warnings.append(
                    f"{self.code}: degenerate region at {path} ({n_r} units for {k} districts)"
                )
                return (region[mask_u], k_a), (region[~mask_u], k - k_a)
            best = min(pool, key=lambda c: (not c.connected, round(c.nd, 6), c.score, c.order))
            mask_u = best.mask[atoms.lab]
            balanced, nd = self._balance_cut(reg, mask_u, best.k_a, best.k_b, path)
            if balanced is not None:
                self.stats["balanced_cuts"] += 1
                return (region[balanced], best.k_a), (region[~balanced], best.k_b)
            self.warnings.append(
                f"{self.code}: no cut within tolerance at node {path} (k={k}); best effort "
                f"deviation factor {min(nd, best.nd):.2f}{'' if best.connected else ', non-contiguous'}"
            )
        if best.new_splits:
            self.stats["split_cuts"] += 1
        mask_u = best.mask[atoms.lab]
        return (region[mask_u], best.k_a), (region[~mask_u], best.k_b)

    def _balance_cut(
        self, reg: _Region, mask_u: np.ndarray, k_a: int, k_b: int, path: str
    ) -> tuple[np.ndarray | None, float]:
        """Rebalance an out-of-tolerance cut with a two-part local search at unit level.

        Returns ``(side-A mask, normalised deviation)``; the mask is ``None`` unless both sides
        end up connected, within their funnel tolerance and with enough units.
        """
        cfg = self.cfg
        tol_a, tol_b = cfg.side_tolerance(k_a), cfg.side_tolerance(k_b)
        sub = ProvinceProblem(
            code=self.code,
            seats=2,
            pop=reg.w,
            xy=reg.xy,
            area=np.zeros(reg.n),
            muni=reg.muni,
            wijk=reg.wijk,
            edges=np.column_stack([reg.ea, reg.eb]),
            edge_km=reg.ew,
            seed=self.seed,
            config=cfg,
        )
        start = np.where(mask_u, 0, 1).astype(np.int64)
        tols = np.array([tol_a, tol_b]) * 100.0
        search = _LocalSearch(sub, cfg, start, reg.w, np.array([k_a * self.T, k_b * self.T]), tols, tols)
        out = search.run(make_rng(self.seed, "districts", "balance", self.code, path))
        side_a = out == 0
        pop_a = float(reg.w[side_a].sum())
        total = float(reg.w.sum())
        nd = max(
            abs(pop_a / (k_a * self.T) - 1.0) / tol_a, abs((total - pop_a) / (k_b * self.T) - 1.0) / tol_b
        )
        if nd > 1.0 + 1e-12 or int(side_a.sum()) < k_a or int((~side_a).sum()) < k_b:
            return None, nd
        same = side_a[reg.ea] == side_a[reg.eb]
        nc, _ = components(reg.n, reg.ea[same], reg.eb[same])
        return (side_a if nc == 2 else None), nd

    def _count_safe_cut(self, reg: _Region, k: int, opts: list[int]) -> tuple[np.ndarray, int]:
        """Prefix of a geodesic unit ordering with ≥ k_side units per side (best balance)."""
        n_r = reg.n
        start = int(np.lexsort((np.arange(n_r), reg.xy[:, 0] + reg.xy[:, 1]))[0])
        if len(reg.ea):
            length = np.hypot(*(reg.xy[reg.ea] - reg.xy[reg.eb]).T) + 1.0
            dist = dijkstra(
                csr_matrix((length, (reg.ea, reg.eb)), shape=(n_r, n_r)), directed=False, indices=start
            )
            dist = np.where(np.isfinite(dist), dist, 1e18)
        else:
            dist = np.arange(n_r, dtype=float)
        order = np.lexsort((np.arange(n_r), dist))
        cp = np.cumsum(reg.w[order])
        total = float(cp[-1])
        best: tuple[float, int, int] = (np.inf, 1, opts[0])
        for kp in opts:
            ks = k - kp
            lo, hi = kp, n_r - ks  # prefix sizes leaving enough units on both sides
            if lo > hi:
                continue
            p = np.arange(lo, hi + 1)
            nd = np.maximum(
                np.abs(cp[p - 1] / (kp * self.T) - 1.0), np.abs((total - cp[p - 1]) / (ks * self.T) - 1.0)
            )
            i = int(np.argmin(nd))
            if nd[i] < best[0]:
                best = (float(nd[i]), int(p[i]), kp)
        mask = np.zeros(n_r, dtype=bool)
        mask[order[: best[1]]] = True
        return mask, best[2]

    def _side_options(self, k: int) -> list[int]:
        lo, hi = k // 2, k - k // 2
        s = self.cfg.split_slack
        return sorted({x for x in range(lo - s, hi + s + 1) if 1 <= x <= k - 1})

    # ------------------------------------------------------------------ atoms
    def _atoms(self, reg: _Region, muni_level: np.ndarray, wijk_ref: np.ndarray) -> _Atoms:
        M, W, n_r = self.M, self.W, reg.n
        lvl = muni_level[reg.muni]
        unit_level = (lvl == 2) | ((lvl == 1) & wijk_ref[reg.wijk])
        key = np.where(lvl == 0, reg.muni, np.where(unit_level, M + W + np.arange(n_r), M + reg.wijk))
        same = key[reg.ea] == key[reg.eb]
        n_a, lab = components(n_r, reg.ea[same], reg.eb[same])
        pop = np.bincount(lab, weights=reg.w, minlength=n_a)
        units = np.bincount(lab, minlength=n_a)
        cw = reg.w + 1.0
        sw = np.bincount(lab, weights=cw, minlength=n_a)
        xy = np.column_stack(
            [
                np.bincount(lab, weights=cw * reg.xy[:, 0], minlength=n_a) / sw,
                np.bincount(lab, weights=cw * reg.xy[:, 1], minlength=n_a) / sw,
            ]
        )
        amuni = np.empty(n_a, dtype=np.int64)
        amuni[lab] = reg.muni
        awijk = np.empty(n_a, dtype=np.int64)
        awijk[lab] = reg.wijk
        akey = np.empty(n_a, dtype=np.int64)
        akey[lab] = key
        level = np.where(akey < M, 0, np.where(akey < M + W, 1, 2)).astype(np.int8)
        cross = ~same
        a, b = lab[reg.ea[cross]], lab[reg.eb[cross]]
        lo, hi = np.minimum(a, b), np.maximum(a, b)
        uniq, inv = np.unique(lo * n_a + hi, return_inverse=True)
        ew = np.bincount(inv, weights=reg.ew[cross], minlength=len(uniq))
        return _Atoms(
            n=n_a,
            lab=lab,
            pop=pop,
            units=units,
            xy=xy,
            muni=amuni,
            wijk=awijk,
            level=level,
            ea=uniq // n_a,
            eb=uniq % n_a,
            ew=ew,
        )

    # ------------------------------------------------------------------ orderings
    def _orderings(self, atoms: _Atoms, off_sweep: float, off_geo: float) -> np.ndarray:
        cfg = self.cfg
        n_a = atoms.n
        xy = atoms.xy - atoms.xy.mean(axis=0)
        perms: list[np.ndarray] = []
        ang = off_sweep + np.arange(cfg.sweep_angles) * math.pi / cfg.sweep_angles
        proj = xy[:, :1] * np.cos(ang)[None, :] + xy[:, 1:] * np.sin(ang)[None, :]  # (n_a, S), no BLAS
        perms.extend(np.argsort(np.round(proj, 1), axis=0, kind="stable").T)
        if cfg.geodesic_orderings > 0 and n_a > 2 and len(atoms.ea):
            dirs = off_geo + np.arange(cfg.geodesic_orderings) * 2.0 * math.pi / cfg.geodesic_orderings
            pr = xy[:, :1] * np.cos(dirs)[None, :] + xy[:, 1:] * np.sin(dirs)[None, :]
            seeds = list(dict.fromkeys(np.argmin(pr, axis=0).tolist()))
            length = np.hypot(*(atoms.xy[atoms.ea] - atoms.xy[atoms.eb]).T) / 1000.0 + 1e-3
            g = csr_matrix((length, (atoms.ea, atoms.eb)), shape=(n_a, n_a))
            dist = dijkstra(g, directed=False, indices=seeds)
            dist = np.where(np.isfinite(dist), dist, 1e12)
            perms.extend(np.argsort(np.round(dist, 6), axis=1, kind="stable"))
        if cfg.centre_orderings > 0 and n_a > 2 and len(atoms.ea):
            seeds = np.lexsort((np.arange(n_a), -atoms.pop))[: cfg.centre_orderings].tolist()
            length = np.hypot(*(atoms.xy[atoms.ea] - atoms.xy[atoms.eb]).T) / 1000.0 + 1e-3
            g = csr_matrix((length, (atoms.ea, atoms.eb)), shape=(n_a, n_a))
            dist = dijkstra(g, directed=False, indices=seeds)
            dist = np.where(np.isfinite(dist), dist, 1e12)
            perms.extend(np.argsort(np.round(dist, 6), axis=1, kind="stable"))
        if cfg.spectral_ordering:
            f = _fiedler(n_a, atoms.ea, atoms.eb, atoms.ew)
            if f is not None:
                perms.append(np.argsort(np.round(f, 9), kind="stable"))
        return np.asarray(perms, dtype=np.int64)

    # ------------------------------------------------------------------ candidates
    def _candidates(
        self,
        atoms: _Atoms,
        reg: _Region,
        k: int,
        opts: list[int],
        total: float,
        area_norm: float,
        whole: np.ndarray,
        split_w: np.ndarray,
        off_sweep: float,
        off_geo: float,
    ) -> tuple[list[_Cand], set[int]]:
        cfg = self.cfg
        n_a = atoms.n
        if n_a < 2:
            return [], set()
        perms = self._orderings(atoms, off_sweep, off_geo)
        n_o = len(perms)
        ranks = np.empty_like(perms)
        ranks[np.arange(n_o)[:, None], perms] = np.arange(n_a)[None, :]
        cp = np.cumsum(atoms.pop[perms], axis=1)[:, :-1]  # prefix population at positions 1..n_a-1
        cu = np.cumsum(atoms.units[perms], axis=1)[:, :-1]
        width = n_a + 1
        base_idx = (np.arange(n_o) * width)[:, None]
        # cut length at every prefix position (difference arrays)
        if len(atoms.ea):
            r1 = np.minimum(ranks[:, atoms.ea], ranks[:, atoms.eb])
            r2 = np.maximum(ranks[:, atoms.ea], ranks[:, atoms.eb])
            wt = np.broadcast_to(atoms.ew, r1.shape).ravel()
            d = np.bincount((base_idx + r1 + 1).ravel(), weights=wt, minlength=n_o * width)
            d -= np.bincount((base_idx + r2 + 1).ravel(), weights=wt, minlength=n_o * width)
            cut = np.cumsum(d.reshape(n_o, width), axis=1)[:, 1:n_a]
        else:
            cut = np.zeros((n_o, n_a - 1))
        # municipal split penalty at every prefix position
        cnt = np.bincount(atoms.muni, minlength=self.M)
        multi = np.flatnonzero(cnt[atoms.muni] >= 2)
        pen = np.zeros((n_o, n_a - 1))
        if len(multi):
            order = multi[np.argsort(atoms.muni[multi], kind="stable")]
            g = atoms.muni[order]
            starts = np.flatnonzero(np.r_[True, g[1:] != g[:-1]])
            rr = ranks[:, order]
            rmin = np.minimum.reduceat(rr, starts, axis=1)
            rmax = np.maximum.reduceat(rr, starts, axis=1)
            gw = np.broadcast_to(split_w[g[starts]], rmin.shape).ravel()
            d2 = np.bincount((base_idx + rmin + 1).ravel(), weights=gw, minlength=n_o * width)
            d2 -= np.bincount((base_idx + rmax + 1).ravel(), weights=gw, minlength=n_o * width)
            pen = np.cumsum(d2.reshape(n_o, width), axis=1)[:, 1:n_a]
        base = cfg.cut.cut_length * cut / area_norm + pen
        n_units = int(atoms.units.sum())
        picks: list[tuple[int, int, int, int]] = []  # (ordering, prefix size, k_prefix, k_suffix)
        fallback_picks: list[tuple[int, int, int, int]] = []
        crossing: set[int] = set()
        m = cfg.candidates_per_ordering
        rows = np.arange(n_o)[:, None]
        for kp in opts:
            ks = k - kp
            tol_a, tol_b = cfg.side_tolerance(kp), cfg.side_tolerance(ks)
            dev_a = cp / (kp * self.T) - 1.0
            dev_b = (total - cp) / (ks * self.T) - 1.0
            nd = np.maximum(np.abs(dev_a) / tol_a, np.abs(dev_b) / tol_b)
            ok = (cu >= kp) & (n_units - cu >= ks)
            feas = ok & (nd <= 1.0)
            score = cfg.cut.deviation * nd + base + cfg.cut.unbalanced * abs(kp - ks)
            ideal = total * kp / k
            cross_pos = np.minimum((cp < ideal).sum(axis=1), n_a - 1)
            crossing.update(perms[np.arange(n_o), cross_pos].tolist())
            s_f = np.where(feas, score, np.inf)
            top = np.argsort(s_f, axis=1, kind="stable")[:, :m]
            valid = np.isfinite(s_f[rows, top])
            for o, j in zip(*np.nonzero(valid), strict=True):
                picks.append((int(o), int(top[o, j]) + 1, kp, ks))
            s_b = np.where(ok, nd, np.inf)
            topb = np.argsort(s_b, axis=1, kind="stable")[:, :1]
            validb = np.isfinite(s_b[rows, topb])
            for o, j in zip(*np.nonzero(validb), strict=True):
                fallback_picks.append((int(o), int(topb[o, j]) + 1, kp, ks))
        if not picks:
            picks = fallback_picks
        cands: list[_Cand] = []
        seen: set[tuple[bytes, int]] = set()
        for o, p, kp, ks in picks:
            mask = ranks[o] < p
            key = (np.packbits(mask).tobytes(), kp)
            if key in seen:
                continue
            seen.add(key)
            cands.append(self._evaluate(atoms, mask, kp, ks, total, area_norm, whole, split_w, len(cands)))
        crossing = {a for a in crossing if atoms.level[a] < 2}
        return cands, crossing

    def _exact_candidates(
        self,
        atoms: _Atoms,
        k: int,
        opts: list[int],
        total: float,
        area_norm: float,
        whole: np.ndarray,
        split_w: np.ndarray,
        start_order: int,
    ) -> list[_Cand]:
        """Balanced bipartitions through whole atoms found by enumerating connected subsets.

        Enumerates connected atom subsets (ESU algorithm, each subset once, rooted at its lowest
        atom) whose population fits the side holding ``min(opts)`` districts, keeps those whose
        both sides are within the funnel tolerance, and returns the best few (checked exactly).
        """
        cfg = self.cfg
        n_a = atoms.n
        k_s = min(opts)
        options = [(k_s, k - k_s)] + ([(k - k_s, k_s)] if k - k_s != k_s and (k - k_s) in opts else [])
        lo = min(kp * self.T * (1.0 - cfg.side_tolerance(kp)) for kp, _ in options)
        hi = max(kp * self.T * (1.0 + cfg.side_tolerance(kp)) for kp, _ in options)
        nbr = [0] * n_a
        for a, b in zip(atoms.ea.tolist(), atoms.eb.tolist(), strict=True):
            nbr[a] |= 1 << b
            nbr[b] |= 1 << a
        pops = atoms.pop.tolist()
        found: list[int] = []
        budget = [cfg.exact_search_max_subsets]

        def extend(sub: int, pop: float, ext: int, closed: int, higher: int) -> None:
            if budget[0] <= 0:
                return
            budget[0] -= 1
            if pop >= lo:
                found.append(sub)
            while ext and budget[0] > 0:
                w = (ext & -ext).bit_length() - 1
                ext &= ext - 1
                pw = pop + pops[w]
                if pw > hi:
                    continue
                extend(
                    sub | (1 << w), pw, ext | (nbr[w] & ~closed & higher), closed | nbr[w] | (1 << w), higher
                )

        full = (1 << n_a) - 1
        for root in range(n_a):
            if budget[0] <= 0:
                break
            if pops[root] > hi:
                continue
            higher = full & ~((1 << (root + 1)) - 1)
            extend(1 << root, pops[root], nbr[root] & higher, nbr[root] | (1 << root), higher)
        if not found:
            return []
        arr = np.asarray(found, dtype=np.uint64)
        bits = ((arr[:, None] >> np.arange(n_a, dtype=np.uint64)[None, :]) & np.uint64(1)).astype(bool)
        pop_s = np.where(bits, atoms.pop[None, :], 0.0).sum(axis=1)
        units_s = np.where(bits, atoms.units[None, :], 0).sum(axis=1)
        cut = (
            np.where(bits[:, atoms.ea] != bits[:, atoms.eb], atoms.ew[None, :], 0.0).sum(axis=1)
            if len(atoms.ea)
            else np.zeros(len(found))
        )
        picks: list[tuple[float, int, int, int]] = []
        for kp, ks in options:
            nd = np.maximum(
                np.abs(pop_s / (kp * self.T) - 1.0) / cfg.side_tolerance(kp),
                np.abs((total - pop_s) / (ks * self.T) - 1.0) / cfg.side_tolerance(ks),
            )
            ok = (nd <= 1.0) & (units_s >= kp) & (atoms.units.sum() - units_s >= ks)
            score = cfg.cut.deviation * nd + cfg.cut.cut_length * cut / area_norm
            for i in np.flatnonzero(ok):
                picks.append((float(score[i]), int(i), kp, ks))
        picks.sort()
        out: list[_Cand] = []
        for _, i, kp, ks in picks[: 4 * cfg.candidates_per_ordering]:
            c = self._evaluate(
                atoms, bits[i], kp, ks, total, area_norm, whole, split_w, start_order + len(out)
            )
            if c.connected and np.array_equal(c.mask, bits[i]):
                out.append(c)
        return out

    def _evaluate(
        self,
        atoms: _Atoms,
        mask: np.ndarray,
        k_a: int,
        k_b: int,
        total: float,
        area_norm: float,
        whole: np.ndarray,
        split_w: np.ndarray,
        order: int,
    ) -> _Cand:
        cfg = self.cfg
        original = mask
        mask = mask.copy()
        ea, eb = atoms.ea, atoms.eb
        same = mask[ea] == mask[eb]
        nc, lab = components(atoms.n, ea[same], eb[same])
        if nc > 2:  # contiguity repair: keep each side's main component, flip the rest
            for side in (True, False):
                comps = np.unique(lab[mask == side])
                if len(comps) <= 1:
                    continue
                cpop = np.bincount(lab, weights=atoms.pop, minlength=nc)
                csz = np.bincount(lab, weights=atoms.units, minlength=nc)
                main = comps[np.lexsort((comps, -csz[comps], -cpop[comps]))[0]]
                flip = (mask == side) & (lab != main)
                mask[flip] = not side
                same = mask[ea] == mask[eb]
                nc, lab = components(atoms.n, ea[same], eb[same])
            units_a = int(atoms.units[mask].sum())
            if units_a < k_a or int(atoms.units.sum()) - units_a < k_b:
                # the repair would leave a side with fewer units than districts: keep the raw cut
                mask = original.copy()
                same = mask[ea] == mask[eb]
                nc, lab = components(atoms.n, ea[same], eb[same])
        pop_a = float(atoms.pop[mask].sum())
        units_a = int(atoms.units[mask].sum())
        units_b = int(atoms.units.sum()) - units_a
        dev_a = pop_a / (k_a * self.T) - 1.0
        dev_b = (total - pop_a) / (k_b * self.T) - 1.0
        nd = max(abs(dev_a) / cfg.side_tolerance(k_a), abs(dev_b) / cfg.side_tolerance(k_b))
        cnt = np.bincount(atoms.muni, minlength=self.M)
        cnt_a = np.bincount(atoms.muni[mask], minlength=self.M)
        split = (cnt_a > 0) & (cnt_a < cnt)
        cut_km = float(atoms.ew[mask[ea] != mask[eb]].sum())
        score = (
            cfg.cut.deviation * nd
            + float(split_w[split].sum())
            + cfg.cut.cut_length * cut_km / area_norm
            + cfg.cut.unbalanced * abs(k_a - k_b)
        )
        return _Cand(
            order=order,
            mask=mask,
            k_a=k_a,
            k_b=k_b,
            nd=float(nd),
            connected=(nc == 2 and units_a > 0 and units_b > 0),
            units_ok=(units_a >= k_a and units_b >= k_b),
            score=float(score),
            new_splits=int((split & whole).sum()),
            cut_km=cut_km,
        )


@dataclass
class _Region:
    n: int
    ea: np.ndarray
    eb: np.ndarray
    ew: np.ndarray
    w: np.ndarray
    xy: np.ndarray
    muni: np.ndarray
    wijk: np.ndarray


# =========================================================================== contiguity repair
def repair_contiguity(
    assignment: np.ndarray,
    edges: np.ndarray,
    edge_km: np.ndarray,
    weights: np.ndarray,
    k: int,
    max_iter: int = 10,
) -> tuple[np.ndarray, int]:
    """Move stray pieces of non-contiguous districts to their best-connected neighbour district.

    Returns the repaired assignment and the number of pieces moved.  A district's main piece (by
    population, then size) always stays, so no district becomes empty.
    """
    assignment = assignment.copy()
    n = len(assignment)
    a, b = edges[:, 0], edges[:, 1]
    moved = 0
    for _ in range(max_iter):
        same = assignment[a] == assignment[b]
        nc, lab = components(n, a[same], b[same])
        comp_d = np.empty(nc, dtype=np.int64)
        comp_d[lab] = assignment
        per_d = np.bincount(comp_d, minlength=k)
        bad = np.flatnonzero(per_d > 1)
        if len(bad) == 0:
            break
        cpop = np.bincount(lab, weights=weights, minlength=nc)
        csz = np.bincount(lab, minlength=nc)
        changed = False
        for d in bad.tolist():
            comps = np.flatnonzero(comp_d == d)
            main = comps[np.lexsort((comps, -csz[comps], -cpop[comps]))[0]]
            for c in comps.tolist():
                if c == main:
                    continue
                members = lab == c
                touch = (members[a] & ~members[b]) | (members[b] & ~members[a])
                other = np.where(members[a[touch]], assignment[b[touch]], assignment[a[touch]])
                w = edge_km[touch]
                keep = other != d
                if not keep.any():
                    continue
                score = np.bincount(other[keep], weights=w[keep] + 1e-9, minlength=k)
                target = int(np.argmax(score))
                assignment[members] = target
                moved += 1
                changed = True
        if not changed:
            break
    return assignment, moved


# =========================================================================== local search
class _LocalSearch:
    """Greedy seeded boundary refinement (see module docstring)."""

    def __init__(
        self,
        prob: ProvinceProblem,
        cfg: DistrictConfig,
        assignment: np.ndarray,
        weights: np.ndarray,
        target: float | np.ndarray,
        tol_pct: np.ndarray | None = None,
        hard_pct: np.ndarray | None = None,
    ) -> None:
        ls = cfg.local_search
        self.cfg = cfg
        self.k = prob.seats
        n = len(assignment)
        self.n = n
        tgt = np.broadcast_to(np.asarray(target, dtype=float), (self.k,))
        self.targets: list[float] = np.maximum(tgt, 1e-9).tolist()
        self.T = float(np.mean(self.targets))
        self.tols: list[float] = (
            np.full(self.k, cfg.target_deviation_pct) if tol_pct is None else np.asarray(tol_pct, dtype=float)
        ).tolist()
        self.hards: list[float] = (
            np.full(self.k, cfg.max_deviation_pct) if hard_pct is None else np.asarray(hard_pct, dtype=float)
        ).tolist()
        self.w_hard, self.w_tgt, self.w_tgt_d = ls.weight_hard, ls.weight_target, ls.weight_target_district
        self.w_split, self.w_frag = ls.weight_split, ls.weight_fragment
        self.w_dev, self.w_cut = ls.weight_deviation, ls.weight_cut_km
        a, b = prob.edges[:, 0], prob.edges[:, 1]
        adj = coo_matrix(
            (np.r_[prob.edge_km, prob.edge_km], (np.r_[a, b], np.r_[b, a])), shape=(n, n)
        ).tocsr()
        adj.sum_duplicates()
        indptr, indices, data = adj.indptr, adj.indices, adj.data
        self.nbr: list[list[int]] = [indices[indptr[i] : indptr[i + 1]].tolist() for i in range(n)]
        self.nbw: list[list[float]] = [data[indptr[i] : indptr[i + 1]].tolist() for i in range(n)]
        self.edges = prob.edges
        self.pop: list[float] = weights.astype(float).tolist()
        self.muni: list[int] = prob.muni.tolist()
        self.wijk: list[int] = prob.wijk.tolist()
        self.dist: list[int] = assignment.astype(np.int64).tolist()
        self.dpop: list[float] = np.bincount(assignment, weights=weights, minlength=self.k).tolist()
        self.members: list[set[int]] = [set() for _ in range(self.k)]
        for u, d in enumerate(self.dist):
            self.members[d].add(u)
        M = int(prob.muni.max()) + 1 if n else 0
        W = int(prob.wijk.max()) + 1 if n else 0
        mc = np.zeros((M, self.k), dtype=np.int64)
        np.add.at(mc, (prob.muni, assignment), 1)
        self.mcount: list[list[int]] = mc.tolist()
        self.mnd: list[int] = (mc > 0).sum(axis=1).tolist()
        self.muni_units: list[list[int]] = [[] for _ in range(M)]
        for u, m in enumerate(self.muni):
            self.muni_units[m].append(u)
        self.wijk_units: list[list[int]] = [[] for _ in range(W)]
        for u, w in enumerate(self.wijk):
            self.wijk_units[w].append(u)
        self.moves = {"unit": 0, "wijk": 0, "municipality": 0}
        self.passes = 0

    # ------------------------------------------------------------------ objective pieces
    def _dcost(self, p: float, d: int) -> float:
        a = abs(p / self.targets[d] - 1.0) * 100.0
        c = self.w_dev * a * a
        if a > self.tols[d]:
            c += self.w_tgt * (a - self.tols[d]) + self.w_tgt_d
        if a > self.hards[d]:
            c += self.w_hard * (a - self.hards[d])
        return c

    def _excess(self, p: float, d: int) -> float:
        a = abs(p / self.targets[d] - 1.0) * 100.0
        return max(0.0, a - self.tols[d]) + max(0.0, a - self.hards[d])

    # ------------------------------------------------------------------ move evaluation
    def _delta(self, group: list[int], gset: set[int], p: float, d: int, e: int) -> float | None:
        dpop = self.dpop
        dc = (
            self._dcost(dpop[d] - p, d)
            + self._dcost(dpop[e] + p, e)
            - self._dcost(dpop[d], d)
            - self._dcost(dpop[e], e)
        )
        m = self.muni[group[0]]
        cd, ce, nd0 = self.mcount[m][d], self.mcount[m][e], self.mnd[m]
        nd1 = nd0 - (1 if cd == len(group) else 0) + (1 if ce == 0 else 0)
        if nd1 > nd0:
            fe = (
                self._excess(dpop[d] - p, d)
                + self._excess(dpop[e] + p, e)
                - self._excess(dpop[d], d)
                - self._excess(dpop[e], e)
            )
            if fe > -1e-12:
                return None  # never create a new municipal fragment unless it improves feasibility
        ds = self.w_split * (int(nd1 > 1) - int(nd0 > 1)) + self.w_frag * (nd1 - nd0)
        dist = self.dist
        dcut = 0.0
        for u in group:
            for v, w in zip(self.nbr[u], self.nbw[u], strict=True):
                if v in gset:
                    continue
                dv = dist[v]
                if dv == d:
                    dcut += w
                elif dv == e:
                    dcut -= w
        return dc + ds + self.w_cut * dcut

    def _can_remove(self, d: int, gset: set[int]) -> bool:
        """Is district ``d`` still connected (and non-empty) without the units in ``gset``?"""
        rest = len(self.members[d]) - len(gset)
        if rest <= 0:
            return False
        dist = self.dist
        targets = {v for u in gset for v in self.nbr[u] if dist[v] == d and v not in gset}
        if len(targets) <= 1:
            return True
        start = min(targets)
        seen = {start}
        stack = [start]
        found = 1
        while stack:
            x = stack.pop()
            for y in self.nbr[x]:
                if y not in seen and dist[y] == d and y not in gset:
                    seen.add(y)
                    stack.append(y)
                    if y in targets:
                        found += 1
                        if found == len(targets):
                            return True
        return found == len(targets)

    def _can_add(self, e: int, group: list[int], gset: set[int]) -> bool:
        """Does every piece of ``group`` touch district ``e`` (so ``e ∪ group`` stays connected)?"""
        if len(group) == 1:
            return True
        dist = self.dist
        stack = [u for u in group if any(dist[v] == e for v in self.nbr[u])]
        seen = set(stack)
        while stack:
            x = stack.pop()
            for y in self.nbr[x]:
                if y in gset and y not in seen:
                    seen.add(y)
                    stack.append(y)
        return len(seen) == len(group)

    def _apply(self, group: list[int], p: float, d: int, e: int) -> None:
        for u in group:
            self.dist[u] = e
            self.members[d].discard(u)
            self.members[e].add(u)
        self.dpop[d] -= p
        self.dpop[e] += p
        m = self.muni[group[0]]
        before = self.mcount[m][d] > 0, self.mcount[m][e] > 0
        self.mcount[m][d] -= len(group)
        self.mcount[m][e] += len(group)
        after = self.mcount[m][d] > 0, self.mcount[m][e] > 0
        self.mnd[m] += (after[0] - before[0]) + (after[1] - before[1])

    def _try_group(self, group: list[int], d: int, kind: str) -> bool:
        if not group or len(group) >= len(self.members[d]):
            return False
        gset = set(group)
        dist = self.dist
        cand = sorted({dist[v] for u in group for v in self.nbr[u] if dist[v] != d and v not in gset})
        if not cand:
            return False
        p = sum(self.pop[u] for u in group)
        options: list[tuple[float, int]] = []
        for e in cand:
            delta = self._delta(group, gset, p, d, e)
            if delta is not None and delta < -_EPS:
                options.append((delta, e))
        if not options or not self._can_remove(d, gset):
            return False
        for _, e in sorted(options):
            if self._can_add(e, group, gset):
                self._apply(group, p, d, e)
                self.moves[kind] += 1
                return True
        return False

    # ------------------------------------------------------------------ passes
    def _fragments(self, level: str) -> list[tuple[int, int]]:
        keys = np.asarray(self.muni if level == "municipality" else self.wijk, dtype=np.int64)
        d = np.asarray(self.dist, dtype=np.int64)
        a, b = self.edges[:, 0], self.edges[:, 1]
        cross = d[a] != d[b]
        boundary = np.unique(np.r_[a[cross], b[cross]])
        if len(boundary) == 0:
            return []
        pairs = np.unique(keys[boundary] * self.k + d[boundary])
        return [(int(x // self.k), int(x % self.k)) for x in pairs]

    def _group_pass(self, level: str, rng: np.random.Generator) -> int:
        frags = self._fragments(level)
        if not frags:
            return 0
        units_of = self.muni_units if level == "municipality" else self.wijk_units
        cap = self.cfg.local_search.max_fragment_units
        moved = 0
        for i in rng.permutation(len(frags)).tolist():
            g, d = frags[i]
            group = [u for u in units_of[g] if self.dist[u] == d]
            if len(group) > cap:
                continue
            if self._try_group(group, d, level):
                moved += 1
        return moved

    def _unit_pass(self, rng: np.random.Generator) -> int:
        d = np.asarray(self.dist, dtype=np.int64)
        a, b = self.edges[:, 0], self.edges[:, 1]
        cross = d[a] != d[b]
        boundary = np.unique(np.r_[a[cross], b[cross]])
        moved = 0
        for u in rng.permutation(boundary).tolist():
            if self._try_group([u], self.dist[u], "unit"):
                moved += 1
        return moved

    # ------------------------------------------------------------------ ejection chains
    def _boundary_moves(
        self, districts: set[int], exclude_muni: int | None, exclude_units: set[int]
    ) -> tuple[float, list[int], int, int]:
        """Best improving single-unit move out of ``districts`` (never touching ``exclude_muni``
        or ``exclude_units``)."""
        dist, pop = self.dist, self.pop
        best: tuple[float, list[int], int, int] = (-_EPS, [], -1, -1)
        for x in sorted(districts):
            for u in sorted(self.members[x]):
                if self.muni[u] == exclude_muni or u in exclude_units:
                    continue
                cand = {dist[v] for v in self.nbr[u] if dist[v] != x}
                for f in sorted(cand):
                    delta = self._delta([u], {u}, pop[u], x, f)
                    if delta is not None and delta < best[0]:
                        best = (delta, [u], x, f)
        return best

    def _try_chain(self, group: list[int], e: int, d: int, protect_muni: bool = True) -> bool:
        """Move ``group`` from ``e`` into ``d`` (even if that alone is not an improvement) and
        rebalance greedily; keep the chain only if the objective improves overall."""
        gset = set(group)
        if len(group) >= len(self.members[e]):
            return False
        p = sum(self.pop[u] for u in group)
        delta0 = self._delta(group, gset, p, e, d)
        if delta0 is None or not self._can_remove(e, gset) or not self._can_add(d, group, gset):
            return False
        m = self.muni[group[0]]
        applied: list[tuple[list[int], float, int, int]] = [(group, p, e, d)]
        self._apply(group, p, e, d)
        total = delta0
        involved = {d, e}
        for _ in range(self.cfg.local_search.chain_length):
            if total < -_EPS:
                break
            delta, g, x, f = self._boundary_moves(involved, m if protect_muni else None, gset)
            if not g:
                break
            gs = set(g)
            if not self._can_remove(x, gs):
                # try the next best move only through the standard passes; stop the chain here
                break
            pg = self.pop[g[0]]
            self._apply(g, pg, x, f)
            applied.append((g, pg, x, f))
            total += delta
            involved.add(f)
        if total < -_EPS:
            self.moves["chain"] = self.moves.get("chain", 0) + 1
            return True
        for g, pg, x, f in reversed(applied):
            self._apply(g, pg, f, x)
        return False

    def _chain_pass(self, rng: np.random.Generator) -> int:
        ls = self.cfg.local_search
        frags = [
            (m, d)
            for m in range(len(self.mnd))
            if self.mnd[m] > 1
            for d in range(self.k)
            if self.mcount[m][d] > 0
        ]
        moved = 0
        for i in rng.permutation(len(frags)).tolist():
            m, e = frags[i]
            if self.mnd[m] <= 1 or self.mcount[m][e] == 0:
                continue
            group = [u for u in self.muni_units[m] if self.dist[u] == e]
            if len(group) > ls.max_fragment_units:
                continue
            if sum(self.pop[u] for u in group) > ls.chain_max_share * self.T:
                continue
            gset = set(group)
            targets = sorted(
                {self.dist[v] for u in group for v in self.nbr[u] if v not in gset and self.dist[v] != e}
                & {d for d in range(self.k) if self.mcount[m][d] > 0}
            )
            for d in targets:
                if self._try_chain(group, e, d):
                    moved += 1
                    break
        return moved

    def _rebalance_pass(self, rng: np.random.Generator, max_attempts: int = 60) -> int:
        """Ejection chains starting at districts beyond the target tolerance."""
        dev = [(p / t - 1.0) * 100.0 for p, t in zip(self.dpop, self.targets, strict=True)]
        viol = sorted(
            (d for d in range(self.k) if abs(dev[d]) > self.tols[d]), key=lambda d: (-abs(dev[d]), d)
        )
        moved = 0
        dist = self.dist
        for d in viol:
            if abs(self.dpop[d] / self.targets[d] - 1.0) * 100.0 <= self.tols[d]:
                continue
            over = self.dpop[d] > self.targets[d]
            cands: set[tuple[int, int, int]] = set()
            for u in self.members[d]:
                for v in self.nbr[u]:
                    if dist[v] != d:
                        cands.add((u, d, dist[v]) if over else (v, dist[v], d))
            ordered = sorted(cands)
            for i in rng.permutation(len(ordered))[:max_attempts].tolist():
                u, src, dst = ordered[i]
                if dist[u] != src:
                    continue
                if self._try_chain([u], src, dst, protect_muni=False):
                    moved += 1
                    break
        return moved

    def run(self, rng: np.random.Generator) -> np.ndarray:
        ls = self.cfg.local_search
        for _ in range(ls.max_passes):
            self.passes += 1
            moved = 0
            if ls.fragment_moves:
                moved += self._group_pass("municipality", rng)
            if ls.wijk_moves:
                moved += self._group_pass("wijk", rng)
            if ls.unit_moves:
                moved += self._unit_pass(rng)
            if moved == 0 and ls.chain_moves:
                moved += self._chain_pass(rng)
                if moved == 0:
                    moved += self._rebalance_pass(rng)
            if moved == 0:
                break
        return np.asarray(self.dist, dtype=np.int64)


# =========================================================================== objective
def plan_objective(
    assignment: np.ndarray,
    weights: np.ndarray,
    muni: np.ndarray,
    edges: np.ndarray,
    edge_km: np.ndarray,
    k: int,
    target: float,
    cfg: DistrictConfig,
) -> tuple[float, dict[str, float]]:
    """Local-search objective of a (province) assignment and its components.

    ``weight_hard × Σ excess over the hard maximum + weight_target × Σ excess over the target
    tolerance + weight_split × split municipalities + weight_fragment × extra fragments +
    weight_deviation × Σ deviation² + weight_cut_km × internal boundary length`` (deviations in %).
    """
    ls = cfg.local_search
    dpop = np.bincount(assignment, weights=weights, minlength=k)
    dev = np.abs(dpop / max(float(target), 1e-9) - 1.0) * 100.0
    hard = float(np.clip(dev - cfg.max_deviation_pct, 0, None).sum())
    tgt = float(np.clip(dev - cfg.target_deviation_pct, 0, None).sum())
    n_over = int((dev > cfg.target_deviation_pct).sum())
    pairs = np.unique(muni.astype(np.int64) * k + assignment)
    per_muni = np.bincount(pairs // k)
    per_muni = per_muni[per_muni > 0]
    n_split = int((per_muni > 1).sum())
    n_frag = int((per_muni - 1).sum())
    cut = float(edge_km[assignment[edges[:, 0]] != assignment[edges[:, 1]]].sum()) if len(edges) else 0.0
    value = (
        ls.weight_hard * hard
        + ls.weight_target * tgt
        + ls.weight_target_district * n_over
        + ls.weight_split * n_split
        + ls.weight_fragment * n_frag
        + ls.weight_deviation * float((dev**2).sum())
        + ls.weight_cut_km * cut
    )
    parts = {
        "objective": round(value, 4),
        "excess_hard_pct": round(hard, 4),
        "excess_target_pct": round(tgt, 4),
        "districts_over_target": n_over,
        "split_municipalities": n_split,
        "extra_fragments": n_frag,
        "max_abs_deviation_pct": round(float(dev.max()), 4),
        "cut_km": round(cut, 3),
    }
    return value, parts


def restart_config(cfg: DistrictConfig, seed: int, code: str, restart: int) -> DistrictConfig:
    """Configuration of restart ``restart`` (0 = unchanged; later ones have seeded jitter)."""
    if restart == 0 or cfg.restart_jitter <= 0:
        return cfg
    rng = make_rng(seed, "districts", "restart-jitter", code, restart)
    j = cfg.restart_jitter

    def f() -> float:
        return float(rng.uniform(1.0 - j, 1.0 + j))

    cut = cfg.cut.model_copy(
        update={
            "deviation": cfg.cut.deviation * f(),
            "new_split": cfg.cut.new_split * f(),
            "small_split": cfg.cut.small_split * f(),
            "existing_split": cfg.cut.existing_split * f(),
            "cut_length": cfg.cut.cut_length * f(),
        }
    )
    exponent = float(np.clip(cfg.tolerance_exponent + rng.uniform(-j, j), 0.0, 1.0))
    return cfg.model_copy(update={"cut": cut, "tolerance_exponent": exponent})


# =========================================================================== entry point
def _single_run(
    prob: ProvinceProblem, cfg: DistrictConfig, seed: int
) -> tuple[np.ndarray, list[str], dict[str, Any], _Bisector]:
    n = len(prob.pop)
    bis = _Bisector(prob, cfg, seed)
    assignment = bis.run() if prob.seats > 1 else np.zeros(n, dtype=np.int64)
    warnings = list(bis.warnings)
    assignment, repaired = repair_contiguity(assignment, prob.edges, prob.edge_km, bis.weights, prob.seats)
    if repaired:
        warnings.append(f"{prob.code}: moved {repaired} stray piece(s) to restore contiguity")
    info: dict[str, Any] = dict(bis.stats)
    info["repaired_pieces"] = repaired
    ls_cfg = cfg.local_search
    if ls_cfg.enabled and prob.seats > 1 and ls_cfg.max_passes > 0:
        search = _LocalSearch(prob, cfg, assignment, bis.weights, bis.T)
        assignment = search.run(make_rng(seed, "districts", "local-search", prob.code))
        info["local_search_passes"] = search.passes
        info["local_search_moves"] = dict(search.moves)
    return assignment, warnings, info, bis


def partition_province(prob: ProvinceProblem) -> ProvinceResult:
    """Partition one province into ``prob.seats`` contiguous, population-balanced districts.

    Runs ``config.restarts`` independent attempts (restart 0 uses the root seed and the configured
    weights; later restarts use derived seeds and jittered weights) and keeps the assignment with
    the lowest :func:`plan_objective` (ties: lowest restart number).
    """
    t0 = time.perf_counter()
    n = len(prob.pop)
    cfg = prob.config
    if prob.seats < 1:
        raise ValueError(f"{prob.code}: seats must be >= 1")
    if n < prob.seats:
        raise ValueError(f"{prob.code}: {n} units cannot form {prob.seats} non-empty districts")
    best: tuple[float, int, np.ndarray, list[str], dict[str, Any], dict[str, float]] | None = None
    objectives: list[float] = []
    n_restarts = cfg.restarts if prob.seats > 1 else 1
    for r in range(n_restarts):
        cfg_r = restart_config(cfg, prob.seed, prob.code, r)
        seed_r = prob.seed if r == 0 else derive_seed(prob.seed, "districts", "restart", prob.code, r)
        assignment, warnings, info, bis = _single_run(prob, cfg_r, seed_r)
        value, parts = plan_objective(
            assignment, bis.weights, prob.muni, prob.edges, prob.edge_km, prob.seats, bis.T, cfg
        )
        objectives.append(round(value, 4))
        if best is None or value < best[0] - 1e-9:
            best = (value, r, assignment, warnings, info, parts)
        balance_by_units = bis.balance_by_units
    assert best is not None
    value, r, assignment, warnings, info, parts = best
    if balance_by_units:
        warnings = [*warnings, f"{prob.code}: province has no population; balancing by unit count"]
    info = {**info, **parts, "restart": r, "restarts": n_restarts, "restart_objectives": objectives}
    info["seconds"] = round(time.perf_counter() - t0, 3)
    return ProvinceResult(code=prob.code, assignment=assignment, warnings=warnings, info=info)
