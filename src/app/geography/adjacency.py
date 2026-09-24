"""Polygon adjacency (rook contiguity) and water links.

``compute_adjacency`` finds neighbouring polygons with an STRtree ``dwithin`` query on the
original geometries and measures the *shared border* as the length of one polygon's boundary that
lies inside the other polygon's ``snap_buffer_m`` buffer (computed in both directions; the smaller
value is kept).  Pairs whose shared border is shorter than ``min_shared_border_m`` are dropped —
this excludes point (queen) touches and slivers from slightly misaligned boundaries.

``connect_components`` then makes each group's graph (e.g. each province) connected: disconnected
pieces — Wadden and Zeeland islands, IJsselmeer polders, enclaves — are joined to their nearest
polygon in another component of the same group by explicit ``water_link`` edges (Borůvka style:
every component but the largest adds its shortest outgoing link, repeated until one component
remains).  A link
whose gap is within ``touch_tolerance_m`` is a genuine but short land border and is labelled
``border`` instead.

Edge tables have the columns ``a, b, shared_border_m, kind, gap_m`` with ``a < b`` (by code),
sorted by ``(a, b)``.
"""

from __future__ import annotations

from collections.abc import Mapping

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from app.core.logging import Timer, get_logger, log_ctx

log = get_logger(__name__)

EDGE_COLUMNS: tuple[str, ...] = ("a", "b", "shared_border_m", "kind", "gap_m")
BORDER = "border"
WATER_LINK = "water_link"


def empty_edges() -> pd.DataFrame:
    """An empty edge table with the canonical columns and dtypes."""
    return pd.DataFrame(
        {
            "a": pd.Series(dtype=object),
            "b": pd.Series(dtype=object),
            "shared_border_m": pd.Series(dtype=float),
            "kind": pd.Series(dtype=object),
            "gap_m": pd.Series(dtype=float),
        }
    )


def _normalise(edges: pd.DataFrame) -> pd.DataFrame:
    """Orient ``a < b``, drop self loops and duplicates (keeping the longest border), sort."""
    if edges.empty:
        return empty_edges()
    a = edges["a"].astype(str).to_numpy()
    b = edges["b"].astype(str).to_numpy()
    swap = a > b
    out = edges.copy()
    out["a"] = np.where(swap, b, a)
    out["b"] = np.where(swap, a, b)
    out = out[out["a"] != out["b"]]
    if "gap_m" not in out:
        out["gap_m"] = 0.0
    out = out.sort_values(["a", "b", "shared_border_m"], ascending=[True, True, False], kind="mergesort")
    out = out.drop_duplicates(["a", "b"], keep="first")
    out = out[list(EDGE_COLUMNS)].reset_index(drop=True)
    out["shared_border_m"] = out["shared_border_m"].astype(float)
    out["gap_m"] = out["gap_m"].astype(float)
    return out


def compute_adjacency(
    gdf: gpd.GeoDataFrame,
    code_col: str = "code",
    snap_buffer_m: float = 2.0,
    min_shared_border_m: float = 20.0,
) -> pd.DataFrame:
    """Rook adjacency of the polygons in ``gdf`` (projected CRS in metres).

    Returns an edge table ``a, b, shared_border_m, kind='border', gap_m=0`` with ``a < b``.
    """
    if len(gdf) == 0:
        return empty_edges()
    codes = gdf[code_col].astype(str).to_numpy()
    if len(set(codes)) != len(codes):
        raise ValueError(f"duplicate values in {code_col!r}")
    geoms = np.asarray(gdf.geometry.values, dtype=object)
    with Timer(log, f"adjacency candidates ({len(geoms)} polygons)"):
        tree = shapely.STRtree(geoms)
        left, right = tree.query(geoms, predicate="dwithin", distance=max(snap_buffer_m, 1e-9))
        keep = left < right
        left, right = left[keep], right[keep]
    if len(left) == 0:
        return empty_edges()
    with Timer(log, f"shared borders ({len(left)} candidate pairs)"):
        used = np.unique(np.concatenate([left, right]))
        boundary = np.empty(len(geoms), dtype=object)
        buffered = np.empty(len(geoms), dtype=object)
        boundary[used] = shapely.boundary(geoms[used])
        buffered[used] = shapely.buffer(
            geoms[used], max(snap_buffer_m, 1e-6), quad_segs=1, join_style="mitre", mitre_limit=2.0
        )
        forward = shapely.length(shapely.intersection(boundary[left], buffered[right]))
        backward = shapely.length(shapely.intersection(boundary[right], buffered[left]))
        shared = np.minimum(forward, backward)
    mask = shared >= min_shared_border_m
    edges = pd.DataFrame(
        {
            "a": codes[left[mask]],
            "b": codes[right[mask]],
            "shared_border_m": np.round(shared[mask], 2),
            "kind": BORDER,
            "gap_m": 0.0,
        }
    )
    out = _normalise(edges)
    log.info(
        "adjacency: %d edges (%d point/short touches dropped)",
        len(out),
        int((~mask).sum()),
        extra=log_ctx(edges=len(out), dropped=int((~mask).sum())),
    )
    return out


