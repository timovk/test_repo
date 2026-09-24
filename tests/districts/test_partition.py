"""Per-province engine: connected components, recombination (merge-split), objective."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

from app.districts.config import DistrictConfig
from app.districts.partition import (
    ProvinceProblem,
    components,
    merge_split,
    partition_province,
    plan_objective,
)


def _grid(rows: int, cols: int, muni: np.ndarray, pop: float = 100.0, seats: int = 2, seed: int = 1):  # type: ignore[no-untyped-def]
    idx = np.arange(rows * cols).reshape(rows, cols)
    edges = np.vstack(
        [
            np.column_stack([idx[:, :-1].ravel(), idx[:, 1:].ravel()]),
            np.column_stack([idx[:-1].ravel(), idx[1:].ravel()]),
        ]
    )
    yy, xx = np.divmod(np.arange(rows * cols), cols)
    return ProvinceProblem(
        code="XX",
        seats=seats,
        pop=np.full(rows * cols, pop, dtype=np.int64),
        xy=np.column_stack([xx * 1000.0, -yy * 1000.0]),
        area=np.ones(rows * cols),
        muni=np.asarray(muni, dtype=np.int64).ravel(),
        wijk=np.arange(rows * cols, dtype=np.int64),
        edges=np.sort(edges, axis=1).astype(np.int64),
        edge_km=np.ones(len(edges)),
        seed=seed,
        config=DistrictConfig(restarts=1),
    )


def _objective(prob: ProvinceProblem, assignment: np.ndarray) -> tuple[float, dict]:  # type: ignore[type-arg]
    w = prob.pop.astype(float)
    return plan_objective(
        assignment, w, prob.muni, prob.edges, prob.edge_km, prob.seats, w.sum() / prob.seats, prob.config
    )


def _contiguous(prob: ProvinceProblem, assignment: np.ndarray) -> bool:
    same = assignment[prob.edges[:, 0]] == assignment[prob.edges[:, 1]]
    nc, _ = components(len(assignment), prob.edges[same, 0], prob.edges[same, 1])
    return nc == prob.seats


def test_components_matches_scipy_labels() -> None:
    rng = np.random.default_rng(4)
    for n, e in [(1, 0), (5, 0), (40, 30), (120, 400), (300, 250), (2000, 3000)]:
        a, b = rng.integers(0, n, e), rng.integers(0, n, e)
        nc, lab = components(n, a, b)
        want_nc, want = connected_components(csr_matrix((np.ones(e), (a, b)), shape=(n, n)), directed=False)
        assert nc == want_nc and np.array_equal(lab, want)
    assert components(0, np.zeros(0, int), np.zeros(0, int))[0] == 0


def test_merge_split_straightens_a_c_shaped_boundary() -> None:
    rows = cols = 8
    prob = _grid(rows, cols, np.arange(rows * cols))  # every unit its own municipality
    i, j = np.divmod(np.arange(rows * cols), cols)
    c_shape = (j < 2) | ((i < 2) & (j < 6)) | ((i >= 6) & (j < 6))
    start = np.where(c_shape, 0, 1).astype(np.int64)
    assert c_shape.sum() == 32 and _contiguous(prob, start)
    before, parts0 = _objective(prob, start)
    out, info = merge_split(prob, prob.config, start, prob.pop.astype(float), 3200.0, seed=5)
    after, parts1 = _objective(prob, out)
    assert info["merge_split_accepted"] >= 1 and after < before
    assert parts1["cut_km"] == 8.0 < parts0["cut_km"]  # a straight cut through the grid
    assert _contiguous(prob, out) and np.bincount(out).tolist() == [32, 32]
    again, _ = merge_split(prob, prob.config, start, prob.pop.astype(float), 3200.0, seed=5)
    assert np.array_equal(again, out)  # deterministic


def test_merge_split_removes_avoidable_municipal_splits() -> None:
    # four municipalities of 3 × 2 units; the start plan is balanced but splits two of them
    rows, cols = 6, 4
    i, j = np.divmod(np.arange(rows * cols), cols)
    muni = (i // 3) * 2 + (j // 2)
    prob = _grid(rows, cols, muni)
    start = np.where(i < 3, 0, 1).astype(np.int64)
    start[2 * cols + 3] = 1  # corner of municipality 1 → district 1
    start[3 * cols + 0] = 0  # corner of municipality 2 → district 0
    assert _contiguous(prob, start) and np.bincount(start).tolist() == [12, 12]
    assert _objective(prob, start)[1]["split_municipalities"] == 2
    out, info = merge_split(prob, prob.config, start, prob.pop.astype(float), 1200.0, seed=3)
    parts = _objective(prob, out)[1]
    assert parts["split_municipalities"] == 0 and parts["max_abs_deviation_pct"] == 0.0
    assert _contiguous(prob, out) and info["merge_split_accepted"] >= 1


def test_merge_split_is_a_no_op_when_disabled_or_single_district() -> None:
    prob = _grid(4, 4, np.zeros(16))
    start = (np.arange(16) % 4 >= 2).astype(np.int64)
    off = prob.config.model_copy(
        update={"merge_split": prob.config.merge_split.model_copy(update={"enabled": False})}
    )
    out, info = merge_split(prob, off, start, prob.pop.astype(float), 800.0, seed=1)
    assert np.array_equal(out, start) and info["merge_split_tried"] == 0
    one = _grid(4, 4, np.zeros(16), seats=1)
    out1, info1 = merge_split(one, one.config, np.zeros(16, dtype=np.int64), one.pop.astype(float), 1600.0, 1)
    assert (out1 == 0).all() and info1["merge_split_tried"] == 0


@pytest.mark.parametrize("seats", [2, 3, 5])
def test_recombination_never_worsens_the_objective(seats: int) -> None:
    rng = np.random.default_rng(seats)
    rows = cols = 10
    i, j = np.divmod(np.arange(rows * cols), cols)
    prob = _grid(rows, cols, (i // 3) * 4 + (j // 3), seats=seats)
    prob.pop[:] = rng.integers(50, 150, rows * cols)
    base = prob.config.model_copy(
        update={"merge_split": prob.config.merge_split.model_copy(update={"enabled": False})}
    )
    without = partition_province(ProvinceProblem(**{**prob.__dict__, "config": base}))
    with_ms = partition_province(prob)
    assert with_ms.info["objective"] <= without.info["objective"] + 1e-9
    assert _contiguous(prob, with_ms.assignment)
    assert "merge_split_tried" in with_ms.info and "merge_split_tried" not in without.info
