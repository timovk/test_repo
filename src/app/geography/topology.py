"""Polygon-coverage helpers: repair of tiny edge mismatches and fast dissolves.

CBS neighbourhood polygons form an (almost) valid *polygonal coverage*: neighbours share exactly
the same vertices along common borders.  A handful of polygons have sub-millimetre overlaps or
edge mismatches; :func:`repair_coverage` snaps those onto their neighbours and removes the overlap,
so the coverage becomes valid and can be dissolved with GEOS ``CoverageUnion`` (orders of magnitude
faster than a generic union) and simplified with ``coverage_simplify`` without creating slivers.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry

from app.core.logging import Timer, get_logger, log_ctx

log = get_logger(__name__)


def polygonal(geom: BaseGeometry | None) -> BaseGeometry:
    """Keep only the polygonal parts of ``geom`` (a Polygon or MultiPolygon, possibly empty)."""
    if geom is None or geom.is_empty:
        return shapely.MultiPolygon()
    if shapely.get_type_id(geom) in (3, 6):
        return geom
    parts = shapely.get_parts(geom)
    polys = [p for p in parts if shapely.get_type_id(p) in (3, 6)]
    flat: list[BaseGeometry] = []
    for p in polys:
        flat.extend(shapely.get_parts(p).tolist() if shapely.get_type_id(p) == 6 else [p])
    if not flat:
        return shapely.MultiPolygon()
    return flat[0] if len(flat) == 1 else shapely.MultiPolygon(flat)


def invalid_coverage_mask(geoms: np.ndarray, gap_width: float = 0.0) -> np.ndarray:
    """Boolean mask of polygons with invalid coverage edges (overlaps / mismatched borders)."""
    if len(geoms) == 0:
        return np.zeros(0, dtype=bool)
    edges = shapely.coverage_invalid_edges(np.asarray(geoms, dtype=object), gap_width=gap_width)
    return ~shapely.is_empty(edges)


def repair_coverage(
    geoms: Sequence[BaseGeometry] | np.ndarray,
    snap_tolerance_m: float = 0.05,
    max_rounds: int = 8,
    zone_m: float = 1.0,
) -> tuple[np.ndarray, list[int]]:
    """Make a nearly valid polygonal coverage valid.

    Each polygon with invalid coverage edges is snapped (tolerance ``snap_tolerance_m``) onto its
    neighbours' boundaries near the offending edges (within ``zone_m``) and the remaining overlap
    with the neighbours is removed.  Only the neighbourhood of repaired polygons is re-checked
    between rounds; a last round snaps against the complete neighbour boundaries.  Returns the
    repaired geometry array (a copy) and the positional indices that were modified.
    """
    out = np.asarray(geoms, dtype=object).copy()
    touched: set[int] = set()
    if len(out) == 0:
        return out, []
    with Timer(log, "coverage repair"):
        edges = shapely.coverage_invalid_edges(out)
        bad = np.flatnonzero(~shapely.is_empty(edges))
        bad_edges = edges[bad]
        tree = shapely.STRtree(out)
        for round_no in range(max_rounds + 1):
            if len(bad) == 0:
                break
            final_round = round_no == max_rounds
            for i, edge in zip(bad.tolist(), bad_edges, strict=True):
                nb = tree.query(out[i], predicate="dwithin", distance=zone_m)
                nb = nb[nb != i]
                if len(nb) == 0:
                    continue
                others = shapely.union_all(out[nb])
                target = shapely.boundary(others)
                if not final_round:
                    target = shapely.intersection(target, shapely.buffer(edge, zone_m))
                snapped = shapely.make_valid(shapely.snap(out[i], target, snap_tolerance_m))
                fixed = polygonal(shapely.make_valid(shapely.difference(snapped, others)))
                if fixed.is_empty:
                    continue
                out[i] = fixed
                touched.add(i)
            local = np.unique(
                np.concatenate(
                    [tree.query(out[i], predicate="dwithin", distance=zone_m) for i in bad.tolist()]
                )
            )
            local_edges = shapely.coverage_invalid_edges(out[local])
            mask = ~shapely.is_empty(local_edges)
            bad, bad_edges = local[mask], local_edges[mask]
        if len(bad):
            log.warning("coverage repair: %d polygons still have invalid edges", len(bad))
    result = sorted(touched)
    if result:
        log.info("coverage repair: %d polygons adjusted", len(result), extra=log_ctx(repaired=len(result)))
    return out, result


def dissolve_coverage(geoms: np.ndarray, groups: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Union polygons per group.  Uses ``coverage_union_all`` and falls back to ``union_all``
    whenever the fast result is invalid.  Returns ``(sorted unique group keys, geometries)``."""
    geoms = np.asarray(geoms, dtype=object)
    groups = np.asarray(groups)
    order = np.argsort(groups, kind="stable")
    keys, starts = np.unique(groups[order], return_index=True)
    ends = np.append(starts[1:], len(order))
    result = np.empty(len(keys), dtype=object)
    fallbacks = 0
    for k, (s, e) in enumerate(zip(starts, ends, strict=True)):
        part = geoms[order[s:e]]
        merged = shapely.coverage_union_all(part) if len(part) > 1 else part[0]
        if not shapely.is_valid(merged):
            merged = shapely.make_valid(shapely.union_all(part))
            fallbacks += 1
        result[k] = polygonal(merged)
    if fallbacks:
        log.info("dissolve: %d groups needed a full union", fallbacks)
    return keys, result
