"""Vectorised numerical kernels of the Monte Carlo forecast (pure NumPy, no I/O).

The same functions evaluate the zero-shock expectation during plan preparation (float64, one
"draw") and the shocked draws of the Monte Carlo loop (float32, a block of draws at once), so the
anchor computed at preparation time and the runtime transformation are the same code.

Layout is *cell-major*: fragment / cell arrays are ``(F or C, n, …)`` with ``n`` draws, ``P``
parties and ``L`` ballot lines (padded to the layer maximum).  Per-cell linear maps (routing,
strategic voting, undervote) are stacked matrix products with one small ``(n, P) @ (P, L)`` product
per cell; group sums run over contiguous cell blocks (``np.add.reduceat``).  Every operation's
result for a draw depends only on the array *shapes*, which are fixed per block, so results do not
depend on how blocks are scheduled.

Softmaxes are evaluated without max-subtraction: log preference shares lie in ``[log 1e-30, 0]``
with at least one party at or above ``log(1/P)`` (shares sum to one) and log line masses are ``≤ 0`` (plus
candidate effects of a few logit points), so ``exp`` can neither overflow nor underflow every
entry for any shock size remotely compatible with the model (float32 ``exp`` overflows only above
88).  Reductions over the short party / line axes use explicit slice loops, which NumPy executes far
faster than ``axis=2`` reductions.
"""

from __future__ import annotations

import numpy as np

#: Floor for line masses and turnout before taking logs / dividing (float32-safe).
TINY = 1e-30


def expit(x: np.ndarray) -> np.ndarray:
    """Logistic function in the input's precision (``tanh`` form, overflow-free)."""
    t = x.dtype.type
    return t(0.5) * (t(1.0) + np.tanh(t(0.5) * x))


def _sum_last(x: np.ndarray) -> np.ndarray:
    """Sum over the (short) last axis with a slice loop (same order for every element)."""
    out = x[..., 0].copy()
    for k in range(1, x.shape[-1]):
        out += x[..., k]
    return out


def fragment_state(
    V: np.ndarray,
    Tq: np.ndarray,
    utility: np.ndarray | None,
    turnout_shift: np.ndarray | None,
    party_turnout: np.ndarray | None,
    q_min: float,
    q_max: float,
    turnout_gain: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Party vote shares ``π`` (F, n, P) and turnout ``T`` (F, n) of fragments under shocks.

    ``V`` (F, P) is the log preference share and ``Tq`` (F, P) the turnout logit of each party's
    supporters at the model expectation; ``utility`` (F, n, P) adds party-utility shocks,
    ``turnout_shift`` (F, n) a turnout shock and ``party_turnout`` (n, P) the differential
    mobilisation of each party's supporters (``None`` = zero, one draw); ``turnout_gain`` (F, P)
    scales the turnout shocks per party.  Mirrors ``StructuralModel.party_state``:
    ``s = softmax(V + ε)``, ``q = clip(σ(Tq + g·(τ + t_p)))``, ``T = Σ s q``, ``π = s q / T``.
    """
    dt = V.dtype.type
    s = V[:, None, :] + utility if utility is not None else V[:, None, :].copy()
    np.exp(s, out=s)
    s /= _sum_last(s)[:, :, None]
    if turnout_shift is None and party_turnout is None:
        tq = Tq[:, None, :].copy()
    else:
        sh = np.zeros((1, 1, 1), dtype=V.dtype)
        if turnout_shift is not None:
            sh = sh + turnout_shift[:, :, None]
        if party_turnout is not None:
            sh = sh + party_turnout[None, :, :]
        if turnout_gain is not None:
            sh = sh * turnout_gain[:, None, :]
        tq = Tq[:, None, :] + sh
    q = np.clip(expit(tq), dt(q_min), dt(q_max))
    s *= q
    T = _sum_last(s)
    s /= np.maximum(T, dt(TINY))[:, :, None]
    return s, T


def group_sum(values: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """Sum contiguous groups of rows (axis 0) in the input precision; ``starts`` strictly
    increasing, first entry 0 (groups are a handful to a few hundred cells, so float32 sums of
    vote counts stay within ~1e-6 relative error)."""
    return np.add.reduceat(values, starts, axis=0)


def municipality_state(
    pi: np.ndarray, ballots: np.ndarray, starts: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Aggregate fragment vote shares (F, n, P) to municipalities, weighted by ballots (F, n).

    Fragments are sorted by municipality and ``starts`` holds each municipality's first fragment;
    returns (``π`` (M, n, P), ballots (M, n)) in ``pi``'s precision.
    """
    dt = pi.dtype
    mv = group_sum(pi * ballots[:, :, None], starts)
    mb = group_sum(ballots, starts)
    mv /= np.maximum(mb, dt.type(TINY))[:, :, None]
    return mv, mb


def line_shares(
    pi: np.ndarray,
    R: np.ndarray,
    a: np.ndarray,
    indep: np.ndarray,
    delta: np.ndarray,
    G: np.ndarray | None,
    ratio: np.ndarray | None,
    blank_base: np.ndarray,
    invalid: np.ndarray,
    line_shock: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Ballot-line shares (C, n, L) and the valid fraction of ballots (C, n) of cells.

    Per cell (mirrors :class:`app.simulation.voting.RacePlan`): line masses ``m = π R + indep``;
    ``W = log m + Δ (+ line shock)``; sincere shares ``softmax_L(W)``; strategic voting
    ``s ← max(s G, 0)``; the anchor ``ratio`` (exact model expectation at zero shocks) is applied
    multiplicatively and the shares renormalised.  The undervote ``π · a`` becomes blank ballots:
    ``blank = clip(b0 + (1 − b0 − inv) π·a, 0, 0.95)``; the valid fraction is ``1 − inv − blank``.
    Padding lines carry ``Δ = −inf`` and end with share 0.
    """
    dt = pi.dtype.type
    m = np.matmul(pi, R)
    m += indep[:, None, :]
    np.maximum(m, dt(TINY), out=m)
    W = np.log(m, out=m)
    W += delta[:, None, :]
    if line_shock is not None:
        W += line_shock
    np.exp(W, out=W)
    W /= _sum_last(W)[:, :, None]
    if G is not None:
        W = np.matmul(W, G)
        np.maximum(W, dt(0.0), out=W)
    if ratio is not None:
        W *= ratio[:, None, :]
    W /= np.maximum(_sum_last(W), dt(TINY))[:, :, None]
    abst = np.matmul(pi, a[:, :, None])[:, :, 0]
    blank = np.clip(blank_base[:, None] + (dt(1.0) - blank_base - invalid)[:, None] * abst, 0.0, 0.95)
    valid = np.clip(dt(1.0) - invalid[:, None] - blank, 0.0, 1.0)
    return W, valid
