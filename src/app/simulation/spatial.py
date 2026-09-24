"""Spatially correlated Gaussian-process fields over municipality centroids.

Both the persistent local lean (structural model) and the election-specific spatial shock are
unit-variance Gaussian fields ``f ~ N(0, K)`` evaluated at municipality centroids (EPSG:28992
metres), drawn as ``f = L z`` with ``L = chol(K + jitter·I)``.  The Cholesky factor depends only
on the centroids, the kernel and the length scale, so it is cached per process.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

import numpy as np

from app.core.logging import get_logger

log = get_logger(__name__)

_CACHE: OrderedDict[tuple[str, str, float], np.ndarray] = OrderedDict()
_CACHE_SIZE = 16


def kernel_matrix(xy: np.ndarray, length_km: float, kernel: str = "matern32") -> np.ndarray:
    """Unit-variance covariance matrix between points ``xy`` (N, 2) in metres."""
    xy = np.asarray(xy, dtype=float)
    diff = xy[:, None, :] - xy[None, :, :]
    d = np.sqrt((diff**2).sum(axis=-1)) / (1000.0 * float(length_km))
    if kernel == "exponential":
        return np.exp(-d)
    if kernel == "matern32":
        s = np.sqrt(3.0) * d
        return (1.0 + s) * np.exp(-s)
    if kernel == "squared_exponential":
        return np.exp(-0.5 * d * d)
    raise ValueError(f"unknown kernel {kernel!r}")


def gp_cholesky(xy: np.ndarray, length_km: float, kernel: str = "matern32") -> np.ndarray:
    """Lower Cholesky factor of the kernel matrix (cached; jitter added until it is positive definite)."""
    xy = np.ascontiguousarray(np.asarray(xy, dtype=float))
    key = (hashlib.blake2b(xy.tobytes(), digest_size=12).hexdigest(), kernel, float(length_km))
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    n = len(xy)
    if n == 0:
        chol = np.zeros((0, 0))
    else:
        K = kernel_matrix(xy, length_km, kernel)
        jitter = 1e-8
        while True:
            try:
                chol = np.linalg.cholesky(K + jitter * np.eye(n))
                break
            except np.linalg.LinAlgError:
                jitter *= 10.0
                if jitter > 1e-1:  # pragma: no cover - only for degenerate inputs
                    raise
        if jitter > 1e-6:
            log.debug("GP Cholesky needed jitter %.1e (n=%d, kernel=%s)", jitter, n, kernel)
    chol.setflags(write=False)
    _CACHE[key] = chol
    if len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return chol


def draw_field(chol: np.ndarray, rng: np.random.Generator, k: int) -> np.ndarray:
    """``k`` independent unit-variance fields at the Cholesky factor's points → (N, k)."""
    z = rng.standard_normal((chol.shape[0], k))
    return chol @ z


def clear_cache() -> None:
    _CACHE.clear()
