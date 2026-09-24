"""House district plan generator (FICTIONAL districts over REAL geography).

    from app.districts.apportionment import apportion
    from app.districts.generator import generate_plan

    seats = apportion(province_populations, 150).seats
    plan = generate_plan(units_gdf, unit_adjacency, seats, config, seed=2028)

Every province is partitioned independently (optionally in parallel worker processes — results are
identical to a serial run because every random stream is derived from the root seed, the province
code and the position in the bisection tree).  The per-province engine lives in
:mod:`app.districts.partition`; this module prepares the unit graph, dispatches the provinces,
numbers and names the districts, applies manual overrides and records provenance (seed, config
hash, timings, warnings).  See docs/DISTRICTING.md.
"""

from __future__ import annotations

import multiprocessing
import os
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from app.core.errors import DistrictingError
from app.core.logging import Timer, get_logger, log_ctx
from app.core.rng import config_hash
from app.districts.config import DistrictConfig, load_district_config
from app.districts.naming import name_districts, serpentine_order
from app.districts.partition import ProvinceProblem, ProvinceResult, components, partition_province
from app.districts.plan import GeneratedPlan, district_code

log = get_logger(__name__)

REQUIRED_UNIT_COLUMNS: tuple[str, ...] = (
    "code",
    "municipality_code",
    "province_code",
    "population",
    "centroid_x",
    "centroid_y",
)
WATER_LINK = "water_link"