def graph_components(codes: np.ndarray, edges: pd.DataFrame) -> tuple[int, np.ndarray]:
    """Connected components of the graph over ``codes`` using ``edges`` (both ends must be in codes)."""
    n = len(codes)
    if n == 0:
        return 0, np.zeros(0, dtype=np.int64)
    index = pd.Index(codes)
    if edges.empty:
        return n, np.arange(n, dtype=np.int64)
    ia = index.get_indexer(edges["a"].to_numpy())
    ib = index.get_indexer(edges["b"].to_numpy())
    ok = (ia >= 0) & (ib >= 0)
    ia, ib = ia[ok], ib[ok]
    mat = coo_matrix((np.ones(len(ia), dtype=np.int8), (ia, ib)), shape=(n, n))
    ncomp, labels = connected_components(mat, directed=False)
    return int(ncomp), labels.astype(np.int64)


def connect_components(
    adj: pd.DataFrame,
    gdf: gpd.GeoDataFrame,
    group_col: str = "province_code",
    code_col: str = "code",
    touch_tolerance_m: float = 2.0,
    max_rounds: int = 64,
) -> pd.DataFrame:
    """Add links so that the polygons of every ``group_col`` value form one connected graph.

    Returns ``adj`` plus the added edges (``kind='water_link'`` with ``gap_m`` = nearest-polygon
    distance; ``kind='border'`` for gaps ≤ ``touch_tolerance_m``).  Deterministic: ties are broken
    by distance, then codes.
    """
    adj = _normalise(adj) if not adj.empty else empty_edges()
    codes_all = gdf[code_col].astype(str).to_numpy()
    groups_all = gdf[group_col].astype(str).to_numpy()
    geoms_all = np.asarray(gdf.geometry.values, dtype=object)
    added: list[dict[str, object]] = []
    for group in sorted(set(groups_all.tolist())):
        idx = np.flatnonzero(groups_all == group)
        codes = codes_all[idx]
        order = np.argsort(codes, kind="stable")
        idx, codes = idx[order], codes[order]
        geoms = geoms_all[idx]
        code_set = set(codes.tolist())
        local = adj[adj["a"].isin(code_set) & adj["b"].isin(code_set)]
        group_added: list[dict[str, object]] = []
        for _ in range(max_rounds):
            current = (
                pd.concat([local, pd.DataFrame(group_added)], ignore_index=True) if group_added else local
            )
            ncomp, labels = graph_components(codes, current)
            if ncomp <= 1:
                break
            candidates: dict[tuple[str, str], float] = {}
            sizes = np.bincount(labels, minlength=ncomp)
            largest = int(np.argmax(sizes))  # its shortest link is found from the other side
            for comp in range(ncomp):
                if comp == largest:
                    continue
                inside = np.flatnonzero(labels == comp)
                outside = np.flatnonzero(labels != comp)
                tree = shapely.STRtree(geoms[outside])
                (src, dst), dist = tree.query_nearest(geoms[inside], return_distance=True, all_matches=True)
                best = float(dist.min())
                tie = np.flatnonzero(dist <= best + 1e-9)
                pairs = sorted(tuple(sorted((codes[inside[src[t]]], codes[outside[dst[t]]]))) for t in tie)
                pair = pairs[0]
                candidates[pair] = min(candidates.get(pair, np.inf), best)
            for (a, b), dist_m in sorted(candidates.items()):
                kind = BORDER if dist_m <= touch_tolerance_m else WATER_LINK
                group_added.append(
                    {"a": a, "b": b, "shared_border_m": 0.0, "kind": kind, "gap_m": round(float(dist_m), 2)}
                )
        else:
            raise RuntimeError(
                f"connect_components: group {group!r} still disconnected after {max_rounds} rounds"
            )
        for edge in group_added:
            log.info(
                "%s link in %s: %s — %s (gap %.0f m)",
                edge["kind"],
                group,
                edge["a"],
                edge["b"],
                edge["gap_m"],
                extra=log_ctx(group=group, kind=edge["kind"], gap_m=edge["gap_m"]),
            )
        added.extend(group_added)
    if not added:
        return adj
    return _normalise(pd.concat([adj, pd.DataFrame(added)], ignore_index=True))


