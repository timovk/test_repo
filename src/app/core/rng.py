"""Reproducible randomness.

The application never touches NumPy's or Python's global random state.  Every stochastic
component receives a :class:`numpy.random.Generator` derived from a *root seed* plus a
tuple of *stream keys* (strings/ints).  Derivation is order-independent: the stream for
``("election", 2028, "turnout")`` is identical no matter which other streams were drawn
before it.  This guarantees that e.g. changing the number of candidates in one race does not
perturb the random draws of an unrelated race.

    >>> rng = make_rng(42, "election", 2028, "house", "NB-07")
    >>> rng2 = make_rng(42, "election", 2028, "house", "NB-07")
    >>> float(rng.random()) == float(rng2.random())
    True
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

import numpy as np

StreamKey = str | int

_MASK64 = (1 << 64) - 1


def derive_seed(root_seed: int, *keys: StreamKey) -> int:
    """Derive a stable 64-bit child seed from ``root_seed`` and stream ``keys``.

    Uses BLAKE2b over a canonical encoding so results are stable across Python versions,
    platforms and processes (unlike ``hash()``).
    """
    h = hashlib.blake2b(digest_size=8)
    h.update(b"root" + str(int(root_seed)).encode() + b"\x1e")
    for key in keys:
        if isinstance(key, bool):  # bool is an int subclass; keep it distinct
            h.update(b"b" + (b"1" if key else b"0"))
        elif isinstance(key, int):
            h.update(b"i" + str(key).encode())
        else:
            h.update(b"s" + str(key).encode("utf-8"))
        h.update(b"\x1f")
    return int.from_bytes(h.digest(), "little") & _MASK64


def make_rng(root_seed: int, *keys: StreamKey) -> np.random.Generator:
    """Return an independent PCG64 generator for the given root seed and stream keys."""
    return np.random.Generator(np.random.PCG64(derive_seed(root_seed, *keys)))


def spawn_seeds(root_seed: int, n: int, *keys: StreamKey) -> list[int]:
    """``n`` independent child seeds (e.g. for multiprocessing workers)."""
    return [derive_seed(root_seed, *keys, i) for i in range(n)]


def stable_choice_order(items: Iterable[str], root_seed: int, *keys: StreamKey) -> list[str]:
    """Deterministic pseudo-random ordering of ``items`` (used for tie-breaking by lot)."""
    return sorted(items, key=lambda it: derive_seed(root_seed, *keys, it))


def config_hash(payload: str | bytes) -> str:
    """Short stable hash of a configuration document (stored with every run)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