# =========================================================================== unit graph
def build_unit_edges(
    unit_codes: Sequence[str], adjacency: pd.DataFrame | None
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Map an adjacency table (``a, b, shared_border_m, kind``) to unique unit-index pairs.

    Returns ``(edges (E, 2) with i < j, water (E,) bool, border_m (E,) float, n_unknown)`` where
    ``n_unknown`` counts rows referencing codes absent from ``unit_codes`` (dropped).  Duplicate
    pairs are merged (border lengths summed; a pair is a water link only if all its rows are).
    """
    if adjacency is None or len(adjacency) == 0:
        return np.zeros((0, 2), dtype=np.int64), np.zeros(0, dtype=bool), np.zeros(0), 0
    idx = pd.Index(np.asarray(unit_codes, dtype=object))
    if not idx.is_unique:
        raise DistrictingError("duplicate unit codes in units table")
    ia = idx.get_indexer(adjacency["a"].astype(str))
    ib = idx.get_indexer(adjacency["b"].astype(str))
    border = (
        pd.to_numeric(adjacency["shared_border_m"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        if "shared_border_m" in adjacency
        else np.zeros(len(adjacency))
    )
    water = (
        (adjacency["kind"].astype(str) == WATER_LINK).to_numpy()
        if "kind" in adjacency
        else np.zeros(len(adjacency), dtype=bool)
    )
    known = (ia >= 0) & (ib >= 0)
    n_unknown = int((~known).sum())
    keep = known & (ia != ib)
    lo = np.minimum(ia[keep], ib[keep]).astype(np.int64)
    hi = np.maximum(ia[keep], ib[keep]).astype(np.int64)
    n = len(idx)
    key = lo * n + hi
    uniq, inv = np.unique(key, return_inverse=True)
    border_sum = np.bincount(inv, weights=border[keep], minlength=len(uniq))
    land_rows = np.bincount(inv, weights=(~water[keep]).astype(float), minlength=len(uniq))
    edges = np.column_stack([uniq // n, uniq % n]).astype(np.int64)
    return edges, land_rows == 0, border_sum, n_unknown


def connect_province_graph(
    n: int, edges: np.ndarray, xy: np.ndarray
) -> tuple[np.ndarray, list[tuple[int, int, float]]]:
    """Make a (local) province graph connected by linking components to their nearest unit.

    The smallest component is linked first (ties by component label) to the nearest unit centroid
    of any other component.  Returns ``(added_edges (K, 2), [(i, j, distance_m), …])``.
    """
    added: list[tuple[int, int, float]] = []
    cur = edges
    while True:
        nc, lab = (
            components(n, cur[:, 0], cur[:, 1])
            if len(cur)
            else components(n, np.zeros(0, int), np.zeros(0, int))
        )
        if nc <= 1:
            break
        sizes = np.bincount(lab, minlength=nc)
        comp = int(np.lexsort((np.arange(nc), sizes))[0])
        inside = np.flatnonzero(lab == comp)
        outside = np.flatnonzero(lab != comp)
        dist, j = cKDTree(xy[outside]).query(xy[inside], k=1)
        best = int(np.lexsort((inside, dist))[0])
        a, b = int(inside[best]), int(outside[int(j[best])])
        added.append((min(a, b), max(a, b), float(dist[best])))
        cur = np.vstack([cur, [[min(a, b), max(a, b)]]]) if len(cur) else np.asarray([[min(a, b), max(a, b)]])
    extra = np.asarray([[a, b] for a, b, _ in added], dtype=np.int64).reshape(-1, 2)
    return extra, added


# =========================================================================== main entry
def generate_plan(
    units_gdf: pd.DataFrame,
    adjacency: pd.DataFrame | None,
    seats_by_province: Mapping[str, int],
    config: DistrictConfig | None = None,
    seed: int = 0,
    overrides: Sequence[Any] | None = None,
    *,
    municipality_names: Mapping[str, str] | None = None,
    workers: int | None = None,
) -> GeneratedPlan:
    """Generate a complete House district plan.

    Args:
        units_gdf: one row per geographic unit (CBS buurt) with at least
            :data:`REQUIRED_UNIT_COLUMNS`; ``wijk_code``, ``land_area_km2`` (or a geometry),
            ``municipality_name`` and ``address_density`` (or ``density``; locates city centres
            for district names) are used when present.  A plain DataFrame works too (geometry is
            only used for the area fallback).
        adjacency: unit adjacency ``a, b, shared_border_m, kind`` (``border`` / ``water_link``).
        seats_by_province: province code → number of districts (e.g. ``apportion(...).seats``).
        config: generation parameters (default: ``config/districts.yaml``).
        seed: root seed; the same seed, data and configuration always give the same plan.
        overrides: manual assignments applied after generation (``OverrideEntry`` objects or
            dicts, see :mod:`app.districts.overrides`); ``None`` applies none.
        municipality_names: code → display name used for district names (merged over a
            ``municipality_name`` column).  ``None`` also consults the processed store's
            municipality names when they cover every municipality; otherwise codes are used.
        workers: worker processes (overrides ``config.workers``; 0 = auto, 1 = serial).

    Raises:
        DistrictingError: missing columns, duplicate unit codes, units in provinces without
            seats, a province with fewer units than seats, or an invalid override.
    """
    t_start = time.perf_counter()
    cfg = config if config is not None else load_district_config()
    timings: dict[str, float] = {}
    warnings: list[str] = []
    missing = [c for c in REQUIRED_UNIT_COLUMNS if c not in units_gdf.columns]
    if missing:
        raise DistrictingError(f"units table lacks required columns {missing}")
    seats = {str(p): int(s) for p, s in seats_by_province.items()}
    with Timer(log, "district plan: prepare") as tm:
        codes = units_gdf["code"].astype(str).to_numpy(dtype=object)
        if len(np.unique(codes)) != len(codes):
            dup = pd.Index(codes)[pd.Index(codes).duplicated()].unique()[:5].tolist()
            raise DistrictingError(f"duplicate unit codes in units table (e.g. {dup})")
        prov = units_gdf["province_code"].astype(str).to_numpy(dtype=object)
        muni = units_gdf["municipality_code"].astype(str).to_numpy(dtype=object)
        raw_pop = pd.to_numeric(units_gdf["population"], errors="coerce").to_numpy(dtype=float)
        bad_pop = ~np.isfinite(raw_pop) | (raw_pop < 0)
        if bad_pop.any():
            warnings.append(
                f"{int(bad_pop.sum())} units have a missing, non-numeric or negative population "
                f"(treated as 0), e.g. {', '.join(codes[bad_pop][:5].tolist())}"
            )
        pop = np.round(np.where(bad_pop, 0.0, raw_pop)).astype(np.int64)
        xy = units_gdf[["centroid_x", "centroid_y"]].to_numpy(dtype=float)
        if not np.isfinite(xy).all():
            raise DistrictingError("units table has missing centroid coordinates")
        area = _unit_area(units_gdf)
        wijk = _wijk_keys(units_gdf, muni, cfg.use_wijk_level)
        unknown_prov = sorted(set(prov.tolist()) - set(seats))
        if unknown_prov:
            raise DistrictingError(f"units in provinces without apportioned seats: {unknown_prov}")
        no_units = sorted(p for p, s in seats.items() if s > 0 and p not in set(prov.tolist()))
        if no_units:
            raise DistrictingError(f"provinces with seats but no units: {no_units}")
        bad_seats = sorted(p for p, s in seats.items() if s < 1 and p in set(prov.tolist()))
        if bad_seats:
            raise DistrictingError(f"provinces with units but no seats: {bad_seats}")
        edges, water, border_m, n_unknown = build_unit_edges(codes, adjacency)
        if n_unknown:
            log.info("%d adjacency rows reference units outside the unit table (ignored)", n_unknown)
        problems, extra_edges = _build_problems(
            codes, prov, muni, wijk, pop, xy, area, edges, water, border_m, seats, cfg, seed, warnings
        )
        if len(extra_edges):
            edges = np.vstack([edges, extra_edges])
            water = np.r_[water, np.ones(len(extra_edges), dtype=bool)]
            border_m = np.r_[border_m, np.zeros(len(extra_edges))]
    timings["prepare"] = tm.elapsed

    with Timer(log, "district plan: partition provinces") as tm:
        results = _run(problems, cfg.workers if workers is None else workers)
    timings["partition"] = tm.elapsed

    with Timer(log, "district plan: number and name") as tm:
        unit_district = np.full(len(codes), -1, dtype=np.int64)
        d_codes: list[str] = []
        d_prov: list[str] = []
        d_num: list[int] = []
        d_target: list[float] = []
        diagnostics: dict[str, dict[str, Any]] = {}
        for p in seats:
            if p not in results:
                continue
            res, prob_idx = results[p]
            k = seats[p]
            local = res.assignment
            w = pop[prob_idx].astype(float) + 1e-3
            cxy = (
                np.column_stack(
                    [
                        np.bincount(local, weights=w * xy[prob_idx, 0], minlength=k),
                        np.bincount(local, weights=w * xy[prob_idx, 1], minlength=k),
                    ]
                )
                / np.maximum(np.bincount(local, weights=w, minlength=k), 1e-12)[:, None]
            )
            order = serpentine_order(cxy)
            rank = np.empty(k, dtype=np.int64)
            rank[order] = np.arange(k)
            base = len(d_codes)
            unit_district[prob_idx] = base + rank[local]
            target = float(pop[prob_idx].sum()) / k
            for j in range(k):
                d_codes.append(district_code(p, j + 1))
                d_prov.append(p)
                d_num.append(j + 1)
                d_target.append(target)
            warnings.extend(res.warnings)
            diagnostics[p] = res.info
        if (unit_district < 0).any():
            raise DistrictingError(f"{int((unit_district < 0).sum())} units were not assigned to a district")
        names_map = _municipality_names(units_gdf, municipality_names)
        density = _unit_density(units_gdf)
        d_names = name_districts(
            unit_district, muni, pop, xy, len(d_codes), names_map, cfg.naming, unit_density=density
        )
    timings["naming"] = tm.elapsed

    plan = GeneratedPlan(
        unit_codes=codes,
        unit_province=prov,
        unit_municipality=muni,
        unit_population=pop,
        unit_district=unit_district,
        unit_overridden=np.zeros(len(codes), dtype=bool),
        district_codes=d_codes,
        district_names=d_names,
        district_province=d_prov,
        district_numbers=d_num,
        district_target=np.asarray(d_target, dtype=float),
        seats_by_province=seats,
        seed=int(seed),
        config=cfg,
        config_json=cfg.canonical_json([]),
        config_hash=config_hash(cfg.canonical_json([])),
        method=cfg.method,
        edges=edges,
        edge_water=water,
        edge_border_m=border_m,
        timings=timings,
        warnings=warnings,
        diagnostics=diagnostics,
    )
    if overrides:
        from app.districts.overrides import apply_overrides

        with Timer(log, "district plan: overrides") as tm:
            plan = apply_overrides(plan, overrides)
            if plan.overrides_applied:
                # names describe the final districts (an override may move a whole municipality)
                plan.district_names = name_districts(
                    plan.unit_district,
                    muni,
                    pop,
                    xy,
                    plan.n_districts,
                    names_map,
                    cfg.naming,
                    unit_density=density,
                )
        timings["overrides"] = tm.elapsed
        plan.timings = timings  # apply_overrides returns a copy with its own containers
    _post_checks(plan)
    timings["total"] = time.perf_counter() - t_start
    log.info(
        "generated %d districts (max |dev| %.2f%%, %d split municipalities)",
        plan.n_districts,
        plan.max_abs_deviation_pct,
        len(plan.split_municipalities()),
        extra=log_ctx(seed=int(seed), config_hash=plan.config_hash, seconds=round(timings["total"], 2)),
    )
    return plan


# =========================================================================== helpers
def _unit_area(units: pd.DataFrame) -> np.ndarray:
    if "land_area_km2" in units.columns:
        a = pd.to_numeric(units["land_area_km2"], errors="coerce").to_numpy(dtype=float)
    elif "area_km2" in units.columns:
        a = pd.to_numeric(units["area_km2"], errors="coerce").to_numpy(dtype=float)
    else:
        a = np.full(len(units), np.nan)
    if np.isnan(a).any() and "geometry" in units.columns:
        try:
            geo_area = np.asarray(units.geometry.area, dtype=float) / 1e6
            a = np.where(np.isnan(a), geo_area, a)
        except Exception:  # non-geographic frame
            pass
    a = np.nan_to_num(a, nan=0.0)
    return np.clip(a, 0.0, None)


def _unit_density(units: pd.DataFrame) -> np.ndarray | None:
    """Per-unit address density (CBS ``address_density``, else ``density``) for naming, or ``None``."""
    for col in ("address_density", "density"):
        if col in units.columns:
            return pd.to_numeric(units[col], errors="coerce").to_numpy(dtype=float)
    return None


def _wijk_keys(units: pd.DataFrame, muni: np.ndarray, use_wijk: bool) -> np.ndarray:
    """Per-unit wijk key (always nested in the municipality)."""
    if use_wijk and "wijk_code" in units.columns:
        w = units["wijk_code"].astype("string").fillna("").to_numpy(dtype=object)
        return np.asarray([f"{m}|{x}" for m, x in zip(muni, w, strict=True)], dtype=object)
    return np.asarray([f"{m}|" for m in muni], dtype=object)


def _municipality_names(units: pd.DataFrame, names: Mapping[str, str] | None) -> dict[str, str]:
    """Municipality display names: ``municipality_name`` column, then the explicit mapping.

    Without an explicit mapping, codes still unnamed are looked up in the processed store's
    ``municipalities.parquet`` (default vintage) — but only when it names *every* municipality of
    the units, so synthetic or foreign geographies keep their codes.
    """
    out: dict[str, str] = {}
    if "municipality_name" in units.columns:
        df = units[["municipality_code", "municipality_name"]].dropna().drop_duplicates("municipality_code")
        out.update(zip(df["municipality_code"].astype(str), df["municipality_name"].astype(str), strict=True))
    if names is not None:
        out.update({str(k): str(v) for k, v in names.items()})
        return out
    missing = set(units["municipality_code"].astype(str)) - set(out)
    if missing:
        store_names = _store_municipality_names()
        if store_names and missing <= set(store_names):
            out.update({c: store_names[c] for c in missing})
    return out


def _store_municipality_names() -> dict[str, str]:
    """``code → name`` of the processed store's municipalities (empty when unavailable)."""
    try:
        from app.geography import store

        if not store.is_prepared():
            return {}
        df = pd.read_parquet(store.store_dir() / "municipalities.parquet", columns=["code", "name"])
    except Exception as exc:  # the store is optional here: names fall back to CBS codes
        log.debug("municipality names unavailable from the processed store (%s)", exc)
        return {}
    df = df.dropna()
    return dict(zip(df["code"].astype(str), df["name"].astype(str), strict=True))


def _build_problems(
    codes: np.ndarray,
    prov: np.ndarray,
    muni: np.ndarray,
    wijk: np.ndarray,
    pop: np.ndarray,
    xy: np.ndarray,
    area: np.ndarray,
    edges: np.ndarray,
    water: np.ndarray,
    border_m: np.ndarray,
    seats: dict[str, int],
    cfg: DistrictConfig,
    seed: int,
    warnings: list[str],
) -> tuple[list[tuple[ProvinceProblem, np.ndarray]], np.ndarray]:
    """Per-province problems (units sorted by code for row-order independence) + synthetic links."""
    n = len(codes)
    edge_km = np.where(water, cfg.water_link_border_m, np.maximum(border_m, 1.0)) / 1000.0
    problems: list[tuple[ProvinceProblem, np.ndarray]] = []
    extra_global: list[np.ndarray] = []
    for p in seats:
        idx = np.flatnonzero(prov == p)
        if len(idx) == 0:
            continue
        idx = idx[np.argsort(codes[idx].astype(str), kind="stable")]
        if len(idx) < seats[p]:
            raise DistrictingError(f"province {p} has {len(idx)} units but {seats[p]} seats")
        pos = np.full(n, -1, dtype=np.int64)
        pos[idx] = np.arange(len(idx))
        inside = (pos[edges[:, 0]] >= 0) & (pos[edges[:, 1]] >= 0) if len(edges) else np.zeros(0, dtype=bool)
        le = np.sort(pos[edges[inside]], axis=1) if len(edges) else np.zeros((0, 2), dtype=np.int64)
        lkm = edge_km[inside] if len(edges) else np.zeros(0)
        extra, added = connect_province_graph(len(idx), le, xy[idx])
        if added:
            warnings.append(
                f"{p}: unit graph was disconnected; added {len(added)} synthetic water link(s) "
                + ", ".join(f"{codes[idx[a]]}–{codes[idx[b]]} ({d:.0f} m)" for a, b, d in added[:5])
            )
            le = np.vstack([le, extra]) if len(le) else extra
            lkm = np.r_[lkm, np.full(len(extra), cfg.water_link_border_m / 1000.0)]
            extra_global.append(idx[extra])
        _, muni_local = np.unique(muni[idx].astype(str), return_inverse=True)
        _, wijk_local = np.unique(wijk[idx].astype(str), return_inverse=True)
        problems.append(
            (
                ProvinceProblem(
                    code=p,
                    seats=seats[p],
                    pop=pop[idx],
                    xy=xy[idx],
                    area=area[idx],
                    muni=muni_local.astype(np.int64),
                    wijk=wijk_local.astype(np.int64),
                    edges=le.astype(np.int64),
                    edge_km=lkm.astype(float),
                    seed=int(seed),
                    config=cfg,
                ),
                idx,
            )
        )
    extra_edges = np.vstack(extra_global) if extra_global else np.zeros((0, 2), dtype=np.int64)
    return problems, np.sort(extra_edges, axis=1)


def _run(
    problems: list[tuple[ProvinceProblem, np.ndarray]], workers: int
) -> dict[str, tuple[ProvinceResult, np.ndarray]]:
    """Partition every province (serially or in worker processes; identical results)."""
    if workers == 0:
        workers = min(len(problems), os.cpu_count() or 1)
    out: dict[str, tuple[ProvinceResult, np.ndarray]] = {}
    if workers <= 1 or len(problems) <= 1:
        for prob, idx in problems:
            out[prob.code] = (partition_province(prob), idx)
        return out
    ordered = sorted(problems, key=lambda t: (-len(t[1]), t[0].code))
    ctx = multiprocessing.get_context("spawn")
    # Worker processes run single-threaded BLAS (the per-province matrices are small; threaded
    # BLAS would oversubscribe the CPUs).  Spawned children inherit the environment at start.
    saved = {k: os.environ.get(k) for k in _BLAS_ENV}
    os.environ.update(dict.fromkeys(_BLAS_ENV, "1"))
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            futures = {prob.code: (ex.submit(partition_province, prob), idx) for prob, idx in ordered}
            for code, (fut, idx) in futures.items():
                out[code] = (fut.result(), idx)
    except BrokenProcessPool as exc:
        log.warning("worker pool failed (%s); partitioning serially", exc)
        return _run(problems, 1)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return out


_BLAS_ENV = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")


def _post_checks(plan: GeneratedPlan) -> None:
    """Record plan-level warnings (deviation beyond tolerance, non-contiguity)."""
    cfg = plan.config
    dev = plan.deviation_pct()
    for i in np.flatnonzero(np.abs(dev) > cfg.target_deviation_pct):
        msg = f"{plan.district_codes[i]}: deviation {dev[i]:+.2f}% exceeds the target tolerance ±{cfg.target_deviation_pct}%"
        if msg not in plan.warnings:
            plan.warnings.append(msg)
    for c in plan.noncontiguous_districts():
        msg = f"{c}: district is not contiguous"
        if msg not in plan.warnings:
            plan.warnings.append(msg)