def aggregate_adjacency(unit_adj: pd.DataFrame, unit_to_group: Mapping[str, str] | pd.Series) -> pd.DataFrame:
    """Lift a unit edge table to a coarser level (e.g. municipalities).

    Border lengths are summed over unit pairs crossing the two groups; water links are kept (with the
    smallest gap) only where the groups share no land border.
    """
    if unit_adj.empty:
        return empty_edges()
    mapping = pd.Series(unit_to_group) if not isinstance(unit_to_group, pd.Series) else unit_to_group
    ga = unit_adj["a"].map(mapping)
    gb = unit_adj["b"].map(mapping)
    if ga.isna().any() or gb.isna().any():
        raise ValueError("unit adjacency references units missing from the group mapping")
    e = pd.DataFrame(
        {
            "a": ga.astype(str),
            "b": gb.astype(str),
            "shared_border_m": unit_adj["shared_border_m"],
            "kind": unit_adj["kind"],
            "gap_m": unit_adj.get("gap_m", pd.Series(0.0, index=unit_adj.index)),
        }
    )
    e = e[e["a"] != e["b"]]
    if e.empty:
        return empty_edges()
    swap = e["a"] > e["b"]
    e.loc[swap, ["a", "b"]] = e.loc[swap, ["b", "a"]].to_numpy()
    borders = (
        e[e["kind"] == BORDER]
        .groupby(["a", "b"], as_index=False)
        .agg(shared_border_m=("shared_border_m", "sum"), gap_m=("gap_m", "min"))
    )
    borders["kind"] = BORDER
    links = (
        e[e["kind"] != BORDER]
        .groupby(["a", "b"], as_index=False)
        .agg(shared_border_m=("shared_border_m", "sum"), gap_m=("gap_m", "min"), kind=("kind", "first"))
    )
    if not borders.empty and not links.empty:
        key_b = set(zip(borders["a"], borders["b"], strict=True))
        links = links[[(a, b) not in key_b for a, b in zip(links["a"], links["b"], strict=True)]]
    out = pd.concat([borders, links], ignore_index=True)
    out["shared_border_m"] = out["shared_border_m"].round(2)
    return _normalise(out)


def component_counts(
    adj: pd.DataFrame, gdf: pd.DataFrame, group_col: str, code_col: str = "code"
) -> dict[str, int]:
    """Number of connected components per group (1 everywhere = every group graph is connected)."""
    out: dict[str, int] = {}
    codes_all = gdf[code_col].astype(str).to_numpy()
    groups_all = gdf[group_col].astype(str).to_numpy()
    for group in sorted(set(groups_all.tolist())):
        codes = np.sort(codes_all[groups_all == group])
        code_set = set(codes.tolist())
        local = adj[adj["a"].isin(code_set) & adj["b"].isin(code_set)]
        out[group] = graph_components(codes, local)[0]
    return out
